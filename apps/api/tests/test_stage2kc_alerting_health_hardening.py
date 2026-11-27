"""Focused unit tests for Stage 2K-C:
1. Operational billing alerting conditions and threshold boundaries.
2. Edge-triggered deduplication, severity escalation, recovery, and recurrence.
3. Opt-in WebhookAlertSink timeouts, failures, secret redaction, and disabled defaults.
4. billing_health CLI JSON output, read-only guarantee, and scheduler exit codes (0/1/2).
5. Worker ownership-loss claim-loop halt, shutdown fencing, and heartbeat retention guards.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest

from app.config import Settings
from app.services.billing.razorpay.alerting import (
    AlertConfigurationError,
    AlertDispatchItem,
    AlertNotificationPayload,
    FileAlertStateStore,
    InMemoryAlertStateStore,
    LogAlertSink,
    NoOpAlertSink,
    WebhookAlertSink,
    dispatch_billing_alerts,
    resolve_alert_sink_from_settings,
    validate_alert_webhook_url,
)
from app.services.billing.razorpay.monitoring import (
    WORKER_TYPE_RECOVERY,
    WORKER_TYPE_WEBHOOK,
    AlertCondition,
    BillingHealthReport,
    BillingHealthThresholds,
    ReconciliationMetrics,
    RecoveryQueueMetrics,
    WebhookQueueMetrics,
    WorkerHeartbeatTracker,
    WorkerLivenessSnapshot,
    cleanup_stale_worker_heartbeats,
    evaluate_billing_health_alerts,
)
from app.workers.billing_health import run_billing_health_check
from app.workers.razorpay_checkout_recovery_worker import RazorpayCheckoutRecoveryWorker
from app.workers.razorpay_webhook_worker import RazorpayWebhookWorker


def _healthy_workers(now: datetime) -> list[WorkerLivenessSnapshot]:
    return [
        WorkerLivenessSnapshot(
            worker_id="wh_1",
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test",
            status="running",
            last_heartbeat_at=now - timedelta(seconds=10),
            heartbeat_age_seconds=10.0,
            is_stale=False,
            last_processed_at=now - timedelta(seconds=10),
            last_error_at=None,
            processed_count=10,
            error_count=0,
        ),
        WorkerLivenessSnapshot(
            worker_id="rec_1",
            worker_type=WORKER_TYPE_RECOVERY,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test",
            status="running",
            last_heartbeat_at=now - timedelta(seconds=10),
            heartbeat_age_seconds=10.0,
            is_stale=False,
            last_processed_at=now - timedelta(seconds=10),
            last_error_at=None,
            processed_count=4,
            error_count=0,
        ),
    ]


# =============================================================================
# 1. Alert Conditions & Threshold Boundaries
# =============================================================================


def test_2kc_alert_threshold_boundaries_and_all_conditions() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    thresholds = BillingHealthThresholds(
        heartbeat_stale_seconds=90,
        webhook_backlog_age_seconds=300,
        webhook_backlog_count_threshold=50,
        webhook_failed_count_threshold=5,
        recovery_overdue_age_seconds=300,
        reconciliation_backlog_age_seconds=3600,
        repeated_lease_expiry_threshold=2,
    )

    # Just below every threshold -> healthy
    status_below, alerts_below = evaluate_billing_health_alerts(
        workers=_healthy_workers(now),
        webhook_queue=WebhookQueueMetrics(45, 4, 0, 1, now - timedelta(seconds=300), 300.0, now),
        recovery_queue=RecoveryQueueMetrics(1, 0, 0, 1, 0, now - timedelta(seconds=300), 300.0, now),
        reconciliation=ReconciliationMetrics(0, None, None, 0),
        thresholds=thresholds,
    )
    assert status_below == "healthy"
    assert alerts_below == []

    # Trigger webhook backlog count (warning at 50), repeated failed webhooks (5),
    # expired webhook leases (2), expired recovery claims (2), backlog age (301s), recovery overdue (301s)
    status_warn, alerts_warn = evaluate_billing_health_alerts(
        workers=_healthy_workers(now),
        webhook_queue=WebhookQueueMetrics(45, 5, 0, 2, now - timedelta(seconds=301), 301.0, now),
        recovery_queue=RecoveryQueueMetrics(1, 0, 0, 2, 0, now - timedelta(seconds=301), 301.0, now),
        reconciliation=ReconciliationMetrics(1, now - timedelta(seconds=100), 100.0, 1),
        thresholds=thresholds,
    )
    assert status_warn == "unhealthy"
    by_code = {a.code: a for a in alerts_warn}
    assert by_code["WEBHOOK_BACKLOG_COUNT_EXCEEDED"].severity == "warning"
    assert by_code["REPEATED_WEBHOOK_FAILURES"].severity == "warning"
    assert by_code["WEBHOOK_EXPIRED_LEASES"].severity == "warning"
    assert by_code["RECOVERY_EXPIRED_CLAIMS"].severity == "warning"
    assert by_code["WEBHOOK_WORKER_STALLED_OR_BACKLOGGED"].severity == "critical"
    assert by_code["RECOVERY_WORKER_STALLED_OR_BACKLOGGED"].severity == "critical"
    assert by_code["PENDING_RECONCILIATION_CASES"].severity == "warning"

    # Backlog >= 2x escalates WEBHOOK_BACKLOG_COUNT_EXCEEDED to critical
    _, alerts_crit = evaluate_billing_health_alerts(
        workers=_healthy_workers(now),
        webhook_queue=WebhookQueueMetrics(95, 5, 0, 0, now - timedelta(seconds=10), 10.0, now),
        recovery_queue=RecoveryQueueMetrics(0, 0, 0, 0, 0, None, None, now),
        reconciliation=ReconciliationMetrics(0, None, None, 0),
        thresholds=thresholds,
    )
    assert {a.code: a.severity for a in alerts_crit}["WEBHOOK_BACKLOG_COUNT_EXCEEDED"] == "critical"


# =============================================================================
# 2. Deduplication, Escalation, Recovery & Recurrence
# =============================================================================


class _RecordingSink:
    name = "recording"

    def __init__(self, *, should_fail: bool = False) -> None:
        self.payloads: list[AlertNotificationPayload] = []
        self.should_fail = should_fail

    async def send(self, payload: AlertNotificationPayload) -> None:
        if self.should_fail:
            from app.services.billing.razorpay.alerting import AlertDeliveryError

            raise AlertDeliveryError("simulated delivery failure")
        self.payloads.append(payload)


def _make_report(
    now: datetime,
    alerts: list[AlertCondition],
    *,
    env: str = "test",
) -> BillingHealthReport:
    overall = (
        "unhealthy"
        if any(a.severity == "critical" for a in alerts)
        else ("degraded" if alerts else "healthy")
    )
    return BillingHealthReport(
        checked_at=now,
        environment_filter=env,
        overall_status=overall,
        workers=_healthy_workers(now),
        webhook_queue=WebhookQueueMetrics(0, 0, 0, 0, None, None, now),
        recovery_queue=RecoveryQueueMetrics(0, 0, 0, 0, 0, None, None, now),
        reconciliation=ReconciliationMetrics(0, None, None, 0),
        alerts=alerts,
    )


@pytest.mark.asyncio
async def test_2kc_deduplication_escalation_recovery_and_recurrence(tmp_path: Path) -> None:
    t0 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "alert_state.json"
    store = FileAlertStateStore(state_file)
    sink = _RecordingSink()

    # 1. Initial incident fires once
    alert_warn = AlertCondition("WEBHOOK_BACKLOG_COUNT_EXCEEDED", "warning", "backlog=55", 55, 50)
    res1 = await dispatch_billing_alerts(_make_report(t0, [alert_warn]), sink=sink, state_store=store)
    assert res1.attempted_delivery is True
    assert res1.delivery_succeeded is True
    assert len(res1.emitted_events) == 1
    assert res1.emitted_events[0].event_type == "firing"

    # 2. Unchanged active incident on next tick is suppressed (deduplicated)
    res2 = await dispatch_billing_alerts(
        _make_report(t0 + timedelta(minutes=1), [alert_warn]),
        sink=sink,
        state_store=store,
    )
    assert res2.attempted_delivery is False
    assert res2.emitted_events == ()
    assert res2.suppressed_codes == ("WEBHOOK_BACKLOG_COUNT_EXCEEDED",)

    # 3. Severity escalation warning -> critical triggers escalated delivery
    alert_crit = AlertCondition("WEBHOOK_BACKLOG_COUNT_EXCEEDED", "critical", "backlog=110", 110, 50)
    res3 = await dispatch_billing_alerts(
        _make_report(t0 + timedelta(minutes=2), [alert_crit]),
        sink=sink,
        state_store=store,
    )
    assert res3.attempted_delivery is True
    assert res3.emitted_events[0].event_type == "escalated"
    assert res3.emitted_events[0].previous_severity == "warning"

    # 4. Incident resolves -> emits resolved & clears active state
    res4 = await dispatch_billing_alerts(
        _make_report(t0 + timedelta(minutes=3), []),
        sink=sink,
        state_store=store,
    )
    assert res4.attempted_delivery is True
    assert res4.resolved_codes == ("WEBHOOK_BACKLOG_COUNT_EXCEEDED",)
    assert res4.emitted_events[0].event_type == "resolved"

    # 5. Same incident recurs later -> emits fresh firing alert
    res5 = await dispatch_billing_alerts(
        _make_report(t0 + timedelta(minutes=10), [alert_warn]),
        sink=sink,
        state_store=store,
    )
    assert res5.attempted_delivery is True
    assert res5.emitted_events[0].event_type == "firing"


@pytest.mark.asyncio
async def test_2kc_delivery_failure_preserves_retry_and_does_not_mask_error() -> None:
    t0 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    store = InMemoryAlertStateStore()
    failing_sink = _RecordingSink(should_fail=True)
    alert = AlertCondition("STALE_WORKER_HEARTBEAT", "critical", "stale", 120.0, 90)

    res_fail = await dispatch_billing_alerts(
        _make_report(t0, [alert]), sink=failing_sink, state_store=store
    )
    assert res_fail.attempted_delivery is True
    assert res_fail.delivery_succeeded is False
    assert "simulated delivery failure" in (res_fail.error_message or "")
    # Because delivery failed, state_store remains empty so next run retries!
    assert store.load_scope("env:test") == {}

    ok_sink = _RecordingSink()
    res_retry = await dispatch_billing_alerts(
        _make_report(t0 + timedelta(minutes=1), [alert]), sink=ok_sink, state_store=store
    )
    assert res_retry.delivery_succeeded is True
    assert len(ok_sink.payloads) == 1


# =============================================================================
# 3. Webhook Sink Security, Timeouts, Defaults & Evaluation Failure Exit Codes
# =============================================================================


@pytest.mark.asyncio
async def test_2kc_webhook_sink_defaults_sanitization_timeout_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default settings keep webhook disabled -> LogAlertSink when notify=True, NoOp when False
    assert isinstance(resolve_alert_sink_from_settings(notify=False), NoOpAlertSink)
    assert isinstance(resolve_alert_sink_from_settings(notify=True), LogAlertSink)

    s = Settings(
        process_role="migration",
        environment="staging",
        database_url="postgresql+asyncpg://postgres:pw@localhost:5432/test",
    )
    assert s.billing_health_alert_webhook_enabled is False

    # Reject missing or credentialed URLs
    with pytest.raises(AlertConfigurationError):
        validate_alert_webhook_url("")
    with pytest.raises(AlertConfigurationError):
        validate_alert_webhook_url("https://user:pass@alerts.example.com/hook")

    # Timeout and HTTP 500 handling via httpx.MockTransport (no real network)
    async def _timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    timeout_sink = WebhookAlertSink(
        url="https://alerts.example.com/hook",
        bearer_token="BearerSuperSecret99",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_timeout_handler),
    )
    payload = AlertNotificationPayload(
        checked_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
        environment_filter="test",
        overall_status="unhealthy",
        events=(
            AlertDispatchItem(
                event_type="firing",
                code="MONITORING_EVALUATION_FAILED",
                severity="critical",
                message="postgresql://postgres:DbSecret99@db.internal:5432/app",
            ),
        ),
        active_alert_codes=("MONITORING_EVALUATION_FAILED",),
    )
    assert "DbSecret99" not in json.dumps(payload.to_dict())

    res_timeout = await dispatch_billing_alerts(
        _make_report(
            payload.checked_at,
            [AlertCondition("MONITORING_EVALUATION_FAILED", "critical", "DbSecret99 failed", 1, 0)],
        ),
        sink=timeout_sink,
        state_store=InMemoryAlertStateStore(),
    )
    assert res_timeout.delivery_succeeded is False

    captured_headers: dict[str, str] = {}

    async def _error_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(502, text="bad gateway")

    err_sink = WebhookAlertSink(
        url="https://alerts.example.com/hook",
        bearer_token="SecretTokenXYZ",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_error_handler),
    )
    res_502 = await dispatch_billing_alerts(
        _make_report(
            payload.checked_at,
            [AlertCondition("STALE_WORKER_HEARTBEAT", "critical", "stale", 100, 90)],
        ),
        sink=err_sink,
        state_store=InMemoryAlertStateStore(),
    )
    assert res_502.delivery_succeeded is False
    assert captured_headers.get("authorization") == "Bearer SecretTokenXYZ"
    assert "SecretTokenXYZ" not in json.dumps(res_502.to_dict())


@pytest.mark.asyncio
async def test_2kc_cli_db_failure_returns_sanitized_json_and_notifies_and_delivery_fail_exits_2() -> None:
    class _FailingSessionFactory:
        def __call__(self) -> "_FailingSessionFactory":
            return self

        async def __aenter__(self):
            raise ConnectionRefusedError(
                "connect failed to postgresql://user:SecretDbPw@internal.host:5432/db"
            )

        async def __aexit__(self, *args):
            return None

    sink_ok = _RecordingSink(should_fail=False)
    code_db_err, out_db_err = await run_billing_health_check(
        session_factory=_FailingSessionFactory(),  # type: ignore[arg-type]
        environment="test",
        output_json=True,
        fail_on_alert=True,
        notify=True,
        alert_sink=sink_ok,
        state_store=InMemoryAlertStateStore(),
    )
    assert code_db_err == 1
    assert "SecretDbPw" not in out_db_err
    parsed = json.loads(out_db_err)
    assert parsed["overall_status"] == "unhealthy"
    assert parsed["alerts"][0]["code"] == "MONITORING_EVALUATION_FAILED"
    assert parsed["alert_delivery"]["delivery_succeeded"] is True
    assert len(sink_ok.payloads) == 1

    # Delivery failure -> exit code 2 (even when evaluation ran)
    sink_fail = _RecordingSink(should_fail=True)
    code_del_fail, out_del_fail = await run_billing_health_check(
        session_factory=_FailingSessionFactory(),  # type: ignore[arg-type]
        environment="test",
        output_json=True,
        fail_on_alert=True,
        notify=True,
        alert_sink=sink_fail,
        state_store=InMemoryAlertStateStore(),
    )
    assert code_del_fail == 2
    assert json.loads(out_del_fail)["alert_delivery"]["delivery_succeeded"] is False


# =============================================================================
# 4. Worker Ownership Loss, Shutdown Fencing & Retention Validation
# =============================================================================


@pytest.mark.asyncio
async def test_2kc_webhook_and_recovery_workers_halt_immediately_on_ownership_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    # Mock webhook worker where first run heartbeat detects ownership_lost=True
    wh_worker = RazorpayWebhookWorker(
        session_factory=lambda: AsyncMock(),  # type: ignore[arg-type,return-value]
        worker_id="wh_superseded_1",
        environment="test",
        clock=lambda: now,
        poll_interval=0.01,
    )
    wh_claims = 0

    async def _wh_process_one():
        nonlocal wh_claims
        wh_claims += 1
        return None

    monkeypatch.setattr(wh_worker, "process_one", _wh_process_one)

    statuses_seen: list[str] = []

    async def _wh_hb(session_factory, *, status: str, **kwargs) -> bool:
        statuses_seen.append(status)
        wh_worker.heartbeat_tracker.ownership_lost = True
        return False

    monkeypatch.setattr(wh_worker.heartbeat_tracker, "record_heartbeat", _wh_hb)
    await wh_worker.start()
    # Must NOT execute claim loop or emit "stopped" overwrite after ownership loss!
    assert wh_claims == 0
    assert statuses_seen == ["running"]
    assert "stopped" not in statuses_seen

    # Also test record_heartbeat itself refuses "stopped" when ownership_lost is already True
    tracker = WorkerHeartbeatTracker(
        worker_id="w_fenced",
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test",
    )
    tracker.ownership_lost = True
    unused_factory = AsyncMock()
    assert await tracker.record_heartbeat(unused_factory, status="stopped", force=True) is False
    unused_factory.assert_not_called()

    # Recovery worker: loses ownership after first batch heartbeat -> stops without 2nd claim!
    monkeypatch.setattr(Settings, "razorpay_enabled", True, raising=False)
    monkeypatch.setattr(
        "app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled",
        lambda: True,
    )
    mock_client = AsyncMock()
    mock_client.environment = "test"
    mock_client.merchant_account_id = None
    rec_worker = RazorpayCheckoutRecoveryWorker(
        session_factory=lambda: AsyncMock(),  # type: ignore[arg-type,return-value]
        worker_id="rec_superseded_1",
        environment="test",
        clock=lambda: now,
        poll_interval=0.01,
        client=mock_client,
    )
    rec_claims = 0

    async def _rec_process_one():
        nonlocal rec_claims
        rec_claims += 1
        rec_worker.heartbeat_tracker.ownership_lost = True
        return None

    rec_statuses: list[str] = []

    async def _rec_hb(session_factory, *, status: str, **kwargs) -> bool:
        rec_statuses.append(status)
        if len(rec_statuses) >= 2:
            rec_worker.heartbeat_tracker.ownership_lost = True
            return False
        return True

    monkeypatch.setattr(rec_worker, "process_one", _rec_process_one)
    monkeypatch.setattr(rec_worker.heartbeat_tracker, "record_heartbeat", _rec_hb)
    await rec_worker.start()
    assert rec_claims == 1
    assert "stopped" not in rec_statuses


@pytest.mark.asyncio
async def test_2kc_worker_cancellation_does_not_write_stopped_heartbeat() -> None:
    wh_worker = RazorpayWebhookWorker(
        session_factory=lambda: AsyncMock(),  # type: ignore[arg-type,return-value]
        worker_id="wh_cancel_1",
        environment="test",
        poll_interval=10.0,
    )
    statuses: list[str] = []

    async def _hb(session_factory, *, status: str, **kwargs) -> bool:
        statuses.append(status)
        return True

    wh_worker.heartbeat_tracker.record_heartbeat = _hb  # type: ignore[method-assign]
    wh_worker.run_batch = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await wh_worker.start()

    assert "stopped" not in statuses


@pytest.mark.asyncio
async def test_2kc_heartbeat_retention_rejects_unsafe_floor() -> None:
    db = AsyncMock()
    with pytest.raises(ValueError, match="stopped_retention_seconds"):
        await cleanup_stale_worker_heartbeats(db, stopped_retention_seconds=60, stale_takeover_seconds=90)
    with pytest.raises(ValueError, match="abandoned_retention_seconds"):
        await cleanup_stale_worker_heartbeats(db, abandoned_retention_seconds=60, stale_takeover_seconds=90)
    with pytest.raises(ValueError, match="batch_size"):
        await cleanup_stale_worker_heartbeats(db, batch_size=0)
    db.execute.assert_not_called()
