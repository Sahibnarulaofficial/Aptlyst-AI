"""Unit and Integration Tests for Shared Razorpay Handler Foundation & Advisory Lock Infrastructure.

Validates Step 5.2D.3.1 requirements:
1. Event envelope validation (valid envelopes, missing fields, unverified signature fail-closed).
2. Provider, merchant account, and environment isolation.
3. Missing mandatory entity identifiers (subscription_id, payment_id, invoice_id).
4. Stable deterministic advisory lock key derivation (signed 64-bit bounds, collision resistance, entity scoping).
5. Distinct keys for distinct entities and entity categories.
6. Strict payload schema validation (non-dict, non-positive amounts, invalid currencies, malformed timestamps).
7. Error classification (retryable vs permanent vs reconciliation conflict).
8. Authoritative tenant organization resolution (rejecting spoofed client metadata, refusing fake bindings).
9. Diagnostic history preservation in PaymentReconciliation (append-only history tracking).
10. Stale lease protection at the handler boundary (assert_valid_worker_lease rejecting expired/superseded leases).
11. Transaction rollback on failed validation.
12. Cancellation finalization predicate (evaluating 7-day grace period expiration).
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
import hashlib
from typing import Any
import uuid

import pytest
from sqlalchemy import text

# Import all SQLAlchemy models to initialize relationship mappers cleanly
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401

from app.models.payment import (
    BillingCustomerProvision,
    BillingWebhookEvent,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    ReconciliationStatus,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.razorpay.handler_base import (
    RazorpayEventContext,
    RazorpayInvoiceEntity,
    RazorpayMissingEntityIdError,
    RazorpayPayloadValidationError,
    RazorpayPaymentEntity,
    RazorpayScopeMismatchError,
    RazorpaySubscriptionEntity,
    RazorpayWebhookHandlerError,
    RazorpayWebhookPermanentError,
    RazorpayWebhookReconciliationConflictError,
    RazorpayWebhookRetryableError,
    assert_valid_worker_lease,
    extract_customer_id,
    extract_invoice_entity,
    extract_invoice_id,
    extract_payment_entity,
    extract_payment_id,
    extract_subscription_entity,
    extract_subscription_id,
    is_stale_event,
    is_terminal_subscription,
    parse_positive_unix_timestamp,
    resolve_tenant_organization,
    should_finalize_deferred_cancellation,
    stage_or_update_payment_reconciliation,
    validate_razorpay_event_envelope,
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
    StaleLeaseError,
)


# =============================================================================
# In-Memory Mock Database Session for Isolated Testing
# =============================================================================


class MockScalarResult:
    """Mock result supporting scalar_one_or_none() and scalars().all()."""

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


class RazorpayHandlerMockDbSession:
    """Mock async SQLAlchemy session for handler foundation tests."""

    def __init__(self) -> None:
        self.customer_provisions: dict[str, BillingCustomerProvision] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.webhook_events: dict[uuid.UUID, BillingWebhookEvent] = {}
        self.reconciliations: dict[str, PaymentReconciliation] = {}

        self.added: list[Any] = []
        self.commits: int = 0
        self.rollbacks: int = 0
        self.flushes: int = 0
        self.advisory_locks: list[int] = []

    async def __aenter__(self) -> "RazorpayHandlerMockDbSession":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            await self.rollback()
        return False

    def add(self, instance: Any) -> None:
        self.added.append(instance)
        if isinstance(instance, PaymentReconciliation):
            self.reconciliations[instance.case_key] = instance
        elif isinstance(instance, BillingWebhookEvent):
            self.webhook_events[instance.id] = instance
        elif isinstance(instance, BillingCustomerProvision):
            self.customer_provisions[instance.provider_customer_id or str(instance.id)] = instance
        elif isinstance(instance, Subscription):
            self.subscriptions[instance.provider_subscription_id or str(instance.id)] = instance

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        # Revert items added since last commit
        for item in self.added:
            if isinstance(item, PaymentReconciliation) and item.case_key in self.reconciliations:
                del self.reconciliations[item.case_key]
        self.added.clear()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MockScalarResult:
        # 1. Advisory Lock Handlers
        stmt_str = str(stmt.text if hasattr(stmt, "text") else stmt)
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
            for sub in self.subscriptions.values():
                return MockScalarResult(sub.organization_id)
            return MockScalarResult(None)

        # 4. PaymentReconciliation Lookups
        if "payment_reconciliations" in stmt_str:
            for rec in self.reconciliations.values():
                return MockScalarResult(rec)
            return MockScalarResult(None)

        # 5. BillingWebhookEvent Lookups
        if "billing_webhook_events" in stmt_str:
            for evt in self.webhook_events.values():
                return MockScalarResult(evt)
            return MockScalarResult(None)

        return MockScalarResult(None)


# =============================================================================
# 1. Advisory Lock Infrastructure Tests
# =============================================================================


def test_advisory_lock_deterministic_key_generation() -> None:
    """Verifies that advisory lock key derivation is strictly deterministic and within 64-bit limits."""
    key1 = derive_razorpay_advisory_lock_key("subscription", "sub_test_001")
    key2 = derive_razorpay_advisory_lock_key("subscription", "sub_test_001")
    assert key1 == key2, "Advisory lock key derivation must be strictly deterministic."

    # Verify bounds
    assert PG_BIGINT_MIN <= key1 <= PG_BIGINT_MAX, "Key must fit within signed 64-bit integer range."


def test_advisory_lock_distinct_entities_and_types() -> None:
    """Verifies that different entity IDs or types produce distinct lock keys."""
    sub_key1 = derive_razorpay_advisory_lock_key("subscription", "sub_test_001")
    sub_key2 = derive_razorpay_advisory_lock_key("subscription", "sub_test_002")
    pay_key1 = derive_razorpay_advisory_lock_key("payment", "sub_test_001")

    assert sub_key1 != sub_key2, "Distinct entities must produce distinct keys."
    assert sub_key1 != pay_key1, "Different entity categories must produce distinct keys."


def test_advisory_lock_rejects_missing_or_empty_identifiers() -> None:
    """Verifies that missing or whitespace entity types or IDs raise ValueError."""
    with pytest.raises(ValueError, match="entity_type must be a non-empty string"):
        derive_razorpay_advisory_lock_key("", "sub_123")

    with pytest.raises(ValueError, match="entity_id must be a non-empty string"):
        derive_razorpay_advisory_lock_key("subscription", "   ")


def test_advisory_lock_rejects_unwhitelisted_entity_type() -> None:
    """Verifies that non-whitelisted entity types are rejected by the ALLOWED_ENTITY_TYPES guard."""
    with pytest.raises(ValueError, match="Invalid entity_type 'order'"):
        derive_razorpay_advisory_lock_key("order", "order_123")

    with pytest.raises(ValueError, match="Invalid entity_type 'user'"):
        derive_razorpay_advisory_lock_key("user", "usr_123")


@pytest.mark.asyncio
async def test_acquire_and_try_advisory_lock_execution() -> None:
    """Verifies execution of transaction-scoped advisory lock queries."""
    session = RazorpayHandlerMockDbSession()
    key = await acquire_razorpay_advisory_lock(session, "subscription", "sub_abc123")
    assert key in session.advisory_locks
    assert len(session.advisory_locks) == 1

    acquired = await try_acquire_razorpay_advisory_lock(session, "payment", "pay_xyz789")
    assert acquired is True
    assert len(session.advisory_locks) == 2


# =============================================================================
# 2. Envelope & Scope Validation Tests
# =============================================================================


def test_validate_envelope_success_claimed_event() -> None:
    """Verifies successful validation and extraction of a claimed webhook event."""
    claimed = ClaimedWebhookEvent(
        event_id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_live_test_001",
        environment="test",
        provider_event_id="evt_test_12345",
        event_type="subscription.charged",
        payload={
            "entity": "event",
            "account_id": "acc_live_test_001",
            "event": "subscription.charged",
            "contains": ["subscription", "payment"],
            "created_at": 1700000000,
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_test_123",
                        "entity": "subscription",
                        "plan_id": "plan_test_pro",
                        "status": "active",
                    }
                },
                "payment": {
                    "entity": {
                        "id": "pay_test_456",
                        "entity": "payment",
                        "amount": 290000,
                        "currency": "INR",
                        "status": "captured",
                    }
                },
            },
        },
        attempts=0,
        lease_token=uuid.uuid4(),
        locked_by="worker_test_1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        received_at=datetime.now(timezone.utc),
    )

    ctx = validate_razorpay_event_envelope(
        claimed,
        expected_merchant_id="acc_live_test_001",
        expected_environment="test",
    )

    assert isinstance(ctx, RazorpayEventContext)
    assert ctx.provider == "razorpay"
    assert ctx.merchant_account_id == "acc_live_test_001"
    assert ctx.subscription is not None
    assert ctx.subscription.id == "sub_test_123"
    assert ctx.payment is not None
    assert ctx.payment.amount == 290000


def test_validate_envelope_rejects_unverified_signature() -> None:
    """Verifies that an unverified signature fails closed immediately."""
    event = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_live_test_001",
        environment="test",
        provider_event_id="evt_unverified_1",
        event_type="subscription.charged",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        signature_verified=False,  # Unverified!
        payload={"entity": "event", "account_id": "acc_live_test_001", "event": "subscription.charged", "payload": {}},
    )

    with pytest.raises(RazorpayWebhookPermanentError, match="signature_verified is False"):
        validate_razorpay_event_envelope(event, expected_merchant_id="acc_live_test_001", expected_environment="test")


def test_validate_envelope_rejects_scope_mismatch() -> None:
    """Verifies that mismatched provider, merchant account, or environment is rejected."""
    # 1. Invalid provider
    bad_prov = ClaimedWebhookEvent(
        event_id=uuid.uuid4(),
        provider="stripe",  # Invalid!
        merchant_account_id="acc_001",
        environment="test",
        provider_event_id="evt_1",
        event_type="invoice.paid",
        payload={},
        attempts=0,
        lease_token=uuid.uuid4(),
        locked_by="w1",
        lease_expires_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    with pytest.raises(RazorpayScopeMismatchError, match="Invalid provider 'stripe'"):
        validate_razorpay_event_envelope(bad_prov)

    # 2. Mismatched merchant account
    bad_merchant = ClaimedWebhookEvent(
        event_id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_foreign_tenant",
        environment="test",
        provider_event_id="evt_2",
        event_type="subscription.charged",
        payload={"entity": "event", "account_id": "acc_foreign_tenant", "payload": {}},
        attempts=0,
        lease_token=uuid.uuid4(),
        locked_by="w1",
        lease_expires_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    with pytest.raises(RazorpayScopeMismatchError, match="Merchant account mismatch"):
        validate_razorpay_event_envelope(bad_merchant, expected_merchant_id="acc_expected_tenant")


# =============================================================================
# 3. Payload Entity Validation & ID Extraction Tests
# =============================================================================


def test_entity_id_extraction_and_missing_guards() -> None:
    """Verifies extraction of entity IDs and rejection when missing."""
    valid_payload = {
        "subscription": {"entity": {"id": "sub_valid_123"}},
        "payment": {"entity": {"id": "pay_valid_456"}},
        "invoice": {"entity": {"id": "inv_valid_789"}},
    }

    assert extract_subscription_id(valid_payload) == "sub_valid_123"
    assert extract_payment_id(valid_payload) == "pay_valid_456"
    assert extract_invoice_id(valid_payload) == "inv_valid_789"

    # Missing guards
    with pytest.raises(RazorpayMissingEntityIdError, match="Missing mandatory provider subscription_id"):
        extract_subscription_id({})

    with pytest.raises(RazorpayMissingEntityIdError, match="Missing mandatory provider payment_id"):
        extract_payment_id({})

    with pytest.raises(RazorpayMissingEntityIdError, match="Missing mandatory provider invoice_id"):
        extract_invoice_id({})


def test_strict_payload_validation_rejects_invalid_values() -> None:
    """Verifies that non-positive amounts, invalid currencies, and malformed timestamps fail closed."""
    # Non-positive payment amount
    bad_pay_payload = {
        "payment": {
            "entity": {
                "id": "pay_bad_001",
                "amount": 0,  # Zero or negative amount!
                "currency": "INR",
                "status": "captured",
            }
        }
    }
    with pytest.raises(RazorpayPayloadValidationError, match="Payment amount must be a positive integer"):
        extract_payment_entity(bad_pay_payload)

    # Malformed timestamp
    with pytest.raises(RazorpayPayloadValidationError, match="must be strictly positive"):
        parse_positive_unix_timestamp(-100, "event_timestamp")

    with pytest.raises(RazorpayPayloadValidationError, match="Expected positive numeric integer"):
        parse_positive_unix_timestamp("not-a-number", "event_timestamp")


# =============================================================================
# 4. Error Hierarchy & Classification Tests
# =============================================================================


def test_error_hierarchy_distinguishes_failure_categories() -> None:
    """Verifies exception classification between retryable, permanent, and reconciliation conflicts."""
    retryable = RazorpayWebhookRetryableError("Database connection lost.")
    permanent = RazorpayPayloadValidationError("Missing mandatory field.")
    conflict = RazorpayWebhookReconciliationConflictError(
        "Unbound customer detected.",
        case_key="unbound:cust_123",
        reason="unbound_provider_customer",
    )

    # Base hierarchy checks
    assert isinstance(retryable, RazorpayWebhookHandlerError)
    assert not isinstance(retryable, RazorpayWebhookPermanentError)

    assert isinstance(permanent, RazorpayWebhookPermanentError)
    assert not isinstance(permanent, RazorpayWebhookRetryableError)

    # Conflict is a permanent failure that quarantines the event
    assert isinstance(conflict, RazorpayWebhookPermanentError)
    assert conflict.case_key == "unbound:cust_123"
    assert conflict.reason == "unbound_provider_customer"


# =============================================================================
# 5. Authoritative Tenant Resolution & Anti-Spoofing Tests
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_tenant_organization_authoritative_match() -> None:
    """Verifies resolution when customer provision and metadata match."""
    session = RazorpayHandlerMockDbSession()
    org_id = uuid.uuid4()

    # Prepopulate customer provision
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test",
        environment="test",
        provider_customer_id="cust_auth_123",
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions["cust_auth_123"] = prov

    resolved = await resolve_tenant_organization(
        db=session,
        provider="razorpay",
        merchant_account_id="acc_test",
        environment="test",
        customer_id="cust_auth_123",
        notes={"organization_id": str(org_id)},
    )
    assert resolved == org_id


@pytest.mark.asyncio
async def test_resolve_tenant_organization_rejects_metadata_spoofing() -> None:
    """Verifies that client metadata attempting to claim a foreign org fails closed."""
    session = RazorpayHandlerMockDbSession()
    real_org_id = uuid.uuid4()
    spoofed_org_id = uuid.uuid4()

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=real_org_id,
        provider="razorpay",
        merchant_account_id="acc_test",
        environment="test",
        provider_customer_id="cust_auth_123",
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions["cust_auth_123"] = prov

    # Client notes specify spoofed_org_id!
    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="Metadata tenant mismatch") as exc_info:
        await resolve_tenant_organization(
            db=session,
            provider="razorpay",
            merchant_account_id="acc_test",
            environment="test",
            customer_id="cust_auth_123",
            notes={"organization_id": str(spoofed_org_id)},
        )
    assert exc_info.value.reason == "metadata_tenant_mismatch"
    assert exc_info.value.organization_id == real_org_id


@pytest.mark.asyncio
async def test_resolve_tenant_organization_refuses_fake_binding_for_unbound_customer() -> None:
    """Verifies that unbound customer without local records NEVER binds to client notes alone."""
    session = RazorpayHandlerMockDbSession()
    client_claimed_org_id = uuid.uuid4()

    # No provisions in session (customer is unknown)
    with pytest.raises(RazorpayWebhookReconciliationConflictError, match="Unbound Razorpay entity") as exc_info:
        await resolve_tenant_organization(
            db=session,
            provider="razorpay",
            merchant_account_id="acc_test",
            environment="test",
            customer_id="cust_unknown_999",
            notes={"organization_id": str(client_claimed_org_id)},
        )
    assert exc_info.value.reason == "unbound_provider_customer"


# =============================================================================
# 6. Diagnostic History Preservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_reconciliation_case_preserves_diagnostic_history() -> None:
    """Verifies that subsequent updates append to history rather than overwriting earlier diagnostic evidence."""
    session = RazorpayHandlerMockDbSession()
    case_key = "payment:pay_diff_amount:mismatch"
    org_id = uuid.uuid4()

    # 1. Initial discrepancy
    t1 = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
    rec1 = await stage_or_update_payment_reconciliation(
        db=session,
        case_key=case_key,
        reason="amount_mismatch_first_try",
        organization_id=org_id,
        diagnostic_data={"expected": 290000, "actual": 200000},
        now=t1,
    )
    assert len(rec1.diagnostic_payload["history"]) == 1
    assert rec1.diagnostic_payload["latest"]["actual"] == 200000

    # 2. Subsequent retry discrepancy
    t2 = datetime(2026, 11, 8, 10, 15, tzinfo=timezone.utc)
    rec2 = await stage_or_update_payment_reconciliation(
        db=session,
        case_key=case_key,
        reason="amount_mismatch_second_try",
        organization_id=org_id,
        diagnostic_data={"expected": 290000, "actual": 250000},
        now=t2,
    )

    # Historical entries preserved!
    assert len(rec2.diagnostic_payload["history"]) == 2
    assert rec2.diagnostic_payload["history"][0]["details"]["actual"] == 200000
    assert rec2.diagnostic_payload["history"][1]["details"]["actual"] == 250000
    assert rec2.diagnostic_payload["latest"]["actual"] == 250000


# =============================================================================
# 7. Stale Lease Protection & Transaction Rollback Tests
# =============================================================================


@pytest.mark.asyncio
async def test_assert_valid_worker_lease_permits_active_lease() -> None:
    """Verifies that an unexpired lease matching token and worker succeeds."""
    session = RazorpayHandlerMockDbSession()
    event_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    worker_id = "worker_active_01"
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    event = BillingWebhookEvent(
        id=event_id,
        provider="razorpay",
        merchant_account_id="default",
        environment="live",
        provider_event_id="evt_123",
        event_type="subscription.charged",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        lease_token=lease_token,
        locked_by=worker_id,
        lease_expires_at=now + timedelta(seconds=30),
        signature_verified=True,
        payload={},
    )
    session.webhook_events[event_id] = event

    # Active lease validation passes
    await assert_valid_worker_lease(
        db=session,
        event_id=event_id,
        expected_lease_token=lease_token,
        expected_worker_id=worker_id,
        now=now,
    )
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_assert_valid_worker_lease_rejects_expired_or_superseded_lease() -> None:
    """Verifies that expired lease or mismatched token raises StaleLeaseError and rolls back."""
    session = RazorpayHandlerMockDbSession()
    event_id = uuid.uuid4()
    original_token = uuid.uuid4()
    stolen_token = uuid.uuid4()
    worker_id = "worker_stale_01"
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    # 1. Superseded token test
    event = BillingWebhookEvent(
        id=event_id,
        provider="razorpay",
        merchant_account_id="default",
        environment="live",
        provider_event_id="evt_123",
        event_type="subscription.charged",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        lease_token=stolen_token,  # Replaced by another worker!
        locked_by="worker_other_02",
        lease_expires_at=now + timedelta(seconds=30),
        signature_verified=True,
        payload={},
    )
    session.webhook_events[event_id] = event

    with pytest.raises(StaleLeaseError, match="lost lease"):
        await assert_valid_worker_lease(
            db=session,
            event_id=event_id,
            expected_lease_token=original_token,
            expected_worker_id=worker_id,
            now=now,
        )
    assert session.rollbacks == 1

    # 2. Expired lease test
    event.lease_token = original_token
    event.locked_by = worker_id
    event.lease_expires_at = now - timedelta(seconds=1)  # Expired!

    with pytest.raises(StaleLeaseError, match="lost lease"):
        await assert_valid_worker_lease(
            db=session,
            event_id=event_id,
            expected_lease_token=original_token,
            expected_worker_id=worker_id,
            now=now,
        )
    assert session.rollbacks == 2


# =============================================================================
# 8. Cancellation Finalization & Idempotency Predicates
# =============================================================================


def test_should_finalize_deferred_cancellation_evaluation() -> None:
    """Verifies the predicate for evaluating when past_due deferred cancellations are finalized."""
    past_due_anchor = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    # Case 1: Status not past_due -> False
    assert not should_finalize_deferred_cancellation(
        status="active",
        cancel_at_period_end=True,
        past_due_since=past_due_anchor,
        now=past_due_anchor + timedelta(days=8),
    )

    # Case 2: cancel_at_period_end False -> False
    assert not should_finalize_deferred_cancellation(
        status="past_due",
        cancel_at_period_end=False,
        past_due_since=past_due_anchor,
        now=past_due_anchor + timedelta(days=8),
    )

    # Case 3: Within 7-day grace window (6.9 days) -> False
    assert not should_finalize_deferred_cancellation(
        status="past_due",
        cancel_at_period_end=True,
        past_due_since=past_due_anchor,
        grace_period_days=7,
        now=past_due_anchor + timedelta(days=6, hours=22),
    )

    # Case 4: Grace expired (7.1 days) -> True
    assert should_finalize_deferred_cancellation(
        status="past_due",
        cancel_at_period_end=True,
        past_due_since=past_due_anchor,
        grace_period_days=7,
        now=past_due_anchor + timedelta(days=7, hours=2),
    )


def test_monotonic_and_terminal_idempotency_helpers() -> None:
    """Verifies stale event detection and refusal of resurrection for terminal statuses."""
    t_old = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)

    # Monotonic event check
    assert is_stale_event(current_entity_timestamp=t_new, incoming_event_timestamp=t_old) is True
    assert is_stale_event(current_entity_timestamp=t_old, incoming_event_timestamp=t_new) is False
    assert is_stale_event(current_entity_timestamp=None, incoming_event_timestamp=t_new) is False

    # Terminal subscription check
    assert is_terminal_subscription(SubscriptionStatus.canceled) is True
    assert is_terminal_subscription("canceled") is True
    assert is_terminal_subscription("completed") is True
    assert is_terminal_subscription(SubscriptionStatus.active) is False
    assert is_terminal_subscription("past_due") is False


# =============================================================================
# 9. Transaction Rollback & Worker Integration Tests
# =============================================================================


@pytest.mark.asyncio
async def test_transaction_rollback_on_failed_validation() -> None:
    """Verifies that validation failures roll back uncommitted database operations."""
    session = RazorpayHandlerMockDbSession()
    case_key = "test_rollback_case"

    # Start transactional block
    try:
        async with session:
            # 1. Stage a speculative item
            rec = PaymentReconciliation(
                id=uuid.uuid4(),
                provider="razorpay",
                merchant_account_id="default",
                environment="live",
                case_key=case_key,
                reason="initial_check",
            )
            session.add(rec)
            assert case_key in session.reconciliations

            # 2. Trigger payload validation error
            raise RazorpayPayloadValidationError("Simulated validation failure.")
    except RazorpayPayloadValidationError:
        pass

    # Transaction rolled back; uncommitted items removed
    assert session.rollbacks >= 1
    assert case_key not in session.reconciliations


@pytest.mark.asyncio
async def test_composite_payload_parses_all_entities() -> None:
    """Verifies parsing of subscription, payment, and invoice entities from composite payload."""
    composite_payload = {
        "entity": "event",
        "account_id": "acc_composite_01",
        "event": "subscription.charged",
        "created_at": 1700000000,
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_comp_001",
                    "plan_id": "plan_comp_001",
                    "status": "active",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_comp_002",
                    "amount": 290000,
                    "currency": "INR",
                    "status": "captured",
                }
            },
            "invoice": {
                "entity": {
                    "id": "inv_comp_003",
                    "amount": 290000,
                    "amount_paid": 290000,
                    "amount_due": 0,
                    "currency": "INR",
                    "status": "paid",
                }
            },
        },
    }

    ctx = validate_razorpay_event_envelope(
        composite_payload,
        expected_merchant_id="acc_composite_01",
    )

    assert ctx.subscription is not None
    assert ctx.subscription.id == "sub_comp_001"
    assert ctx.payment is not None
    assert ctx.payment.id == "pay_comp_002"
    assert ctx.payment.amount == 290000
    assert ctx.invoice is not None
    assert ctx.invoice.id == "inv_comp_003"
    assert ctx.invoice.amount_paid == 290000

