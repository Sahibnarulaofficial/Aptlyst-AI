"""Stage 1 Option A Safety Tests — Razorpay Production Environment & Accounting Isolation.

Covers all Stage 1D requirements (pure offline deterministic tests, zero remote API calls):
1. Production configuration rejects Razorpay test mode ('production' and 'prod', plus worker startup).
2. Production configuration accepts valid Razorpay live mode.
3. Non-production configuration accepts Razorpay test mode and rejects live mode.
4. Production webhook ingress rejects invalid or mismatched environment provenance (HTTP 503 / fail-closed).
5. Worker-side and handler-side environment mismatch cannot activate a subscription (safe quarantine)
   and a webhook cannot mutate or overwrite an existing subscription's environment.
6. A test subscription cannot authorize production Pro usage (and 'legacy' is not assumed to mean 'live').
7. A test subscription cannot cause a production past_due customer's accounting query to fail as a
   duplicate eligible subscription (while two live eligible subscriptions still fail closed).
8. Existing live subscription accounting continues to work (active, grace-window past_due, expired grace, canceled).
9. Existing Razorpay webhook behavior remains compatible in both non-production (test) and production (live).
10. Missing or unknown environment provenance fails safely across config, ingress, worker, handler_base,
    and accounting (no silent fallback to 'live').
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any
from unittest.mock import patch
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401

from app.config import (
    Settings,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
    validate_razorpay_runtime_scope,
)
from app.db.session import get_db
from app.main import app
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    CustomerProvisionStatus,
)
from app.models.subscription import (
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.accounting import AccountingErrorCode, reserve_usage
from app.services.billing.pricing import MOCK_GPT_4O_MINI_PRICING
from app.services.billing.razorpay.agreement_handlers import (
    handle_subscription_activated,
    handle_subscription_authenticated,
)
from app.services.billing.razorpay.client import RazorpayConfigurationError
from app.services.billing.razorpay.customer import _get_provisioning_scope
from app.services.billing.razorpay.handler_base import (
    RazorpayScopeMismatchError,
    validate_razorpay_event_envelope,
)
from app.services.billing.razorpay.webhook_engine import (
    ClaimedWebhookEvent,
    RazorpayWebhookDispatcher,
    execute_claimed_razorpay_webhook_event,
)
from app.services.billing.razorpay.webhook_ingress import (
    get_webhook_scope,
    ingest_razorpay_webhook_event,
)
from app.workers.razorpay_webhook_worker import RazorpayWebhookWorker
from tests.test_accounting_subscription_auth import (
    MockAsyncSession as AccountingMockSession,
    make_period,
    make_task,
)
from tests.test_razorpay_agreement_handlers import (
    MockScalarResult,
    RazorpayAgreementMockDbSession,
    make_test_claimed_event,
)
from tests.test_razorpay_webhook_ingestion import InMemoryRazorpayDbSession
from tests.test_razorpay_webhook_worker import (
    RazorpayWorkerMockDbSession,
    SharedRazorpayEventRepository,
    make_test_event,
)

PROD_VISITOR_SECRET = "prod-super-secure-visitor-session-secret-32chars-xyz"
PROD_WEBHOOK_SECRET = "rzp_whsec_prod_test_isolation_secret_123456"
PROD_MERCHANT_ID = "acc_LIVE_PROD_001"
TEST_MERCHANT_ID = "acc_TEST_DEV_001"


def _make_razorpay_sub(
    *,
    org_id: uuid.UUID,
    environment: str,
    status: SubscriptionStatus = SubscriptionStatus.active,
    plan_tier: str = "pro",
    past_due_since: datetime | None = None,
    provider_subscription_id: str | None = None,
    now: datetime | None = None,
) -> Subscription:
    ref = now or datetime.now(timezone.utc)
    sub_id = provider_subscription_id or f"sub_{uuid.uuid4().hex[:14]}"
    return Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID if environment == "live" else TEST_MERCHANT_ID,
        environment=environment,
        provider_customer_id=f"cust_{uuid.uuid4().hex[:12]}",
        provider_subscription_id=sub_id,
        provider_plan_id="plan_pro_001",
        status=status,
        plan_tier=plan_tier,
        current_period_start=ref - timedelta(days=5),
        current_period_end=ref + timedelta(days=25),
        past_due_since=past_due_since,
        created_at=ref,
    )


def _sign_body(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


# =============================================================================
# 1, 2, 3. Configuration & Startup Validation (Prod vs Non-Prod)
# =============================================================================


@pytest.mark.parametrize("prod_env", ["production", "prod", "PRODUCTION", "  Prod  "])
def test_01_production_config_rejects_razorpay_test_mode(prod_env: str) -> None:
    """1. Production configuration ('production' and 'prod') must reject Razorpay test mode."""
    with pytest.raises(ValueError, match="RAZORPAY_ENVIRONMENT must be 'live' in production"):
        Settings(
            environment=prod_env,
            visitor_session_secret=PROD_VISITOR_SECRET,
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id=PROD_MERCHANT_ID,
            razorpay_key_id="rzp_live_1234567890",
            razorpay_key_secret=SecretStr("sec_live_123"),
            razorpay_webhook_secret=SecretStr(PROD_WEBHOOK_SECRET),
            razorpay_pro_plan_id="plan_live_pro_001",
            razorpay_plan_currency="USD",
        )

    # Runtime scope helper & provisioning scope must also reject test mode under 'prod'
    with pytest.raises(ValueError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
        validate_razorpay_runtime_scope(
            deployment_env=prod_env,
            razorpay_env="test",
            merchant_account_id=PROD_MERCHANT_ID,
        )

    with (
        patch.object(settings, "environment", prod_env),
        patch.object(settings, "razorpay_environment", "test"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
            _get_provisioning_scope()
        with pytest.raises(ValueError, match="Production RazorpayWebhookWorker strictly requires"):
            RazorpayWebhookWorker()
        with pytest.raises(ValueError, match="Production RazorpayWebhookWorker strictly requires"):
            RazorpayWebhookWorker(environment="test")


@pytest.mark.parametrize("prod_env", ["production", "prod"])
def test_02_production_config_accepts_valid_razorpay_live_mode(prod_env: str) -> None:
    """2. Production configuration accepts valid Razorpay live mode across settings, scope, and worker."""
    cfg = Settings(
        environment=prod_env,
        visitor_session_secret=PROD_VISITOR_SECRET,
        razorpay_enabled=True,
        razorpay_environment="live",
        razorpay_merchant_account_id=PROD_MERCHANT_ID,
        razorpay_key_id="rzp_live_1234567890",
        razorpay_key_secret=SecretStr("sec_live_123"),
        razorpay_webhook_secret=SecretStr(PROD_WEBHOOK_SECRET),
        razorpay_pro_plan_id="plan_live_pro_001",
        razorpay_plan_currency="USD",
    )
    assert cfg.razorpay_environment == "live"
    cfg.validate_razorpay_configuration()

    with (
        patch.object(settings, "environment", prod_env),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        assert _get_provisioning_scope() == (PROD_MERCHANT_ID, "live")
        assert get_webhook_scope() == (PROD_MERCHANT_ID, "live")
        worker = RazorpayWebhookWorker()
        assert worker.environment == "live"


@pytest.mark.parametrize("non_prod_env", ["development", "staging", "test", "ci"])
def test_03_non_production_config_accepts_test_mode_and_rejects_live(non_prod_env: str) -> None:
    """3. Non-production deployments accept Razorpay test mode and reject live mode."""
    cfg = Settings(
        environment=non_prod_env,
        razorpay_enabled=True,
        razorpay_environment="test",
        razorpay_merchant_account_id=TEST_MERCHANT_ID,
        razorpay_key_id="rzp_test_1234567890",
        razorpay_key_secret=SecretStr("sec_test_123"),
        razorpay_webhook_secret=SecretStr("whsec_test_123"),
        razorpay_pro_plan_id="plan_test_pro_001",
        razorpay_plan_currency="USD",
    )
    assert cfg.razorpay_environment == "test"
    assert resolve_required_razorpay_environment(non_prod_env) == "test"

    with pytest.raises(ValueError, match="RAZORPAY_ENVIRONMENT must be 'test' in non-production"):
        Settings(
            environment=non_prod_env,
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id=TEST_MERCHANT_ID,
            razorpay_key_id="rzp_test_1234567890",
            razorpay_key_secret=SecretStr("sec_test_123"),
            razorpay_webhook_secret=SecretStr("whsec_test_123"),
            razorpay_pro_plan_id="plan_test_pro_001",
            razorpay_plan_currency="USD",
        )


# =============================================================================
# 4. Production Webhook Ingress Isolation
# =============================================================================


@pytest.mark.asyncio
async def test_04_production_webhook_ingress_rejects_test_or_invalid_provenance() -> None:
    """4. Production webhook ingress fails closed (HTTP 503) on test/mismatched/invalid provenance."""
    db = InMemoryRazorpayDbSession()
    payload = {
        "entity": "event",
        "account_id": PROD_MERCHANT_ID,
        "event": "subscription.activated",
        "contains": ["subscription"],
        "payload": {"subscription": {"entity": {"id": "sub_prod_test_reject"}}},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = _sign_body(raw_body, PROD_WEBHOOK_SECRET)

    # 4a. Direct ingest_razorpay_webhook_event refuses 'test' in production
    with patch.object(settings, "environment", "production"):
        with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
            await ingest_razorpay_webhook_event(
                db=db,  # type: ignore[arg-type]
                event_id="evt_prod_reject_test",
                event_type="subscription.activated",
                payload=payload,
                merchant_account_id=PROD_MERCHANT_ID,
                environment="test",
            )
    assert len(db.events) == 0

    # 4b. HTTP endpoint returns 503 when production has razorpay_environment='test'
    # Use an async generator dependency override (matching test_razorpay_webhook_ingestion.setup_db_override)
    # so FastAPI resolves get_db directly on the event loop without AnyIO threadpool call_soon_threadsafe.
    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with (
            patch.object(settings, "environment", "production"),
            patch.object(settings, "razorpay_enabled", True),
            patch.object(settings, "razorpay_environment", "test"),
            patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
            patch.object(settings, "razorpay_webhook_secret", SecretStr(PROD_WEBHOOK_SECRET)),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_body,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": "evt_prod_http_reject_test",
                    },
                )
        assert resp.status_code == 503
        assert len(db.events) == 0
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 5. Worker-Side & Handler-Side Environment Mismatch Cannot Activate Subscription
# =============================================================================


@pytest.mark.asyncio
async def test_05a_worker_quarantines_test_event_in_production_before_handler() -> None:
    """5a. Worker-side environment guard quarantines test events in production without invoking handler."""
    now = datetime.now(timezone.utc)
    test_event = make_test_event(
        "evt_test_leaked_into_prod",
        provider="razorpay",
        environment="test",
        merchant_account_id=PROD_MERCHANT_ID,
        event_type="subscription.activated",
        status=WebhookProcessingStatus.pending,
    )
    lease_token = uuid.uuid4()
    test_event.lease_token = lease_token
    test_event.locked_by = "worker_prod_01"
    test_event.lease_expires_at = now + timedelta(seconds=60)
    repo = SharedRazorpayEventRepository([test_event])

    claimed = ClaimedWebhookEvent(
        event_id=test_event.id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="test",
        provider_event_id=test_event.provider_event_id,
        event_type=test_event.event_type,
        payload=test_event.payload,
        attempts=0,
        lease_token=lease_token,
        lease_expires_at=test_event.lease_expires_at,
        locked_by="worker_prod_01",
        received_at=now,
    )

    handler_called = False

    async def spy_handler(ev: Any, db: Any) -> None:
        nonlocal handler_called
        handler_called = True

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        result = await execute_claimed_razorpay_webhook_event(
            session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),  # type: ignore[arg-type]
            claimed=claimed,
            handler=spy_handler,
            expected_merchant_id=PROD_MERCHANT_ID,
            expected_environment=None,  # Even when caller omits expected_environment!
            now_fn=lambda: now,
        )

    assert handler_called is False
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert result.failure_category == "scope_mismatch"
    assert repo.events[test_event.id].status == WebhookProcessingStatus.quarantined


@pytest.mark.asyncio
async def test_05b_handler_rejects_test_activation_in_production_and_protects_sub_environment() -> None:
    """5b. Direct handler call refuses test activation in production and refuses cross-env mutation."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    sub_id = "sub_prod_guard_001"
    cust_id = "cust_prod_guard_001"

    db = RazorpayAgreementMockDbSession()
    db.customer_provisions[cust_id] = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="live",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )

    claimed_test = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        merchant_account_id=PROD_MERCHANT_ID,
        environment="test",
        notes={"organization_id": str(org_id)},
        created_at_ts=int(now.timestamp()),
        session=db,
    )

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        with pytest.raises(RazorpayScopeMismatchError):
            await handle_subscription_activated(claimed_test, db)  # type: ignore[arg-type]

    assert len(db.subscriptions) == 0

    # Verify immutable subscription environment guard: even if a query returned a row
    # with a mismatched persisted environment, handler must refuse to mutate it.
    existing_live_sub = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
        provider_subscription_id=sub_id,
        now=now,
    )
    existing_live_sub.merchant_account_id = TEST_MERCHANT_ID
    db_cross = RazorpayAgreementMockDbSession()
    db_cross.customer_provisions[cust_id] = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=TEST_MERCHANT_ID,
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    db_cross.subscriptions[sub_id] = existing_live_sub

    orig_execute = db_cross.execute

    async def _returning_mismatched_sub(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        col_descs = getattr(stmt, "column_descriptions", None)
        if col_descs and col_descs[0].get("entity") is Subscription and col_descs[0].get("name") == "Subscription":
            return MockScalarResult(existing_live_sub)
        return await orig_execute(stmt, *args, **kwargs)

    db_cross.execute = _returning_mismatched_sub  # type: ignore[method-assign]

    claimed_dev_test = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        merchant_account_id=TEST_MERCHANT_ID,
        environment="test",
        notes={"organization_id": str(org_id)},
        created_at_ts=int(now.timestamp()),
        session=db_cross,
    )

    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "razorpay_environment", "test"),
        patch.object(settings, "razorpay_merchant_account_id", TEST_MERCHANT_ID),
    ):
        with pytest.raises(RazorpayScopeMismatchError, match="Refusing subscription mutation"):
            await handle_subscription_activated(claimed_dev_test, db_cross)  # type: ignore[arg-type]

    assert existing_live_sub.environment == "live"
    assert existing_live_sub.status == SubscriptionStatus.incomplete


# =============================================================================
# 6, 7, 8. Production Accounting Isolation
# =============================================================================


@pytest.mark.asyncio
async def test_06_test_subscription_cannot_authorize_production_pro_usage() -> None:
    """6. A test subscription (and legacy without live provenance) cannot authorize production Pro usage."""
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    org_id = uuid.uuid4()
    period = make_period(org_id=org_id, plan_tier="pro", now=now)
    task = make_task(org_id=org_id, period_id=period.id)
    test_sub = _make_razorpay_sub(
        org_id=org_id,
        environment="test",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        now=now,
    )

    session = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task.id: task},
        subscriptions={org_id: [test_sub]},
    )

    with patch.object(settings, "environment", "production"):
        result = await reserve_usage(
            session=session,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )

    assert result.success is False
    assert result.error_code == AccountingErrorCode.SUBSCRIPTION_NOT_FOUND
    assert period.tokens_reserved == 0

    # Verify the SQL query itself explicitly filters for environment == 'live' in production
    sub_queries = [q for q in session.executed_queries if q["entity"] is Subscription]
    assert len(sub_queries) >= 1
    assert "live" in sub_queries[0]["params"].values()

    # Verify 'legacy' is NOT assumed to mean 'live' in production (fail closed)
    legacy_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_legacy_ambiguous_01",
        stripe_subscription_id="sub_legacy_ambiguous_01",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        created_at=now,
    )
    assert legacy_sub.environment == "legacy"

    task2 = make_task(org_id=org_id, period_id=period.id)
    session_legacy = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task2.id: task2},
        subscriptions={org_id: [legacy_sub]},
    )
    with patch.object(settings, "environment", "production"):
        res_legacy = await reserve_usage(
            session=session_legacy,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task2.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert res_legacy.success is False
    assert res_legacy.error_code == AccountingErrorCode.SUBSCRIPTION_NOT_FOUND


@pytest.mark.asyncio
async def test_07_test_subscription_does_not_break_production_past_due_customer() -> None:
    """7. A stray test active sub must NOT trigger RECONCILIATION_REQUIRED for a live past_due customer."""
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    org_id = uuid.uuid4()
    period = make_period(org_id=org_id, plan_tier="pro", now=now)
    task = make_task(org_id=org_id, period_id=period.id)

    live_past_due_sub = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=now - timedelta(days=2),  # Inside 7-day grace window
        now=now,
    )
    stray_test_active_sub = _make_razorpay_sub(
        org_id=org_id,
        environment="test",
        status=SubscriptionStatus.active,
        plan_tier="free",
        now=now,
    )

    session = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task.id: task},
        subscriptions={org_id: [live_past_due_sub, stray_test_active_sub]},
    )

    with patch.object(settings, "environment", "production"):
        result = await reserve_usage(
            session=session,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )

    assert result.success is True
    assert result.error_code is None
    assert result.tokens_reserved > 0

    # Two LIVE eligible subscriptions must STILL fail closed with RECONCILIATION_REQUIRED
    second_live_sub = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        now=now,
    )
    task2 = make_task(org_id=org_id, period_id=period.id)
    session_dup_live = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task2.id: task2},
        subscriptions={org_id: [live_past_due_sub, second_live_sub]},
    )
    with patch.object(settings, "environment", "production"):
        dup_result = await reserve_usage(
            session=session_dup_live,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task2.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert dup_result.success is False
    assert dup_result.error_code == AccountingErrorCode.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_08_existing_live_subscription_accounting_continues_to_work() -> None:
    """8. Live subscription accounting preserves active, grace-period expiry, and canceled semantics."""
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    org_id = uuid.uuid4()
    period = make_period(org_id=org_id, plan_tier="pro", now=now)

    # 8a. Live active subscription succeeds
    task_active = make_task(org_id=org_id, period_id=period.id)
    live_active = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        now=now,
    )
    session_active = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task_active.id: task_active},
        subscriptions={org_id: [live_active]},
    )
    with patch.object(settings, "environment", "production"):
        res_active = await reserve_usage(
            session=session_active,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task_active.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert res_active.success is True

    # 8b. Live canceled subscription still returns SUBSCRIPTION_INACTIVE even if stray test active exists
    period_canc = make_period(org_id=org_id, plan_tier="pro", now=now)
    task_canc = make_task(org_id=org_id, period_id=period_canc.id)
    live_canceled = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        now=now,
    )
    stray_test = _make_razorpay_sub(
        org_id=org_id,
        environment="test",
        status=SubscriptionStatus.active,
        plan_tier="free",
        now=now,
    )
    session_canc = AccountingMockSession(
        billing_periods={period_canc.id: period_canc},
        tasks={task_canc.id: task_canc},
        subscriptions={org_id: [live_canceled, stray_test]},
    )
    with patch.object(settings, "environment", "production"):
        res_canc = await reserve_usage(
            session=session_canc,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task_canc.id,
            billing_period_id=period_canc.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert res_canc.success is False
    assert res_canc.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE
    assert res_canc.details.get("subscription_status") == "canceled"

    # 8c. Live past_due beyond 7-day grace returns GRACE_PERIOD_EXPIRED
    period_exp = make_period(org_id=org_id, plan_tier="pro", now=now)
    task_exp = make_task(org_id=org_id, period_id=period_exp.id)
    live_expired_grace = _make_razorpay_sub(
        org_id=org_id,
        environment="live",
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=now - timedelta(days=8),
        now=now,
    )
    session_exp = AccountingMockSession(
        billing_periods={period_exp.id: period_exp},
        tasks={task_exp.id: task_exp},
        subscriptions={org_id: [live_expired_grace]},
    )
    with patch.object(settings, "environment", "production"):
        res_exp = await reserve_usage(
            session=session_exp,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task_exp.id,
            billing_period_id=period_exp.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert res_exp.success is False
    assert res_exp.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED


# =============================================================================
# 9. Existing Razorpay Webhook Behavior Compatibility
# =============================================================================


@pytest.mark.asyncio
async def test_09_existing_razorpay_webhook_behavior_compatible_in_prod_and_non_prod() -> None:
    """9. Valid live webhooks in production and valid test webhooks in development continue to work."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    sub_id_live = "sub_live_compat_001"
    cust_id_live = "cust_live_compat_001"

    # 9a. Production live activation succeeds cleanly
    db_prod = RazorpayAgreementMockDbSession()
    db_prod.customer_provisions[cust_id_live] = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="live",
        provider_customer_id=cust_id_live,
        status=CustomerProvisionStatus.resolved,
    )
    db_prod.checkout_attempts[sub_id_live] = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="live",
        provider_customer_id=cust_id_live,
        provider_subscription_id=sub_id_live,
        idempotency_key="idem_live_compat_001",
        status=CheckoutAttemptStatus.payment_pending,
        amount_minor_units=2900,
        currency="USD",
        attempt_lease_expires_at=now + timedelta(minutes=5),
    )
    claimed_live = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id_live,
        status="active",
        customer_id=cust_id_live,
        merchant_account_id=PROD_MERCHANT_ID,
        environment="live",
        notes={"organization_id": str(org_id)},
        created_at_ts=int(now.timestamp()),
        session=db_prod,
    )

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        await handle_subscription_activated(claimed_live, db_prod)  # type: ignore[arg-type]

    assert sub_id_live in db_prod.subscriptions
    assert db_prod.subscriptions[sub_id_live].environment == "live"
    assert db_prod.subscriptions[sub_id_live].status == SubscriptionStatus.active
    assert db_prod.subscriptions[sub_id_live].plan_tier == "free"  # Strict Stage 1 invariant preserved

    # 9b. Non-production test authentication succeeds cleanly
    db_dev = RazorpayAgreementMockDbSession()
    sub_id_test = "sub_test_compat_001"
    cust_id_test = "cust_test_compat_001"
    db_dev.customer_provisions[cust_id_test] = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=TEST_MERCHANT_ID,
        environment="test",
        provider_customer_id=cust_id_test,
        status=CustomerProvisionStatus.resolved,
    )
    claimed_test = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id_test,
        status="authenticated",
        customer_id=cust_id_test,
        merchant_account_id=TEST_MERCHANT_ID,
        environment="test",
        notes={"organization_id": str(org_id)},
        created_at_ts=int(now.timestamp()),
        session=db_dev,
    )

    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "razorpay_environment", "test"),
        patch.object(settings, "razorpay_merchant_account_id", TEST_MERCHANT_ID),
    ):
        await handle_subscription_authenticated(claimed_test, db_dev)  # type: ignore[arg-type]

    assert sub_id_test in db_dev.subscriptions
    assert db_dev.subscriptions[sub_id_test].environment == "test"
    assert db_dev.subscriptions[sub_id_test].status == SubscriptionStatus.incomplete


# =============================================================================
# 10. Missing or Unknown Environment Provenance Fails Safely
# =============================================================================


@pytest.mark.asyncio
async def test_10_missing_or_unknown_environment_provenance_fails_safely() -> None:
    """10. Empty, missing, or unrecognized environment provenance must fail closed everywhere."""
    now = datetime.now(timezone.utc)

    # 10a. Deployment environment validation fails closed
    for bad_env in ("", "   ", "unknown_env", "canary-99"):
        with pytest.raises(ValueError, match="ENVIRONMENT"):
            validate_deployment_environment(bad_env)

    # 10b. handler_base MUST NOT silently default missing configured environment to 'live'
    valid_payload = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id="sub_missing_env",
        customer_id="cust_missing_env",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="live",
        created_at_ts=int(now.timestamp()),
    ).payload
    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "razorpay_environment", ""),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
    ):
        with pytest.raises(RazorpayScopeMismatchError, match="Missing or invalid Razorpay environment"):
            validate_razorpay_event_envelope(valid_payload)

    # 10c. Missing/blank event.environment quarantined safely by worker engine
    blank_env_event = make_test_event(
        "evt_blank_env",
        provider="razorpay",
        environment="",
        merchant_account_id=PROD_MERCHANT_ID,
        event_type="subscription.activated",
    )
    lease_token = uuid.uuid4()
    blank_env_event.lease_token = lease_token
    blank_env_event.locked_by = "worker_01"
    blank_env_event.lease_expires_at = now + timedelta(seconds=60)
    repo = SharedRazorpayEventRepository([blank_env_event])
    claimed_blank = ClaimedWebhookEvent(
        event_id=blank_env_event.id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID,
        environment="",
        provider_event_id=blank_env_event.provider_event_id,
        event_type=blank_env_event.event_type,
        payload=blank_env_event.payload,
        attempts=0,
        lease_token=lease_token,
        lease_expires_at=blank_env_event.lease_expires_at,
        locked_by="worker_01",
        received_at=now,
    )
    called = False

    async def _never_called(ev: Any, db: Any) -> None:
        nonlocal called
        called = True

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_environment", "live"),
    ):
        res = await execute_claimed_razorpay_webhook_event(
            session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),  # type: ignore[arg-type]
            claimed=claimed_blank,
            handler=_never_called,
            now_fn=lambda: now,
        )
    assert called is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert res.failure_category == "scope_mismatch"

    # 10d. Accounting fails closed when deployment ENVIRONMENT is missing/unrecognized
    org_id = uuid.uuid4()
    period = make_period(org_id=org_id, plan_tier="pro", now=now)
    task = make_task(org_id=org_id, period_id=period.id)
    live_sub = _make_razorpay_sub(org_id=org_id, environment="live", now=now)
    acc_session = AccountingMockSession(
        billing_periods={period.id: period},
        tasks={task.id: task},
        subscriptions={org_id: [live_sub]},
    )
    with patch.object(settings, "environment", "mystery_env"):
        acc_res = await reserve_usage(
            session=acc_session,  # type: ignore[arg-type]
            organization_id=org_id,
            task_id=task.id,
            billing_period_id=period.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    assert acc_res.success is False
    assert acc_res.error_code == AccountingErrorCode.RECONCILIATION_REQUIRED
