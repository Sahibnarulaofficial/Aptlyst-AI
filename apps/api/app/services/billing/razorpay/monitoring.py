"""Lightweight PostgreSQL-backed worker heartbeat and read-only queue monitoring.

Stage 2J.4 Observability Foundation:
1. Worker Heartbeats (`billing_worker_heartbeats`):
   - Distinct per-process identity (`worker_id` + `instance_token`) so restarts never inherit
     stale liveness and concurrent processes cannot collide.
   - Throttled writes (`heartbeat_interval_seconds`, default 30s) plus immediate flush on
     startup, shutdown, error, or completed batch to avoid excessive DB write load.
   - Strictly isolated short DB sessions wrapped in non-fatal error handling so heartbeat
     failures never disrupt or roll back billing transactions.
2. Read-Only Queue & Reconciliation Inspection:
   - Strictly read-only `SELECT` queries across `billing_webhook_events`,
     `billing_checkout_attempts`, `payment_reconciliations`, and `billing_worker_heartbeats`.
   - Explicitly separates process liveness (`last_heartbeat_at`) from queue progress
     (`last_processed_at`, backlog age, and error progression).
   - Evaluates configurable alert thresholds and emits structured logs without exposing
     credentials, signatures, or customer PII.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import os
import socket
from typing import Any
import uuid

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import register_all_models
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    BillingWorkerHeartbeat,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus, WebhookProcessingStatus

register_all_models()
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.services.billing.razorpay.monitoring")

WORKER_TYPE_WEBHOOK = "razorpay_webhook"
WORKER_TYPE_RECOVERY = "razorpay_checkout_recovery"


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    utc_ts = _ensure_utc(ts)
    if utc_ts is None:
        return None
    return max(0.0, (now - utc_ts).total_seconds())


class WorkerHeartbeatTracker:
    """Throttled, non-fatal heartbeat emitter for independent background workers."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_type: str,
        deployment_environment: str,
        environment: str | None,
        merchant_account_id: str | None,
        heartbeat_interval_seconds: float | None = None,
        stale_takeover_seconds: int | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.instance_token = uuid.uuid4()
        self.worker_type = worker_type
        self.deployment_environment = deployment_environment
        self.environment = environment
        self.merchant_account_id = merchant_account_id
        self.heartbeat_interval_seconds = (
            float(heartbeat_interval_seconds)
            if heartbeat_interval_seconds is not None
            else float(getattr(settings, "worker_heartbeat_interval_seconds", 30.0))
        )
        self.stale_takeover_seconds = (
            int(stale_takeover_seconds)
            if stale_takeover_seconds is not None
            else int(getattr(settings, "billing_health_heartbeat_stale_seconds", 90))
        )
        self.pid = os.getpid()
        self.hostname = socket.gethostname()[:255]
        self.started_at = _ensure_utc(started_at) or datetime.now(timezone.utc)

        self.processed_count: int = 0
        self.error_count: int = 0
        self.last_processed_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error_message: str | None = None
        self._last_written_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self.write_timeout_seconds: float = 2.0
        self.ownership_lost: bool = False

    def should_flush(self, now: datetime, *, force: bool = False) -> bool:
        if force or self._last_attempt_at is None:
            return True
        return (now - self._last_attempt_at).total_seconds() >= self.heartbeat_interval_seconds

    async def record_heartbeat(
        self,
        session_factory: Callable[[], AsyncSession] | None,
        *,
        status: str,
        now: datetime | None = None,
        force: bool = False,
        processed_delta: int = 0,
        error_message: str | None = None,
    ) -> bool:
        """Persist worker heartbeat in an isolated transaction without failing worker execution."""
        now_utc = _ensure_utc(now) or datetime.now(timezone.utc)

        if processed_delta > 0:
            self.processed_count += processed_delta
            self.last_processed_at = now_utc
        if error_message is not None:
            self.error_count += 1
            self.last_error_at = now_utc
            self.last_error_message = sanitize_billing_error_message(error_message)

        if session_factory is None:
            return False

        if status == "stopped" and self.ownership_lost:
            logger.info(
                "Skipping shutdown heartbeat for worker %s because ownership was already lost.",
                self.worker_id,
            )
            return False

        # Avoid hammering an unreachable DB on rapid error loops; always flush clean shutdown ("stopped")
        # and initial/processed updates unless a recent attempt failed within the throttle window.
        recently_attempted = (
            self._last_attempt_at is not None
            and (now_utc - self._last_attempt_at).total_seconds() < self.heartbeat_interval_seconds
        )
        last_attempt_failed = (
            self._last_attempt_at is not None
            and (self._last_written_at is None or self._last_attempt_at > self._last_written_at)
        )
        if status == "stopped":
            effective_force = True
        elif last_attempt_failed and recently_attempted:
            # Previous attempt failed recently; back off until interval elapses
            effective_force = False
        else:
            effective_force = force or processed_delta > 0 or (error_message is not None and not recently_attempted)

        if not self.should_flush(now_utc, force=effective_force):
            return False

        self._last_attempt_at = now_utc
        stale_cutoff = now_utc - timedelta(seconds=self.stale_takeover_seconds)

        async def _persist() -> bool:
            async with session_factory() as db:
                stmt = (
                    pg_insert(BillingWorkerHeartbeat)
                    .values(
                        worker_id=self.worker_id,
                        instance_token=self.instance_token,
                        worker_type=self.worker_type,
                        deployment_environment=self.deployment_environment,
                        environment=self.environment,
                        merchant_account_id=self.merchant_account_id,
                        status=status,
                        pid=self.pid,
                        hostname=self.hostname,
                        started_at=self.started_at,
                        last_heartbeat_at=now_utc,
                        last_processed_at=self.last_processed_at,
                        last_error_at=self.last_error_at,
                        last_error_message=self.last_error_message,
                        processed_count=self.processed_count,
                        error_count=self.error_count,
                        created_at=now_utc,
                        updated_at=now_utc,
                    )
                    .on_conflict_do_update(
                        index_elements=[BillingWorkerHeartbeat.worker_id],
                        set_={
                            "instance_token": self.instance_token,
                            "worker_type": self.worker_type,
                            "deployment_environment": self.deployment_environment,
                            "environment": self.environment,
                            "merchant_account_id": self.merchant_account_id,
                            "status": status,
                            "pid": self.pid,
                            "hostname": self.hostname,
                            "started_at": self.started_at,
                            "last_heartbeat_at": now_utc,
                            "last_processed_at": self.last_processed_at,
                            "last_error_at": self.last_error_at,
                            "last_error_message": self.last_error_message,
                            "processed_count": self.processed_count,
                            "error_count": self.error_count,
                            "updated_at": now_utc,
                        },
                        where=or_(
                            BillingWorkerHeartbeat.instance_token == self.instance_token,
                            BillingWorkerHeartbeat.status == "stopped",
                            BillingWorkerHeartbeat.last_heartbeat_at <= stale_cutoff,
                        ),
                    )
                    .returning(BillingWorkerHeartbeat.instance_token)
                )
                res = await db.execute(stmt)
                await db.commit()
                if res is not None and hasattr(res, "scalar_one_or_none"):
                    returned_token = res.scalar_one_or_none()
                    if returned_token is None:
                        return False
            return True

        try:
            persisted = await asyncio.wait_for(_persist(), timeout=self.write_timeout_seconds)
            if not persisted:
                self.ownership_lost = True
                logger.warning(
                    "Worker %s heartbeat write rejected by instance_token fencing (superseded by active instance).",
                    self.worker_id,
                )
                return False
            self.ownership_lost = False
            self._last_written_at = now_utc
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Worker %s heartbeat persistence failed (non-fatal): %s",
                self.worker_id,
                sanitize_billing_error_message(str(exc)),
            )
            return False


@dataclass(frozen=True)
class BillingHealthThresholds:
    """Configurable thresholds governing operational alerts."""

    heartbeat_stale_seconds: int = 90
    webhook_backlog_age_seconds: int = 300
    webhook_backlog_count_threshold: int = 50
    webhook_failed_count_threshold: int = 5
    recovery_overdue_age_seconds: int = 300
    reconciliation_backlog_age_seconds: int = 3600
    repeated_lease_expiry_threshold: int = 2

    @classmethod
    def from_settings(cls) -> "BillingHealthThresholds":
        return cls(
            heartbeat_stale_seconds=int(
                getattr(settings, "billing_health_heartbeat_stale_seconds", 90)
            ),
            webhook_backlog_age_seconds=int(
                getattr(settings, "billing_health_webhook_backlog_age_seconds", 300)
            ),
            webhook_backlog_count_threshold=int(
                getattr(settings, "billing_health_webhook_backlog_count_threshold", 50)
            ),
            webhook_failed_count_threshold=int(
                getattr(settings, "billing_health_webhook_failed_count_threshold", 5)
            ),
            recovery_overdue_age_seconds=int(
                getattr(settings, "billing_health_recovery_overdue_age_seconds", 300)
            ),
            reconciliation_backlog_age_seconds=int(
                getattr(settings, "billing_health_reconciliation_backlog_age_seconds", 3600)
            ),
            repeated_lease_expiry_threshold=int(
                getattr(settings, "billing_health_repeated_lease_expiry_threshold", 2)
            ),
        )


@dataclass(frozen=True)
class WorkerLivenessSnapshot:
    worker_id: str
    worker_type: str
    deployment_environment: str
    environment: str | None
    merchant_account_id: str | None
    status: str
    last_heartbeat_at: datetime
    heartbeat_age_seconds: float
    is_stale: bool
    last_processed_at: datetime | None
    last_error_at: datetime | None
    processed_count: int
    error_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worker_type": self.worker_type,
            "deployment_environment": self.deployment_environment,
            "environment": self.environment,
            "merchant_account_id": self.merchant_account_id,
            "status": self.status,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat(),
            "heartbeat_age_seconds": round(self.heartbeat_age_seconds, 2),
            "is_stale": self.is_stale,
            "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "processed_count": self.processed_count,
            "error_count": self.error_count,
        }


@dataclass(frozen=True)
class WebhookQueueMetrics:
    pending_count: int
    failed_count: int
    quarantined_count: int
    expired_lease_count: int
    oldest_pending_received_at: datetime | None
    oldest_pending_age_seconds: float | None
    last_processed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_count": self.pending_count,
            "failed_count": self.failed_count,
            "quarantined_count": self.quarantined_count,
            "expired_lease_count": self.expired_lease_count,
            "oldest_pending_received_at": (
                self.oldest_pending_received_at.isoformat()
                if self.oldest_pending_received_at
                else None
            ),
            "oldest_pending_age_seconds": (
                round(self.oldest_pending_age_seconds, 2)
                if self.oldest_pending_age_seconds is not None
                else None
            ),
            "last_processed_at": (
                self.last_processed_at.isoformat() if self.last_processed_at else None
            ),
        }


@dataclass(frozen=True)
class RecoveryQueueMetrics:
    due_count: int
    scheduled_count: int
    quarantined_count: int
    expired_claim_count: int
    repeated_lease_expiry_count: int
    oldest_overdue_retry_at: datetime | None
    oldest_overdue_age_seconds: float | None
    last_recovered_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "due_count": self.due_count,
            "scheduled_count": self.scheduled_count,
            "quarantined_count": self.quarantined_count,
            "expired_claim_count": self.expired_claim_count,
            "repeated_lease_expiry_count": self.repeated_lease_expiry_count,
            "oldest_overdue_retry_at": (
                self.oldest_overdue_retry_at.isoformat()
                if self.oldest_overdue_retry_at
                else None
            ),
            "oldest_overdue_age_seconds": (
                round(self.oldest_overdue_age_seconds, 2)
                if self.oldest_overdue_age_seconds is not None
                else None
            ),
            "last_recovered_at": (
                self.last_recovered_at.isoformat() if self.last_recovered_at else None
            ),
        }


@dataclass(frozen=True)
class ReconciliationMetrics:
    pending_review_count: int
    oldest_pending_created_at: datetime | None
    oldest_pending_age_seconds: float | None
    recent_created_24h_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_review_count": self.pending_review_count,
            "oldest_pending_created_at": (
                self.oldest_pending_created_at.isoformat()
                if self.oldest_pending_created_at
                else None
            ),
            "oldest_pending_age_seconds": (
                round(self.oldest_pending_age_seconds, 2)
                if self.oldest_pending_age_seconds is not None
                else None
            ),
            "recent_created_24h_count": self.recent_created_24h_count,
        }


@dataclass(frozen=True)
class AlertCondition:
    code: str
    severity: str  # "warning" | "critical"
    message: str
    metric_value: float | int
    threshold: float | int

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class BillingHealthReport:
    checked_at: datetime
    environment_filter: str | None
    overall_status: str  # "healthy" | "degraded" | "unhealthy"
    workers: list[WorkerLivenessSnapshot]
    webhook_queue: WebhookQueueMetrics
    recovery_queue: RecoveryQueueMetrics
    reconciliation: ReconciliationMetrics
    alerts: list[AlertCondition] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "environment_filter": self.environment_filter,
            "overall_status": self.overall_status,
            "workers": [w.to_dict() for w in self.workers],
            "webhook_queue": self.webhook_queue.to_dict(),
            "recovery_queue": self.recovery_queue.to_dict(),
            "reconciliation": self.reconciliation.to_dict(),
            "alerts": [a.to_dict() for a in self.alerts],
        }


def evaluate_billing_health_alerts(
    *,
    workers: list[WorkerLivenessSnapshot],
    webhook_queue: WebhookQueueMetrics,
    recovery_queue: RecoveryQueueMetrics,
    reconciliation: ReconciliationMetrics,
    thresholds: BillingHealthThresholds,
) -> tuple[str, list[AlertCondition]]:
    """Evaluate operational alert rules, separating liveness from queue progress."""
    alerts: list[AlertCondition] = []

    active_types: dict[str, list[WorkerLivenessSnapshot]] = {
        WORKER_TYPE_WEBHOOK: [],
        WORKER_TYPE_RECOVERY: [],
    }

    for worker in workers:
        if worker.status in ("starting", "running"):
            if worker.is_stale:
                alerts.append(
                    AlertCondition(
                        code="STALE_WORKER_HEARTBEAT",
                        severity="critical",
                        message=(
                            f"Worker {worker.worker_id} ({worker.worker_type}) heartbeat is stale "
                            f"({worker.heartbeat_age_seconds:.1f}s > {thresholds.heartbeat_stale_seconds}s)."
                        ),
                        metric_value=round(worker.heartbeat_age_seconds, 2),
                        threshold=thresholds.heartbeat_stale_seconds,
                    )
                )
            else:
                active_types.setdefault(worker.worker_type, []).append(worker)

                # Distinguish liveness from progress: worker alive but hitting errors without processing
                if (
                    worker.last_error_at is not None
                    and (worker.last_processed_at is None or worker.last_error_at > worker.last_processed_at)
                    and worker.error_count > 0
                ):
                    alerts.append(
                        AlertCondition(
                            code="WORKER_ERRORING_NO_PROGRESS",
                            severity="warning",
                            message=(
                                f"Worker {worker.worker_id} ({worker.worker_type}) is alive but "
                                f"reported {worker.error_count} error(s) after last progress."
                            ),
                            metric_value=worker.error_count,
                            threshold=0,
                        )
                    )

    if not active_types.get(WORKER_TYPE_WEBHOOK):
        severity = "critical" if (webhook_queue.pending_count > 0 or webhook_queue.failed_count > 0) else "warning"
        alerts.append(
            AlertCondition(
                code="NO_ACTIVE_WEBHOOK_WORKER",
                severity=severity,
                message="No non-stale active Razorpay webhook worker heartbeat detected.",
                metric_value=0,
                threshold=1,
            )
        )

    if not active_types.get(WORKER_TYPE_RECOVERY):
        severity = "critical" if recovery_queue.due_count > 0 else "warning"
        alerts.append(
            AlertCondition(
                code="NO_ACTIVE_RECOVERY_WORKER",
                severity=severity,
                message="No non-stale active Razorpay checkout recovery worker heartbeat detected.",
                metric_value=0,
                threshold=1,
            )
        )

    if (
        webhook_queue.oldest_pending_age_seconds is not None
        and webhook_queue.oldest_pending_age_seconds > thresholds.webhook_backlog_age_seconds
    ):
        if active_types.get(WORKER_TYPE_WEBHOOK):
            alerts.append(
                AlertCondition(
                    code="WEBHOOK_WORKER_STALLED_OR_BACKLOGGED",
                    severity="critical",
                    message=(
                        f"Webhook worker is alive, but oldest pending event is "
                        f"{webhook_queue.oldest_pending_age_seconds:.1f}s old (> {thresholds.webhook_backlog_age_seconds}s)."
                    ),
                    metric_value=round(webhook_queue.oldest_pending_age_seconds, 2),
                    threshold=thresholds.webhook_backlog_age_seconds,
                )
            )
        else:
            alerts.append(
                AlertCondition(
                    code="WEBHOOK_BACKLOG_AGE_EXCEEDED",
                    severity="critical",
                    message=(
                        f"Oldest pending webhook event is {webhook_queue.oldest_pending_age_seconds:.1f}s old "
                        f"(> {thresholds.webhook_backlog_age_seconds}s)."
                    ),
                    metric_value=round(webhook_queue.oldest_pending_age_seconds, 2),
                    threshold=thresholds.webhook_backlog_age_seconds,
                )
            )

    unprocessed_webhook_count = webhook_queue.pending_count + webhook_queue.failed_count
    if unprocessed_webhook_count >= thresholds.webhook_backlog_count_threshold:
        alerts.append(
            AlertCondition(
                code="WEBHOOK_BACKLOG_COUNT_EXCEEDED",
                severity="critical" if unprocessed_webhook_count >= thresholds.webhook_backlog_count_threshold * 2 else "warning",
                message=(
                    f"Unprocessed webhook backlog count is {unprocessed_webhook_count} "
                    f"(>= {thresholds.webhook_backlog_count_threshold})."
                ),
                metric_value=unprocessed_webhook_count,
                threshold=thresholds.webhook_backlog_count_threshold,
            )
        )

    if webhook_queue.failed_count >= thresholds.webhook_failed_count_threshold:
        alerts.append(
            AlertCondition(
                code="REPEATED_WEBHOOK_FAILURES",
                severity="warning",
                message=(
                    f"{webhook_queue.failed_count} Razorpay webhook event(s) are in retrying 'failed' state "
                    f"(>= {thresholds.webhook_failed_count_threshold})."
                ),
                metric_value=webhook_queue.failed_count,
                threshold=thresholds.webhook_failed_count_threshold,
            )
        )

    if webhook_queue.expired_lease_count >= thresholds.repeated_lease_expiry_threshold:
        alerts.append(
            AlertCondition(
                code="WEBHOOK_EXPIRED_LEASES",
                severity="warning",
                message=(
                    f"{webhook_queue.expired_lease_count} webhook event(s) hold expired worker leases "
                    f"(>= {thresholds.repeated_lease_expiry_threshold})."
                ),
                metric_value=webhook_queue.expired_lease_count,
                threshold=thresholds.repeated_lease_expiry_threshold,
            )
        )

    if webhook_queue.quarantined_count > 0:
        alerts.append(
            AlertCondition(
                code="QUARANTINED_WEBHOOK_EVENTS",
                severity="critical",
                message=f"{webhook_queue.quarantined_count} Razorpay webhook event(s) are quarantined.",
                metric_value=webhook_queue.quarantined_count,
                threshold=0,
            )
        )

    if recovery_queue.expired_claim_count >= thresholds.repeated_lease_expiry_threshold:
        alerts.append(
            AlertCondition(
                code="RECOVERY_EXPIRED_CLAIMS",
                severity="warning",
                message=(
                    f"{recovery_queue.expired_claim_count} checkout recovery attempt(s) hold expired claims "
                    f"(>= {thresholds.repeated_lease_expiry_threshold})."
                ),
                metric_value=recovery_queue.expired_claim_count,
                threshold=thresholds.repeated_lease_expiry_threshold,
            )
        )

    if (
        recovery_queue.oldest_overdue_age_seconds is not None
        and recovery_queue.oldest_overdue_age_seconds > thresholds.recovery_overdue_age_seconds
    ):
        code = (
            "RECOVERY_WORKER_STALLED_OR_BACKLOGGED"
            if active_types.get(WORKER_TYPE_RECOVERY)
            else "RECOVERY_OVERDUE_BACKLOG"
        )
        alerts.append(
            AlertCondition(
                code=code,
                severity="critical",
                message=(
                    f"Oldest overdue recovery attempt is {recovery_queue.oldest_overdue_age_seconds:.1f}s past due "
                    f"(> {thresholds.recovery_overdue_age_seconds}s)."
                ),
                metric_value=round(recovery_queue.oldest_overdue_age_seconds, 2),
                threshold=thresholds.recovery_overdue_age_seconds,
            )
        )

    if recovery_queue.repeated_lease_expiry_count > 0 or recovery_queue.quarantined_count > 0:
        total_anom = max(recovery_queue.repeated_lease_expiry_count, recovery_queue.quarantined_count)
        alerts.append(
            AlertCondition(
                code="REPEATED_EXPIRED_LEASES_OR_QUARANTINE",
                severity="critical" if recovery_queue.quarantined_count > 0 else "warning",
                message=(
                    f"Recovery anomalies detected: {recovery_queue.repeated_lease_expiry_count} attempt(s) with "
                    f"repeated expired leases, {recovery_queue.quarantined_count} quarantined attempt(s)."
                ),
                metric_value=total_anom,
                threshold=0,
            )
        )

    if reconciliation.pending_review_count > 0:
        is_old = (
            reconciliation.oldest_pending_age_seconds is not None
            and reconciliation.oldest_pending_age_seconds > thresholds.reconciliation_backlog_age_seconds
        )
        alerts.append(
            AlertCondition(
                code="PENDING_RECONCILIATION_CASES",
                severity="critical" if is_old else "warning",
                message=(
                    f"{reconciliation.pending_review_count} Razorpay payment reconciliation case(s) "
                    "pending operator review."
                ),
                metric_value=reconciliation.pending_review_count,
                threshold=0,
            )
        )

    if any(a.severity == "critical" for a in alerts):
        overall_status = "unhealthy"
    elif alerts:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return overall_status, alerts


async def collect_billing_health_snapshot(
    db: AsyncSession,
    *,
    environment: str | None = None,
    now: datetime | None = None,
    thresholds: BillingHealthThresholds | None = None,
    emit_alert_logs: bool = True,
) -> BillingHealthReport:
    """Collect read-only worker heartbeat and billing queue health metrics."""
    now_utc = _ensure_utc(now) or datetime.now(timezone.utc)
    cfg_thresholds = thresholds or BillingHealthThresholds.from_settings()
    clean_env = environment.strip().lower() if isinstance(environment, str) and environment.strip() else None

    # 1. Worker heartbeats
    hb_stmt = select(BillingWorkerHeartbeat).order_by(BillingWorkerHeartbeat.last_heartbeat_at.desc())
    if clean_env is not None:
        if clean_env == "live":
            hb_stmt = hb_stmt.where(BillingWorkerHeartbeat.environment == clean_env)
        else:
            hb_stmt = hb_stmt.where(
                or_(
                    BillingWorkerHeartbeat.environment == clean_env,
                    BillingWorkerHeartbeat.environment.is_(None),
                )
            )
    hb_rows = list((await db.execute(hb_stmt)).scalars().all())
    workers: list[WorkerLivenessSnapshot] = []
    for row in hb_rows:
        hb_ts = _ensure_utc(row.last_heartbeat_at) or now_utc
        age_sec = _age_seconds(hb_ts, now_utc) or 0.0
        is_stale = row.status in ("starting", "running") and age_sec > cfg_thresholds.heartbeat_stale_seconds
        workers.append(
            WorkerLivenessSnapshot(
                worker_id=row.worker_id,
                worker_type=row.worker_type,
                deployment_environment=row.deployment_environment,
                environment=row.environment,
                merchant_account_id=row.merchant_account_id,
                status=row.status,
                last_heartbeat_at=hb_ts,
                heartbeat_age_seconds=age_sec,
                is_stale=is_stale,
                last_processed_at=_ensure_utc(row.last_processed_at),
                last_error_at=_ensure_utc(row.last_error_at),
                processed_count=int(row.processed_count or 0),
                error_count=int(row.error_count or 0),
            )
        )

    # 2. Webhook queue metrics (read-only)
    wh_base = [BillingWebhookEvent.provider == "razorpay"]
    if clean_env is not None:
        wh_base.append(BillingWebhookEvent.environment == clean_env)

    wh_pending_cnt, wh_oldest_pending = (
        await db.execute(
            select(func.count(BillingWebhookEvent.id), func.min(BillingWebhookEvent.received_at)).where(
                *wh_base,
                BillingWebhookEvent.status.in_(
                    [WebhookProcessingStatus.pending, WebhookProcessingStatus.failed]
                ),
            )
        )
    ).one()

    wh_failed_cnt = (
        await db.execute(
            select(func.count(BillingWebhookEvent.id)).where(
                *wh_base,
                BillingWebhookEvent.status == WebhookProcessingStatus.failed,
            )
        )
    ).scalar_one()

    wh_quarantined_cnt = (
        await db.execute(
            select(func.count(BillingWebhookEvent.id)).where(
                *wh_base,
                BillingWebhookEvent.status == WebhookProcessingStatus.quarantined,
            )
        )
    ).scalar_one()

    wh_expired_lease_cnt = (
        await db.execute(
            select(func.count(BillingWebhookEvent.id)).where(
                *wh_base,
                BillingWebhookEvent.status.in_(
                    [WebhookProcessingStatus.pending, WebhookProcessingStatus.failed]
                ),
                BillingWebhookEvent.lease_expires_at.is_not(None),
                BillingWebhookEvent.lease_expires_at <= now_utc,
            )
        )
    ).scalar_one()

    wh_last_processed = (
        await db.execute(
            select(func.max(BillingWebhookEvent.processed_at)).where(
                *wh_base,
                BillingWebhookEvent.status == WebhookProcessingStatus.processed,
            )
        )
    ).scalar_one()

    webhook_queue = WebhookQueueMetrics(
        pending_count=int(wh_pending_cnt or 0),
        failed_count=int(wh_failed_cnt or 0),
        quarantined_count=int(wh_quarantined_cnt or 0),
        expired_lease_count=int(wh_expired_lease_cnt or 0),
        oldest_pending_received_at=_ensure_utc(wh_oldest_pending),
        oldest_pending_age_seconds=_age_seconds(wh_oldest_pending, now_utc),
        last_processed_at=_ensure_utc(wh_last_processed),
    )

    # 3. Recovery queue metrics (read-only)
    rec_base = [BillingCheckoutAttempt.provider == "razorpay"]
    if clean_env is not None:
        rec_base.append(BillingCheckoutAttempt.environment == clean_env)

    rec_due_cnt, rec_oldest_overdue = (
        await db.execute(
            select(
                func.count(BillingCheckoutAttempt.id),
                func.min(BillingCheckoutAttempt.next_recovery_retry_at),
            ).where(
                *rec_base,
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
                BillingCheckoutAttempt.next_recovery_retry_at.is_not(None),
                BillingCheckoutAttempt.next_recovery_retry_at <= now_utc,
            )
        )
    ).one()

    rec_scheduled_cnt = (
        await db.execute(
            select(func.count(BillingCheckoutAttempt.id)).where(
                *rec_base,
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
                BillingCheckoutAttempt.next_recovery_retry_at.is_not(None),
            )
        )
    ).scalar_one()

    rec_quarantined_cnt = (
        await db.execute(
            select(func.count(BillingCheckoutAttempt.id)).where(
                *rec_base,
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
                BillingCheckoutAttempt.next_recovery_retry_at.is_(None),
            )
        )
    ).scalar_one()

    rec_expired_claim_cnt = (
        await db.execute(
            select(func.count(BillingCheckoutAttempt.id)).where(
                *rec_base,
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
                BillingCheckoutAttempt.recovery_claim_expires_at.is_not(None),
                BillingCheckoutAttempt.recovery_claim_expires_at <= now_utc,
            )
        )
    ).scalar_one()

    # Inspect request_payload for active recovery_required rows safely in Python
    rec_payload_rows = (
        await db.execute(
            select(BillingCheckoutAttempt.request_payload).where(
                *rec_base,
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
            )
        )
    ).scalars().all()
    repeated_lease_expiry_cnt = 0
    for payload in rec_payload_rows:
        if isinstance(payload, dict):
            try:
                exp_cnt = int(payload.get("expired_lease_count", 0))
            except (ValueError, TypeError):
                exp_cnt = 0
            if exp_cnt >= cfg_thresholds.repeated_lease_expiry_threshold:
                repeated_lease_expiry_cnt += 1

    rec_last_recovered = (
        await db.execute(
            select(func.max(BillingCheckoutAttempt.updated_at)).where(
                *rec_base,
                BillingCheckoutAttempt.status.in_(
                    [CheckoutAttemptStatus.open, CheckoutAttemptStatus.complete]
                ),
                BillingCheckoutAttempt.request_payload["recovery_evidence"].is_not(None),
            )
        )
    ).scalar_one()

    recovery_queue = RecoveryQueueMetrics(
        due_count=int(rec_due_cnt or 0),
        scheduled_count=int(rec_scheduled_cnt or 0),
        quarantined_count=int(rec_quarantined_cnt or 0),
        expired_claim_count=int(rec_expired_claim_cnt or 0),
        repeated_lease_expiry_count=repeated_lease_expiry_cnt,
        oldest_overdue_retry_at=_ensure_utc(rec_oldest_overdue),
        oldest_overdue_age_seconds=_age_seconds(rec_oldest_overdue, now_utc),
        last_recovered_at=_ensure_utc(rec_last_recovered),
    )

    # 4. Reconciliation metrics (read-only)
    recon_base = [PaymentReconciliation.provider == "razorpay"]
    if clean_env is not None:
        recon_base.append(PaymentReconciliation.environment == clean_env)

    recon_pending_cnt, recon_oldest_pending = (
        await db.execute(
            select(
                func.count(PaymentReconciliation.id),
                func.min(PaymentReconciliation.created_at),
            ).where(
                *recon_base,
                PaymentReconciliation.status == ReconciliationStatus.pending_review,
            )
        )
    ).one()

    recon_recent_cnt = (
        await db.execute(
            select(func.count(PaymentReconciliation.id)).where(
                *recon_base,
                PaymentReconciliation.created_at >= now_utc - timedelta(hours=24),
            )
        )
    ).scalar_one()

    reconciliation = ReconciliationMetrics(
        pending_review_count=int(recon_pending_cnt or 0),
        oldest_pending_created_at=_ensure_utc(recon_oldest_pending),
        oldest_pending_age_seconds=_age_seconds(recon_oldest_pending, now_utc),
        recent_created_24h_count=int(recon_recent_cnt or 0),
    )

    overall_status, alerts = evaluate_billing_health_alerts(
        workers=workers,
        webhook_queue=webhook_queue,
        recovery_queue=recovery_queue,
        reconciliation=reconciliation,
        thresholds=cfg_thresholds,
    )

    if emit_alert_logs:
        for alert in alerts:
            log_fn = logger.error if alert.severity == "critical" else logger.warning
            log_fn(
                "Billing health alert [%s] (%s): %s",
                alert.code,
                alert.severity,
                alert.message,
                extra={
                    "alert_code": alert.code,
                    "severity": alert.severity,
                    "metric_value": alert.metric_value,
                    "threshold": alert.threshold,
                    "environment": clean_env,
                },
            )

    return BillingHealthReport(
        checked_at=now_utc,
        environment_filter=clean_env,
        overall_status=overall_status,
        workers=workers,
        webhook_queue=webhook_queue,
        recovery_queue=recovery_queue,
        reconciliation=reconciliation,
        alerts=alerts,
    )


async def cleanup_stale_worker_heartbeats(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    stopped_retention_seconds: int | None = None,
    abandoned_retention_seconds: int | None = None,
    stale_takeover_seconds: int | None = None,
    exclude_worker_id: str | None = None,
    batch_size: int = 500,
) -> int:
    """Safely prune expired historical worker heartbeat rows in bounded, lock-safe batches.

    Invariants:
    - Never deletes active worker heartbeats: enforces a hard safety floor of at least
      max(stale_takeover_seconds * 2, 300) seconds before deletion.
    - Retains cleanly stopped rows for ``stopped_retention_seconds`` (default 7 days) and
      ungraceful non-stopped rows for ``abandoned_retention_seconds`` (default 30 days) so
      incident evidence remains available for investigation.
    - Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` over primary keys before ``DELETE`` so
      concurrent cleanup invocations never deadlock or double-delete, making execution
      idempotent.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")

    now_utc = _ensure_utc(now) or datetime.now(timezone.utc)
    stale_sec = int(
        stale_takeover_seconds
        if stale_takeover_seconds is not None
        else getattr(settings, "billing_health_heartbeat_stale_seconds", 90)
    )
    min_safe_floor = max(stale_sec * 2, 300)

    stopped_ret = int(
        stopped_retention_seconds
        if stopped_retention_seconds is not None
        else getattr(settings, "worker_heartbeat_stopped_retention_seconds", 604800)
    )
    abandoned_ret = int(
        abandoned_retention_seconds
        if abandoned_retention_seconds is not None
        else getattr(settings, "worker_heartbeat_abandoned_retention_seconds", 2592000)
    )

    if stopped_ret < min_safe_floor:
        raise ValueError(
            f"stopped_retention_seconds ({stopped_ret}) must be >= {min_safe_floor}s "
            "to avoid deleting active or recently stale worker heartbeats."
        )
    if abandoned_ret < min_safe_floor:
        raise ValueError(
            f"abandoned_retention_seconds ({abandoned_ret}) must be >= {min_safe_floor}s "
            "to preserve incident investigation evidence."
        )

    stopped_cutoff = now_utc - timedelta(seconds=stopped_ret)
    abandoned_cutoff = now_utc - timedelta(seconds=abandoned_ret)

    eligibility = or_(
        (BillingWorkerHeartbeat.status == "stopped")
        & (BillingWorkerHeartbeat.last_heartbeat_at <= stopped_cutoff),
        (BillingWorkerHeartbeat.status != "stopped")
        & (BillingWorkerHeartbeat.last_heartbeat_at <= abandoned_cutoff),
    )

    claim_stmt = (
        select(BillingWorkerHeartbeat.worker_id)
        .where(eligibility)
        .order_by(BillingWorkerHeartbeat.last_heartbeat_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    if exclude_worker_id:
        claim_stmt = claim_stmt.where(BillingWorkerHeartbeat.worker_id != exclude_worker_id)

    candidate_ids = list((await db.execute(claim_stmt)).scalars().all())
    if not candidate_ids:
        await db.rollback()
        return 0

    del_stmt = delete(BillingWorkerHeartbeat).where(
        BillingWorkerHeartbeat.worker_id.in_(candidate_ids)
    )
    res = await db.execute(del_stmt)
    await db.commit()
    return int(res.rowcount or len(candidate_ids))
