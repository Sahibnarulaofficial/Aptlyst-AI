"""Focused regression tests for Stage 2J:
1. Process-specific configuration isolation (API, webhook worker, recovery worker, Alembic).
2. Configurable CORS origin validation and FastAPI CORS middleware enforcement.
3. Worker lifecycle, SIGTERM graceful shutdown, engine disposal, and recovery lease/fencing hardening.
4. Worker heartbeat throttling, non-fatal DB failure isolation, concurrency protection, and queue monitoring alerts.
5. Alembic migration a4b5c6d7e8f3 (billing_worker_heartbeats) offline DDL & metadata verification.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest

from app.config import (
    Settings,
    parse_and_validate_cors_origins,
)
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    BillingWorkerHeartbeat,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus, WebhookProcessingStatus
from app.services.billing.razorpay.monitoring import (
    WORKER_TYPE_RECOVERY,
    WORKER_TYPE_WEBHOOK,
    BillingHealthThresholds,
    RecoveryQueueMetrics,
    ReconciliationMetrics,
    WebhookQueueMetrics,
    WorkerHeartbeatTracker,
    WorkerLivenessSnapshot,
    collect_billing_health_snapshot,
    evaluate_billing_health_alerts,
)
from app.services.billing.razorpay.recovery_engine import (
    ClaimedRecoveryAttempt,
    RecoveryOutcome,
    StaleRecoveryClaimError,
    _record_recovery_outcome,
    claim_recovery_batch,
    execute_claimed_recovery_attempt,
)
from app.workers.billing_health import run_billing_health_check
from app.workers.razorpay_checkout_recovery_worker import (
    RazorpayCheckoutRecoveryWorker,
    run_recovery_worker,
    setup_signal_handlers as setup_recovery_signal_handlers,
)
from app.workers.razorpay_webhook_worker import (
    RazorpayWebhookWorker,
    run_worker as run_webhook_worker,
    setup_signal_handlers as setup_webhook_signal_handlers,
)

API_DIR = Path(__file__).resolve().parent.parent
HEARTBEAT_MIGRATION_FILE = (
    API_DIR
    / "alembic"
    / "versions"
    / "20261125_0100_a4b5c6d7e8f3_add_billing_worker_heartbeats.py"
)


# =============================================================================
# 1. Process-Specific Configuration Isolation (Stage 2J.1)
# =============================================================================


def test_2j1_api_fails_closed_when_required_prod_secrets_missing() -> None:
    """API process fails closed if visitor session secret or Razorpay ingress/API secrets are missing."""
    # Missing VISITOR_SESSION_SECRET in prod
    with pytest.raises(ValueError, match="VISITOR_SESSION_SECRET"):
        Settings(
            process_role="api",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            visitor_session_secret="",
        )

    # Missing RAZORPAY_WEBHOOK_SECRET in API when Razorpay enabled
    with pytest.raises(ValueError, match="RAZORPAY_WEBHOOK_SECRET"):
        Settings(
            process_role="api",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            visitor_session_secret="prod-visitor-secret-at-least-32-characters-long",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="acc_live_merchant_01",
            razorpay_key_id="rzp_live_1234567890",
            razorpay_key_secret=SecretStr("sec_live_123"),
            razorpay_webhook_secret=SecretStr(""),
            razorpay_pro_plan_id="plan_live_pro_123",
        )


def test_2j1_webhook_worker_initializes_without_unrelated_api_secrets() -> None:
    """Webhook worker succeeds in production without VISITOR_SESSION_SECRET, webhook secret, or API keys."""
    cfg = Settings(
        process_role="webhook_worker",
        environment="production",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
        visitor_session_secret="",
        razorpay_enabled=True,
        razorpay_environment="live",
        razorpay_merchant_account_id="acc_live_merchant_01",
        razorpay_key_id="",
        razorpay_key_secret=SecretStr(""),
        razorpay_webhook_secret=SecretStr(""),
        razorpay_pro_plan_id="",
    )
    assert cfg.process_role == "webhook_worker"
    assert cfg.razorpay_environment == "live"
    assert cfg.razorpay_merchant_account_id == "acc_live_merchant_01"


def test_2j1_webhook_worker_rejects_invalid_merchant_or_mismatched_credentials() -> None:
    """Webhook worker still fails closed on invalid merchant ID or leaked test keys in production."""
    with pytest.raises(ValueError, match="Production RAZORPAY_MERCHANT_ACCOUNT_ID"):
        Settings(
            process_role="webhook_worker",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="default",
        )

    with pytest.raises(ValueError, match="Test Razorpay key is prohibited in production"):
        Settings(
            process_role="webhook_worker",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="acc_live_merchant_01",
            razorpay_key_id="rzp_test_accidentally_leaked",
        )


def test_2j1_recovery_worker_requires_api_creds_but_not_webhook_or_visitor_secrets() -> None:
    """Recovery worker succeeds without VISITOR_SESSION_SECRET or RAZORPAY_WEBHOOK_SECRET,
    but fails closed if Razorpay API key/secret/plan are missing."""
    cfg = Settings(
        process_role="recovery_worker",
        environment="production",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
        visitor_session_secret="",
        razorpay_enabled=True,
        razorpay_environment="live",
        razorpay_merchant_account_id="acc_live_merchant_01",
        razorpay_key_id="rzp_live_1234567890",
        razorpay_key_secret=SecretStr("sec_live_123"),
        razorpay_webhook_secret=SecretStr(""),
        razorpay_pro_plan_id="plan_live_pro_123",
        razorpay_plan_currency="USD",
    )
    assert cfg.process_role == "recovery_worker"

    with pytest.raises(ValueError, match="RAZORPAY_KEY_ID"):
        Settings(
            process_role="recovery_worker",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="acc_live_merchant_01",
            razorpay_key_id="",
            razorpay_key_secret=SecretStr("sec_live_123"),
            razorpay_pro_plan_id="plan_live_pro_123",
        )


def test_2j1_prod_vs_nonprod_prefix_isolation_across_roles() -> None:
    """Production cannot use test credentials; non-production cannot use live credentials."""
    with pytest.raises(ValueError, match="RAZORPAY_ENVIRONMENT must be 'live' in production"):
        Settings(
            process_role="recovery_worker",
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id="acc_live_01",
            razorpay_key_id="rzp_live_1234567890",
            razorpay_key_secret=SecretStr("sec_live"),
            razorpay_pro_plan_id="plan_live_123",
        )

    with pytest.raises(ValueError, match="Live Razorpay key is prohibited in non-production"):
        Settings(
            process_role="recovery_worker",
            environment="staging",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id="acc_test_01",
            razorpay_key_id="rzp_live_should_fail",
            razorpay_key_secret=SecretStr("sec_test"),
            razorpay_pro_plan_id="plan_test_123",
        )


def test_2j1_alembic_initializes_without_api_only_secrets_and_fails_on_invalid_env() -> None:
    """Migration role requires DB & valid environment only; fails closed on blank DB or bad env."""
    cfg = Settings(
        process_role="migration",
        environment="production",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
        visitor_session_secret="",
        razorpay_enabled=True,
        razorpay_environment="test",
        razorpay_key_id="",
        razorpay_webhook_secret=SecretStr(""),
    )
    assert cfg.process_role == "migration"

    with pytest.raises(ValueError, match="Unrecognized deployment ENVIRONMENT"):
        Settings(
            process_role="migration",
            environment="unknown_env",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/db",
        )

    with pytest.raises(ValueError, match="DATABASE_URL must be explicitly configured"):
        Settings(
            process_role="migration",
            environment="production",
            database_url="   ",
        )


def test_2j1_subprocess_worker_and_alembic_startup_isolation() -> None:
    """Subprocess boot under production least-privilege env vars works without API secrets."""
    base_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("RAZORPAY_", "STRIPE_", "VISITOR_", "APTLYST_"))
    }

    # 1. Webhook worker import & init without VISITOR_SESSION_SECRET or RAZORPAY_WEBHOOK_SECRET / KEY_SECRET
    wh_env = {
        **base_env,
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
        "RAZORPAY_ENABLED": "true",
        "RAZORPAY_ENVIRONMENT": "live",
        "RAZORPAY_MERCHANT_ACCOUNT_ID": "acc_live_canonical",
    }
    wh_res = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from app.workers.razorpay_webhook_worker import RazorpayWebhookWorker; w = RazorpayWebhookWorker(); assert w.environment == 'live'",
        ],
        cwd=str(API_DIR),
        env=wh_env,
        capture_output=True,
        text=True,
    )
    assert wh_res.returncode == 0, f"Webhook worker isolated boot failed:\n{wh_res.stderr}"

    # 2. Recovery worker import & init without VISITOR_SESSION_SECRET or RAZORPAY_WEBHOOK_SECRET
    rec_env = {
        **base_env,
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
        "RAZORPAY_ENABLED": "true",
        "RAZORPAY_ENVIRONMENT": "live",
        "RAZORPAY_MERCHANT_ACCOUNT_ID": "acc_live_canonical",
        "RAZORPAY_KEY_ID": "rzp_live_1234567890",
        "RAZORPAY_KEY_SECRET": "sec_live_123456",
        "RAZORPAY_PRO_PLAN_ID": "plan_live_123456",
        "RAZORPAY_PLAN_CURRENCY": "USD",
    }
    rec_res = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from app.workers.razorpay_checkout_recovery_worker import RazorpayCheckoutRecoveryWorker; w = RazorpayCheckoutRecoveryWorker(); assert w.environment == 'live'",
        ],
        cwd=str(API_DIR),
        env=rec_env,
        capture_output=True,
        text=True,
    )
    assert rec_res.returncode == 0, f"Recovery worker isolated boot failed:\n{rec_res.stderr}"

    # 3. Alembic heads check with migration-only env vars in production
    mig_env = {
        **base_env,
        "APTLYST_PROCESS_ROLE": "migration",
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
    }
    mig_res = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "heads"],
        cwd=str(API_DIR),
        env=mig_env,
        capture_output=True,
        text=True,
    )
    assert mig_res.returncode == 0, f"Alembic isolated boot failed:\n{mig_res.stderr}"
    assert "a4b5c6d7e8f3" in mig_res.stdout


# =============================================================================
# 2. Configurable CORS & Security Validation (Stage 2J.2)
# =============================================================================


def test_2j2_cors_origin_parsing_and_security_validation() -> None:
    """CORS parser supports localhost/explicit HTTPS origins and rejects wildcards/invalid URLs."""
    origins = parse_and_validate_cors_origins(
        "http://localhost:3000, https://app.your-domain.example , https://staging.your-domain.example",
        is_production=True,
    )
    assert origins == [
        "http://localhost:3000",
        "https://app.your-domain.example",
        "https://staging.your-domain.example",
    ]

    for bad_origin in [
        "*",
        "https://*.your-domain.example",
        "ftp://app.your-domain.example",
        "https://app.your-domain.example/path",
        "https://app.your-domain.example?foo=1",
        "https://user:pass@app.your-domain.example",
    ]:
        with pytest.raises(ValueError):
            parse_and_validate_cors_origins(bad_origin, is_production=False)

    with pytest.raises(ValueError, match="must use https://"):
        parse_and_validate_cors_origins("http://app.your-domain.example", is_production=True)


@pytest.mark.asyncio
async def test_2j2_fastapi_cors_middleware_allows_configured_origin_and_blocks_arbitrary() -> None:
    """Verify FastAPI CORSMiddleware allows configured origin and omits untrusted origins."""
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed_resp = await client.options(
            "/api/v1/organizations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed_resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert allowed_resp.headers.get("access-control-allow-credentials") == "true"

        blocked_resp = await client.options(
            "/api/v1/organizations",
            headers={
                "Origin": "https://untrusted-attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in blocked_resp.headers


# =============================================================================
# 3. Worker Lifecycle, Shutdown Safety & Recovery Fencing (Stage 2J.3)
# =============================================================================


@pytest.mark.asyncio
async def test_2j3_workers_stop_claiming_new_work_when_shutdown_begins_and_dispose_engines() -> None:
    """Both workers halt claims immediately on stop() and dispose default engines on exit."""
    # Recovery worker stops claiming mid-batch when stop() is signaled
    rec_worker = RazorpayCheckoutRecoveryWorker(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        poll_interval=0.05,
        batch_size=5,
    )
    setup_recovery_signal_handlers(rec_worker)
    processed_rec = 0

    async def fake_rec_process_one():
        nonlocal processed_rec
        processed_rec += 1
        rec_worker.stop()
        return SimpleNamespace(outcome=RecoveryOutcome.RETRY_SCHEDULED)

    rec_worker.process_one = fake_rec_process_one  # type: ignore[method-assign]
    with patch(
        "app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled",
        return_value=True,
    ):
        await rec_worker.start()
    assert processed_rec == 1
    assert rec_worker._stop_event.is_set()

    # Recovery worker engine + client disposal on exit
    mock_engine = AsyncMock()
    mock_client = AsyncMock()
    mock_client.environment = "test"
    mock_client.merchant_account_id = None
    with (
        patch("app.db.session.engine", mock_engine),
        patch(
            "app.workers.razorpay_checkout_recovery_worker.RazorpayCheckoutRecoveryWorker.run_once",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        exit_code = await run_recovery_worker(run_once_only=True, session_factory=None, client=mock_client)
        assert exit_code == 0
        mock_engine.dispose.assert_awaited_once()
        mock_client.close.assert_awaited_once()

    # Webhook worker stops claiming mid-batch when stop() is signaled
    wh_worker = RazorpayWebhookWorker(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        poll_interval=0.05,
        batch_size=5,
    )
    setup_webhook_signal_handlers(wh_worker)
    processed_wh = 0

    async def fake_wh_process_one():
        nonlocal processed_wh
        processed_wh += 1
        wh_worker.stop()
        return SimpleNamespace(success=True)

    wh_worker.process_one = fake_wh_process_one  # type: ignore[method-assign]
    await wh_worker.start()
    assert processed_wh == 1
    assert wh_worker._stop_event.is_set()

    mock_wh_engine = AsyncMock()
    with (
        patch("app.db.session.engine", mock_wh_engine),
        patch(
            "app.workers.razorpay_webhook_worker.RazorpayWebhookWorker.run_once",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        exit_code = await run_webhook_worker(run_once_only=True, session_factory=None)
        assert exit_code == 0
        mock_wh_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_2j3_recovery_fencing_rejects_expired_lease_and_quarantined_null_column() -> None:
    """_record_recovery_outcome must reject expired leases even without reclaim, and reject NULL claim columns."""
    from tests.test_razorpay_checkout_recovery_worker_stage2d import (
        MockRazorpayClient,
        RecoveryWorkerMockDbSession,
        SharedRecoveryAttemptRepository,
        _create_attempt,
    )

    now = datetime(2026, 11, 25, 1, 0, 0, tzinfo=timezone.utc)
    claim_id = uuid.uuid4()

    # Case 1: Lease expired in DB before commit even though recovery_claim_id still matches
    att_expired = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now - timedelta(seconds=1),  # Expired!
    )
    repo_1 = SharedRecoveryAttemptRepository([att_expired])
    claimed_1 = ClaimedRecoveryAttempt(
        attempt_id=att_expired.id,
        organization_id=att_expired.organization_id,
        provider="razorpay",
        merchant_account_id=att_expired.merchant_account_id,
        environment="test",
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now - timedelta(seconds=1),
        recovery_attempts=0,
        created_at=att_expired.created_at,
        request_payload={},
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="slow_worker",
    )
    client_1 = MockRazorpayClient(
        subscription_data={"id": "sub_expired_safe_preserve", "plan_id": "plan_test_pro", "customer_id": "cust_test"}
    )
    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=lambda: RecoveryWorkerMockDbSession(repo_1, clock_now=now),
            claimed=claimed_1,
            client=client_1,
            known_subscription_id="sub_expired_safe_preserve",
            now_fn=lambda: now,
        )
    assert repo_1.get_attempt(att_expired.id).status == CheckoutAttemptStatus.recovery_required
    assert len(repo_1.reconciliations) == 1
    assert repo_1.reconciliations[0].case_key == f"recovery_superseded:{att_expired.id}:sub_expired_safe_preserve"

    # Case 2: Attempt was quarantined (recovery_claim_id=None, next_recovery_retry_at=None),
    # while request_payload still holds historical recovery_claim_id string. Must NOT resurrect!
    att_quarantined = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        recovery_claim_id=None,
        recovery_claim_expires_at=None,
        next_recovery_retry_at=None,
        request_payload={"recovery_claim_id": str(claim_id)},
    )
    att_quarantined.next_recovery_retry_at = None
    repo_2 = SharedRecoveryAttemptRepository([att_quarantined])
    with pytest.raises(StaleRecoveryClaimError):
        await _record_recovery_outcome(
            session_factory=lambda: RecoveryWorkerMockDbSession(repo_2, clock_now=now),
            claimed=claimed_1.__class__(
                **{**claimed_1.__dict__, "attempt_id": att_quarantined.id}
            ),
            outcome=RecoveryOutcome.RECOVERED,
            target_sub_id="sub_should_not_resurrect",
            now_utc=now,
        )
    assert repo_2.get_attempt(att_quarantined.id).status == CheckoutAttemptStatus.recovery_required
    assert repo_2.get_attempt(att_quarantined.id).next_recovery_retry_at is None


# =============================================================================
# 4. Worker Heartbeat & Queue Monitoring (Stage 2J.4)
# =============================================================================


@pytest.mark.asyncio
async def test_2j4_heartbeat_tracker_throttling_error_isolation_and_concurrency_sql() -> None:
    """Heartbeat tracker throttles DB writes, survives DB failures, and uses safe conflict guard."""
    now = datetime(2026, 11, 25, 1, 0, 0, tzinfo=timezone.utc)
    tracker = WorkerHeartbeatTracker(
        worker_id="worker_test_hb_01",
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_1",
        heartbeat_interval_seconds=30.0,
        started_at=now,
    )

    executed_stmts: list[str] = []

    class RecordingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            executed_stmts.append(str(stmt))

        async def commit(self):
            return None

    # First call flushes
    assert await tracker.record_heartbeat(lambda: RecordingSession(), status="running", now=now) is True
    assert len(executed_stmts) == 1
    assert "ON CONFLICT (worker_id) DO UPDATE" in executed_stmts[0]
    assert "instance_token" in executed_stmts[0]

    # Call 5 seconds later without processed_delta is throttled
    assert await tracker.record_heartbeat(
        lambda: RecordingSession(),
        status="running",
        now=now + timedelta(seconds=5),
    ) is False
    assert len(executed_stmts) == 1

    # Call 31 seconds later flushes
    assert await tracker.record_heartbeat(
        lambda: RecordingSession(),
        status="running",
        now=now + timedelta(seconds=31),
    ) is True
    assert len(executed_stmts) == 2

    # DB failure during heartbeat never raises and backs off
    class FailingSession:
        async def __aenter__(self):
            raise RuntimeError("simulated db outage")

        async def __aexit__(self, *args):
            return None

    assert await tracker.record_heartbeat(
        lambda: FailingSession(),
        status="running",
        now=now + timedelta(seconds=62),
        force=True,
    ) is False
    # Retry 2s later is backed off due to _last_attempt_at
    assert await tracker.record_heartbeat(
        lambda: FailingSession(),
        status="error",
        now=now + timedelta(seconds=64),
        error_message="err",
    ) is False
    # Clean shutdown ("stopped") always attempts flush
    assert await tracker.record_heartbeat(
        lambda: RecordingSession(),
        status="stopped",
        now=now + timedelta(seconds=65),
    ) is True


def test_2j4_monitoring_distinguishes_liveness_from_progress_and_evaluates_alerts() -> None:
    """Alert evaluator flags alive-but-stalled workers and triggers all required queue alerts."""
    now = datetime(2026, 11, 25, 1, 0, 0, tzinfo=timezone.utc)
    thresholds = BillingHealthThresholds(
        heartbeat_stale_seconds=90,
        webhook_backlog_age_seconds=300,
        recovery_overdue_age_seconds=300,
        reconciliation_backlog_age_seconds=3600,
        repeated_lease_expiry_threshold=2,
    )

    # Healthy scenario: live workers, clean queues
    healthy_workers = [
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
            processed_count=5,
            error_count=0,
        ),
        WorkerLivenessSnapshot(
            worker_id="rec_1",
            worker_type=WORKER_TYPE_RECOVERY,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test",
            status="running",
            last_heartbeat_at=now - timedelta(seconds=15),
            heartbeat_age_seconds=15.0,
            is_stale=False,
            last_processed_at=now - timedelta(seconds=15),
            last_error_at=None,
            processed_count=2,
            error_count=0,
        ),
    ]
    status_ok, alerts_ok = evaluate_billing_health_alerts(
        workers=healthy_workers,
        webhook_queue=WebhookQueueMetrics(0, 0, 0, 0, None, None, now),
        recovery_queue=RecoveryQueueMetrics(0, 0, 0, 0, 0, None, None, now),
        reconciliation=ReconciliationMetrics(0, None, None, 0),
        thresholds=thresholds,
    )
    assert status_ok == "healthy"
    assert alerts_ok == []

    # Alive-but-stalled & multi-anomaly scenario
    stalled_workers = [
        WorkerLivenessSnapshot(
            worker_id="wh_alive_stalled",
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test",
            status="running",
            last_heartbeat_at=now - timedelta(seconds=5),  # Alive!
            heartbeat_age_seconds=5.0,
            is_stale=False,
            last_processed_at=now - timedelta(minutes=20),
            last_error_at=now - timedelta(seconds=10),  # Erroring after last progress!
            processed_count=1,
            error_count=4,
        ),
        WorkerLivenessSnapshot(
            worker_id="rec_dead",
            worker_type=WORKER_TYPE_RECOVERY,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test",
            status="running",
            last_heartbeat_at=now - timedelta(seconds=200),  # Stale!
            heartbeat_age_seconds=200.0,
            is_stale=True,
            last_processed_at=None,
            last_error_at=None,
            processed_count=0,
            error_count=0,
        ),
    ]
    status_bad, alerts_bad = evaluate_billing_health_alerts(
        workers=stalled_workers,
        webhook_queue=WebhookQueueMetrics(3, 1, 2, 1, now - timedelta(seconds=600), 600.0, None),
        recovery_queue=RecoveryQueueMetrics(2, 2, 1, 1, 1, now - timedelta(seconds=400), 400.0, None),
        reconciliation=ReconciliationMetrics(3, now - timedelta(seconds=7200), 7200.0, 3),
        thresholds=thresholds,
    )
    assert status_bad == "unhealthy"
    codes = {a.code for a in alerts_bad}
    assert "STALE_WORKER_HEARTBEAT" in codes
    assert "WORKER_ERRORING_NO_PROGRESS" in codes
    assert "NO_ACTIVE_RECOVERY_WORKER" in codes
    assert "WEBHOOK_WORKER_STALLED_OR_BACKLOGGED" in codes
    assert "QUARANTINED_WEBHOOK_EVENTS" in codes
    assert "RECOVERY_OVERDUE_BACKLOG" in codes
    assert "REPEATED_EXPIRED_LEASES_OR_QUARANTINE" in codes
    assert "PENDING_RECONCILIATION_CASES" in codes


@pytest.mark.asyncio
async def test_2j4_billing_health_cli_readonly_json_and_fail_on_alert() -> None:
    """billing_health CLI formats JSON cleanly without secrets and returns exit code 1 on alert."""
    now = datetime(2026, 11, 25, 1, 0, 0, tzinfo=timezone.utc)
    hb_row = BillingWorkerHeartbeat(
        worker_id="wh_cli_01",
        instance_token=uuid.uuid4(),
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_1",
        status="running",
        pid=123,
        hostname="render-worker-1",
        started_at=now - timedelta(minutes=10),
        last_heartbeat_at=now - timedelta(seconds=5),
        last_processed_at=now - timedelta(seconds=5),
        last_error_at=None,
        last_error_message=None,
        processed_count=10,
        error_count=0,
    )

    class HealthReadOnlySession:
        def __init__(self) -> None:
            self._call = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            self._call += 1
            idx = self._call
            assert not str(stmt).strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))

            class Res:
                def __init__(self, val):
                    self._val = val

                def scalars(self):
                    return self

                def all(self):
                    return self._val

                def one(self):
                    return self._val

                def scalar_one(self):
                    return self._val

            if idx == 1:
                return Res([hb_row])
            if idx == 2:
                return Res((0, None))  # wh pending
            if idx in (3, 4, 5, 6):
                return Res(0 if idx != 6 else now)  # wh failed/quarantined/expired/last_proc
            if idx == 7:
                return Res((0, None))  # rec due
            if idx in (8, 9, 10):
                return Res(0)  # rec scheduled/quarantined/expired
            if idx == 11:
                return Res([])  # rec payloads
            if idx == 12:
                return Res(now)  # rec last recovered
            if idx == 13:
                return Res((0, None))  # recon pending
            if idx == 14:
                return Res(0)  # recon recent
            raise AssertionError(f"Unexpected query #{idx}: {stmt}")

    code_alert, out_json = await run_billing_health_check(
        session_factory=lambda: HealthReadOnlySession(),
        environment="test",
        output_json=True,
        fail_on_alert=True,
        now=now,
    )
    # Missing active recovery worker triggers NO_ACTIVE_RECOVERY_WORKER warning -> exit 1 under fail_on_alert
    assert code_alert == 1
    parsed = json.loads(out_json)
    assert parsed["overall_status"] == "degraded"
    assert parsed["workers"][0]["worker_id"] == "wh_cli_01"
    assert any(a["code"] == "NO_ACTIVE_RECOVERY_WORKER" for a in parsed["alerts"])


# =============================================================================
# 5. Alembic Migration a4b5c6d7e8f3 Verification (Stage 2J.4)
# =============================================================================


def test_2j4_worker_heartbeat_migration_metadata_and_offline_ddl() -> None:
    """Verify forward-only migration a4b5c6d7e8f3 creates billing_worker_heartbeats with RLS."""
    assert HEARTBEAT_MIGRATION_FILE.exists()
    spec = importlib.util.spec_from_file_location("migration_a4b5c6d7e8f3", HEARTBEAT_MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "a4b5c6d7e8f3"
    assert mod.down_revision == "f3a4b5c6d7e2"

    up_res = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "f3a4b5c6d7e2:a4b5c6d7e8f3", "--sql"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert up_res.returncode == 0, f"alembic upgrade --sql failed:\n{up_res.stderr}"
    assert "CREATE TABLE billing_worker_heartbeats" in up_res.stdout
    assert "CREATE INDEX ix_billing_worker_heartbeats_type_env_hb" in up_res.stdout
    assert "ALTER TABLE billing_worker_heartbeats ENABLE ROW LEVEL SECURITY;" in up_res.stdout
    assert "REVOKE ALL ON billing_worker_heartbeats FROM anon, authenticated;" in up_res.stdout

    down_res = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "downgrade", "a4b5c6d7e8f3:f3a4b5c6d7e2", "--sql"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert down_res.returncode == 0, f"alembic downgrade --sql failed:\n{down_res.stderr}"
    assert "DROP TABLE billing_worker_heartbeats;" in down_res.stdout
    assert "UPDATE alembic_version SET version_num='f3a4b5c6d7e2'" in down_res.stdout



# =============================================================================
# 6. Stage 2J-V Remediation Regression Tests (Fencing, Outage Backoff, Env Filter, Recovery)
# =============================================================================


@pytest.mark.asyncio
async def test_2jv_heartbeat_fencing_rejection_sets_ownership_lost_and_preserves_timestamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ON CONFLICT DO UPDATE ... WHERE is fenced out (0 rows returned), tracker must reject cleanly."""
    now = datetime(2026, 11, 25, 2, 0, 0, tzinfo=timezone.utc)
    tracker = WorkerHeartbeatTracker(
        worker_id="worker_shared_hb",
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_1",
        heartbeat_interval_seconds=30.0,
        started_at=now,
    )

    class FencedOrAllowedResult:
        def __init__(self, token):
            self._token = token

        def scalar_one_or_none(self):
            return self._token

    allow_write = True

    class FencedSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            assert "RETURNING billing_worker_heartbeats.instance_token" in str(stmt)
            return FencedOrAllowedResult(tracker.instance_token if allow_write else None)

        async def commit(self):
            return None

    assert await tracker.record_heartbeat(lambda: FencedSession(), status="running", now=now, force=True) is True
    assert tracker.ownership_lost is False
    assert tracker._last_written_at == now

    allow_write = False
    with caplog.at_level("WARNING", logger="app.services.billing.razorpay.monitoring"):
        assert await tracker.record_heartbeat(
            lambda: FencedSession(),
            status="running",
            now=now + timedelta(seconds=35),
            force=True,
        ) is False

    assert tracker.ownership_lost is True
    assert tracker._last_written_at == now
    assert any("fencing" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_2jv_heartbeat_outage_backoff_after_prior_success_and_stopped_flush() -> None:
    """Consecutive forced error heartbeats back off after a failed DB write while stopped still flushes."""
    now = datetime(2026, 11, 25, 2, 0, 0, tzinfo=timezone.utc)
    tracker = WorkerHeartbeatTracker(
        worker_id="worker_outage_backoff",
        worker_type=WORKER_TYPE_RECOVERY,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_1",
        heartbeat_interval_seconds=30.0,
        started_at=now,
    )
    attempts = 0

    class OkSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            nonlocal attempts
            attempts += 1
            return None

        async def commit(self):
            return None

    class FailingSession:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("db temporarily down")

        async def __aexit__(self, *args):
            return None

    # Initial heartbeat succeeds
    assert await tracker.record_heartbeat(lambda: OkSession(), status="running", now=now, force=True) is True
    assert attempts == 1

    # DB outage starts; first forced error attempts DB write and fails
    assert await tracker.record_heartbeat(
        lambda: FailingSession(),
        status="error",
        now=now + timedelta(seconds=5),
        force=True,
        error_message="loop boom 1",
    ) is False
    assert attempts == 2

    # 2 seconds later, another forced error backs off without hitting FailingSession
    assert await tracker.record_heartbeat(
        lambda: FailingSession(),
        status="error",
        now=now + timedelta(seconds=7),
        force=True,
        error_message="loop boom 2",
    ) is False
    assert attempts == 2

    # Clean shutdown ("stopped") always attempts flush even within backoff window
    assert await tracker.record_heartbeat(
        lambda: OkSession(),
        status="stopped",
        now=now + timedelta(seconds=8),
    ) is True
    assert attempts == 3


@pytest.mark.asyncio
async def test_2jv_live_health_snapshot_excludes_null_env_workers_and_stable_worker_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--environment live ignores unscoped environment=None worker rows; workers honor stable ID env vars."""
    monkeypatch.setenv("RAZORPAY_WEBHOOK_WORKER_ID", "stable-wh-worker-1")
    monkeypatch.setenv("RAZORPAY_RECOVERY_WORKER_ID", "stable-rec-worker-1")
    wh = RazorpayWebhookWorker(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
    rec = RazorpayCheckoutRecoveryWorker(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
    assert wh.worker_id == "stable-wh-worker-1"
    assert rec.worker_id == "stable-rec-worker-1"

    captured_hb_sql: list[str] = []

    class LiveFilterCheckSession:
        def __init__(self) -> None:
            self._call = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            self._call += 1
            if self._call == 1:
                captured_hb_sql.append(str(stmt))

            class Res:
                def __init__(self, val):
                    self._val = val

                def scalars(self):
                    return self

                def all(self):
                    return self._val

                def one(self):
                    return self._val

                def scalar_one(self):
                    return self._val

            if self._call == 1:
                return Res([])
            if self._call in (2, 7, 13):
                return Res((0, None))
            if self._call == 11:
                return Res([])
            return Res(0 if self._call not in (6, 12) else None)

    await collect_billing_health_snapshot(
        db=LiveFilterCheckSession(),  # type: ignore[arg-type]
        environment="live",
        emit_alert_logs=False,
    )
    assert len(captured_hb_sql) == 1
    assert "IS NULL" not in captured_hb_sql[0]


@pytest.mark.asyncio
async def test_2jv_replacement_worker_reclaims_expired_attempt_and_rejects_old_worker() -> None:
    """Expired claim allows replacement reclaim, replacement commits, and returning old worker is rejected."""
    from tests.test_razorpay_checkout_recovery_worker_stage2d import (
        MockRazorpayClient,
        RecoveryWorkerMockDbSession,
        SharedRecoveryAttemptRepository,
        _create_attempt,
    )

    t0 = datetime(2026, 11, 25, 2, 0, 0, tzinfo=timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        next_recovery_retry_at=t0 - timedelta(seconds=10),
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed_old_batch = await claim_recovery_batch(
        db=RecoveryWorkerMockDbSession(repo, clock_now=t0),
        worker_id="old_worker",
        limit=1,
        lease_duration_seconds=5,
        now=t0,
    )
    assert len(claimed_old_batch) == 1
    claimed_old = claimed_old_batch[0]

    t_expired = t0 + timedelta(seconds=10)
    claimed_new_batch = await claim_recovery_batch(
        db=RecoveryWorkerMockDbSession(repo, clock_now=t_expired),
        worker_id="replacement_worker",
        limit=1,
        lease_duration_seconds=60,
        now=t_expired,
    )
    assert len(claimed_new_batch) == 1
    claimed_new = claimed_new_batch[0]
    assert claimed_new.recovery_claim_id != claimed_old.recovery_claim_id

    res_new = await execute_claimed_recovery_attempt(
        session_factory=lambda: RecoveryWorkerMockDbSession(repo, clock_now=t_expired),
        claimed=claimed_new,
        client=MockRazorpayClient(
            subscription_data={"id": "sub_winner_2jv", "plan_id": "plan_test_pro", "customer_id": "cust_test_123"}
        ),
        known_subscription_id="sub_winner_2jv",
        now_fn=lambda: t_expired,
    )
    assert res_new.outcome == RecoveryOutcome.RECOVERED

    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=lambda: RecoveryWorkerMockDbSession(repo, clock_now=t_expired + timedelta(seconds=1)),
            claimed=claimed_old,
            client=MockRazorpayClient(
                subscription_data={"id": "sub_stale_loser_2jv", "plan_id": "plan_test_pro", "customer_id": "cust_test_123"}
            ),
            known_subscription_id="sub_stale_loser_2jv",
            now_fn=lambda: t_expired + timedelta(seconds=1),
        )

    final_att = repo.get_attempt(att.id)
    assert final_att is not None
    assert final_att.status == CheckoutAttemptStatus.open
    assert final_att.provider_subscription_id == "sub_winner_2jv"
