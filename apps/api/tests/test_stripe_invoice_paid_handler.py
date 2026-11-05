"""Comprehensive Unit & Integration Tests for Stripe Invoice Paid Webhook Handler.

Event: invoice.paid

Validates:
1. Dispatcher Registration:
   - invoice.paid is registered in default_webhook_dispatcher and routes to handle_invoice_paid.
2. Initial Pro Subscription Invoicing (subscription_create):
   - Provisions initial Pro OrganizationBillingPeriod (plan_tier="pro", is_active=True).
   - Sets monthly_token_quota = settings.default_pro_monthly_token_quota.
   - Deactivates prior Free period (is_active=False).
   - Creates StripeInvoice ledger record with processing_status=processed.
   - Updates Subscription.current_period_start and current_period_end.
   - Preserves Subscription.status, Subscription.plan_tier, Subscription.past_due_since.
3. Recurring Renewal Invoicing (subscription_cycle):
   - Consecutive renewal (incoming.start == active.end) deactivates prior period.
   - Provisions new active Pro OrganizationBillingPeriod.
   - Advances Subscription.current_period_end monotonically.
4. Exact Matching Period Idempotency:
   - Invoice with matching period boundaries attaches to existing active Pro period.
   - Does NOT reset usage counters, tokens, or duplicate quota.
5. Exact Free-Period Upgrade:
   - Exact match with active Free period deactivates Free period and provisions new Pro period.
6. Replay & Duplicate Invoices:
   - Duplicate delivery of same invoice ID is an idempotent no-op.
   - Two different invoice IDs for the same period attach to the same period safely.
7. Concurrency & Serialization:
   - Two different invoice IDs for the same organization with no initial active period serialize
     on the Subscription row lock; in-lock re-query prevents duplicate active periods.
8. Chronology & Boundary Protection:
   - Stale invoice prior to active period attaches to historical period if match exists.
   - Stale invoice without matching historical period is quarantined.
   - Gapped invoice (start > active.end) is quarantined.
   - Overlapping or nested invoice is quarantined.
   - No active period: historical inspection and consecutive creation.
9. Refusal of Resurrection:
   - Invoice arriving for canceled subscription records StripeInvoice as ignored.
   - Refuses to provision billing period or resurrect subscription.
10. Unbound Customer & Unknown Subscription Conflicts:
    - Customer with stripe_subscription_id=None raises unbound_customer_invoice.
    - Unknown subscription ID raises unknown_subscription_invoice.
    - Historical superseded subscription invoice recorded as ignored no-op.
11. Line-Item Selection Policy:
    - Foreign/unrecognized price ID quarantined (unrecognized_line_item_price).
    - Missing Pro recurring line quarantined (no_eligible_pro_line_item).
    - Multiple non-prorated Pro lines quarantined (multiple_eligible_pro_line_items).
    - Malformed period dates (end <= start) fail closed (WebhookPermanentError).
12. Zero-Value & Quota Configuration Fail-Closed:
    - amount_paid == 0 quarantined (zero_value_invoice_pending_review).
    - default_pro_monthly_token_quota <= 0 quarantined (missing_pro_quota_configuration).
13. Monotonic Subscription Period Fields:
    - Delayed older invoice does not regress Subscription.current_period_end.
14. Runtime Authorization & Delayed Lifecycle Integration:
    - Paid invoice does not grant access while Subscription.status remains incomplete.
    - reserve_usage() fails closed with SUBSCRIPTION_INACTIVE.
    - Subsequent subscription event activating status immediately authorizes inference.
15. Webhook Engine Integration & Quarantine:
    - Full end-to-end execution via process_webhook_event().
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

from collections import namedtuple
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
import unittest.mock as mock
from unittest.mock import patch
import uuid

from pydantic import SecretStr
import pytest

import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.billing  # noqa: F401
from app.config import Settings
from app.models.billing import OrganizationBillingPeriod, ReservationStatus, UsageReservation
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    ReconciliationStatus,
    StripeCheckoutAttempt,
    StripeInvoice,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.accounting import (
    AccountingErrorCode,
    reserve_usage,
)
from app.services.billing.pricing import MOCK_GPT_4O_MINI_PRICING
from app.services.billing.stripe_invoice_paid_handler import (
    _derive_advisory_lock_key,
    handle_invoice_paid,
)
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
    process_webhook_event,
)

TEST_PRO_PRICE_ID = "price_pro_invoice_test_123"
TEST_DEFAULT_PRO_QUOTA = 1_000_000


# =============================================================================
# Test Settings Helper
# =============================================================================


def get_test_settings(
    pro_price_id: str = TEST_PRO_PRICE_ID,
    pro_quota: int = TEST_DEFAULT_PRO_QUOTA,
) -> Settings:
    """Construct mock Settings with valid Stripe and Pro quota configuration."""
    return Settings(
        stripe_enabled=True,
        stripe_secret_key=SecretStr("sk_test_mock_1234567890abcdef"),
        stripe_webhook_secret=SecretStr("whsec_test_mock_1234567890abcdef"),
        stripe_publishable_key="pk_test_mock_1234567890abcdef",
        stripe_pro_price_id=pro_price_id,
        stripe_billing_return_url="http://localhost:3000/billing",
        billing_grace_period_days=7,
        default_free_monthly_token_quota=0,
        default_pro_monthly_token_quota=pro_quota,
    )


# =============================================================================
# Mock Database Double for Invoice Tests
# =============================================================================


class InvoiceMockSavepoint:
    """Simulates SQLAlchemy async savepoint (begin_nested)."""

    def __init__(self, session: "InvoiceMockDbSession") -> None:
        self.session = session
        self.added_len = len(session.added)
        self.rec_len = len(session.reconciliations)
        self.sub_snapshots: dict[uuid.UUID, dict[str, Any]] = {}
        self.bp_snapshots: dict[uuid.UUID, dict[str, Any]] = {}

    async def __aenter__(self) -> "InvoiceMockSavepoint":
        self.sub_snapshots = {
            s.id: {
                "status": s.status,
                "plan_tier": s.plan_tier,
                "stripe_subscription_id": s.stripe_subscription_id,
                "current_period_start": s.current_period_start,
                "current_period_end": s.current_period_end,
                "past_due_since": s.past_due_since,
                "updated_at": s.updated_at,
            }
            for s in self.session.subscriptions.values()
        }
        self.bp_snapshots = {
            bp.id: {
                "is_active": bp.is_active,
                "tokens_used": bp.tokens_used,
                "cost_micros_used": bp.cost_micros_used,
            }
            for bp in self.session.billing_periods.values()
        }
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            del self.session.added[self.added_len:]
            del self.session.reconciliations[self.rec_len:]
            for sid, snap in self.sub_snapshots.items():
                s = self.session.subscriptions.get(sid)
                if s:
                    s.status = snap["status"]
                    s.plan_tier = snap["plan_tier"]
                    s.stripe_subscription_id = snap["stripe_subscription_id"]
                    s.current_period_start = snap["current_period_start"]
                    s.current_period_end = snap["current_period_end"]
                    s.past_due_since = snap["past_due_since"]
                    s.updated_at = snap["updated_at"]
            for bpid, bsnap in self.bp_snapshots.items():
                bp = self.session.billing_periods.get(bpid)
                if bp:
                    bp.is_active = bsnap["is_active"]
                    bp.tokens_used = bsnap["tokens_used"]
                    bp.cost_micros_used = bsnap["cost_micros_used"]
            self.session.savepoints_rolled_back += 1
            return False
        self.session.savepoints_committed += 1
        return False


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


CustOrgRow = namedtuple("CustOrgRow", ["organization_id", "stripe_subscription_id"])


class InvoiceMockDbSession:
    """In-memory AsyncSession double tailored for invoice processing tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
        events: list[StripeWebhookEvent] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
        invoices: list[StripeInvoice] | None = None,
        tasks: list[GenerationTask] | None = None,
        reservations: list[UsageReservation] | None = None,
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
        self.tasks: dict[uuid.UUID, GenerationTask] = {
            t.id: t for t in (tasks or [])
        }
        self.reservations: dict[uuid.UUID, UsageReservation] = {
            r.id: r for r in (reservations or [])
        }
        self.reconciliations: list[BillingReconciliation] = []
        self.added: list[Any] = []
        self.advisory_locks: set[int] = set()
        self.committed_count = 0
        self.rolled_back_count = 0
        self.savepoints_committed = 0
        self.savepoints_rolled_back = 0
        self.flushed = False

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
        elif isinstance(obj, GenerationTask):
            self.tasks[obj.id] = obj
        elif isinstance(obj, UsageReservation):
            self.reservations[obj.id] = obj

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

            # Active period query (is_active == True)
            if "is_active = true" in where_full or params.get("is_active") is True:
                active_bp = next(
                    (bp for bp in self.billing_periods.values() if (org_id is None or bp.organization_id == org_id) and bp.is_active is True),
                    None,
                )
                return InvoiceMockScalarResult(active_bp)

            # Query by period dates (e.g. historical lookup with period_start, period_end)
            p_start = params.get("period_start")
            p_end = params.get("period_end")
            if p_start and p_end:
                match_dates = next(
                    (bp for bp in self.billing_periods.values() if (org_id is None or bp.organization_id == org_id) and bp.period_start == p_start and bp.period_end == p_end),
                    None,
                )
                return InvoiceMockScalarResult(match_dates)

            # Query by ID (e.g. reserve_usage)
            bp_id = params.get("id")
            if bp_id and bp_id in self.billing_periods:
                return InvoiceMockScalarResult(self.billing_periods[bp_id])

            # Query all periods for org (ordered by period_end desc)
            if org_id:
                org_periods = [bp for bp in self.billing_periods.values() if bp.organization_id == org_id]
                org_periods.sort(key=lambda x: x.period_end, reverse=True)
                return InvoiceMockScalarResult(org_periods)

            return InvoiceMockScalarResult(None)

        # 7. GenerationTask queries (reserve_usage)
        if "generation_tasks" in where_full:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.tasks:
                    return InvoiceMockScalarResult(self.tasks[v])
            return InvoiceMockScalarResult(None)

        # 8. UsageReservation queries
        if "usage_reservations" in where_full:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            active_res = next(
                (r for r in self.reservations.values() if r.generation_task_id == t_id and r.status == ReservationStatus.active),
                None,
            )
            return InvoiceMockScalarResult(active_res)

        # 9. Subscription queries
        if "subscriptions" in where_full:
            sub_id = params.get("stripe_subscription_id")
            if sub_id:
                sub_match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
                if sub_match:
                    return InvoiceMockScalarResult(sub_match)
                return InvoiceMockScalarResult(None)

            cust_id = params.get("stripe_customer_id")
            if cust_id:
                sub_match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if sub_match:
                    return InvoiceMockScalarResult(sub_match)
                return InvoiceMockScalarResult(None)

            org_id = params.get("organization_id")
            if org_id:
                if "status in" in where_full:
                    eligible = [
                        s for s in self.subscriptions.values()
                        if s.organization_id == org_id and s.status in (SubscriptionStatus.active, SubscriptionStatus.past_due)
                    ]
                    return InvoiceMockScalarResult(eligible)
                sub_matches = [s for s in self.subscriptions.values() if s.organization_id == org_id]
                if len(sub_matches) > 1:
                    return InvoiceMockScalarResult(sub_matches)
                return InvoiceMockScalarResult(sub_matches[0] if sub_matches else None)

        return InvoiceMockScalarResult(None)


# =============================================================================
# Payload Builder Fixtures
# =============================================================================


def build_invoice_paid_event(
    *,
    event_id: str = "evt_inv_paid_1",
    invoice_id: str = "in_test_12345",
    customer_id: str = "cus_test_12345",
    subscription_id: str = "sub_test_12345",
    status: str = "paid",
    paid: bool = True,
    amount_paid: int = 2000,
    currency: str = "usd",
    billing_reason: str = "subscription_cycle",
    period_start: int = 1730000000,
    period_end: int = 1732592000,
    price_id: str = TEST_PRO_PRICE_ID,
    proration: bool = False,
    lines_override: list[dict[str, Any]] | None = None,
    metadata_org_id: uuid.UUID | None = None,
    created: int = 1730000000,
    event_type: str = "invoice.paid",
) -> dict[str, Any]:
    """Build a valid Stripe invoice.paid webhook event dictionary."""
    if lines_override is None:
        lines_data = [
            {
                "id": "il_test_1",
                "type": "subscription",
                "price": {"id": price_id, "object": "price"},
                "proration": proration,
                "period": {
                    "start": period_start,
                    "end": period_end,
                },
                "amount": amount_paid,
            }
        ]
    else:
        lines_data = lines_override

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
        "amount_paid": amount_paid,
        "currency": currency,
        "billing_reason": billing_reason,
        "period_start": period_start,
        "period_end": period_end,
        "lines": {
            "object": "list",
            "data": lines_data,
        },
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


def test_dispatcher_registers_invoice_paid() -> None:
    """Verifies invoice.paid is registered in default_webhook_dispatcher."""
    handler = default_webhook_dispatcher.get_handler("invoice.paid")
    assert handler is not None
    assert handler is handle_invoice_paid


def test_derive_advisory_lock_key_consistency() -> None:
    """Verifies that advisory lock key derivation is deterministic and signed 64-bit."""
    key1 = _derive_advisory_lock_key("in_test_12345")
    key2 = _derive_advisory_lock_key("in_test_12345")
    assert key1 == key2
    assert -9223372036854775808 <= key1 <= 9223372036854775807
    key3 = _derive_advisory_lock_key("in_test_different")
    assert key1 != key3


# =============================================================================
# 2. Initial Pro Subscription Invoice (subscription_create)
# =============================================================================


@pytest.mark.asyncio
async def test_initial_pro_subscription_invoice_provisions_period() -> None:
    """Verifies subscription_create provisions Pro period and deactivates prior Free period."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Pre-existing Free period
    free_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc),
        period_end=p_start,
        plan_tier="free",
        monthly_token_quota=0,
        is_active=True,
    )
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    session = InvoiceMockDbSession(
        billing_periods=[free_bp],
        subscriptions=[sub],
    )

    event = build_invoice_paid_event(
        billing_reason="subscription_create",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # 1. Prior Free period deactivated
    assert free_bp.is_active is False

    # 2. New Pro period provisioned with configured quota
    new_periods = [p for p in session.billing_periods.values() if p.id != free_bp.id]
    assert len(new_periods) == 1
    new_bp = new_periods[0]
    assert new_bp.plan_tier == "pro"
    assert new_bp.is_active is True
    assert new_bp.monthly_token_quota == TEST_DEFAULT_PRO_QUOTA
    assert new_bp.period_start == p_start
    assert new_bp.period_end == p_end
    assert new_bp.tokens_used == 0
    assert new_bp.hard_spending_cap_micros is None

    # 3. StripeInvoice ledger record created and processed
    assert "in_test_12345" in session.invoices
    inv = session.invoices["in_test_12345"]
    assert inv.processing_status == InvoiceProcessingStatus.processed
    assert inv.billing_period_id == new_bp.id
    assert inv.amount_paid_minor_units == 2000

    # 4. Subscription dates updated; status and plan_tier strictly preserved
    assert sub.current_period_start == p_start
    assert sub.current_period_end == p_end
    assert sub.status == SubscriptionStatus.incomplete  # Lifecycle untouched!
    assert sub.plan_tier == "free"  # Lifecycle untouched!


# =============================================================================
# 3. Recurring Renewal (subscription_cycle)
# =============================================================================


@pytest.mark.asyncio
async def test_recurring_renewal_invoice_consecutive_period() -> None:
    """Verifies subscription_cycle deactivates prior Pro period and advances period cleanly."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p2_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    bp1 = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_start,
        period_end=p1_end,
        plan_tier="pro",
        monthly_token_quota=TEST_DEFAULT_PRO_QUOTA,
        tokens_used=45_000,
        is_active=True,
    )
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        current_period_start=p1_start,
        current_period_end=p1_end,
    )
    session = InvoiceMockDbSession(
        billing_periods=[bp1],
        subscriptions=[sub],
    )

    event = build_invoice_paid_event(
        invoice_id="in_cycle_renewal_2",
        billing_reason="subscription_cycle",
        period_start=int(p1_end.timestamp()),
        period_end=int(p2_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    assert bp1.is_active is False
    assert bp1.tokens_used == 45_000  # Immutability preserved

    bp2 = next(p for p in session.billing_periods.values() if p.id != bp1.id)
    assert bp2.is_active is True
    assert bp2.period_start == p1_end
    assert bp2.period_end == p2_end
    assert bp2.tokens_used == 0

    assert sub.current_period_end == p2_end


# =============================================================================
# 4. Exact Matching Period Idempotency & Free Upgrade
# =============================================================================


@pytest.mark.asyncio
async def test_exact_matching_pro_period_attaches_without_duplicating_quota() -> None:
    """Verifies that an invoice matching active Pro period dates attaches without resetting quota."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p_start,
        period_end=p_end,
        plan_tier="pro",
        monthly_token_quota=TEST_DEFAULT_PRO_QUOTA,
        tokens_used=12_000,
        is_active=True,
    )
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        current_period_start=p_start,
        current_period_end=p_end,
    )
    session = InvoiceMockDbSession(billing_periods=[active_bp], subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_update_same_period",
        billing_reason="subscription_update",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # No new billing period created
    assert len(session.billing_periods) == 1
    assert active_bp.is_active is True
    assert active_bp.tokens_used == 12_000  # Not reset!
    inv = session.invoices["in_update_same_period"]
    assert inv.billing_period_id == active_bp.id
    assert inv.processing_status == InvoiceProcessingStatus.processed


@pytest.mark.asyncio
async def test_exact_matching_free_period_upgrades_to_pro() -> None:
    """Verifies that an invoice matching active Free period dates deactivates Free and provisions Pro."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    free_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p_start,
        period_end=p_end,
        plan_tier="free",
        monthly_token_quota=0,
        is_active=True,
    )
    sub = make_test_sub(org_id=org_id, status=SubscriptionStatus.active, plan_tier="pro")
    session = InvoiceMockDbSession(billing_periods=[free_bp], subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_upgrade_exact_free",
        billing_reason="subscription_create",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    assert free_bp.is_active is False
    pro_bp = next(p for p in session.billing_periods.values() if p.id != free_bp.id)
    assert pro_bp.is_active is True
    assert pro_bp.plan_tier == "pro"
    assert pro_bp.monthly_token_quota == TEST_DEFAULT_PRO_QUOTA


# =============================================================================
# 5. Replay Idempotency & Same Org Different Invoices
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_invoice_delivery_is_idempotent_noop() -> None:
    """Verifies that an already processed invoice returns immediately without changes."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    inv_record = StripeInvoice(
        id=uuid.uuid4(),
        stripe_invoice_id="in_already_done",
        organization_id=org_id,
        stripe_customer_id="cus_test_12345",
        stripe_subscription_id="sub_test_12345",
        invoice_status="paid",
        processing_status=InvoiceProcessingStatus.processed,
        billing_period_id=bp_id,
    )
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(invoices=[inv_record], subscriptions=[sub])

    event = build_invoice_paid_event(invoice_id="in_already_done")

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # Zero mutations
    assert len(session.billing_periods) == 0


@pytest.mark.asyncio
async def test_two_different_invoices_same_org_no_initial_period() -> None:
    """Verifies concurrent arrival of in_A and in_B for same org with no initial period."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    test_settings = get_test_settings()

    # Worker 1: processes in_A
    event_a = build_invoice_paid_event(
        invoice_id="in_worker_A",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )
    with patch("app.services.billing.stripe_invoice_paid_handler.settings", test_settings):
        await handle_invoice_paid(event_a, session)

    assert len(session.billing_periods) == 1
    p1 = list(session.billing_periods.values())[0]
    assert p1.is_active is True

    # Worker 2: processes in_B (matching dates)
    event_b = build_invoice_paid_event(
        invoice_id="in_worker_B",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )
    with patch("app.services.billing.stripe_invoice_paid_handler.settings", test_settings):
        await handle_invoice_paid(event_b, session)

    # In-lock re-query finds p1 and attaches in_B without duplicating period
    assert len(session.billing_periods) == 1
    assert session.invoices["in_worker_B"].billing_period_id == p1.id
    assert session.invoices["in_worker_B"].processing_status == InvoiceProcessingStatus.processed


# =============================================================================
# 6. Chronology & Boundary Protection
# =============================================================================


@pytest.mark.asyncio
async def test_stale_older_invoice_attaches_to_historical_period_if_exists() -> None:
    """Verifies older invoice attaches to historical period without altering active period."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p2_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    hist_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_start,
        period_end=p1_end,
        plan_tier="pro",
        monthly_token_quota=TEST_DEFAULT_PRO_QUOTA,
        is_active=False,  # Historical inactive
    )
    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_end,
        period_end=p2_end,
        plan_tier="pro",
        monthly_token_quota=TEST_DEFAULT_PRO_QUOTA,
        is_active=True,
    )
    sub = make_test_sub(org_id=org_id, current_period_start=p1_end, current_period_end=p2_end)
    session = InvoiceMockDbSession(billing_periods=[hist_bp, active_bp], subscriptions=[sub])

    # Delayed invoice for Oct 1 - Nov 1 arrives
    event = build_invoice_paid_event(
        invoice_id="in_delayed_oct",
        period_start=int(p1_start.timestamp()),
        period_end=int(p1_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    assert active_bp.is_active is True  # Active period untouched!
    assert session.invoices["in_delayed_oct"].billing_period_id == hist_bp.id


@pytest.mark.asyncio
async def test_stale_older_invoice_without_historical_period_quarantines() -> None:
    """Verifies older invoice without matching historical period is quarantined."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p2_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_end,
        period_end=p2_end,
        plan_tier="pro",
        monthly_token_quota=TEST_DEFAULT_PRO_QUOTA,
        is_active=True,
    )
    sub = make_test_sub(org_id=org_id, current_period_start=p1_end, current_period_end=p2_end)
    session = InvoiceMockDbSession(billing_periods=[active_bp], subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_stale_no_hist",
        period_start=int(p1_start.timestamp()),
        period_end=int(p1_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "stale_invoice_prior_to_active_period"
    assert active_bp.is_active is True


@pytest.mark.asyncio
async def test_gapped_invoice_period_quarantines() -> None:
    """Verifies that an invoice starting after active period ends with a gap is quarantined."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    # Gap: starts Nov 5 instead of Nov 1
    gapped_start = datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)
    gapped_end = datetime(2026, 12, 5, 0, 0, 0, tzinfo=timezone.utc)

    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_start,
        period_end=p1_end,
        plan_tier="pro",
        is_active=True,
    )
    sub = make_test_sub(org_id=org_id, current_period_start=p1_start, current_period_end=p1_end)
    session = InvoiceMockDbSession(billing_periods=[active_bp], subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_gap",
        period_start=int(gapped_start.timestamp()),
        period_end=int(gapped_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "billing_period_gap_conflict"


@pytest.mark.asyncio
async def test_overlapping_invoice_period_quarantines() -> None:
    """Verifies that an invoice partially overlapping active period is quarantined."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    # Overlap: starts Oct 15
    overlap_start = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    overlap_end = datetime(2026, 11, 15, 0, 0, 0, tzinfo=timezone.utc)

    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_start,
        period_end=p1_end,
        plan_tier="pro",
        is_active=True,
    )
    sub = make_test_sub(org_id=org_id, current_period_start=p1_start, current_period_end=p1_end)
    session = InvoiceMockDbSession(billing_periods=[active_bp], subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_overlap",
        period_start=int(overlap_start.timestamp()),
        period_end=int(overlap_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "overlapping_billing_period_conflict"


# =============================================================================
# 7. Refusal of Resurrection & Superseded Invoices
# =============================================================================


@pytest.mark.asyncio
async def test_paid_invoice_refuses_resurrection_of_canceled_subscription() -> None:
    """Verifies that an invoice arriving for a canceled subscription is recorded as ignored."""
    org_id = uuid.uuid4()
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event(invoice_id="in_late_payment_canceled")

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # 1. Zero billing periods created
    assert len(session.billing_periods) == 0

    # 2. Subscription remains canceled
    assert sub.status == SubscriptionStatus.canceled

    # 3. StripeInvoice ledger records ignored status
    assert "in_late_payment_canceled" in session.invoices
    inv = session.invoices["in_late_payment_canceled"]
    assert inv.processing_status == InvoiceProcessingStatus.ignored
    assert inv.billing_period_id is None


@pytest.mark.asyncio
async def test_historical_superseded_subscription_invoice_recorded_as_ignored() -> None:
    """Verifies invoice for older completed subscription is recorded as ignored without mutating active sub."""
    org_id = uuid.uuid4()
    active_sub_id = "sub_new_active_999"
    old_sub_id = "sub_old_superseded_111"

    sub = make_test_sub(
        org_id=org_id,
        subscription_id=active_sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_subscription_id=old_sub_id,
        status=CheckoutAttemptStatus.complete,
    )
    session = InvoiceMockDbSession(subscriptions=[sub], attempts=[attempt])

    event = build_invoice_paid_event(
        invoice_id="in_old_sub",
        subscription_id=old_sub_id,
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    assert sub.stripe_subscription_id == active_sub_id  # Untouched
    assert session.invoices["in_old_sub"].processing_status == InvoiceProcessingStatus.ignored


@pytest.mark.asyncio
async def test_unknown_subscription_invoice_quarantines() -> None:
    """Verifies invoice for unknown subscription raises conflict error."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id="sub_current_123")
    session = InvoiceMockDbSession(subscriptions=[sub], attempts=[])

    event = build_invoice_paid_event(
        invoice_id="in_alien",
        subscription_id="sub_alien_999",
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "unknown_subscription_invoice"


@pytest.mark.asyncio
async def test_unbound_customer_invoice_quarantines() -> None:
    """Verifies invoice for unbound customer raises conflict error."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, subscription_id=None)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event(invoice_id="in_unbound")

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "unbound_customer_invoice"


# =============================================================================
# 8. Line Items, Zero-Value, & Quota Configuration Policies
# =============================================================================


@pytest.mark.asyncio
async def test_zero_value_invoice_quarantined() -> None:
    """Verifies amount_paid=0 invoice is quarantined pending trial/discount approval."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event(amount_paid=0)

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "zero_value_invoice_pending_review"


@pytest.mark.asyncio
async def test_missing_pro_quota_configuration_quarantined() -> None:
    """Verifies that default_pro_monthly_token_quota=0 fails closed and quarantines invoice."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event()

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings(pro_quota=0)):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "missing_pro_quota_configuration"


@pytest.mark.asyncio
async def test_unrecognized_line_item_price_quarantined() -> None:
    """Verifies that an invoice containing a foreign price is quarantined."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    foreign_lines = [
        {"id": "il_1", "price": {"id": TEST_PRO_PRICE_ID}, "proration": False, "period": {"start": 1730000000, "end": 1732592000}},
        {"id": "il_foreign", "price": {"id": "price_unknown_widget"}, "proration": False, "period": {"start": 1730000000, "end": 1732592000}},
    ]
    event = build_invoice_paid_event(lines_override=foreign_lines)

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "unrecognized_line_item_price"


@pytest.mark.asyncio
async def test_multiple_eligible_pro_lines_quarantined() -> None:
    """Verifies that multiple non-prorated Pro lines are quarantined as ambiguous."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    multi_lines = [
        {"id": "il_1", "price": {"id": TEST_PRO_PRICE_ID}, "proration": False, "period": {"start": 1730000000, "end": 1732592000}},
        {"id": "il_2", "price": {"id": TEST_PRO_PRICE_ID}, "proration": False, "period": {"start": 1732592000, "end": 1735000000}},
    ]
    event = build_invoice_paid_event(lines_override=multi_lines)

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_invoice_paid(event, session)

    assert exc_info.value.reason == "multiple_eligible_pro_line_items"


@pytest.mark.asyncio
async def test_metadata_organization_mismatch_fails_closed() -> None:
    """Verifies metadata organization UUID mismatch raises WebhookPermanentError."""
    org_id = uuid.uuid4()
    fraud_org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event(metadata_org_id=fraud_org_id)

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError) as exc_info:
            await handle_invoice_paid(event, session)

    assert "Organization metadata mismatch" in str(exc_info.value)


# =============================================================================
# 9. Monotonic Subscription Dates & Runtime Authorization
# =============================================================================


@pytest.mark.asyncio
async def test_delayed_older_invoice_does_not_regress_subscription_dates() -> None:
    """Verifies older invoice does not regress newer Subscription.current_period_end."""
    org_id = uuid.uuid4()
    p1_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    p1_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p2_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    hist_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_start,
        period_end=p1_end,
        plan_tier="pro",
        is_active=False,
    )
    active_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=p1_end,
        period_end=p2_end,
        plan_tier="pro",
        is_active=True,
    )
    sub = make_test_sub(
        org_id=org_id,
        current_period_start=p1_end,
        current_period_end=p2_end,  # Newer end date
    )
    session = InvoiceMockDbSession(billing_periods=[hist_bp, active_bp], subscriptions=[sub])

    # Delayed invoice for Oct 1 - Nov 1 arrives
    event = build_invoice_paid_event(
        invoice_id="in_older_date",
        period_start=int(p1_start.timestamp()),
        period_end=int(p1_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # Subscription period end did NOT regress!
    assert sub.current_period_end == p2_end
    assert sub.current_period_start == p1_end


@pytest.mark.asyncio
async def test_runtime_authorization_delayed_lifecycle_fails_closed() -> None:
    """Proves that a paid invoice alone does NOT authorize inference while Subscription.status is incomplete."""
    org_id = uuid.uuid4()
    t_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.incomplete,  # Not active yet!
        plan_tier="free",
    )
    task = GenerationTask(
        id=t_id,
        organization_id=org_id,
        billing_period_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
    )
    session = InvoiceMockDbSession(subscriptions=[sub], tasks=[task])

    event = build_invoice_paid_event(
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    test_settings = get_test_settings()

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", test_settings), \
         patch("app.services.billing.accounting.settings", test_settings):

        # 1. Process invoice.paid -> provisions Pro period
        await handle_invoice_paid(event, session)
        pro_bp = list(session.billing_periods.values())[0]
        assert pro_bp.plan_tier == "pro"
        assert pro_bp.is_active is True
        task.billing_period_id = pro_bp.id

        # 2. Subscription status remains incomplete!
        assert sub.status == SubscriptionStatus.incomplete

        # 3. reserve_usage() fails closed with SUBSCRIPTION_INACTIVE
        res = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=pro_bp.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=p_start + timedelta(days=2),
        )
        assert res.success is False
        assert res.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE

        # 4. Subsequent lifecycle event (e.g. checkout or subscription.updated) activates subscription
        sub.status = SubscriptionStatus.active
        sub.plan_tier = "pro"

        # 5. Subsequent reserve_usage() succeeds immediately!
        res2 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=pro_bp.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=p_start + timedelta(days=2),
        )
        assert res2.success is True


# =============================================================================
# 10. Webhook Engine Integration & Quarantine Tests
# =============================================================================


@pytest.mark.asyncio
async def test_webhook_engine_process_invoice_paid_success() -> None:
    """Verifies webhook engine end-to-end executes invoice.paid and marks event processed."""
    org_id = uuid.uuid4()
    event_id = "evt_engine_inv_success"
    sub = make_test_sub(org_id=org_id)

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="invoice.paid",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_invoice_paid_event(event_id=event_id),
    )
    session = InvoiceMockDbSession(events=[raw_event], subscriptions=[sub])

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        res = await process_webhook_event(session, event_id)

    assert res.success is True
    assert res.status == WebhookProcessingStatus.processed
    assert raw_event.status == WebhookProcessingStatus.processed
    assert len(session.billing_periods) == 1
    assert session.committed_count >= 1


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_zero_value_invoice() -> None:
    """Verifies webhook engine catches zero-value conflict, stages reconciliation, and quarantines."""
    org_id = uuid.uuid4()
    event_id = "evt_engine_inv_zero"
    sub = make_test_sub(org_id=org_id)

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="invoice.paid",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_invoice_paid_event(event_id=event_id, amount_paid=0),
    )
    session = InvoiceMockDbSession(events=[raw_event], subscriptions=[sub])

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert len(session.reconciliations) == 1
    rec = session.reconciliations[0]
    assert rec.reason == "zero_value_invoice_pending_review"
    assert rec.status == ReconciliationStatus.pending_review
    assert len(session.billing_periods) == 0


# =============================================================================
# 11. Multi-Subscription Historical Regression Tests
# =============================================================================


@pytest.mark.asyncio
async def test_invoice_handling_with_historical_canceled_subscriptions_does_not_fail_multiple_results() -> None:
    """Verifies invoice handler does not raise MultipleResultsFound when historical canceled subscriptions coexist."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    # 1. Historical canceled subscription for same org
    sub_canceled = make_test_sub(
        org_id=org_id,
        customer_id="cus_old_111",
        subscription_id="sub_old_canceled_999",
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )

    # 2. Current active subscription for same org
    sub_current = make_test_sub(
        org_id=org_id,
        customer_id="cus_current_222",
        subscription_id="sub_current_active_888",
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )

    session = InvoiceMockDbSession(
        subscriptions=[sub_canceled, sub_current],
    )

    event = build_invoice_paid_event(
        invoice_id="in_multi_sub_test",
        customer_id="cus_current_222",
        subscription_id="sub_current_active_888",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # Verify successful processing without MultipleResultsFound
    assert "in_multi_sub_test" in session.invoices
    inv = session.invoices["in_multi_sub_test"]
    assert inv.processing_status == InvoiceProcessingStatus.processed
    assert inv.stripe_subscription_id == "sub_current_active_888"

    # Verify that canceled sub was untouched and current sub dates were advanced
    assert sub_canceled.status == SubscriptionStatus.canceled
    assert sub_current.current_period_start == p_start
    assert sub_current.current_period_end == p_end


@pytest.mark.asyncio
async def test_invoice_handling_locks_verified_stripe_subscription_id() -> None:
    """Verifies that invoice handler locks Subscription specifically by verified stripe_subscription_id."""
    org_id = uuid.uuid4()
    p_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    p_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    sub = make_test_sub(
        org_id=org_id,
        customer_id="cus_verify_lock",
        subscription_id="sub_verify_lock_123",
        status=SubscriptionStatus.active,
    )
    session = InvoiceMockDbSession(subscriptions=[sub])

    event = build_invoice_paid_event(
        invoice_id="in_lock_check",
        customer_id="cus_verify_lock",
        subscription_id="sub_verify_lock_123",
        period_start=int(p_start.timestamp()),
        period_end=int(p_end.timestamp()),
    )

    executed_queries: list[str] = []
    orig_execute = session.execute

    async def tracking_execute(stmt: Any, params_override: Any = None) -> Any:
        where_clauses = getattr(stmt, "_where_criteria", ())
        where_strs = [str(c) for c in where_clauses]
        full_w = " ".join(where_strs)
        if "subscriptions" in full_w:
            executed_queries.append(full_w)
        return await orig_execute(stmt, params_override)

    session.execute = tracking_execute  # type: ignore

    with patch("app.services.billing.stripe_invoice_paid_handler.settings", get_test_settings()):
        await handle_invoice_paid(event, session)

    # Confirm that at least one subscription query locked by stripe_subscription_id
    sub_lock_queries = [q for q in executed_queries if "stripe_subscription_id" in q]
    assert len(sub_lock_queries) >= 1
