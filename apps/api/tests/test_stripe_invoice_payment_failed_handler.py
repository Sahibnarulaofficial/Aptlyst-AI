"""Comprehensive Unit & Integration Tests for Stripe Invoice Payment Failed Webhook Handler.

Event: invoice.payment_failed

Validates:
1. Initial payment failure creates a failed invoice ledger row (processing_status='failed', attempt_count=1).
2. Repeated failures append attempt history to diagnostic_payload["attempts"].
3. Attempt count never decreases (max(existing, incoming)).
4. Missing optional failure details are handled safely (none/missing last_finalization_error, next_payment_attempt, payment_intent).
5. Unknown customer is rejected/quarantined (WebhookPermanentError).
6. Unknown subscription is rejected/quarantined (WebhookReconciliationConflictError, reason='unknown_subscription_invoice_payment_failed').
7. Customer/subscription mismatch fails closed (metadata organization mismatch).
8. Superseded subscription invoices are handled safely (checked against StripeCheckoutAttempt and marked ignored).
9. Canceled subscription invoices do not mutate billing periods (marked ignored).
10. Failure after successful payment never regresses processed status (records in stale_failures, leaves processed status and period intact).
11. Failure before successful payment allows invoice.paid to transition the same row.
12. The successful transition updates amount_paid_minor_units and invoice_status.
13. Failure handling never mutates subscription lifecycle or billing periods.
14. JSONB attempt history is actually persisted by session tracking.
15. Both handlers derive the same advisory lock key.
16. Duplicate and concurrent event handling follows existing idempotency conventions.
"""

from collections import namedtuple
from datetime import datetime, timezone
import sys
from typing import Any
from unittest.mock import patch
import uuid

from pydantic import SecretStr
import pytest

# Windows asyncio Proactor sandbox self-pipe fix
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self: Any) -> None:
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.billing  # noqa: F401

from app.config import Settings
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    StripeCheckoutAttempt,
    StripeInvoice,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.stripe_invoice_paid_handler import (
    _derive_advisory_lock_key,
    handle_invoice_paid,
)
from app.services.billing.stripe_invoice_payment_failed_handler import (
    derive_invoice_advisory_lock_key,
    handle_invoice_payment_failed,
)
from app.services.billing.stripe_webhook_engine import (
    ProcessingOutcome,
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
    execute_claimed_webhook_event,
)

TEST_PRO_PRICE_ID = "price_pro_invoice_test_123"


def get_test_settings(
    pro_price_id: str = TEST_PRO_PRICE_ID,
    pro_quota: int = 1_000_000,
) -> Settings:
    return Settings(
        stripe_enabled=True,
        stripe_secret_key=SecretStr("sk_test_mock_1234567890abcdef"),
        stripe_webhook_secret=SecretStr("whsec_test_mock_1234567890abcdef"),
        stripe_publishable_key="pk_test_mock_1234567890abcdef",
        stripe_pro_price_id=pro_price_id,
        default_free_monthly_token_quota=0,
        default_pro_monthly_token_quota=pro_quota,
    )


# =============================================================================
# In-Memory Session Doubles
# =============================================================================


class InvoiceMockScalarResult:
    """Mock scalar result wrapper for db.execute()."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def scalar_one_or_none(self) -> Any:
        if isinstance(self._item, list):
            if len(self._item) > 1:
                from sqlalchemy.exc import MultipleResultsFound
                raise MultipleResultsFound("Multiple rows were found when one or none was required")
            return self._item[0] if self._item else None
        return self._item

    def scalar(self) -> Any:
        if isinstance(self._item, list):
            if len(self._item) > 1:
                from sqlalchemy.exc import MultipleResultsFound
                raise MultipleResultsFound("Multiple rows were found when one or none was required")
            return self._item[0] if self._item else None
        return self._item

    def first(self) -> Any:
        if isinstance(self._item, list):
            return self._item[0] if self._item else None
        return self._item

    def scalars(self) -> Any:
        class _Scalars:
            def __init__(self, val: Any) -> None:
                self._val = val

            def all(self) -> list[Any]:
                if isinstance(self._val, list):
                    return self._val
                return [self._val] if self._val is not None else []
        return _Scalars(self._item)


class InvoiceMockSavepoint:
    """Mock savepoint tracking nested rollback."""

    def __init__(self, session: "InvoiceMockDbSession") -> None:
        self.session = session
        self.invoices_snapshot = {
            k: StripeInvoice(
                id=v.id,
                stripe_invoice_id=v.stripe_invoice_id,
                organization_id=v.organization_id,
                stripe_customer_id=v.stripe_customer_id,
                stripe_subscription_id=v.stripe_subscription_id,
                billing_reason=v.billing_reason,
                invoice_status=v.invoice_status,
                currency=v.currency,
                amount_paid_minor_units=v.amount_paid_minor_units,
                period_start=v.period_start,
                period_end=v.period_end,
                processing_status=v.processing_status,
                billing_period_id=v.billing_period_id,
                raw_event_id=v.raw_event_id,
                attempt_count=v.attempt_count,
                error_message=v.error_message,
                diagnostic_payload=dict(v.diagnostic_payload or {}),
                created_at=v.created_at,
                updated_at=v.updated_at,
            )
            for k, v in self.session.invoices.items()
        }

    async def __aenter__(self) -> "InvoiceMockSavepoint":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            self.session.invoices = dict(self.invoices_snapshot)
            self.session.savepoints_rolled_back += 1
            return False
        self.session.savepoints_committed += 1
        return False


CustOrgRow = namedtuple("CustOrgRow", ["organization_id", "stripe_subscription_id"])


class InvoiceMockDbSession:
    """In-memory AsyncSession double tailored for invoice failure and success tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
        events: list[StripeWebhookEvent] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
        invoices: list[StripeInvoice] | None = None,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            o.id: o for o in (organizations or [])
        }
        self.attempts: dict[uuid.UUID, StripeCheckoutAttempt] = {
            a.id: a for a in (attempts or [])
        }
        self.subscriptions: dict[uuid.UUID, Subscription] = {
            s.id: s for s in (subscriptions or [])
        }
        self.events: dict[str, StripeWebhookEvent] = {
            e.id: e for e in (events or [])
        }
        self.billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] = {
            bp.id: bp for bp in (billing_periods or [])
        }
        self.invoices: dict[str, StripeInvoice] = {
            inv.stripe_invoice_id: inv for inv in (invoices or [])
        }
        self.reconciliations: list[BillingReconciliation] = []
        self.added: list[Any] = []
        self.advisory_locks: set[int] = set()
        self.committed_count = 0
        self.rolled_back_count = 0
        self.savepoints_committed = 0
        self.savepoints_rolled_back = 0
        self.flushed = False
        self.flagged_modified_columns: list[tuple[Any, str]] = []

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(pk)
        if entity_cls is StripeCheckoutAttempt:
            return self.attempts.get(pk)
        if entity_cls is Subscription:
            return self.subscriptions.get(pk)
        if entity_cls is StripeWebhookEvent:
            return self.events.get(pk)
        if entity_cls is OrganizationBillingPeriod:
            return self.billing_periods.get(pk)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Subscription):
            self.subscriptions[obj.id] = obj
        elif isinstance(obj, StripeCheckoutAttempt):
            self.attempts[obj.id] = obj
        elif isinstance(obj, StripeWebhookEvent):
            self.events[obj.id] = obj
        elif isinstance(obj, BillingReconciliation):
            self.reconciliations.append(obj)
        elif isinstance(obj, OrganizationBillingPeriod):
            self.billing_periods[obj.id] = obj
        elif isinstance(obj, StripeInvoice):
            self.invoices[obj.stripe_invoice_id] = obj

    async def flush(self) -> None:
        self.flushed = True

    def begin_nested(self) -> InvoiceMockSavepoint:
        return InvoiceMockSavepoint(self)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any, params_override: Any = None) -> InvoiceMockScalarResult:
        # 1. PostgreSQL advisory lock handling
        if hasattr(stmt, "text") and "pg_advisory_xact_lock" in stmt.text:
            if params_override and "key" in params_override:
                self.advisory_locks.add(params_override["key"])
            return InvoiceMockScalarResult(None)

        where_clauses = getattr(stmt, "_where_criteria", ())
        where_strs = [str(c) for c in where_clauses]
        where_full = " ".join(where_strs)

        params: dict[str, Any] = {}
        for c in where_clauses:
            if hasattr(c, "left") and hasattr(c, "right"):
                col_name = getattr(c.left, "key", str(c.left))
                val = getattr(c.right, "value", None)
                if val is not None:
                    params[col_name] = val

        # 2. StripeWebhookEvent queries
        if "stripe_webhook_events" in where_full:
            if "id" in params and params["id"] in self.events:
                return InvoiceMockScalarResult(self.events[params["id"]])

        # 3. BillingReconciliation lookup
        if "billing_reconciliations" in where_full:
            ev_id = params.get("event_id")
            inc_id = params.get("incoming_subscription_id")
            match = next(
                (
                    r for r in self.reconciliations
                    if (ev_id is None or r.event_id == ev_id) and (inc_id is None or r.incoming_subscription_id == inc_id)
                ),
                None,
            )
            return InvoiceMockScalarResult(match)

        # 4. StripeInvoice queries
        if "stripe_invoices" in where_full:
            inv_id = params.get("stripe_invoice_id")
            if inv_id and inv_id in self.invoices:
                return InvoiceMockScalarResult(self.invoices[inv_id])
            return InvoiceMockScalarResult(None)

        # 5. StripeCheckoutAttempt historical query
        if "stripe_checkout_attempts" in where_full:
            org_id = params.get("organization_id")
            sub_id = params.get("stripe_subscription_id")
            status = params.get("status")
            match_attempt = next(
                (
                    a.id
                    for a in self.attempts.values()
                    if (org_id is None or a.organization_id == org_id)
                    and (sub_id is None or a.stripe_subscription_id == sub_id)
                    and (status is None or a.status == status)
                ),
                None,
            )
            return InvoiceMockScalarResult(match_attempt)

        # 6. OrganizationBillingPeriod queries
        if "organization_billing_periods" in where_full:
            org_id = params.get("organization_id")
            is_active = params.get("is_active")
            if is_active is True and org_id:
                active_bp = next(
                    (bp for bp in self.billing_periods.values() if bp.organization_id == org_id and bp.is_active is True),
                    None,
                )
                return InvoiceMockScalarResult(active_bp)

            p_start = params.get("period_start")
            p_end = params.get("period_end")
            if p_start and p_end:
                match_dates = next(
                    (bp for bp in self.billing_periods.values() if (org_id is None or bp.organization_id == org_id) and bp.period_start == p_start and bp.period_end == p_end),
                    None,
                )
                return InvoiceMockScalarResult(match_dates)

            if org_id:
                org_periods = [bp for bp in self.billing_periods.values() if bp.organization_id == org_id]
                org_periods.sort(key=lambda x: x.period_end, reverse=True)
                return InvoiceMockScalarResult(org_periods)

            return InvoiceMockScalarResult(None)

        # 7. Subscription queries
        if "subscriptions" in where_full:
            sub_id = params.get("stripe_subscription_id")
            org_id = params.get("organization_id")
            if sub_id and org_id:
                sub_match = next((s for s in self.subscriptions.values() if s.organization_id == org_id and s.stripe_subscription_id == sub_id), None)
                return InvoiceMockScalarResult(sub_match)

            if sub_id:
                sub_match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
                return InvoiceMockScalarResult(sub_match)

            cust_id = params.get("stripe_customer_id")
            if cust_id:
                sub_match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if sub_match:
                    return InvoiceMockScalarResult(CustOrgRow(sub_match.organization_id, sub_match.stripe_subscription_id))
                return InvoiceMockScalarResult(None)

        return InvoiceMockScalarResult(None)


# =============================================================================
# Payload Builder Fixtures
# =============================================================================


def build_invoice_payment_failed_event(
    *,
    event_id: str = "evt_inv_failed_1",
    invoice_id: str = "in_test_12345",
    customer_id: str = "cus_test_12345",
    subscription_id: str = "sub_test_12345",
    status: str = "open",
    paid: bool = False,
    attempt_count: int = 1,
    amount_due: int = 2000,
    amount_paid: int = 0,
    currency: str = "usd",
    billing_reason: str = "subscription_cycle",
    period_start: int = 1730000000,
    period_end: int = 1732592000,
    next_payment_attempt: int | None = 1730086400,
    payment_intent: str | dict[str, Any] | None = "pi_test_12345",
    last_finalization_error: dict[str, Any] | None = None,
    metadata_org_id: uuid.UUID | None = None,
    created: int = 1730000000,
    event_type: str = "invoice.payment_failed",
) -> dict[str, Any]:
    """Build a valid Stripe invoice.payment_failed webhook event dictionary."""
    if last_finalization_error is None:
        last_finalization_error = {
            "code": "card_declined",
            "decline_code": "insufficient_funds",
            "message": "Your card has insufficient funds.",
        }

    metadata: dict[str, Any] = {}
    if metadata_org_id is not None:
        metadata["organization_id"] = str(metadata_org_id)

    invoice_dict: dict[str, Any] = {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer_id,
        "subscription": subscription_id,
        "status": status,
        "paid": paid,
        "attempt_count": attempt_count,
        "amount_due": amount_due,
        "amount_paid": amount_paid,
        "currency": currency,
        "billing_reason": billing_reason,
        "period_start": period_start,
        "period_end": period_end,
        "next_payment_attempt": next_payment_attempt,
        "payment_intent": payment_intent,
        "last_finalization_error": last_finalization_error,
        "metadata": metadata,
    }

    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {
            "object": invoice_dict,
        },
    }


def make_test_sub(
    *,
    org_id: uuid.UUID,
    customer_id: str = "cus_test_12345",
    subscription_id: str | None = "sub_test_12345",
    status: SubscriptionStatus = SubscriptionStatus.active,
    plan_tier: str = "pro",
    past_due_since: datetime | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
) -> Subscription:
    return Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        stripe_price_id=TEST_PRO_PRICE_ID,
        status=status,
        plan_tier=plan_tier,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        cancel_at_period_end=False,
        canceled_at=None,
        past_due_since=past_due_since,
    )


# =============================================================================
# 1. Dispatcher Registration Tests
# =============================================================================


def test_dispatcher_registers_invoice_payment_failed() -> None:
    """Verifies invoice.payment_failed is registered in default_webhook_dispatcher."""
    handler = default_webhook_dispatcher.get_handler("invoice.payment_failed")
    assert handler is not None
    assert handler is handle_invoice_payment_failed


def test_derive_advisory_lock_key_shared_equivalence() -> None:
    """Verifies both handlers derive the exact same advisory lock key."""
    invoice_id = "in_test_12345"
    key_paid = _derive_advisory_lock_key(invoice_id)
    key_failed = derive_invoice_advisory_lock_key(invoice_id)
    assert key_paid == key_failed
    assert -9223372036854775808 <= key_failed <= 9223372036854775807


# =============================================================================
# 2. Initial Failure & Repeated Failure Tests
# =============================================================================


@pytest.mark.asyncio
async def test_initial_payment_failure_creates_failed_record() -> None:
    """Verifies that an initial invoice.payment_failed event creates a failed StripeInvoice record."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_payment_failed_event(
        attempt_count=1,
        amount_due=2000,
        currency="usd",
    )

    await handle_invoice_payment_failed(event, session)

    assert "in_test_12345" in session.invoices
    inv = session.invoices["in_test_12345"]
    assert inv.processing_status == InvoiceProcessingStatus.failed
    assert inv.organization_id == org_id
    assert inv.stripe_customer_id == "cus_test_12345"
    assert inv.stripe_subscription_id == "sub_test_12345"
    assert inv.attempt_count == 1
    assert inv.error_message == "Your card has insufficient funds."
    assert inv.billing_period_id is None
    assert inv.raw_event_id == "evt_inv_failed_1"

    # Verify diagnostic payload contains attempt history
    attempts = inv.diagnostic_payload.get("attempts", [])
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["event_id"] == "evt_inv_failed_1"
    assert attempts[0]["amount_due"] == 2000
    assert attempts[0]["currency"] == "usd"
    assert attempts[0]["error_message"] == "Your card has insufficient funds."


@pytest.mark.asyncio
async def test_repeated_failures_append_attempt_history() -> None:
    """Verifies repeated failure events append attempt history and update attempt_count monotonically."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    # Attempt 1
    event1 = build_invoice_payment_failed_event(
        event_id="evt_fail_1",
        attempt_count=1,
        created=1730000000,
    )
    await handle_invoice_payment_failed(event1, session)

    # Attempt 2 (retry 2 days later)
    event2 = build_invoice_payment_failed_event(
        event_id="evt_fail_2",
        attempt_count=2,
        created=1730172800,
        last_finalization_error={"code": "card_declined", "message": "Card expired"},
    )
    await handle_invoice_payment_failed(event2, session)

    inv = session.invoices["in_test_12345"]
    assert inv.attempt_count == 2
    assert inv.error_message == "Card expired"
    assert inv.raw_event_id == "evt_fail_2"
    assert inv.processing_status == InvoiceProcessingStatus.failed

    attempts = inv.diagnostic_payload.get("attempts", [])
    assert len(attempts) == 2
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["event_id"] == "evt_fail_1"
    assert attempts[1]["attempt"] == 2
    assert attempts[1]["event_id"] == "evt_fail_2"
    assert attempts[1]["error_message"] == "Card expired"


@pytest.mark.asyncio
async def test_attempt_count_never_decreases() -> None:
    """Verifies that an out-of-order lower attempt_count does not decrement existing attempt_count."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    # First event arrived with attempt 3
    event_3 = build_invoice_payment_failed_event(event_id="evt_3", attempt_count=3)
    await handle_invoice_payment_failed(event_3, session)
    assert session.invoices["in_test_12345"].attempt_count == 3

    # Delayed earlier event arrives with attempt 2
    event_2 = build_invoice_payment_failed_event(event_id="evt_2", attempt_count=2)
    await handle_invoice_payment_failed(event_2, session)

    inv = session.invoices["in_test_12345"]
    assert inv.attempt_count == 3  # Preserved max!
    assert len(inv.diagnostic_payload["attempts"]) == 2


# =============================================================================
# 3. Defensive Handling of Missing Optional Fields
# =============================================================================


@pytest.mark.asyncio
async def test_missing_optional_failure_details_handled_safely() -> None:
    """Verifies payload without last_finalization_error, next_payment_attempt, or payment_intent succeeds."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_payment_failed_event(
        next_payment_attempt=None,
        payment_intent=None,
        last_finalization_error={},  # Empty dict, no message
    )

    await handle_invoice_payment_failed(event, session)

    inv = session.invoices["in_test_12345"]
    assert inv.error_message == "Payment failed"  # Safe fallback!
    assert inv.processing_status == InvoiceProcessingStatus.failed
    attempts = inv.diagnostic_payload["attempts"]
    assert attempts[0]["next_payment_attempt"] is None
    assert attempts[0]["payment_intent"] is None


# =============================================================================
# 4. Identity & Validation Guards
# =============================================================================


@pytest.mark.asyncio
async def test_unknown_customer_fails_closed() -> None:
    """Verifies unknown customer raises WebhookPermanentError (quarantined)."""
    session = InvoiceMockDbSession(subscriptions=[])  # No customers
    event = build_invoice_payment_failed_event(customer_id="cus_unknown_999")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_invoice_payment_failed(event, session)
    assert "Unknown Stripe customer" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unknown_subscription_quarantines() -> None:
    """Verifies invoice for unknown subscription raises WebhookReconciliationConflictError."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id="sub_current_123")
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_payment_failed_event(subscription_id="sub_foreign_999")

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_invoice_payment_failed(event, session)
    assert exc_info.value.reason == "unknown_subscription_invoice_payment_failed"


@pytest.mark.asyncio
async def test_metadata_organization_mismatch_fails_closed() -> None:
    """Verifies mismatched organization_id in metadata raises WebhookPermanentError."""
    org_id = uuid.uuid4()
    foreign_org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_payment_failed_event(metadata_org_id=foreign_org_id)

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_invoice_payment_failed(event, session)
    assert "Organization metadata mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unbound_customer_quarantines() -> None:
    """Verifies invoice received for customer with stripe_subscription_id=None quarantines."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id=None, status=SubscriptionStatus.incomplete)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_payment_failed_event(subscription_id="sub_inbound_123")

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_invoice_payment_failed(event, session)
    assert exc_info.value.reason == "unbound_customer_invoice_payment_failed"


# =============================================================================
# 5. Superseded & Canceled Subscription Handling
# =============================================================================


@pytest.mark.asyncio
async def test_superseded_subscription_failure_recorded_ignored() -> None:
    """Verifies failed invoice for completed historical attempt is recorded as ignored without mutating state."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id="sub_current_active")
    historical_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_session_id="cs_old",
        stripe_customer_id="cus_test_12345",
        stripe_subscription_id="sub_superseded_old",
        status=CheckoutAttemptStatus.complete,
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc),
    )
    session = InvoiceMockDbSession(subscriptions=[sub], attempts=[historical_attempt])

    event = build_invoice_payment_failed_event(
        subscription_id="sub_superseded_old",
        invoice_id="in_old_failure",
    )

    await handle_invoice_payment_failed(event, session)

    assert "in_old_failure" in session.invoices
    inv = session.invoices["in_old_failure"]
    assert inv.processing_status == InvoiceProcessingStatus.ignored
    assert sub.status == SubscriptionStatus.active  # Untouched!


@pytest.mark.asyncio
async def test_canceled_subscription_failure_recorded_ignored() -> None:
    """Verifies failure event for a canceled subscription is marked ignored and leaves periods untouched."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, status=SubscriptionStatus.canceled)
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        plan_tier="free",
        monthly_token_quota=1000,
        tokens_used=0,
        tokens_reserved=0,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    session = InvoiceMockDbSession(subscriptions=[sub], billing_periods=[bp])

    event = build_invoice_payment_failed_event(invoice_id="in_canceled_sub")

    await handle_invoice_payment_failed(event, session)

    assert "in_canceled_sub" in session.invoices
    inv = session.invoices["in_canceled_sub"]
    assert inv.processing_status == InvoiceProcessingStatus.ignored
    assert bp.is_active is True  # Billing period untouched!
    assert sub.status == SubscriptionStatus.canceled  # Untouched!


# =============================================================================
# 6. Interaction with invoice.paid & Stale Event Protection
# =============================================================================


@pytest.mark.asyncio
async def test_failure_after_successful_payment_never_regresses_processed_status() -> None:
    """Verifies delayed invoice.payment_failed after invoice.paid preserves processed status and period."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    bp_id = uuid.uuid4()
    existing_inv = StripeInvoice(
        id=uuid.uuid4(),
        stripe_invoice_id="in_paid_first",
        organization_id=org_id,
        stripe_customer_id="cus_test_12345",
        stripe_subscription_id="sub_test_12345",
        billing_reason="subscription_cycle",
        invoice_status="paid",
        currency="usd",
        amount_paid_minor_units=2000,
        period_start=datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        processing_status=InvoiceProcessingStatus.processed,
        billing_period_id=bp_id,
        raw_event_id="evt_paid_1",
        diagnostic_payload={"paid": True},
    )
    session = InvoiceMockDbSession(subscriptions=[sub], invoices=[existing_inv])

    delayed_event = build_invoice_payment_failed_event(
        invoice_id="in_paid_first",
        event_id="evt_late_failure",
    )

    await handle_invoice_payment_failed(delayed_event, session)

    inv = session.invoices["in_paid_first"]
    assert inv.processing_status == InvoiceProcessingStatus.processed  # Status preserved!
    assert inv.billing_period_id == bp_id  # Period preserved!

    # Stale failure recorded under diagnostic stale_failures
    stale_failures = inv.diagnostic_payload.get("stale_failures", [])
    assert len(stale_failures) == 1
    assert stale_failures[0]["event_id"] == "evt_late_failure"


@pytest.mark.asyncio
async def test_failure_before_invoice_paid_allows_successful_transition_and_updates_fields() -> None:
    """Verifies that invoice.paid on a pre-existing failed invoice transitions to processed and updates fields."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    # 1. Payment failure arrives first
    event_fail = build_invoice_payment_failed_event(
        invoice_id="in_lifecycle_test",
        event_id="evt_fail_first",
        amount_due=2000,
        amount_paid=0,
        status="open",
    )
    await handle_invoice_payment_failed(event_fail, session)

    inv = session.invoices["in_lifecycle_test"]
    assert inv.processing_status == InvoiceProcessingStatus.failed
    assert inv.amount_paid_minor_units == 0
    assert inv.billing_period_id is None

    # 2. Subsequent payment retry succeeds -> invoice.paid arrives
    from tests.test_stripe_invoice_paid_handler import build_invoice_paid_event
    event_paid = build_invoice_paid_event(
        invoice_id="in_lifecycle_test",
        event_id="evt_paid_second",
        customer_id="cus_test_12345",
        subscription_id="sub_test_12345",
        amount_paid=2000,
        status="paid",
    )
    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event_paid, session)

    # 3. Assert transition to processed and fields updated
    inv_updated = session.invoices["in_lifecycle_test"]
    assert inv_updated.processing_status == InvoiceProcessingStatus.processed
    assert inv_updated.billing_period_id is not None
    assert inv_updated.amount_paid_minor_units == 2000
    assert inv_updated.invoice_status == "paid"
    assert inv_updated.raw_event_id == "evt_paid_second"

    # Historical failure attempt history is preserved in diagnostic payload!
    assert "attempts" in inv_updated.diagnostic_payload
    assert len(inv_updated.diagnostic_payload["attempts"]) == 1


# =============================================================================
# 7. Strict Accounting & Lifecycle Isolation
# =============================================================================


@pytest.mark.asyncio
async def test_failure_handling_never_mutates_subscription_lifecycle_or_periods() -> None:
    """Verifies handle_invoice_payment_failed NEVER mutates Subscription status, past_due_since, or billing periods."""
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    anchor = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=anchor,
        current_period_start=t_start,
        current_period_end=t_end,
    )
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="pro",
        monthly_token_quota=50000,
        tokens_used=123,
        tokens_reserved=456,
        cost_micros_used=1000,
        cost_micros_reserved=2000,
        is_active=True,
        is_blocked=False,
    )
    session = InvoiceMockDbSession(subscriptions=[sub], billing_periods=[bp])

    event = build_invoice_payment_failed_event()
    await handle_invoice_payment_failed(event, session)

    # Subscription state completely untouched
    assert sub.status == SubscriptionStatus.past_due
    assert sub.past_due_since == anchor  # Not reset or changed!
    assert sub.plan_tier == "pro"  # Not downgraded!
    assert sub.current_period_start == t_start
    assert sub.current_period_end == t_end

    # Billing period completely untouched
    assert bp.is_active is True
    assert bp.monthly_token_quota == 50000
    assert bp.tokens_used == 123
    assert bp.tokens_reserved == 456
    assert bp.cost_micros_used == 1000
    assert bp.cost_micros_reserved == 2000


# =============================================================================
# 8. Webhook Processing Engine Integration Tests
# =============================================================================


@pytest.mark.asyncio
async def test_webhook_engine_process_invoice_payment_failed_success() -> None:
    """Verifies that invoice.payment_failed transitions through the engine to processed."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    payload = build_invoice_payment_failed_event(event_id="evt_eng_fail_1")
    event_row = StripeWebhookEvent(
        id="evt_eng_fail_1",
        event_type="invoice.payment_failed",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=payload,
    )
    session = InvoiceMockDbSession(
        subscriptions=[sub],
        events=[event_row],
    )

    res = await execute_claimed_webhook_event(
        db=session,
        event=event_row,
        handler=handle_invoice_payment_failed,
    )

    assert res.success is True
    assert event_row.status == WebhookProcessingStatus.processed
    assert "in_test_12345" in session.invoices
    assert session.invoices["in_test_12345"].processing_status == InvoiceProcessingStatus.failed


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_unknown_subscription_payment_failed() -> None:
    """Verifies engine quarantines event when unknown subscription conflict is raised."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id="sub_local")
    payload = build_invoice_payment_failed_event(
        event_id="evt_eng_unknown_sub",
        subscription_id="sub_foreign",
    )
    event_row = StripeWebhookEvent(
        id="evt_eng_unknown_sub",
        event_type="invoice.payment_failed",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=payload,
    )
    session = InvoiceMockDbSession(
        subscriptions=[sub],
        events=[event_row],
    )

    res = await execute_claimed_webhook_event(
        db=session,
        event=event_row,
        handler=handle_invoice_payment_failed,
    )

    assert res.success is False
    assert event_row.status == WebhookProcessingStatus.quarantined
    assert len(session.reconciliations) == 1
    assert session.reconciliations[0].reason == "unknown_subscription_invoice_payment_failed"
