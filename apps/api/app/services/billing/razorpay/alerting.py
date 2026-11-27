"""Operational billing alert delivery and deduplication (Stage 2K-C).

Design Guarantees:
1. Separation of Concerns: Alert rule evaluation happens in read-only monitoring;
   notification state transitions and delivery run strictly outside DB transactions/locks.
2. Edge-Triggered Deduplication: Active incidents are tracked by stable incident key
   (environment + alert code). Repeating identical active conditions does not re-alert on
   every scheduler tick, while severity escalations (`warning` -> `critical`) and
   resolved-then-recurred incidents emit fresh notifications.
3. Delivery Failure Safety: Notification state is persisted ONLY after the configured
   AlertSink succeeds. Delivery errors fail closed (`AlertDeliveryError` / `delivery_ok=False`)
   so failed deliveries are never reported as successful and can retry on the next tick.
4. Safe Configuration Defaults: External HTTP webhook delivery is strictly opt-in
   (`BILLING_HEALTH_ALERT_WEBHOOK_ENABLED=false` by default), requires explicit HTTP(S) URL
   configuration, enforces strict timeouts, and redacts credentials/DSNs/tokens from logs
   and payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol
import urllib.parse

import httpx

from app.config import settings
from app.services.billing.razorpay.monitoring import (
    AlertCondition,
    BillingHealthReport,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.services.billing.razorpay.alerting")

_SEVERITY_RANK: dict[str, int] = {
    "warning": 1,
    "critical": 2,
}


class AlertDeliveryError(Exception):
    """Raised when an enabled alert delivery sink fails or times out."""


class AlertConfigurationError(Exception):
    """Raised when opt-in alert delivery configuration is invalid."""


@dataclass(frozen=True)
class AlertDispatchItem:
    """Single edge-triggered alert transition or recovery event."""

    event_type: str  # "firing" | "escalated" | "resolved"
    code: str
    severity: str
    message: str
    metric_value: float | int | None = None
    threshold: float | int | None = None
    previous_severity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "code": self.code,
            "severity": self.severity,
            "previous_severity": self.previous_severity,
            "message": sanitize_billing_error_message(self.message, max_chars=512),
            "metric_value": self.metric_value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class AlertNotificationPayload:
    """Structured, secret-free notification envelope emitted to alert sinks."""

    checked_at: datetime
    environment_filter: str | None
    overall_status: str
    events: tuple[AlertDispatchItem, ...]
    active_alert_codes: tuple[str, ...]
    suppressed_alert_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "environment_filter": self.environment_filter,
            "overall_status": self.overall_status,
            "events": [ev.to_dict() for ev in self.events],
            "active_alert_codes": list(self.active_alert_codes),
            "suppressed_alert_codes": list(self.suppressed_alert_codes),
        }


@dataclass(frozen=True)
class AlertDispatchResult:
    """Outcome of evaluating deduplication and invoking an AlertSink."""

    attempted_delivery: bool
    delivery_succeeded: bool
    emitted_events: tuple[AlertDispatchItem, ...]
    suppressed_codes: tuple[str, ...]
    resolved_codes: tuple[str, ...]
    error_message: str | None = None
    sink_name: str = "noop"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sink_name": self.sink_name,
            "attempted_delivery": self.attempted_delivery,
            "delivery_succeeded": self.delivery_succeeded,
            "emitted_count": len(self.emitted_events),
            "emitted_events": [ev.to_dict() for ev in self.emitted_events],
            "suppressed_count": len(self.suppressed_codes),
            "suppressed_codes": list(self.suppressed_codes),
            "resolved_codes": list(self.resolved_codes),
            "error_message": self.error_message,
        }


# =============================================================================
# Pluggable Deduplication State Store
# =============================================================================


class AlertStateStore(Protocol):
    """Persistence interface for edge-triggered alert deduplication state."""

    def load_scope(self, scope_key: str) -> dict[str, dict[str, Any]]:
        """Load active alert map (code -> metadata) for the given scope."""

    def save_scope(self, scope_key: str, active_alerts: dict[str, dict[str, Any]]) -> None:
        """Persist updated active alert map for the given scope."""


class NullAlertStateStore:
    """Stateless store that never caches prior active alerts (every active condition fires)."""

    def load_scope(self, scope_key: str) -> dict[str, dict[str, Any]]:
        return {}

    def save_scope(self, scope_key: str, active_alerts: dict[str, dict[str, Any]]) -> None:
        return None


class InMemoryAlertStateStore:
    """Process-local in-memory state store for tests and persistent daemon invocations."""

    def __init__(self) -> None:
        self._scopes: dict[str, dict[str, dict[str, Any]]] = {}

    def load_scope(self, scope_key: str) -> dict[str, dict[str, Any]]:
        raw = self._scopes.get(scope_key, {})
        return {k: dict(v) for k, v in raw.items()}

    def save_scope(self, scope_key: str, active_alerts: dict[str, dict[str, Any]]) -> None:
        self._scopes[scope_key] = {k: dict(v) for k, v in active_alerts.items()}


class FileAlertStateStore:
    """Atomic local JSON-file state store for cron invocations with persistent disk.

    Limitation: Host-local only. On ephemeral/stateless containers without persistent
    volumes (such as default Render Cron Jobs), local files are discarded across runs;
    cross-host durable deduplication would require a dedicated DB migration.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"scopes": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"scopes": {}}
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("scopes"), dict):
                return parsed
        except Exception as exc:
            logger.warning(
                "Corrupted or unreadable alert state file at %s (%s); starting cleanly.",
                self.path.name,
                sanitize_billing_error_message(exc),
            )
        return {"scopes": {}}

    def load_scope(self, scope_key: str) -> dict[str, dict[str, Any]]:
        data = self._read_all()
        scope = data["scopes"].get(scope_key, {})
        if not isinstance(scope, dict):
            return {}
        return {
            str(k): dict(v)
            for k, v in scope.items()
            if isinstance(k, str) and isinstance(v, dict)
        }

    def save_scope(self, scope_key: str, active_alerts: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read_all()
        data["scopes"][scope_key] = {k: dict(v) for k, v in active_alerts.items()}
        serialized = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


# =============================================================================
# Pluggable Alert Sinks
# =============================================================================


class AlertSink(Protocol):
    """Pluggable alert notification delivery sink."""

    name: str

    async def send(self, payload: AlertNotificationPayload) -> None:
        """Deliver alert notification envelope or raise AlertDeliveryError."""


class NoOpAlertSink:
    """Default sink when external alert delivery is disabled."""

    name: str = "noop"

    async def send(self, payload: AlertNotificationPayload) -> None:
        return None


class LogAlertSink:
    """Delivers sanitized alert transition events via Python structured logging."""

    name: str = "log"

    async def send(self, payload: AlertNotificationPayload) -> None:
        for ev in payload.events:
            if ev.event_type == "resolved":
                logger.info(
                    "Billing alert RESOLVED [%s] (env=%s): %s",
                    ev.code,
                    payload.environment_filter or "all",
                    sanitize_billing_error_message(ev.message, max_chars=512),
                )
            elif ev.severity == "critical":
                logger.error(
                    "Billing alert %s [%s] (%s, env=%s): %s",
                    ev.event_type.upper(),
                    ev.code,
                    ev.severity,
                    payload.environment_filter or "all",
                    sanitize_billing_error_message(ev.message, max_chars=512),
                )
            else:
                logger.warning(
                    "Billing alert %s [%s] (%s, env=%s): %s",
                    ev.event_type.upper(),
                    ev.code,
                    ev.severity,
                    payload.environment_filter or "all",
                    sanitize_billing_error_message(ev.message, max_chars=512),
                )


def validate_alert_webhook_url(raw_url: str) -> str:
    """Validate opt-in webhook destination URL and reject embedded credentials."""
    clean = (raw_url or "").strip()
    if not clean:
        raise AlertConfigurationError(
            "BILLING_HEALTH_ALERT_WEBHOOK_URL must be configured when webhook alerting is enabled."
        )
    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise AlertConfigurationError(
            "BILLING_HEALTH_ALERT_WEBHOOK_URL must be a valid http:// or https:// URL."
        )
    if parsed.username is not None or parsed.password is not None:
        raise AlertConfigurationError(
            "BILLING_HEALTH_ALERT_WEBHOOK_URL must not contain embedded userinfo credentials."
        )
    return clean


class WebhookAlertSink:
    """Strictly opt-in HTTP JSON webhook sink with bounded timeouts and secret safety."""

    name: str = "webhook"

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = validate_alert_webhook_url(url)
        self._bearer_token = (bearer_token or "").strip() or None
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
        self._transport = transport

    async def send(self, payload: AlertNotificationPayload) -> None:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Aptlyst-AI-BillingHealth/1.0",
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.url,
                    json=payload.to_dict(),
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise AlertDeliveryError(
                f"Alert webhook request timed out: {sanitize_billing_error_message(exc)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AlertDeliveryError(
                f"Alert webhook transport error: {sanitize_billing_error_message(exc)}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise AlertDeliveryError(
                f"Alert webhook endpoint rejected notification with HTTP {response.status_code}."
            )


def resolve_alert_sink_from_settings(
    *,
    notify: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AlertSink:
    """Resolve alert sink from environment settings; webhook remains disabled unless opted in."""
    if not notify:
        return NoOpAlertSink()

    if bool(getattr(settings, "billing_health_alert_webhook_enabled", False)):
        url = str(getattr(settings, "billing_health_alert_webhook_url", "") or "")
        secret_obj = getattr(settings, "billing_health_alert_webhook_bearer_token", None)
        token = (
            secret_obj.get_secret_value()
            if hasattr(secret_obj, "get_secret_value")
            else str(secret_obj or "")
        )
        timeout = float(getattr(settings, "billing_health_alert_webhook_timeout_seconds", 5.0))
        return WebhookAlertSink(
            url=url,
            bearer_token=token,
            timeout_seconds=timeout,
            transport=transport,
        )

    return LogAlertSink()


def resolve_alert_state_store_from_settings(
    *,
    state_file: str | None = None,
) -> AlertStateStore:
    """Resolve alert deduplication state store from CLI flag or settings."""
    raw = (
        state_file
        if state_file is not None
        else str(getattr(settings, "billing_health_alert_state_file", "") or "")
    )
    clean = raw.strip()
    if clean:
        return FileAlertStateStore(clean)
    return NullAlertStateStore()


# =============================================================================
# Edge-Triggered Evaluation & Delivery Orchestrator
# =============================================================================


def build_evaluation_failure_report(
    *,
    exc: BaseException,
    environment: str | None,
    checked_at: datetime | None = None,
) -> BillingHealthReport:
    """Construct a sanitized unhealthy BillingHealthReport on monitoring/DB evaluation failure."""
    from app.services.billing.razorpay.monitoring import (
        ReconciliationMetrics,
        RecoveryQueueMetrics,
        WebhookQueueMetrics,
    )

    now_utc = checked_at or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    sanitized_err = sanitize_billing_error_message(exc, max_chars=256) or type(exc).__name__
    alert = AlertCondition(
        code="MONITORING_EVALUATION_FAILED",
        severity="critical",
        message=f"Billing health evaluation failed ({type(exc).__name__}): {sanitized_err}",
        metric_value=1,
        threshold=0,
    )
    return BillingHealthReport(
        checked_at=now_utc,
        environment_filter=environment,
        overall_status="unhealthy",
        workers=[],
        webhook_queue=WebhookQueueMetrics(0, 0, 0, 0, None, None, None),
        recovery_queue=RecoveryQueueMetrics(0, 0, 0, 0, 0, None, None, None),
        reconciliation=ReconciliationMetrics(0, None, None, 0),
        alerts=[alert],
    )


async def dispatch_billing_alerts(
    report: BillingHealthReport,
    *,
    sink: AlertSink,
    state_store: AlertStateStore,
    renotify_interval_seconds: int = 0,
    notify_on_recovery: bool = True,
) -> AlertDispatchResult:
    """Evaluate edge-triggered deduplication and deliver alerts outside DB transactions."""
    scope_key = f"env:{report.environment_filter or 'all'}"
    prior_active = state_store.load_scope(scope_key)
    now_utc = report.checked_at
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    emitted: list[AlertDispatchItem] = []
    suppressed_codes: list[str] = []
    resolved_codes: list[str] = []
    next_state: dict[str, dict[str, Any]] = {}

    current_by_code: dict[str, AlertCondition] = {}
    for alert in report.alerts:
        # Keep the highest severity if the same code appears more than once
        existing = current_by_code.get(alert.code)
        if existing is None or _SEVERITY_RANK.get(alert.severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
            current_by_code[alert.code] = alert

    for code, alert in sorted(current_by_code.items()):
        prev = prior_active.get(code)
        if prev is None:
            emitted.append(
                AlertDispatchItem(
                    event_type="firing",
                    code=code,
                    severity=alert.severity,
                    message=alert.message,
                    metric_value=alert.metric_value,
                    threshold=alert.threshold,
                )
            )
            next_state[code] = {
                "severity": alert.severity,
                "first_seen_at": now_utc.isoformat(),
                "last_notified_at": now_utc.isoformat(),
            }
            continue

        prev_sev = str(prev.get("severity", "warning"))
        prev_rank = _SEVERITY_RANK.get(prev_sev, 0)
        curr_rank = _SEVERITY_RANK.get(alert.severity, 0)

        should_renotify = False
        if curr_rank > prev_rank:
            emitted.append(
                AlertDispatchItem(
                    event_type="escalated",
                    code=code,
                    severity=alert.severity,
                    previous_severity=prev_sev,
                    message=alert.message,
                    metric_value=alert.metric_value,
                    threshold=alert.threshold,
                )
            )
            should_renotify = True
        elif renotify_interval_seconds > 0:
            raw_last = prev.get("last_notified_at")
            try:
                last_dt = datetime.fromisoformat(str(raw_last))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() >= renotify_interval_seconds:
                    emitted.append(
                        AlertDispatchItem(
                            event_type="firing",
                            code=code,
                            severity=alert.severity,
                            message=alert.message,
                            metric_value=alert.metric_value,
                            threshold=alert.threshold,
                        )
                    )
                    should_renotify = True
            except Exception:
                pass

        if not should_renotify:
            suppressed_codes.append(code)

        next_state[code] = {
            "severity": alert.severity,
            "first_seen_at": str(prev.get("first_seen_at", now_utc.isoformat())),
            "last_notified_at": (
                now_utc.isoformat()
                if should_renotify
                else str(prev.get("last_notified_at", now_utc.isoformat()))
            ),
        }

    # Detect recovered incidents (cleared from active set so recurrence re-alerts cleanly)
    for old_code, prev_meta in sorted(prior_active.items()):
        if old_code not in current_by_code:
            resolved_codes.append(old_code)
            if notify_on_recovery:
                emitted.append(
                    AlertDispatchItem(
                        event_type="resolved",
                        code=old_code,
                        severity=str(prev_meta.get("severity", "warning")),
                        message=f"Billing alert {old_code} resolved.",
                    )
                )

    if not emitted or getattr(sink, "name", "") == "noop":
        # Update cleared recoveries if no delivery needed or noop
        if getattr(sink, "name", "") != "noop" and resolved_codes and not notify_on_recovery:
            state_store.save_scope(scope_key, next_state)
        elif getattr(sink, "name", "") == "noop" and not emitted:
            pass
        return AlertDispatchResult(
            attempted_delivery=False,
            delivery_succeeded=True,
            emitted_events=tuple(emitted),
            suppressed_codes=tuple(suppressed_codes),
            resolved_codes=tuple(resolved_codes),
            sink_name=getattr(sink, "name", "noop"),
        )

    payload = AlertNotificationPayload(
        checked_at=now_utc,
        environment_filter=report.environment_filter,
        overall_status=report.overall_status,
        events=tuple(emitted),
        active_alert_codes=tuple(sorted(current_by_code.keys())),
        suppressed_alert_codes=tuple(suppressed_codes),
    )

    try:
        await sink.send(payload)
    except AlertDeliveryError as exc:
        safe_err = sanitize_billing_error_message(exc, max_chars=256)
        logger.error("Alert delivery failed via sink '%s': %s", sink.name, safe_err)
        # Do NOT update state_store on delivery failure so the alert retries cleanly next tick
        return AlertDispatchResult(
            attempted_delivery=True,
            delivery_succeeded=False,
            emitted_events=tuple(emitted),
            suppressed_codes=tuple(suppressed_codes),
            resolved_codes=tuple(resolved_codes),
            error_message=safe_err,
            sink_name=sink.name,
        )

    state_store.save_scope(scope_key, next_state)
    return AlertDispatchResult(
        attempted_delivery=True,
        delivery_succeeded=True,
        emitted_events=tuple(emitted),
        suppressed_codes=tuple(suppressed_codes),
        resolved_codes=tuple(resolved_codes),
        sink_name=sink.name,
    )
