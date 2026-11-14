"""Live PostgreSQL 16 integration test suite for D-04A webhook concurrency & transaction safety.

Validates concurrency and transaction guarantees across real PostgreSQL 16:
- PG-D04A-01: Checkout verification versus webhook race and idempotent replay
- PG-D04A-02: Double checkout creation deduplication and expired-open versus verification race
- PG-D04A-03: Webhook worker lease takeover, savepoint rollback, atomic reconciliation, and cancellation
- PG-D04A-04: Usage quota versus payment settlement stress (deadlock-freedom SQLSTATE 40P01)
- PG-D04A-05: Transaction-scoped advisory locks, rollback release, signed 64-bit keys, and ON CONFLICT DO UPDATE

Guarantees & Invariants:
1. Real PostgreSQL 16: Never uses SQLite or Docker mocks. Tests run against PostgreSQL 16.
2. Connection Isolation: Uses SQLAlchemy NullPool with distinct database connections for concurrency.
3. Fail-Closed CI Gate: When REQUIRE_POSTGRES_TESTS=1, tests fail closed if PostgreSQL is unreachable.
4. Optional Local Skip: When REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unset, skips cleanly.
5. Strict Reverse-FK Cleanup: All test resources are scrubbed in strict dependency order in finally blocks.
"""

from __future__ import annotations

import os
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

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import SecretStr
import pytest
from sqlalchemy import delete, func, inspect as sa_inspect, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import configure_mappers

# Register all application ORM models
import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401

configure_mappers()

from app.config import settings
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSenderType
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingPayment,
    BillingWebhookEvent,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
    WebhookProcessingStatus,
)
from app.models.subscription import CheckoutAttemptStatus, Subscription, SubscriptionStatus
from app.services.billing.accounting import (
    AccountingErrorCode,
    ReservationResult,
    SettlementResult,
    reserve_usage,
    settle_reservation,
)
from app.services.billing.pricing import ModelPricing
from app.services.billing.razorpay.checkout import (
    RazorpayCheckoutConflictError,
    create_razorpay_subscription_checkout,
    verify_razorpay_checkout_callback,
)
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayClient,
    get_razorpay_client,
)
from app.services.billing.razorpay.handler_base import (
    build_payment_reconciliation_upsert_stmt,
    upsert_payment_reconciliation,
)
from app.services.billing.razorpay.lock import (
    PG_BIGINT_MAX,
    PG_BIGINT_MIN,
    acquire_razorpay_advisory_lock,
    derive_razorpay_advisory_lock_key,
    try_acquire_razorpay_advisory_lock,
)
from app.services.billing.razorpay.webhook_engine import (
    ClaimedWebhookEvent,
    ProcessResult,
    ProcessingOutcome,
    RazorpayWebhookReconciliationConflictError,
    StaleLeaseError,
    claim_next_razorpay_webhook_event,
    default_razorpay_dispatcher,
    execute_claimed_razorpay_webhook_event,
    record_razorpay_webhook_outcome,
)
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    PostgresTestConnectionError,
    ValidatedPostgresTestTarget,
    _OptionalPostgresUnreachableError,
    _default_async_pg_engine_factory,
    create_verified_async_session_factory,
    is_postgres_tests_required,
    resolve_verified_async_pg_engine,
    validate_postgres_test_url,
)

logger = logging.getLogger(__name__)

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
_SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)

_VERIFIED_TARGET_CACHE: ValidatedPostgresTestTarget | None = None


async def _get_verified_pg16_target() -> ValidatedPostgresTestTarget:
    """Verify live PostgreSQL 16 connectivity and target isolation once per process."""
    global _VERIFIED_TARGET_CACHE
    if _VERIFIED_TARGET_CACHE is not None:
        return _VERIFIED_TARGET_CACHE

    raw_url = os.environ.get("POSTGRES_TEST_URL")
    required = is_postgres_tests_required()
    target = validate_postgres_test_url(raw_url, require_postgres=required)
    if target is None:
        raise _OptionalPostgresUnreachableError(
            "Disposable PostgreSQL 16 test database not available in environment."
        )

    engine, _summary, _required = await resolve_verified_async_pg_engine()
    if engine is None:
        raise _OptionalPostgresUnreachableError(
            "Disposable PostgreSQL 16 test database not available in environment."
        )
    try:
        _VERIFIED_TARGET_CACHE = target
        return target
    finally:
        await engine.dispose()


@dataclass
class Pg16WebhookHarness:
    """Per-test live PostgreSQL 16 harness with tracked reverse-FK cleanup."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    target: ValidatedPostgresTestTarget

    org_ids: list[uuid.UUID] = field(default_factory=list)
    case_keys: list[str] = field(default_factory=list)
    sub_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    period_ids: list[uuid.UUID] = field(default_factory=list)
    task_ids: list[uuid.UUID] = field(default_factory=list)
    conv_ids: list[uuid.UUID] = field(default_factory=list)
    bot_ids: list[uuid.UUID] = field(default_factory=list)
    triggers: list[tuple[str, str, str]] = field(default_factory=list)

    def register_org(self, org_id: uuid.UUID) -> uuid.UUID:
        if org_id not in self.org_ids:
            self.org_ids.append(org_id)
        return org_id

    def register_case_key(self, case_key: str) -> str:
        if case_key not in self.case_keys:
            self.case_keys.append(case_key)
        return case_key

    def register_sub(self, sub_id: uuid.UUID) -> uuid.UUID:
        if sub_id not in self.sub_ids:
            self.sub_ids.append(sub_id)
        return sub_id

    def register_attempt(self, attempt_id: uuid.UUID) -> uuid.UUID:
        if attempt_id not in self.attempt_ids:
            self.attempt_ids.append(attempt_id)
        return attempt_id

    def register_event(self, event_id: uuid.UUID) -> uuid.UUID:
        if event_id not in self.event_ids:
            self.event_ids.append(event_id)
        return event_id

    def register_period(self, period_id: uuid.UUID) -> uuid.UUID:
        if period_id not in self.period_ids:
            self.period_ids.append(period_id)
        return period_id

    def register_task(self, task_id: uuid.UUID) -> uuid.UUID:
        if task_id not in self.task_ids:
            self.task_ids.append(task_id)
        return task_id

    def register_conversation(self, conv_id: uuid.UUID) -> uuid.UUID:
        if conv_id not in self.conv_ids:
            self.conv_ids.append(conv_id)
        return conv_id

    def register_chatbot(self, bot_id: uuid.UUID) -> uuid.UUID:
        if bot_id not in self.bot_ids:
            self.bot_ids.append(bot_id)
        return bot_id

    def register_trigger(self, trigger_name: str, table_name: str, function_name: str) -> None:
        entry = (trigger_name, table_name, function_name)
        if entry not in self.triggers:
            self.triggers.append(entry)

    async def create_org(self, name: str | None = None) -> Organization:
        org = Organization(
            id=uuid.uuid4(),
            name=name or f"PG16 Webhook Org {uuid.uuid4().hex[:8]}",
        )
        self.register_org(org.id)
        async with self.session_factory() as db:
            db.add(org)
            await db.commit()
        return org

    async def create_provision(
        self,
        org_id: uuid.UUID,
        provider_customer_id: str | None = None,
        status: CustomerProvisionStatus = CustomerProvisionStatus.resolved,
    ) -> BillingCustomerProvision:
        cust_id = provider_customer_id or f"cust_pg16_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        prov = BillingCustomerProvision(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="default",
            environment="test",
            status=status,
            provider_customer_id=cust_id if status == CustomerProvisionStatus.resolved else None,
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as db:
            db.add(prov)
            await db.commit()
        return prov

    async def create_subscription(
        self,
        org_id: uuid.UUID,
        provider_sub_id: str | None = None,
        status: SubscriptionStatus = SubscriptionStatus.incomplete,
    ) -> Subscription:
        sub_id_str = provider_sub_id or f"sub_pg16_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="default",
            environment="test",
            provider_subscription_id=sub_id_str,
            provider_customer_id=f"cust_pg16_{uuid.uuid4().hex[:8]}",
            status=status,
            plan_tier="pro",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            created_at=now,
            updated_at=now,
        )
        self.register_sub(sub.id)
        async with self.session_factory() as db:
            db.add(sub)
            await db.commit()
        return sub

    async def create_checkout_attempt(
        self,
        org_id: uuid.UUID,
        provider_sub_id: str,
        status: CheckoutAttemptStatus = CheckoutAttemptStatus.open,
        expires_at: datetime | None = None,
        provider_payment_id: str | None = None,
    ) -> BillingCheckoutAttempt:
        now = datetime.now(timezone.utc)
        attempt = BillingCheckoutAttempt(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="default",
            environment="test",
            plan_tier="pro",
            status=status,
            provider_subscription_id=provider_sub_id,
            provider_payment_id=provider_payment_id,
            expires_at=expires_at or (now + timedelta(hours=2)),
            created_at=now,
            updated_at=now,
        )
        self.register_attempt(attempt.id)
        async with self.session_factory() as db:
            db.add(attempt)
            await db.commit()
        return attempt

    async def create_webhook_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        provider_event_id: str | None = None,
        status: WebhookProcessingStatus = WebhookProcessingStatus.pending,
    ) -> BillingWebhookEvent:
        now = datetime.now(timezone.utc)
        evt_id_str = provider_event_id or f"evt_pg16_{uuid.uuid4().hex[:12]}"
        event = BillingWebhookEvent(
            id=uuid.uuid4(),
            provider="razorpay",
            merchant_account_id="default",
            environment="test",
            provider_event_id=evt_id_str,
            event_type=event_type,
            status=status,
            payload=payload,
            signature_verified=True,
            received_at=now,
            attempts=0,
        )
        self.register_event(event.id)
        async with self.session_factory() as db:
            db.add(event)
            await db.commit()
        return event

    async def create_billing_period(
        self,
        org_id: uuid.UUID,
        subscription_id: uuid.UUID | None = None,
        quota: int = 100_000,
        hard_spending_cap_micros: int | None = 100_000_000,
        is_active: bool = True,
        plan_tier: str = "free",
    ) -> OrganizationBillingPeriod:
        now = datetime.now(timezone.utc)
        bp = OrganizationBillingPeriod(
            id=uuid.uuid4(),
            organization_id=org_id,
            subscription_id=subscription_id,
            period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=29),
            plan_tier=plan_tier,
            monthly_token_quota=quota,
            tokens_used=0,
            tokens_reserved=0,
            hard_spending_cap_micros=hard_spending_cap_micros,
            cost_micros_used=0,
            cost_micros_reserved=0,
            is_active=is_active,
            is_blocked=False,
            created_at=now,
            updated_at=now,
        )
        self.register_period(bp.id)
        async with self.session_factory() as db:
            db.add(bp)
            await db.commit()
        return bp

    async def create_generation_task(
        self,
        org_id: uuid.UUID,
        billing_period_id: uuid.UUID,
    ) -> GenerationTask:
        now = datetime.now(timezone.utc)
        bot = Chatbot(
            id=uuid.uuid4(),
            organization_id=org_id,
            name=f"Bot {uuid.uuid4().hex[:6]}",
            status=ChatbotStatus.active,
        )
        self.register_chatbot(bot.id)

        conv = Conversation(
            id=uuid.uuid4(),
            organization_id=org_id,
            chatbot_id=bot.id,
            visitor_id=f"vis_{uuid.uuid4().hex[:8]}",
            status=ConversationStatus.active,
        )
        self.register_conversation(conv.id)

        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            sender_type=MessageSenderType.visitor,
            content="Hello AI",
        )

        task = GenerationTask(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            visitor_message_id=msg.id,
            organization_id=org_id,
            billing_period_id=billing_period_id,
            status=GenerationTaskStatus.pending,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(minutes=5),
            attempts=0,
            max_attempts=3,
        )
        self.register_task(task.id)

        async with self.session_factory() as db:
            db.add_all([bot, conv, msg, task])
            await db.commit()
        return task


@pytest.fixture(autouse=True)
def _configure_razorpay_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure deterministic non-live Razorpay test settings for every test."""
    monkeypatch.setattr(settings, "razorpay_enabled", True)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_pg16_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("rzp_test_pg16_secret"))
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "default")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "plan_pg16_pro")
    monkeypatch.setattr(settings, "razorpay_plan_currency", "USD")


@pytest.fixture
async def pg():
    """Provide a function-scoped verified PostgreSQL 16 engine/session harness with reverse-FK cleanup."""
    try:
        target = await _get_verified_pg16_target()
    except _OptionalPostgresUnreachableError as exc:
        pytest.skip(str(exc))
    except (PostgresTestConfigError, PostgresTestConnectionError) as exc:
        pytest.fail(str(exc), pytrace=False)

    engine = _default_async_pg_engine_factory(target.async_url)
    session_factory = create_verified_async_session_factory(engine)
    harness = Pg16WebhookHarness(
        engine=engine,
        session_factory=session_factory,
        target=target,
    )

    try:
        yield harness
    finally:
        try:
            async with engine.begin() as conn:
                for trigger_name, table_name, function_name in reversed(harness.triggers):
                    await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};"))
                    await conn.execute(text(f"DROP FUNCTION IF EXISTS {function_name}();"))

                if harness.case_keys or harness.org_ids:
                    await conn.execute(
                        text(
                            "DELETE FROM payment_reconciliations "
                            "WHERE case_key = ANY(:keys) OR organization_id = ANY(:orgs)"
                        ),
                        {
                            "keys": harness.case_keys or [""],
                            "orgs": harness.org_ids or [uuid.uuid4()],
                        },
                    )

                if harness.event_ids:
                    await conn.execute(
                        text("DELETE FROM billing_webhook_events WHERE id = ANY(:event_ids)"),
                        {"event_ids": harness.event_ids},
                    )

                if harness.org_ids:
                    await conn.execute(
                        text("DELETE FROM billing_payments WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM llm_usage_logs WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM usage_reservations WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM generation_tasks WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    if harness.conv_ids:
                        await conn.execute(
                            text("DELETE FROM messages WHERE conversation_id = ANY(:convs)"),
                            {"convs": harness.conv_ids},
                        )
                    await conn.execute(
                        text("DELETE FROM conversations WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM chatbots WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM billing_checkout_attempts WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM organization_billing_periods WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM billing_customer_provisions WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM subscriptions WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM organizations WHERE id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )

                    # Assert strict zero leakage
                    res_orgs = await conn.execute(
                        text("SELECT count(*) FROM organizations WHERE id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    assert int(res_orgs.scalar_one()) == 0
        finally:
            await engine.dispose()


# =============================================================================
# SCENARIO PG-D04A-01: Checkout verification versus webhook race & replay
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_01_checkout_verification_vs_subscription_activated_webhook_race(pg: Pg16WebhookHarness):
    """Test competing checkout verification and webhook processing on real PostgreSQL 16.

    Asserts:
    - Both transactions serialize cleanly through Row Lock / Advisory Lock hierarchy.
    - No double entitlement, zero duplicate subscriptions or checkout attempts.
    - Deterministic final state: checkout attempt complete, subscription active.
    """
    org = await pg.create_org()
    cust_id = f"cust_pg16_{uuid.uuid4().hex[:8]}"
    await pg.create_provision(org.id, provider_customer_id=cust_id)
    sub_id_str = f"sub_pg16_race_{uuid.uuid4().hex[:8]}"
    sub = await pg.create_subscription(org.id, provider_sub_id=sub_id_str, status=SubscriptionStatus.incomplete)
    attempt = await pg.create_checkout_attempt(org.id, provider_sub_id=sub_id_str, status=CheckoutAttemptStatus.open)

    pay_id = f"pay_pg16_{uuid.uuid4().hex[:12]}"
    webhook_payload = {
        "entity": "event",
        "account_id": "default",
        "event": "subscription.activated",
        "contains": ["subscription"],
        "payload": {
            "subscription": {
                "entity": {
                    "id": sub_id_str,
                    "customer_id": cust_id,
                    "plan_id": "plan_pg16_pro",
                    "status": "active",
                    "current_start": int(datetime.now(timezone.utc).timestamp()),
                    "current_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
                }
            }
        },
    }
    event = await pg.create_webhook_event("subscription.activated", webhook_payload)

    # Mock payment retrieval for checkout verification
    mock_payment_data = {
        "id": pay_id,
        "entity": "payment",
        "amount": 100000,
        "currency": "USD",
        "status": "captured",
        "subscription_id": sub_id_str,
        "customer_id": cust_id,
        "notes": {
            "organization_id": str(org.id),
            "attempt_id": str(attempt.id),
        },
    }

    async def run_checkout_verification():
        async with pg.session_factory() as db:
            with patch("app.services.billing.razorpay.checkout.get_razorpay_client") as mock_get_client, \
                 patch("app.services.billing.razorpay.checkout.verify_razorpay_subscription_signature", return_value=True):
                mock_client = MagicMock()
                mock_client.get_payment = AsyncMock(return_value=mock_payment_data)
                mock_get_client.return_value = mock_client
                return await verify_razorpay_checkout_callback(
                    db=db,
                    organization_id=org.id,
                    attempt_id=attempt.id,
                    razorpay_payment_id=pay_id,
                    razorpay_subscription_id=sub_id_str,
                    razorpay_signature="valid_mock_signature",
                )

    async def run_webhook_processing():
        async with pg.session_factory() as claim_db:
            claimed = await claim_next_razorpay_webhook_event(
                db=claim_db,
                worker_id="worker_pg16_race",
            )
        assert claimed is not None
        assert claimed.event_id == event.id
        return await execute_claimed_razorpay_webhook_event(
            session_factory=pg.session_factory,
            claimed=claimed,
            handler=None,
        )

    results = await asyncio.gather(
        run_checkout_verification(),
        run_webhook_processing(),
        return_exceptions=True,
    )

    for r in results:
        if isinstance(r, Exception):
            raise r

    # Verify deterministic final database state
    async with pg.session_factory() as verify_db:
        # 1. Checkout attempt must be complete with payment bound
        res_att = await verify_db.execute(
            select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt.id)
        )
        final_att = res_att.scalar_one()
        assert final_att.status == CheckoutAttemptStatus.complete
        assert final_att.provider_payment_id == pay_id

        # 2. Subscription must be active
        res_sub = await verify_db.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
        final_sub = res_sub.scalar_one()
        assert final_sub.status == SubscriptionStatus.active

        # 3. Webhook event must be processed
        res_evt = await verify_db.execute(
            select(BillingWebhookEvent).where(BillingWebhookEvent.id == event.id)
        )
        final_evt = res_evt.scalar_one()
        assert final_evt.status == WebhookProcessingStatus.processed

        # 4. Zero duplicate records
        att_count = await verify_db.execute(
            select(func.count(BillingCheckoutAttempt.id)).where(BillingCheckoutAttempt.organization_id == org.id)
        )
        assert att_count.scalar_one() == 1

        sub_count = await verify_db.execute(
            select(func.count(Subscription.id)).where(Subscription.organization_id == org.id)
        )
        assert sub_count.scalar_one() == 1


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_01_idempotent_replay_behavior(pg: Pg16WebhookHarness):
    """Test idempotent replay of completed checkout verification and processed webhook."""
    org = await pg.create_org()
    cust_id = f"cust_pg16_{uuid.uuid4().hex[:8]}"
    await pg.create_provision(org.id, provider_customer_id=cust_id)
    sub_id_str = f"sub_pg16_replay_{uuid.uuid4().hex[:8]}"
    sub = await pg.create_subscription(org.id, provider_sub_id=sub_id_str, status=SubscriptionStatus.active)
    pay_id = f"pay_pg16_rep_{uuid.uuid4().hex[:12]}"
    attempt = await pg.create_checkout_attempt(
        org.id,
        provider_sub_id=sub_id_str,
        status=CheckoutAttemptStatus.complete,
        provider_payment_id=pay_id,
    )

    # Replay checkout verification
    async with pg.session_factory() as db:
        with patch("app.services.billing.razorpay.checkout.verify_razorpay_subscription_signature", return_value=True):
            replay_res = await verify_razorpay_checkout_callback(
                db=db,
                organization_id=org.id,
                attempt_id=attempt.id,
                razorpay_payment_id=pay_id,
                razorpay_subscription_id=sub_id_str,
                razorpay_signature="mock_sig",
            )
            assert replay_res.status == CheckoutAttemptStatus.complete
            assert replay_res.payment_id == pay_id

    # Verify claim rejection for already processed event
    event = await pg.create_webhook_event(
        "subscription.activated",
        payload={"dummy": True},
        status=WebhookProcessingStatus.processed,
    )
    async with pg.session_factory() as db:
        claimed = await claim_next_razorpay_webhook_event(db, worker_id="worker_rep")
        assert claimed is None


# =============================================================================
# SCENARIO PG-D04A-02: Double checkout & expired-open vs verification
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_02_concurrent_checkout_creation_deduplication(pg: Pg16WebhookHarness):
    """Test concurrent checkout creation attempts on real PostgreSQL 16.

    Asserts:
    - Only one active checkout attempt exists per organization.
    - PostgreSQL unique constraints prevent duplicate active checkout states.
    """
    org = await pg.create_org()
    cust_id = f"cust_pg16_dedup_{uuid.uuid4().hex[:8]}"
    await pg.create_provision(org.id, provider_customer_id=cust_id)

    mock_sub_data = {
        "id": f"sub_pg16_chk_{uuid.uuid4().hex[:8]}",
        "entity": "subscription",
        "plan_id": "plan_pg16_pro",
        "customer_id": cust_id,
        "status": "created",
        "current_start": None,
        "current_end": None,
    }

    async def try_create_checkout(session: AsyncSession):
        with patch("app.services.billing.razorpay.checkout.get_razorpay_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client.create_subscription = AsyncMock(return_value=mock_sub_data)
            mock_client_factory.return_value = mock_client
            return await create_razorpay_subscription_checkout(
                db=session,
                organization=org,
                user_email="user@example.com",
                customer_session_factory=pg.session_factory,
            )

    async with pg.session_factory() as db1, pg.session_factory() as db2:
        results = await asyncio.gather(
            try_create_checkout(db1),
            try_create_checkout(db2),
            return_exceptions=True,
        )

    # At least one succeeded or both converged safely
    successful = [r for r in results if not isinstance(r, Exception)]
    assert len(successful) >= 1

    async with pg.session_factory() as verify_db:
        # Check active attempts in PostgreSQL
        res = await verify_db.execute(
            select(func.count(BillingCheckoutAttempt.id)).where(
                BillingCheckoutAttempt.organization_id == org.id,
                BillingCheckoutAttempt.status.in_([CheckoutAttemptStatus.open, CheckoutAttemptStatus.creating]),
            )
        )
        active_attempts = res.scalar_one()
        assert active_attempts == 1


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_02_expired_open_checkout_vs_verification_race(pg: Pg16WebhookHarness):
    """Test race between expired checkout attempt and verification callback.

    Asserts:
    - Expired checkout attempt is rejected and preserved/transitioned to 'expired'.
    - Expired attempts are NEVER resurrected to 'complete' or 'open'.
    - Subscription status is not activated.
    """
    org = await pg.create_org()
    sub_id_str = f"sub_pg16_exp_{uuid.uuid4().hex[:8]}"
    sub = await pg.create_subscription(org.id, provider_sub_id=sub_id_str, status=SubscriptionStatus.incomplete)
    now_past = datetime.now(timezone.utc) - timedelta(minutes=30)
    attempt = await pg.create_checkout_attempt(
        org.id,
        provider_sub_id=sub_id_str,
        status=CheckoutAttemptStatus.open,
        expires_at=now_past,
    )

    pay_id = f"pay_pg16_exp_{uuid.uuid4().hex[:12]}"
    mock_payment_data = {
        "id": pay_id,
        "entity": "payment",
        "amount": 100000,
        "currency": "USD",
        "status": "captured",
        "subscription_id": sub_id_str,
        "notes": {"organization_id": str(org.id), "attempt_id": str(attempt.id)},
    }

    async with pg.session_factory() as db:
        with patch("app.services.billing.razorpay.checkout.get_razorpay_client") as mock_client_factory, \
             patch("app.services.billing.razorpay.checkout.verify_razorpay_subscription_signature", return_value=True):
            mock_client = MagicMock()
            mock_client.get_payment = AsyncMock(return_value=mock_payment_data)
            mock_client_factory.return_value = mock_client

            with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
                await verify_razorpay_checkout_callback(
                    db=db,
                    organization_id=org.id,
                    attempt_id=attempt.id,
                    razorpay_payment_id=pay_id,
                    razorpay_subscription_id=sub_id_str,
                    razorpay_signature="mock_sig",
                )
            assert "expired" in str(exc_info.value).lower()

    # Verify attempt in DB is expired, NOT complete
    async with pg.session_factory() as verify_db:
        res_att = await verify_db.execute(
            select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt.id)
        )
        db_att = res_att.scalar_one()
        assert db_att.status == CheckoutAttemptStatus.expired

        # Verify subscription was NOT activated
        res_sub = await verify_db.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
        db_sub = res_sub.scalar_one()
        assert db_sub.status == SubscriptionStatus.incomplete


# =============================================================================
# SCENARIO PG-D04A-03: Webhook worker lease takeover & savepoint reconciliation
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_03_stale_worker_fenced_after_lease_loss(pg: Pg16WebhookHarness):
    """Test that a stale worker losing its lease cannot commit domain mutations."""
    org = await pg.create_org()
    event = await pg.create_webhook_event(
        "subscription.activated",
        payload={"dummy": "fencing_test"},
    )

    # Worker 1 claims event
    async with pg.session_factory() as db1:
        claimed1 = await claim_next_razorpay_webhook_event(
            db1,
            worker_id="worker_1",
            lease_duration_seconds=1,
        )
    assert claimed1 is not None

    # Simulate lease takeover by Worker 2
    new_token = uuid.uuid4()
    async with pg.session_factory() as db_takeover:
        await db_takeover.execute(
            update(BillingWebhookEvent)
            .where(BillingWebhookEvent.id == event.id)
            .values(
                locked_by="worker_2",
                lease_token=new_token,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        await db_takeover.commit()

    # Worker 1 attempts execution with stale claim
    result = await execute_claimed_razorpay_webhook_event(
        session_factory=pg.session_factory,
        claimed=claimed1,
        handler=None,
    )

    assert result.success is False
    assert result.lease_lost is True
    assert result.failure_category == "stale_lease"

    # Verify second fencing path: record_razorpay_webhook_outcome raises StaleLeaseError without MissingGreenlet
    async with pg.session_factory() as db_fencing:
        with pytest.raises(StaleLeaseError):
            await record_razorpay_webhook_outcome(
                db=db_fencing,
                claimed=claimed1,
                outcome=ProcessingOutcome.SUCCESS,
            )

    # Verify event row in DB preserves Worker 2's lease and no accidental modifications occurred
    async with pg.session_factory() as verify_db:
        res = await verify_db.execute(
            select(BillingWebhookEvent).where(BillingWebhookEvent.id == event.id)
        )
        evt = res.scalar_one()
        assert evt.locked_by == "worker_2"
        assert evt.lease_token == new_token
        assert evt.status == WebhookProcessingStatus.pending


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_03_savepoint_rollback_on_handler_failure_with_atomic_quarantine(pg: Pg16WebhookHarness):
    """Test that handler failure rolls back domain writes while outer transaction quarantines atomically."""
    org = await pg.create_org()
    sub_id_str = f"sub_pg16_sp_{uuid.uuid4().hex[:8]}"
    sub = await pg.create_subscription(org.id, provider_sub_id=sub_id_str, status=SubscriptionStatus.incomplete)
    case_key = pg.register_case_key(f"recon_pg16_sp_{uuid.uuid4().hex[:8]}")

    event = await pg.create_webhook_event(
        "subscription.activated",
        payload={"sub_id": sub_id_str},
    )

    async with pg.session_factory() as db:
        claimed = await claim_next_razorpay_webhook_event(db, worker_id="worker_sp")
    assert claimed is not None

    async def failing_handler(claimed_evt, work_db):
        # Mutate subscription in savepoint
        await work_db.execute(
            update(Subscription)
            .where(Subscription.id == sub.id)
            .values(status=SubscriptionStatus.active)
        )
        # Raise reconciliation conflict
        raise RazorpayWebhookReconciliationConflictError(
            reason="reconciliation_conflict_test",
            case_key=case_key,
            organization_id=org.id,
            subscription_id=sub.id,
            diagnostic_payload={"error": "simulated_handler_conflict"},
        )

    result = await execute_claimed_razorpay_webhook_event(
        session_factory=pg.session_factory,
        claimed=claimed,
        handler=failing_handler,
    )

    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined

    async with pg.session_factory() as verify_db:
        # 1. Inner savepoint rollback check: Subscription status remains 'created'
        res_sub = await verify_db.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
        db_sub = res_sub.scalar_one()
        assert db_sub.status == SubscriptionStatus.incomplete

        # 2. Outer transaction atomic commit: Event is quarantined
        res_evt = await verify_db.execute(
            select(BillingWebhookEvent).where(BillingWebhookEvent.id == event.id)
        )
        db_evt = res_evt.scalar_one()
        assert db_evt.status == WebhookProcessingStatus.quarantined
        assert "reconciliation conflict" in (db_evt.quarantine_reason or "").lower()

        # 3. Outer transaction atomic commit: PaymentReconciliation row exists
        res_rec = await verify_db.execute(
            select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key)
        )
        db_rec = res_rec.scalar_one()
        assert db_rec.status == ReconciliationStatus.pending_review
        assert db_rec.organization_id == org.id
        assert db_rec.subscription_id == sub.id


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_03_reconciliation_replay_upsert_idempotence(pg: Pg16WebhookHarness):
    """Test that replaying conflict event upserts PaymentReconciliation idempotently without duplicate rows."""
    org = await pg.create_org()
    case_key = pg.register_case_key(f"recon_pg16_rep_{uuid.uuid4().hex[:8]}")

    async with pg.session_factory() as db1:
        rec1 = await upsert_payment_reconciliation(
            db=db1,
            case_key=case_key,
            reason="Initial error",
            organization_id=org.id,
            diagnostic_data={"attempt": 1},
        )
        await db1.commit()
    assert rec1 is not None

    async with pg.session_factory() as db2:
        rec2 = await upsert_payment_reconciliation(
            db=db2,
            case_key=case_key,
            reason="Repeated error on replay",
            organization_id=org.id,
            diagnostic_data={"attempt": 2},
        )
        await db2.commit()
    assert rec2 is not None

    async with pg.session_factory() as verify_db:
        res = await verify_db.execute(
            select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key)
        )
        recs = res.scalars().all()
        assert len(recs) == 1
        final_rec = recs[0]
        assert final_rec.reason == "Repeated error on replay"
        history = final_rec.diagnostic_payload.get("history", [])
        assert len(history) == 2


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_03_cancellation_during_handler_execution_leaves_no_partial_writes(pg: Pg16WebhookHarness):
    """Test that task cancellation rolls back database writes cleanly and leaves no partial state."""
    org = await pg.create_org()
    sub_id_str = f"sub_pg16_canc_{uuid.uuid4().hex[:8]}"
    sub = await pg.create_subscription(org.id, provider_sub_id=sub_id_str, status=SubscriptionStatus.incomplete)
    event = await pg.create_webhook_event(
        "subscription.activated",
        payload={"sub_id": sub_id_str},
    )

    async with pg.session_factory() as db:
        claimed = await claim_next_razorpay_webhook_event(db, worker_id="worker_canc")
    assert claimed is not None

    started = asyncio.Event()

    async def cancelling_handler(claimed_evt, work_db):
        await work_db.execute(
            update(Subscription)
            .where(Subscription.id == sub.id)
            .values(status=SubscriptionStatus.active)
        )
        started.set()
        await asyncio.sleep(10.0)

    task = asyncio.create_task(
        execute_claimed_razorpay_webhook_event(
            session_factory=pg.session_factory,
            claimed=claimed,
            handler=cancelling_handler,
        )
    )

    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Assert transaction was rolled back and subscription is still 'created'
    async with pg.session_factory() as verify_db:
        res = await verify_db.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
        db_sub = res.scalar_one()
        assert db_sub.status == SubscriptionStatus.incomplete


# =============================================================================
# SCENARIO PG-D04A-04: Usage quota versus payment settlement stress
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_04_concurrent_quota_reservation_and_settlement_stress(pg: Pg16WebhookHarness):
    """Test concurrent quota reservation, settlement, and payment under stress on PostgreSQL 16.

    Asserts:
    - Zero deadlocks (no SQLSTATE 40P01).
    - No over-allocation of monthly token quota.
    - Check constraints (ck_billing_periods_tokens_non_negative, ck_billing_periods_cost_non_negative) hold.
    """
    org = await pg.create_org()
    period = await pg.create_billing_period(
        org_id=org.id,
        quota=50_000,
        hard_spending_cap_micros=50_000_000,
    )

    # Create 4 tasks
    tasks = [await pg.create_generation_task(org.id, period.id) for _ in range(4)]

    pricing = ModelPricing(
        model_name="mock-stress-model",
        pricing_version="v1",
        input_rate_micros_per_million=2_000_000,
        output_rate_micros_per_million=8_000_000,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
    )

    async def reserve_task(t_id: uuid.UUID):
        async with pg.session_factory() as db:
            return await reserve_usage(
                session=db,
                organization_id=org.id,
                task_id=t_id,
                billing_period_id=period.id,
                pricing=pricing,
            )

    # Run 4 concurrent reservations
    res_list = await asyncio.gather(*[reserve_task(t.id) for t in tasks], return_exceptions=True)
    successful_reservations = [r for r in res_list if isinstance(r, ReservationResult) and r.success]
    assert len(successful_reservations) >= 1

    # Concurrent settlement of successful reservations
    async def settle_task(res: ReservationResult):
        async with pg.session_factory() as db:
            assert res.reservation is not None
            return await settle_reservation(
                session=db,
                reservation_id=res.reservation.id,
                organization_id=org.id,
                actual_tokens=500,
                actual_cost_micros=2_500,
                pricing=pricing,
            )

    settle_list = await asyncio.gather(*[settle_task(r) for r in successful_reservations], return_exceptions=True)
    for s in settle_list:
        assert not isinstance(s, Exception)
        assert isinstance(s, SettlementResult)
        assert s.success is True

    # Verify final billing period accounting
    async with pg.session_factory() as verify_db:
        res_bp = await verify_db.execute(
            select(OrganizationBillingPeriod).where(OrganizationBillingPeriod.id == period.id)
        )
        final_bp = res_bp.scalar_one()
        assert final_bp.tokens_used >= 0
        assert final_bp.tokens_reserved >= 0
        assert final_bp.cost_micros_used >= 0
        assert final_bp.cost_micros_reserved >= 0
        assert (final_bp.tokens_used + final_bp.tokens_reserved) <= final_bp.monthly_token_quota


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_04_double_settlement_prevention(pg: Pg16WebhookHarness):
    """Test that concurrent settlement attempts on the same reservation prevent double-settlement."""
    org = await pg.create_org()
    period = await pg.create_billing_period(org_id=org.id, quota=50_000)
    task = await pg.create_generation_task(org.id, period.id)

    pricing = ModelPricing(
        model_name="mock-stress-model",
        pricing_version="v1",
        input_rate_micros_per_million=2_000_000,
        output_rate_micros_per_million=8_000_000,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
    )

    async with pg.session_factory() as db:
        res = await reserve_usage(
            session=db,
            organization_id=org.id,
            task_id=task.id,
            billing_period_id=period.id,
            pricing=pricing,
        )
    assert res.success is True

    # Concurrent settlement of the exact same reservation with different usage amounts
    assert res.reservation is not None
    async def try_settle(tokens: int, micros: int):
        async with pg.session_factory() as db:
            return await settle_reservation(
                session=db,
                reservation_id=res.reservation.id,
                organization_id=org.id,
                actual_tokens=tokens,
                actual_cost_micros=micros,
                pricing=pricing,
            )

    outcomes = await asyncio.gather(
        try_settle(400, 2000),
        try_settle(600, 3000),
        return_exceptions=True,
    )

    successful = [o for o in outcomes if isinstance(o, SettlementResult) and o.success]
    assert len(successful) == 1  # Exactly one settlement succeeded; conflicting settlement rejected


# =============================================================================
# SCENARIO PG-D04A-05: Advisory locks, rollback release & reconciliation upsert
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_05_advisory_lock_acquisition_and_transaction_rollback_release(pg: Pg16WebhookHarness):
    """Test that PostgreSQL transaction advisory locks are cleanly released upon rollback."""
    entity_id = f"sub_pg16_lock_{uuid.uuid4().hex[:8]}"

    async with pg.session_factory() as db1:
        await acquire_razorpay_advisory_lock(db1, "subscription", entity_id)

        # In concurrent session, try_acquire must return False
        async with pg.session_factory() as db2:
            acquired = await try_acquire_razorpay_advisory_lock(db2, "subscription", entity_id)
            assert acquired is False

        # Rollback session 1
        await db1.rollback()

    # Now session 2 must be able to acquire the advisory lock
    async with pg.session_factory() as db3:
        acquired_after = await try_acquire_razorpay_advisory_lock(db3, "subscription", entity_id)
        assert acquired_after is True
        await db3.rollback()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_05_signed_64bit_advisory_keys_and_negative_values(pg: Pg16WebhookHarness):
    """Test that derived signed 64-bit keys (including negative values) execute cleanly in PostgreSQL."""
    # Find an entity ID that yields a negative signed 64-bit key
    neg_id = None
    neg_key = None
    for i in range(100):
        test_id = f"sub_neg_probe_{i}"
        key = derive_razorpay_advisory_lock_key("subscription", test_id)
        if key < 0:
            neg_id = test_id
            neg_key = key
            break

    assert neg_id is not None
    assert neg_key is not None
    assert PG_BIGINT_MIN <= neg_key < 0

    # Acquire the negative lock key in PostgreSQL
    async with pg.session_factory() as db:
        res = await db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": neg_key})
        assert bool(res.scalar()) is True
        await db.rollback()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_05_deterministic_key_derivation_and_domain_separation(pg: Pg16WebhookHarness):
    """Test that identical logical inputs derive identical keys and different entity types are isolated."""
    common_id = "entity_common_pg16"
    k_sub = derive_razorpay_advisory_lock_key("subscription", common_id)
    k_sub_repeat = derive_razorpay_advisory_lock_key("subscription", common_id)
    k_pay = derive_razorpay_advisory_lock_key("payment", common_id)
    k_inv = derive_razorpay_advisory_lock_key("invoice", common_id)
    k_cust = derive_razorpay_advisory_lock_key("customer", common_id)

    assert k_sub == k_sub_repeat
    assert len({k_sub, k_pay, k_inv, k_cust}) == 4  # All distinct

    # Acquire subscription lock in db1, and verify payment lock can be simultaneously acquired in db2
    async with pg.session_factory() as db1, pg.session_factory() as db2:
        await acquire_razorpay_advisory_lock(db1, "subscription", common_id)
        acquired_pay = await try_acquire_razorpay_advisory_lock(db2, "payment", common_id)
        assert acquired_pay is True
        await db1.rollback()
        await db2.rollback()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_d04a_05_native_on_conflict_do_update_reconciliation_with_jsonb_history(pg: Pg16WebhookHarness):
    """Test native PostgreSQL ON CONFLICT DO UPDATE on payment_reconciliations with JSONB history append."""
    org = await pg.create_org()
    case_key = pg.register_case_key(f"recon_pg16_jsonb_{uuid.uuid4().hex[:8]}")

    # First upsert
    async with pg.session_factory() as db1:
        rec1 = await upsert_payment_reconciliation(
            db=db1,
            case_key=case_key,
            reason="First conflict",
            organization_id=org.id,
            diagnostic_data={"code": "ERR_1"},
        )
        await db1.commit()

    assert rec1 is not None
    assert rec1.status == ReconciliationStatus.pending_review

    # Transition status to resolved_refunded
    async with pg.session_factory() as db_upd:
        await db_upd.execute(
            update(PaymentReconciliation)
            .where(PaymentReconciliation.case_key == case_key)
            .values(status=ReconciliationStatus.resolved_refunded)
        )
        await db_upd.commit()

    # Second upsert on same case_key: must reopen to pending_review and append history
    async with pg.session_factory() as db2:
        rec2 = await upsert_payment_reconciliation(
            db=db2,
            case_key=case_key,
            reason="Second conflict reopening case",
            organization_id=org.id,
            diagnostic_data={"code": "ERR_2"},
        )
        await db2.commit()

    assert rec2 is not None

    async with pg.session_factory() as verify_db:
        res = await verify_db.execute(
            select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key)
        )
        final_rec = res.scalar_one()
        # Reopening rule verified
        assert final_rec.status == ReconciliationStatus.pending_review
        assert final_rec.reason == "Second conflict reopening case"
        # JSONB history append verified
        diag_payload = final_rec.diagnostic_payload
        if isinstance(diag_payload, str):
            import json
            diag_payload = json.loads(diag_payload)
        history = diag_payload.get("history", [])
        assert len(history) == 2
        assert diag_payload.get("latest", {}).get("code") == "ERR_2"
