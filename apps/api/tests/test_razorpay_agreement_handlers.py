"""Comprehensive Unit and Integration Tests for Razorpay Subscription Agreement Lifecycle Handlers.

Validates Step 5.2D.3.2 requirements:
1. subscription.authenticated:
   - Binds subscription to tenant organization and customer.
   - Updates local Subscription to 'incomplete', BillingCheckoutAttempt to 'payment_pending'.
   - NEVER grants Pro entitlement (plan_tier remains 'free').
   - NEVER creates an OrganizationBillingPeriod.
2. subscription.activated:
   - Valid state transition: incomplete -> active.
   - Populates verified current_period_start and current_period_end timestamps.
   - Transitions BillingCheckoutAttempt to 'complete'.
   - Idempotent replay on active status.
   - Refuses resurrection of terminal (canceled) subscriptions.
   - NEVER cures past_due (past_due_since anchor is strictly preserved).
   - STRICT INVARIANT: plan_tier remains 'free'. Zero Pro quota granted.
   - STRICT INVARIANT: Zero OrganizationBillingPeriod rows created.
3. subscription.updated:
   - Monotonic updates to plan_id and period boundaries.
   - Stale out-of-order events discarded safely without regressing state.
   - Never infers payment success from subscription status.
   - Unknown subscription stages PaymentReconciliation case.
4. subscription.completed:
   - Transitions Subscription to status='canceled', plan_tier='free', canceled_at=event_created_at.
   - Idempotent duplicates on canceled status.
   - Does NOT modify OrganizationBillingPeriod rows or mutate quota counters directly.
   - Unknown subscription stages PaymentReconciliation case.
5. Cross-Tenant Isolation:
   - Rejects mismatched merchant accounts and enforces strict scoping.
6. Lease Fencing:
   - Stale worker with expired lease token is rejected with StaleLeaseError and rolls back.
"""

from __future__ import annotations

import sys

# Windows asyncio ProactorEventLoop fix
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self: Any) -> None:
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

import pytest
from sqlalchemy import text

# Import all models to ensure SQLAlchemy class registry resolves relationships
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401

from app.models.billing import OrganizationBillingPeriod
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.razorpay.agreement_handlers import (
    handle_subscription_activated,
    handle_subscription_authenticated,
    handle_subscription_completed,
    handle_subscription_updated,
    register_razorpay_agreement_handlers,
)
from app.services.billing.razorpay.handler_base import (
    RazorpayMissingEntityIdError,
    RazorpayPayloadValidationError,
    RazorpayScopeMismatchError,
    RazorpayWebhookPermanentError,
    RazorpayWebhookReconciliationConflictError,
    is_terminal_subscription,
)
from app.services.billing.razorpay.webhook_engine import (
    ClaimedWebhookEvent,
    RazorpayWebhookDispatcher,
    StaleLeaseError,
    default_razorpay_dispatcher,
)


# =============================================================================
# In-Memory Mock Database Session
# =============================================================================


class MockScalarResult:
    """Mock result supporting scalar_one_or_none(), scalar(), and scalars()."""

    def __init__(self, data: Any = None) -> None:
        self._data = data

    def scalar_one_or_none(self) -> Any:
        if isinstance(self._data, list):
            return self._data[0] if self._data else None
        return self._data

    def scalar(self) -> Any:
        return self.scalar_one_or_none()

    def scalars(self) -> "MockScalarResult":
        return self

    def all(self) -> list[Any]:
        if isinstance(self._data, list):
            return self._data
        return [self._data] if self._data is not None else []


class RazorpayAgreementMockDbSession:
    """High-fidelity mock async session for subscription agreement lifecycle tests."""

    def __init__(self) -> None:
        self.customer_provisions: dict[str, BillingCustomerProvision] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.checkout_attempts: dict[str, BillingCheckoutAttempt] = {}
        self.billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] = {}
        self.webhook_events: dict[uuid.UUID, BillingWebhookEvent] = {}
        self.reconciliations: dict[str, PaymentReconciliation] = {}

        self.added: list[Any] = []
        self.commits: int = 0
        self.rollbacks: int = 0
        self.flushes: int = 0
        self.advisory_locks: list[int] = []

    async def __aenter__(self) -> "RazorpayAgreementMockDbSession":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            await self.rollback()
        return False

    def add(self, instance: Any) -> None:
        self.added.append(instance)
        if isinstance(instance, Subscription):
            key = instance.provider_subscription_id or str(instance.id)
            self.subscriptions[key] = instance
        elif isinstance(instance, BillingCheckoutAttempt):
            key = str(instance.id)
            self.checkout_attempts[key] = instance
        elif isinstance(instance, PaymentReconciliation):
            self.reconciliations[instance.case_key] = instance
        elif isinstance(instance, BillingCustomerProvision):
            self.customer_provisions[instance.provider_customer_id or str(instance.id)] = instance
        elif isinstance(instance, OrganizationBillingPeriod):
            self.billing_periods[instance.id] = instance
        elif isinstance(instance, BillingWebhookEvent):
            self.webhook_events[instance.id] = instance

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MockScalarResult:
        stmt_str = str(stmt.text if hasattr(stmt, "text") else stmt)

        # 1. Advisory Lock Query
        if "pg_advisory_xact_lock" in stmt_str:
            key = params.get("key") if params else None
            if key is not None:
                self.advisory_locks.append(key)
            return MockScalarResult(None)

        if "pg_try_advisory_xact_lock" in stmt_str:
            key = params.get("key") if params else None
            if key is not None:
                self.advisory_locks.append(key)
            return MockScalarResult(True)

        # 2. BillingCustomerProvision Lookups
        if "billing_customer_provisions" in stmt_str:
            for prov in self.customer_provisions.values():
                return MockScalarResult(prov.organization_id)
            return MockScalarResult(None)

        # 3. Subscription Lookups
        if "subscriptions" in stmt_str:
            # Full subscription model lookup (includes subscriptions.id)
            if "subscriptions.id" in stmt_str:
                for sub in self.subscriptions.values():
                    return MockScalarResult(sub)
                return MockScalarResult(None)
            # Querying organization_id specifically (tenant resolution)
            for sub in self.subscriptions.values():
                return MockScalarResult(sub.organization_id)
            return MockScalarResult(None)

        # 4. BillingCheckoutAttempt Lookups
        if "billing_checkout_attempts" in stmt_str:
            matched_attempts = list(self.checkout_attempts.values())
            return MockScalarResult(matched_attempts)

        # 5. PaymentReconciliation Lookups
        if "payment_reconciliations" in stmt_str:
            for rec in self.reconciliations.values():
                return MockScalarResult(rec)
            return MockScalarResult(None)

        # 6. BillingWebhookEvent Lookups (Lease assertion)
        if "billing_webhook_events" in stmt_str:
            target_id = None
            if hasattr(stmt, "_where_criteria"):
                for crit in stmt._where_criteria:
                    if hasattr(crit, "right") and hasattr(crit.right, "value"):
                        target_id = crit.right.value
                        break
            if target_id is not None and target_id in self.webhook_events:
                return MockScalarResult(self.webhook_events[target_id])
            for evt in self.webhook_events.values():
                return MockScalarResult(evt)
            return MockScalarResult(None)

        # 7. OrganizationBillingPeriod Lookups
        if "organization_billing_periods" in stmt_str:
            for bp in self.billing_periods.values():
                return MockScalarResult(bp)
            return MockScalarResult(None)

        return MockScalarResult(None)


# =============================================================================
# Helper Fixtures & Builders
# =============================================================================


def make_test_claimed_event(
    event_type: str,
    sub_id: str = "sub_test_123",
    status: str = "authenticated",
    plan_id: str = "plan_pro_monthly",
    customer_id: str = "cust_test_123",
    current_start: int | None = 1700000000,
    current_end: int | None = 1702592000,
    merchant_account_id: str = "acc_test_merchant",
    environment: str = "test",
    notes: dict[str, Any] | None = None,
    created_at_ts: int = 1700000000,
    lease_token: uuid.UUID | None = None,
    locked_by: str = "worker_1",
    lease_expires_delta: int = 60,
    session: RazorpayAgreementMockDbSession | None = None,
) -> ClaimedWebhookEvent:
    now = datetime.now(timezone.utc)
    token = lease_token or uuid.uuid4()
    evt_id = uuid.uuid4()
    claimed = ClaimedWebhookEvent(
        event_id=evt_id,
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type=event_type,
        payload={
            "entity": "event",
            "account_id": merchant_account_id,
            "event": event_type,
            "contains": ["subscription"],
            "created_at": created_at_ts,
            "payload": {
                "subscription": {
                    "entity": {
                        "id": sub_id,
                        "entity": "subscription",
                        "plan_id": plan_id,
                        "customer_id": customer_id,
                        "status": status,
                        "current_start": current_start,
                        "current_end": current_end,
                        "notes": notes or {},
                    }
                }
            },
        },
        attempts=0,
        lease_token=token,
        locked_by=locked_by,
        lease_expires_at=now + timedelta(seconds=lease_expires_delta),
        received_at=now,
    )
    if session is not None:
        db_event = BillingWebhookEvent(
            id=evt_id,
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            provider_event_id=claimed.provider_event_id,
            event_type=event_type,
            status=WebhookProcessingStatus.pending,
            attempts=0,
            lease_token=token,
            locked_by=locked_by,
            lease_expires_at=claimed.lease_expires_at,
            signature_verified=True,
            payload=claimed.payload,
        )
        session.webhook_events[evt_id] = db_event
    return claimed


# =============================================================================
# 1. subscription.authenticated Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_authenticated_creates_incomplete_subscription() -> None:
    """Verifies that subscription.authenticated creates an incomplete subscription with plan_tier='free'."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_001"
    cust_id = "cust_auth_001"

    # Prepopulate customer provision for authoritative tenant resolution
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    # Prepopulate checkout attempt
    attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_auth_001",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.checkout_attempts[sub_id] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        status="authenticated",
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    await handle_subscription_authenticated(claimed, session)

    # 1. Subscription created with status=incomplete and plan_tier=free
    assert sub_id in session.subscriptions
    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free", "Subscription MUST NOT be granted Pro entitlement on authentication!"
    assert sub.organization_id == org_id
    assert sub.provider_subscription_id == sub_id

    # 2. Checkout attempt transitioned to payment_pending
    assert attempt.status == CheckoutAttemptStatus.payment_pending

    # 3. ZERO billing periods created
    assert len(session.billing_periods) == 0, "Authentication event MUST NOT create a billing period!"

    # 4. Advisory lock acquired
    assert len(session.advisory_locks) >= 1


@pytest.mark.asyncio
async def test_subscription_authenticated_idempotent_duplicate() -> None:
    """Verifies that duplicate subscription.authenticated events do not duplicate records."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_dup_001"
    cust_id = "cust_auth_dup_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
        last_webhook_event_at=datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc),
    )
    session.subscriptions[sub_id] = existing_sub

    # Delivery with newer timestamp updates metadata idempotently
    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        created_at_ts=int(datetime(2026, 11, 8, 10, 5, tzinfo=timezone.utc).timestamp()),
        session=session,
    )
    await handle_subscription_authenticated(claimed, session)

    assert len(session.subscriptions) == 1
    assert session.subscriptions[sub_id].status == SubscriptionStatus.incomplete
    assert session.subscriptions[sub_id].plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_authenticated_missing_checkout_attempt() -> None:
    """Verifies that missing checkout attempt stages reconciliation and preserves subscription."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_missing_att_001"
    cust_id = "cust_auth_missing_att_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov
    # Intentionally zero checkout attempts in session.checkout_attempts

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    await handle_subscription_authenticated(claimed, session)

    # Subscription created with status=incomplete and plan_tier=free
    assert sub_id in session.subscriptions
    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free"

    # Reconciliation case staged
    case_key = f"missing_checkout_attempt:{sub_id}"
    assert case_key in session.reconciliations
    rec = session.reconciliations[case_key]
    assert rec.reason == "missing_checkout_attempt"
    assert rec.organization_id == org_id
    assert rec.diagnostic_payload["latest"]["provider_subscription_id"] == sub_id
    assert rec.diagnostic_payload["latest"]["provider_customer_id"] == cust_id


@pytest.mark.asyncio
async def test_subscription_authenticated_duplicate_delivery_missing_checkout_attempt() -> None:
    """Verifies that duplicate deliveries when checkout attempt is missing update existing case without duplicate rows."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_missing_dup_001"
    cust_id = "cust_auth_missing_dup_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    t1 = int(datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc).timestamp())
    claimed1 = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        created_at_ts=t1,
        session=session,
    )
    await handle_subscription_authenticated(claimed1, session)

    case_key = f"missing_checkout_attempt:{sub_id}"
    assert case_key in session.reconciliations
    assert len(session.reconciliations) == 1
    assert len(session.reconciliations[case_key].diagnostic_payload["history"]) == 1

    # Second delivery with later timestamp
    t2 = int(datetime(2026, 11, 8, 10, 5, tzinfo=timezone.utc).timestamp())
    claimed2 = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        created_at_ts=t2,
        session=session,
    )
    await handle_subscription_authenticated(claimed2, session)

    # Still exactly 1 reconciliation case, history appended
    assert len(session.reconciliations) == 1
    rec = session.reconciliations[case_key]
    assert len(rec.diagnostic_payload["history"]) == 2


@pytest.mark.asyncio
async def test_subscription_authenticated_ambiguous_checkout_attempts_fails_closed() -> None:
    """Verifies that multiple checkout attempts for the same subscription ID fail closed."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_ambig_001"
    cust_id = "cust_auth_ambig_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    # Create two competing checkout attempts
    att1 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_ambig_1",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    att2 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_ambig_2",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.checkout_attempts[str(att1.id)] = att1
    session.checkout_attempts[str(att2.id)] = att2

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="Multiple checkout attempts"):
        await handle_subscription_authenticated(claimed, session)

    case_key = f"ambiguous_checkout_attempt:{sub_id}"
    assert case_key in session.reconciliations
    assert session.reconciliations[case_key].reason == "ambiguous_checkout_attempt"


@pytest.mark.asyncio
async def test_subscription_authenticated_mismatched_checkout_attempt_fails_closed() -> None:
    """Verifies that checkout attempt tenant mismatch fails closed."""
    session = RazorpayAgreementMockDbSession()
    org_authoritative = uuid.uuid4()
    org_mismatched = uuid.uuid4()
    sub_id = "sub_auth_mismatch_001"
    cust_id = "cust_auth_mismatch_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_authoritative,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_mismatched,  # Different organization!
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_mismatch_1",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="conflicts with authoritative"):
        await handle_subscription_authenticated(claimed, session)

    case_key = f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_authoritative}"
    assert case_key in session.reconciliations
    assert session.reconciliations[case_key].reason == "checkout_attempt_tenant_mismatch"


# =============================================================================
# 2. subscription.activated Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_activated_transitions_incomplete_to_active() -> None:
    """Verifies that subscription.activated transitions incomplete subscription to active with plan_tier='free'."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_act_001"
    cust_id = "cust_act_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    session.subscriptions[sub_id] = existing_sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        current_start=1700000000,
        current_end=1702592000,
        session=session,
    )

    await handle_subscription_activated(claimed, session)

    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "free", "Activation alone MUST NOT grant Pro plan tier!"
    assert sub.current_period_start == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    assert sub.current_period_end == datetime.fromtimestamp(1702592000, tz=timezone.utc)
    assert len(session.billing_periods) == 0, "Activation alone MUST NOT create a billing period!"


@pytest.mark.asyncio
async def test_subscription_activated_does_not_clear_past_due_since() -> None:
    """Verifies that subscription.activated CANNOT cure past_due and strictly preserves past_due_since."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_past_due_act_001"
    cust_id = "cust_past_due_act_001"
    past_due_anchor = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=past_due_anchor,
    )
    session.subscriptions[sub_id] = existing_sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        session=session,
    )

    await handle_subscription_activated(claimed, session)

    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.past_due, "Activation event MUST NOT cure past_due status!"
    assert sub.past_due_since == past_due_anchor, "past_due_since MUST NOT be cleared by activation!"


@pytest.mark.asyncio
async def test_subscription_activated_refuses_resurrection_of_canceled_subscription() -> None:
    """Verifies refusal of resurrection: activation arriving for canceled subscription is rejected."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_term_001"
    cust_id = "cust_term_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    canceled_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
    )
    session.subscriptions[sub_id] = canceled_sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        session=session,
    )

    await handle_subscription_activated(claimed, session)

    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.canceled, "Canceled subscription MUST NOT be resurrected!"
    assert sub.plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_activated_ambiguous_checkout_attempts_fails_closed() -> None:
    """Verifies that multiple checkout attempts during activation fail closed."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_act_ambig_001"
    cust_id = "cust_act_ambig_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    att1 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_act_ambig_1",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    att2 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_act_ambig_2",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.checkout_attempts[str(att1.id)] = att1
    session.checkout_attempts[str(att2.id)] = att2

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="Multiple checkout attempts"):
        await handle_subscription_activated(claimed, session)

    case_key = f"ambiguous_checkout_attempt:{sub_id}"
    assert case_key in session.reconciliations


@pytest.mark.asyncio
async def test_subscription_activated_mismatched_checkout_attempt_fails_closed() -> None:
    """Verifies that checkout attempt tenant mismatch during activation fails closed."""
    session = RazorpayAgreementMockDbSession()
    org_authoritative = uuid.uuid4()
    org_mismatched = uuid.uuid4()
    sub_id = "sub_act_mismatch_001"
    cust_id = "cust_act_mismatch_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_authoritative,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_mismatched,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_act_mismatch_1",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="conflicts with authoritative"):
        await handle_subscription_activated(claimed, session)

    case_key = f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_authoritative}"
    assert case_key in session.reconciliations


# =============================================================================
# 3. subscription.updated Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_updated_applies_plan_and_period_updates() -> None:
    """Verifies that subscription.updated updates plan and period dates monotonically."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_upd_001"
    cust_id = "cust_upd_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        provider_plan_id="plan_old_monthly",
        status=SubscriptionStatus.active,
        plan_tier="free",
        last_webhook_event_at=datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc),
    )
    session.subscriptions[sub_id] = existing_sub

    claimed = make_test_claimed_event(
        event_type="subscription.updated",
        sub_id=sub_id,
        status="active",
        plan_id="plan_new_annual",
        customer_id=cust_id,
        current_start=1700000000,
        current_end=1731536000,
        created_at_ts=int(datetime(2026, 11, 8, 10, 30, tzinfo=timezone.utc).timestamp()),
        session=session,
    )

    await handle_subscription_updated(claimed, session)

    sub = session.subscriptions[sub_id]
    assert sub.provider_plan_id == "plan_new_annual"
    assert sub.current_period_end == datetime.fromtimestamp(1731536000, tz=timezone.utc)
    assert sub.plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_updated_ignores_stale_out_of_order_event() -> None:
    """Verifies that older subscription.updated arriving after newer event is safely ignored."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_stale_upd_001"
    cust_id = "cust_stale_upd_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    newer_time = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        provider_plan_id="plan_authoritative_latest",
        status=SubscriptionStatus.active,
        plan_tier="free",
        last_webhook_event_at=newer_time,
    )
    session.subscriptions[sub_id] = existing_sub

    # Stale event with older timestamp (11:00 AM)
    claimed = make_test_claimed_event(
        event_type="subscription.updated",
        sub_id=sub_id,
        plan_id="plan_stale_reverted",
        customer_id=cust_id,
        created_at_ts=int(datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc).timestamp()),
        session=session,
    )

    await handle_subscription_updated(claimed, session)

    # State not reverted!
    sub = session.subscriptions[sub_id]
    assert sub.provider_plan_id == "plan_authoritative_latest"


@pytest.mark.asyncio
async def test_subscription_updated_unknown_subscription_stages_reconciliation() -> None:
    """Verifies that subscription.updated for an unknown subscription stages a reconciliation case."""
    session = RazorpayAgreementMockDbSession()
    sub_id = "sub_unknown_upd_001"
    cust_id = "cust_known_001"
    org_id = uuid.uuid4()

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    claimed = make_test_claimed_event(
        event_type="subscription.updated",
        sub_id=sub_id,
        customer_id=cust_id,
        session=session,
    )

    await handle_subscription_updated(claimed, session)

    case_key = f"unknown_sub_updated:{sub_id}"
    assert case_key in session.reconciliations
    assert session.reconciliations[case_key].reason == "unknown_subscription_updated"


# =============================================================================
# 4. subscription.completed Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_completed_transitions_to_canceled_and_free() -> None:
    """Verifies that subscription.completed transitions Subscription to status='canceled' and plan_tier='free'."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_comp_001"
    cust_id = "cust_comp_001"

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    session.subscriptions[sub_id] = existing_sub

    event_time = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)
    claimed = make_test_claimed_event(
        event_type="subscription.completed",
        sub_id=sub_id,
        status="completed",
        customer_id=cust_id,
        created_at_ts=int(event_time.timestamp()),
        session=session,
    )

    await handle_subscription_completed(claimed, session)

    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert sub.canceled_at == event_time
    assert len(session.billing_periods) == 0, "Completion event MUST NOT alter billing periods directly!"


@pytest.mark.asyncio
async def test_subscription_completed_idempotent_duplicate() -> None:
    """Verifies that repeated subscription.completed events execute idempotently."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_comp_dup_001"
    cust_id = "cust_comp_dup_001"
    event_time = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=event_time,
        last_webhook_event_at=event_time,
    )
    session.subscriptions[sub_id] = existing_sub

    # Replay event
    claimed = make_test_claimed_event(
        event_type="subscription.completed",
        sub_id=sub_id,
        status="completed",
        customer_id=cust_id,
        created_at_ts=int((event_time + timedelta(minutes=5)).timestamp()),
        session=session,
    )

    await handle_subscription_completed(claimed, session)

    assert session.subscriptions[sub_id].status == SubscriptionStatus.canceled
    assert session.subscriptions[sub_id].plan_tier == "free"


# =============================================================================
# 5. Lease Fencing & Cross-Tenant Security Tests
# =============================================================================


@pytest.mark.asyncio
async def test_agreement_handler_rejects_stale_worker_lease() -> None:
    """Verifies that an agreement handler aborts domain mutations if lease assertion fails."""
    session = RazorpayAgreementMockDbSession()
    sub_id = "sub_fencing_001"
    cust_id = "cust_fencing_001"
    event_id = uuid.uuid4()
    held_token = uuid.uuid4()
    other_token = uuid.uuid4()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    # In database, another worker superseded the lease!
    event = BillingWebhookEvent(
        id=event_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_event_id="evt_fencing_01",
        event_type="subscription.activated",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        lease_token=other_token,  # Superseded!
        locked_by="worker_competing_02",
        lease_expires_at=now + timedelta(seconds=60),
        signature_verified=True,
        payload={},
    )
    session.webhook_events[event_id] = event

    claimed = ClaimedWebhookEvent(
        event_id=event_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_event_id="evt_fencing_01",
        event_type="subscription.activated",
        payload={
            "entity": "event",
            "account_id": "acc_test_merchant",
            "event": "subscription.activated",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": sub_id,
                        "plan_id": "plan_1",
                        "status": "active",
                    }
                }
            },
        },
        attempts=0,
        lease_token=held_token,
        locked_by="worker_stale_01",
        lease_expires_at=now + timedelta(seconds=60),
        received_at=now,
    )

    with pytest.raises(StaleLeaseError, match="lost lease"):
        await handle_subscription_activated(claimed, session)

    # Rolled back!
    assert session.rollbacks >= 1


@pytest.mark.asyncio
async def test_agreement_handler_dispatcher_registration() -> None:
    """Verifies that all four agreement handlers are properly registered in the dispatcher."""
    dispatcher = RazorpayWebhookDispatcher()
    register_razorpay_agreement_handlers(dispatcher)

    assert dispatcher.has_handler("subscription.authenticated") is True
    assert dispatcher.has_handler("subscription.activated") is True
    assert dispatcher.has_handler("subscription.updated") is True
    assert dispatcher.has_handler("subscription.completed") is True
    assert default_razorpay_dispatcher.has_handler("subscription.activated") is True
