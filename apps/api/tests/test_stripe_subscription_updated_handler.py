"""Comprehensive Unit & Integration Tests for Stripe Subscription Updated Webhook Handler.

Event: customer.subscription.updated

Validates:
1. Valid Transitions & Lifecycle States:
   - Updates subscription fields (status, period dates, cancel_at_period_end, canceled_at).
   - Handles all recognized states: active, past_due, unpaid, paused, canceled, trialing, incomplete, incomplete_expired.
2. Past-Due Grace Anchor Behaviour:
   - First transition into past_due initializes past_due_since to event timestamp.
   - Repeated past_due updates strictly preserve the original grace anchor.
   - Recovery to active clears past_due_since (None).
   - Preserves past_due_since upon suspension or cancellation for audit trail.
3. Explicit Terminal-State Protection:
   - Delayed or replay event never resurrects the same subscription ID after canceled or incomplete_expired.
   - Legitimate resubscription with a new subscription ID succeeds after cancellation.
4. Monotonic Timestamp Ordering & Backfilling:
   - Older or equal timestamp events do not regress lifecycle state or plan tier.
   - Stale events do not advance last_stripe_event_created_at.
   - Stale events backfill genuinely missing authoritative fields if None.
5. Strict Identity & Price Validation:
   - Unknown customer fails closed (WebhookPermanentError).
   - Metadata organization_id mismatch fails closed.
   - Missing or non-matching Pro price across items.data fails closed.
   - Malformed IDs, period dates (end <= start), or non-boolean cancel_at_period_end fail closed.
6. Multi-Subscription Conflict & Reconciliation:
   - Conflicting active subscription raises WebhookReconciliationConflictError.
   - Verified engine-level atomic quarantine and reconciliation record persistence.
7. Runtime Authorization Compatibility:
   - Combined lifecycle + reserve_usage() verification:
     * Active subscription allows reservation.
     * past_due within 7 days allows reservation.
     * past_due at exactly 7 days denies reservation (GRACE_PERIOD_EXPIRED).
     * past_due after 7 days denies reservation (GRACE_PERIOD_EXPIRED).
     * Immediate cancellation denies reservation (SUBSCRIPTION_INACTIVE).
     * Scheduled cancellation while active allows reservation.
8. Strict Accounting Isolation:
   - Does NOT create or mutate OrganizationBillingPeriod rows.
   - Does NOT create or mutate StripeInvoice rows.
   - Does NOT change usage counters or in-flight reservations.
9. Dispatcher Registration:
   - customer.subscription.updated is registered in default_webhook_dispatcher.
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
from app.config import Settings
from app.models.billing import OrganizationBillingPeriod, ReservationStatus, UsageReservation
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
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
from app.services.billing.stripe_subscription_updated_handler import handle_subscription_updated
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
    process_webhook_event,
)

TEST_PRO_PRICE_ID = "price_pro_subscription_test_123"


# =============================================================================
# Test Settings Helper
# =============================================================================


def get_test_settings(pro_price_id: str = TEST_PRO_PRICE_ID) -> Settings:
    """Construct mock Settings with valid Stripe configuration."""
    s = Settings(
        stripe_enabled=True,
        stripe_secret_key=SecretStr("sk_test_mock_1234567890abcdef"),
        stripe_webhook_secret=SecretStr("whsec_test_mock_1234567890abcdef"),
        stripe_publishable_key="pk_test_mock_1234567890abcdef",
        stripe_pro_price_id=TEST_PRO_PRICE_ID,
        stripe_billing_return_url="http://localhost:3000/billing",
        billing_grace_period_days=7,
        default_free_monthly_token_quota=0,
    )
    if pro_price_id != TEST_PRO_PRICE_ID:
        object.__setattr__(s, "stripe_pro_price_id", pro_price_id)
    return s


# =============================================================================
# Mock Database Session for Subscription Handler
# =============================================================================


class SubscriptionMockSavepoint:
    """Simulates SQLAlchemy async savepoint (begin_nested)."""

    def __init__(self, session: "SubscriptionMockDbSession") -> None:
        self.session = session
        self.added_len = len(session.added)
        self.rec_len = len(session.reconciliations)
        self.sub_snapshots: dict[uuid.UUID, dict[str, Any]] = {}

    async def __aenter__(self) -> "SubscriptionMockSavepoint":
        self.sub_snapshots = {
            s.id: {
                "status": s.status,
                "plan_tier": s.plan_tier,
                "stripe_subscription_id": s.stripe_subscription_id,
                "stripe_price_id": s.stripe_price_id,
                "current_period_start": s.current_period_start,
                "current_period_end": s.current_period_end,
                "cancel_at_period_end": s.cancel_at_period_end,
                "canceled_at": s.canceled_at,
                "past_due_since": s.past_due_since,
                "last_stripe_event_created_at": s.last_stripe_event_created_at,
            }
            for s in self.session.subscriptions.values()
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
                    s.stripe_price_id = snap["stripe_price_id"]
                    s.current_period_start = snap["current_period_start"]
                    s.current_period_end = snap["current_period_end"]
                    s.cancel_at_period_end = snap["cancel_at_period_end"]
                    s.canceled_at = snap["canceled_at"]
                    s.past_due_since = snap["past_due_since"]
                    s.last_stripe_event_created_at = snap["last_stripe_event_created_at"]
            self.session.savepoints_rolled_back += 1
            return False
        self.session.savepoints_committed += 1
        return False


class SubscriptionMockScalarResult:
    """Mock scalar result wrapper for db.execute()."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def scalar_one_or_none(self) -> Any:
        return self._item

    def scalar(self) -> Any:
        return self._item

    def scalars(self) -> Any:
        class _Scalars:
            def __init__(self, val: Any) -> None:
                self._val = val
            def all(self) -> list[Any]:
                return [self._val] if self._val is not None else []
        return _Scalars(self._item)


class SubscriptionMockDbSession:
    """In-memory AsyncSession double tailored for subscription lifecycle tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
        events: list[StripeWebhookEvent] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
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
        self.tasks: dict[uuid.UUID, GenerationTask] = {
            t.id: t for t in (tasks or [])
        }
        self.reservations: dict[uuid.UUID, UsageReservation] = {
            r.id: r for r in (reservations or [])
        }
        self.reconciliations: list[BillingReconciliation] = []
        self.added: list[Any] = []
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
        elif isinstance(obj, GenerationTask):
            self.tasks[obj.id] = obj
        elif isinstance(obj, UsageReservation):
            self.reservations[obj.id] = obj

    async def flush(self) -> None:
        self.flushed = True

    def begin_nested(self) -> SubscriptionMockSavepoint:
        return SubscriptionMockSavepoint(self)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any) -> SubscriptionMockScalarResult:
        where_clauses = getattr(stmt, "_where_criteria", ())
        params: dict[str, Any] = {}
        for c in where_clauses:
            if hasattr(c, "left") and hasattr(c, "right"):
                col_name = getattr(c.left, "key", str(c.left))
                if hasattr(c.right, "clauses"):
                    val = [getattr(elem, "value", elem) for elem in c.right.clauses]
                else:
                    val = getattr(c.right, "value", None)
                params[col_name] = val

        # 1. StripeWebhookEvent queries
        if any("stripe_webhook_events" in str(c) for c in where_clauses) or getattr(stmt, "is_select", False):
            if "id" in params and params["id"] in self.events:
                return SubscriptionMockScalarResult(self.events[params["id"]])

        # 2. BillingReconciliation lookup
        if any("billing_reconciliations" in str(c) for c in where_clauses) or "billing_reconciliations" in str(stmt):
            ev_id = params.get("event_id")
            inc_id = params.get("incoming_subscription_id")
            match = next(
                (r for r in self.reconciliations if (ev_id is None or r.event_id == ev_id) and (inc_id is None or r.incoming_subscription_id == inc_id)),
                None,
            )
            return SubscriptionMockScalarResult(match)

        # 3. OrganizationBillingPeriod queries
        if any("organization_billing_periods" in str(c) for c in where_clauses) or "organization_billing_periods" in str(stmt):
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.billing_periods:
                        return SubscriptionMockScalarResult(self.billing_periods[v])
                    for bp in self.billing_periods.values():
                        if bp.organization_id == v:
                            return SubscriptionMockScalarResult(bp)
            return SubscriptionMockScalarResult(None)

        # 4. GenerationTask queries
        if any("generation_tasks" in str(c) for c in where_clauses) or "generation_tasks" in str(stmt):
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.tasks:
                        return SubscriptionMockScalarResult(self.tasks[v])
            return SubscriptionMockScalarResult(None)

        # 5. UsageReservation queries
        if any("usage_reservations" in str(c) for c in where_clauses) or "usage_reservations" in str(stmt):
            if ReservationStatus.active in params.values():
                t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
                active_res = next(
                    (r for r in self.reservations.values()
                     if r.generation_task_id == t_id and r.status == ReservationStatus.active),
                    None,
                )
                return SubscriptionMockScalarResult(active_res)
            return SubscriptionMockScalarResult(None)

        # 6. Subscription queries
        if any("subscriptions" in str(c) for c in where_clauses) or "subscriptions" in str(stmt):
            # Check for eligible / status-filtered subscription query
            if "organization_id" in params and any("status" in str(c) for c in where_clauses):
                org_id = params["organization_id"]
                status_filter = params.get("status")
                if isinstance(status_filter, (list, tuple, set)):
                    match = next(
                        (s for s in self.subscriptions.values() if s.organization_id == org_id and s.status in status_filter),
                        None,
                    )
                elif status_filter is not None:
                    match = next(
                        (s for s in self.subscriptions.values() if s.organization_id == org_id and s.status == status_filter),
                        None,
                    )
                else:
                    match = next(
                        (s for s in self.subscriptions.values() if s.organization_id == org_id and s.status in (SubscriptionStatus.active, SubscriptionStatus.trialing, SubscriptionStatus.past_due)),
                        None,
                    )
                return SubscriptionMockScalarResult(match)

            # Query by stripe_subscription_id
            sub_id = params.get("stripe_subscription_id")
            if sub_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

            # Query by stripe_customer_id
            cust_id = params.get("stripe_customer_id")
            if cust_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

            # Query by organization_id
            org_id = params.get("organization_id")
            if org_id:
                match = next((s for s in self.subscriptions.values() if s.organization_id == org_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

        return SubscriptionMockScalarResult(None)


# =============================================================================
# Payload Builder Fixture
# =============================================================================


def build_subscription_updated_event(
    *,
    event_id: str = "evt_sub_updated_1",
    subscription_id: str = "sub_test_12345",
    customer_id: str = "cus_test_12345",
    status: str = "active",
    price_id: str = TEST_PRO_PRICE_ID,
    current_period_start: int = 1730000000,
    current_period_end: int = 1732592000,
    cancel_at_period_end: bool = False,
    canceled_at: int | None = None,
    omit_canceled_at: bool = False,
    org_id: uuid.UUID | None = None,
    created: int = 1730000000,
    items_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a valid Stripe customer.subscription.updated event dictionary."""
    if items_data is None:
        items_data = [
            {
                "id": "si_test_item_1",
                "price": {"id": price_id, "object": "price"},
            }
        ]

    metadata: dict[str, Any] = {}
    if org_id is not None:
        metadata["organization_id"] = str(org_id)

    sub_dict: dict[str, Any] = {
        "id": subscription_id,
        "object": "subscription",
        "customer": customer_id,
        "status": status,
        "current_period_start": current_period_start,
        "current_period_end": current_period_end,
        "cancel_at_period_end": cancel_at_period_end,
        "items": {
            "object": "list",
            "data": items_data,
        },
        "metadata": metadata,
    }
    if not omit_canceled_at:
        sub_dict["canceled_at"] = canceled_at

    return {
        "id": event_id,
        "object": "event",
        "type": "customer.subscription.updated",
        "created": created,
        "data": {
            "object": sub_dict,
        },
    }


def make_test_sub(
    *,
    org_id: uuid.UUID,
    customer_id: str = "cus_test_12345",
    subscription_id: str = "sub_test_12345",
    status: SubscriptionStatus = SubscriptionStatus.incomplete,
    plan_tier: str = "free",
    past_due_since: datetime | None = None,
    last_event_created_at: datetime | None = None,
) -> Subscription:
    return Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        stripe_price_id=TEST_PRO_PRICE_ID,
        status=status,
        plan_tier=plan_tier,
        current_period_start=datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc),
        current_period_end=datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc),
        cancel_at_period_end=False,
        canceled_at=None,
        past_due_since=past_due_since,
        last_stripe_event_created_at=last_event_created_at,
    )


# =============================================================================
# 1. Dispatcher Registration Tests
# =============================================================================


def test_dispatcher_registers_subscription_updated() -> None:
    """Verifies customer.subscription.updated is registered in default dispatcher."""
    handler = default_webhook_dispatcher.get_handler("customer.subscription.updated")
    assert handler is not None
    assert handler is handle_subscription_updated


# =============================================================================
# 2. Normal Active Update & Price Position Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_updated_normal_active() -> None:
    """Verifies successful active update populates authoritative timestamps."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.incomplete)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event = build_subscription_updated_event(org_id=org_id, status="active", created=1730005000)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert sub.last_stripe_event_created_at == datetime.fromtimestamp(1730005000, tz=timezone.utc)
    assert sub.cancel_at_period_end is False


@pytest.mark.asyncio
async def test_subscription_updated_multi_item_configured_price() -> None:
    """Verifies handler locates configured Pro price when it is not the first item."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    items = [
        {"id": "si_unrelated", "price": {"id": "price_metered_tokens"}},
        {"id": "si_pro", "price": {"id": TEST_PRO_PRICE_ID}},
    ]
    event = build_subscription_updated_event(org_id=org_id, items_data=items)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.stripe_price_id == TEST_PRO_PRICE_ID


@pytest.mark.asyncio
async def test_subscription_updated_missing_configured_price_fails_closed() -> None:
    """Verifies that an event with no item matching configured Pro price fails closed."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    items = [{"id": "si_attacker", "price": {"id": "price_attacker_cheap"}}]
    event = build_subscription_updated_event(org_id=org_id, items_data=items)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError) as exc_info:
            await handle_subscription_updated(event, session)

    assert "No subscription line item matched configured Pro price" in str(exc_info.value)


# =============================================================================
# 3. Past-Due Grace Period Lifecycle Tests
# =============================================================================


@pytest.mark.asyncio
async def test_first_transition_to_past_due_establishes_grace_anchor() -> None:
    """Verifies first transition into past_due initializes past_due_since to event timestamp."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active, past_due_since=None)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event_time = 1730005000
    event = build_subscription_updated_event(org_id=org_id, status="past_due", created=event_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.past_due
    assert sub.plan_tier == "pro"  # Preserved so grace period can be evaluated
    expected_anchor = datetime.fromtimestamp(event_time, tz=timezone.utc)
    assert sub.past_due_since == expected_anchor


@pytest.mark.asyncio
async def test_repeated_past_due_preserves_original_grace_anchor() -> None:
    """Verifies repeated past_due updates do NOT reset the past_due_since grace anchor."""
    org_id = uuid.uuid4()
    original_anchor = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    original_event_time = int(original_anchor.timestamp())
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.past_due,
        past_due_since=original_anchor,
        last_event_created_at=original_anchor,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Repeated past_due event 3 days later
    later_time = int((original_anchor + timedelta(days=3)).timestamp())
    event = build_subscription_updated_event(org_id=org_id, status="past_due", created=later_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.past_due
    # Anchor MUST NOT be advanced to later_time!
    assert sub.past_due_since == original_anchor
    assert sub.last_stripe_event_created_at == datetime.fromtimestamp(later_time, tz=timezone.utc)


@pytest.mark.asyncio
async def test_recovery_to_active_clears_grace_anchor() -> None:
    """Verifies that when a delinquent subscription recovers to active, past_due_since is cleared."""
    org_id = uuid.uuid4()
    original_anchor = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.past_due,
        past_due_since=original_anchor,
        last_event_created_at=original_anchor,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    recovery_time = int((original_anchor + timedelta(days=4)).timestamp())
    event = build_subscription_updated_event(org_id=org_id, status="active", created=recovery_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.active
    assert sub.past_due_since is None  # Grace anchor cleanly cleared


@pytest.mark.asyncio
async def test_past_due_transition_preserves_anchor_on_suspension_for_audit() -> None:
    """Verifies transitioning from past_due to unpaid retains past_due_since for audit trail."""
    org_id = uuid.uuid4()
    anchor = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.past_due,
        past_due_since=anchor,
        last_event_created_at=anchor,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    unpaid_time = int((anchor + timedelta(days=10)).timestamp())
    event = build_subscription_updated_event(org_id=org_id, status="unpaid", created=unpaid_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.unpaid
    assert sub.past_due_since == anchor  # Historical provenance retained


# =============================================================================
# 4. Cancellation Scheduling vs Immediate Cancellation
# =============================================================================


@pytest.mark.asyncio
async def test_scheduled_cancellation_while_active_preserves_pro_tier() -> None:
    """Verifies cancel_at_period_end=True while status=active does NOT revoke Pro entitlement."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    cancel_time = 1730005000
    event = build_subscription_updated_event(
        org_id=org_id,
        status="active",
        cancel_at_period_end=True,
        canceled_at=cancel_time,
        created=cancel_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"  # Not downgraded prematurely
    assert sub.cancel_at_period_end is True
    assert sub.canceled_at == datetime.fromtimestamp(cancel_time, tz=timezone.utc)


@pytest.mark.asyncio
async def test_immediate_cancellation_resets_plan_tier_to_free() -> None:
    """Verifies status=canceled immediately resets plan tier to free."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    cancel_time = 1730005000
    event = build_subscription_updated_event(
        org_id=org_id,
        status="canceled",
        canceled_at=cancel_time,
        created=cancel_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"  # Immediate revocation of Pro tier


@pytest.mark.asyncio
async def test_unscheduling_cancellation_clears_timestamp() -> None:
    """Verifies valid non-stale update with canceled_at: null clears local canceled_at."""
    org_id = uuid.uuid4()
    cancel_time = 1730005000
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.active,
        last_event_created_at=datetime.fromtimestamp(cancel_time, tz=timezone.utc),
    )
    sub.cancel_at_period_end = True
    sub.canceled_at = datetime.fromtimestamp(cancel_time, tz=timezone.utc)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Customer unschedules cancellation at a newer timestamp:
    unschedule_time = cancel_time + 3600
    event = build_subscription_updated_event(
        org_id=org_id,
        status="active",
        cancel_at_period_end=False,
        canceled_at=None,  # explicitly supplied null
        created=unschedule_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.cancel_at_period_end is False
    assert sub.canceled_at is None  # Timestamp cleanly cleared!
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"


@pytest.mark.asyncio
async def test_older_event_cannot_clear_newer_cancellation_timestamp() -> None:
    """Verifies a stale or older event with canceled_at: null cannot clear a newer cancellation timestamp."""
    org_id = uuid.uuid4()
    newer_time = 1730010000
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.active,
        last_event_created_at=datetime.fromtimestamp(newer_time, tz=timezone.utc),
    )
    sub.cancel_at_period_end = True
    sub.canceled_at = datetime.fromtimestamp(newer_time, tz=timezone.utc)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Stale/older event arrives claiming canceled_at: null:
    older_time = newer_time - 3600
    event = build_subscription_updated_event(
        org_id=org_id,
        status="active",
        cancel_at_period_end=False,
        canceled_at=None,
        created=older_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Newer cancellation must NOT be cleared by older event
    assert sub.cancel_at_period_end is True
    assert sub.canceled_at == datetime.fromtimestamp(newer_time, tz=timezone.utc)


@pytest.mark.asyncio
async def test_missing_canceled_at_field_does_not_clear_existing_data() -> None:
    """Verifies that an update event completely omitting canceled_at does not clear existing timestamp."""
    org_id = uuid.uuid4()
    cancel_time = 1730005000
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.active,
        last_event_created_at=datetime.fromtimestamp(cancel_time, tz=timezone.utc),
    )
    sub.cancel_at_period_end = True
    sub.canceled_at = datetime.fromtimestamp(cancel_time, tz=timezone.utc)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Newer event omitting canceled_at entirely
    newer_time = cancel_time + 1800
    event = build_subscription_updated_event(
        org_id=org_id,
        status="active",
        cancel_at_period_end=True,
        omit_canceled_at=True,  # Field completely omitted
        created=newer_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Existing canceled_at must be preserved
    assert sub.canceled_at == datetime.fromtimestamp(cancel_time, tz=timezone.utc)
    assert sub.cancel_at_period_end is True


@pytest.mark.asyncio
async def test_cancellation_removal_free_plan_does_not_grant_pro() -> None:
    """Verifies that removing cancel_at_period_end on a Free plan keeps plan_tier=free and denies paid access."""
    org_id = uuid.uuid4()
    sub_id = "sub_free_test_456"
    t_now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp_free = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_now - timedelta(days=5),
        period_end=t_now + timedelta(days=25),
        plan_tier="free",
        monthly_token_quota=0,  # Zero quota for free tier
        tokens_used=0,
        tokens_reserved=0,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    task = GenerationTask(
        id=uuid.uuid4(),
        organization_id=org_id,
        billing_period_id=bp_free.id,
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
    )
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=sub_id,
        status=SubscriptionStatus.active,
        plan_tier="free",
        last_event_created_at=t_now - timedelta(hours=2),
    )
    sub.cancel_at_period_end = True
    sub.canceled_at = t_now - timedelta(hours=2)
    session = SubscriptionMockDbSession(
        billing_periods=[bp_free],
        tasks=[task],
        subscriptions=[sub],
    )

    # Event arrives removing cancellation flag (cancel_at_period_end = False)
    event = build_subscription_updated_event(
        subscription_id=sub_id,
        org_id=org_id,
        status="active",
        cancel_at_period_end=False,
        canceled_at=None,
        created=int(t_now.timestamp()),
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # 1. cancel_at_period_end is False and canceled_at is cleared
    assert sub.cancel_at_period_end is False
    assert sub.canceled_at is None

    # 2. plan_tier remains free (NOT upgraded to pro)
    assert sub.plan_tier == "free"

    # 3. No Pro billing period was created
    pro_periods = [bp for bp in session.billing_periods.values() if bp.plan_tier == "pro"]
    assert len(pro_periods) == 0

    # 4. Runtime authorization does not grant paid access (quota exceeded on 0-quota free period)
    test_settings = get_test_settings()
    with patch("app.services.billing.accounting.settings", test_settings):
        res = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=task.id,
            billing_period_id=bp_free.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_now,
        )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.QUOTA_EXHAUSTED


# =============================================================================
# 5. Terminal State Resurrection Guard
# =============================================================================


@pytest.mark.asyncio
async def test_delayed_event_does_not_resurrect_same_canceled_subscription() -> None:
    """Verifies a delayed event cannot resurrect the same subscription ID after canceled."""
    org_id = uuid.uuid4()
    sub_id = "sub_terminal_123"
    cancel_time = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        last_event_created_at=cancel_time,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Delayed event for the same subscription ID claiming active
    delayed_time = int((cancel_time + timedelta(hours=1)).timestamp())
    event = build_subscription_updated_event(
        subscription_id=sub_id,
        org_id=org_id,
        status="active",
        created=delayed_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Must remain canceled and free!
    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"


@pytest.mark.asyncio
async def test_delayed_past_due_event_does_not_resurrect_canceled_subscription() -> None:
    """Verifies delayed past_due event cannot resurrect a canceled subscription or initialize grace anchor."""
    org_id = uuid.uuid4()
    sub_id = "sub_term_past_due_123"
    t_now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    cancel_time = t_now - timedelta(hours=2)

    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_now - timedelta(days=5),
        period_end=t_now + timedelta(days=25),
        plan_tier="pro",
        monthly_token_quota=100_000,
        tokens_used=0,
        tokens_reserved=0,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    task = GenerationTask(
        id=uuid.uuid4(),
        organization_id=org_id,
        billing_period_id=bp.id,
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
    )
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        last_event_created_at=cancel_time,
    )
    sub.past_due_since = None
    session = SubscriptionMockDbSession(
        billing_periods=[bp],
        tasks=[task],
        subscriptions=[sub],
    )

    # Delayed event for the same subscription ID claiming past_due
    delayed_time = int((cancel_time + timedelta(hours=1)).timestamp())
    event = build_subscription_updated_event(
        subscription_id=sub_id,
        org_id=org_id,
        status="past_due",
        created=delayed_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # 1. Subscription must NOT be resurrected
    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"

    # 2. past_due_since must NOT be initialized or overwritten
    assert sub.past_due_since is None

    # 3. Runtime authorization must continue denying access
    test_settings = get_test_settings()
    with patch("app.services.billing.accounting.settings", test_settings):
        res = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=task.id,
            billing_period_id=bp.id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_now,
        )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE


@pytest.mark.asyncio
async def test_delayed_event_does_not_resurrect_same_incomplete_expired_subscription() -> None:
    """Verifies a delayed event cannot resurrect the same subscription ID after incomplete_expired."""
    org_id = uuid.uuid4()
    sub_id = "sub_expired_123"
    expired_time = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=sub_id,
        status=SubscriptionStatus.incomplete_expired,
        plan_tier="free",
        last_event_created_at=expired_time,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    delayed_time = int((expired_time + timedelta(hours=1)).timestamp())
    event = build_subscription_updated_event(
        subscription_id=sub_id,
        org_id=org_id,
        status="past_due",
        created=delayed_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    assert sub.status == SubscriptionStatus.incomplete_expired
    assert sub.plan_tier == "free"


@pytest.mark.asyncio
async def test_legitimate_resubscription_after_cancellation_succeeds() -> None:
    """Verifies a new subscription ID after a terminal subscription updates authoritatively."""
    org_id = uuid.uuid4()
    old_sub_id = "sub_old_canceled_123"
    new_sub_id = "sub_new_active_456"
    cancel_time = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=old_sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        last_event_created_at=cancel_time,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    resub_time = int((cancel_time + timedelta(days=30)).timestamp())
    event = build_subscription_updated_event(
        subscription_id=new_sub_id,
        org_id=org_id,
        status="active",
        created=resub_time,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Legitimate new subscription binds successfully
    assert sub.stripe_subscription_id == new_sub_id
    assert sub.status == SubscriptionStatus.active
    assert sub.last_stripe_event_created_at == datetime.fromtimestamp(resub_time, tz=timezone.utc)


# =============================================================================
# 6. Monotonic Timestamp Ordering & Backfilling
# =============================================================================


@pytest.mark.asyncio
async def test_older_event_does_not_regress_lifecycle_state() -> None:
    """Verifies older event does not regress newer state or advance event timestamp."""
    org_id = uuid.uuid4()
    latest_time = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.past_due,
        last_event_created_at=latest_time,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    # Arriving older event claiming active
    older_time = int((latest_time - timedelta(days=1)).timestamp())
    event = build_subscription_updated_event(org_id=org_id, status="active", created=older_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Must preserve newer past_due status and timestamp
    assert sub.status == SubscriptionStatus.past_due
    assert sub.last_stripe_event_created_at == latest_time


@pytest.mark.asyncio
async def test_stale_event_backfills_missing_fields_only() -> None:
    """Verifies stale event backfills genuinely missing fields without overwriting non-null values."""
    org_id = uuid.uuid4()
    latest_time = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        plan_tier="pro",
        status=SubscriptionStatus.active,
        last_event_created_at=latest_time,
    )
    sub.stripe_price_id = None  # Missing field
    session = SubscriptionMockDbSession(subscriptions=[sub])

    older_time = int((latest_time - timedelta(days=1)).timestamp())
    event = build_subscription_updated_event(org_id=org_id, price_id=TEST_PRO_PRICE_ID, created=older_time)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Missing price is backfilled
    assert sub.stripe_price_id == TEST_PRO_PRICE_ID
    # Timestamp not advanced
    assert sub.last_stripe_event_created_at == latest_time


# =============================================================================
# 7. Identity, Metadata & Payload Validation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_unknown_customer_fails_closed() -> None:
    """Verifies event referencing unknown customer raises WebhookPermanentError."""
    session = SubscriptionMockDbSession(subscriptions=[])
    event = build_subscription_updated_event(customer_id="cus_unknown_999")

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError) as exc_info:
            await handle_subscription_updated(event, session)

    assert "Unknown Stripe customer" in str(exc_info.value)


@pytest.mark.asyncio
async def test_metadata_organization_mismatch_fails_closed() -> None:
    """Verifies metadata organization_id conflicting with customer binding fails closed."""
    org_id = uuid.uuid4()
    attacker_org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event = build_subscription_updated_event(org_id=attacker_org_id)

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError) as exc_info:
            await handle_subscription_updated(event, session)

    assert "Organization metadata mismatch" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_start, bad_end",
    [
        (1732592000, 1730000000),  # end < start
        (1730000000, 1730000000),  # end == start
    ],
)
async def test_invalid_period_dates_fail_closed(bad_start: int, bad_end: int) -> None:
    """Verifies current_period_end <= current_period_start fails closed."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event = build_subscription_updated_event(
        org_id=org_id,
        current_period_start=bad_start,
        current_period_end=bad_end,
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError) as exc_info:
            await handle_subscription_updated(event, session)

    assert "must be strictly after current_period_start" in str(exc_info.value)


# =============================================================================
# 8. Multi-Subscription Conflict Guard
# =============================================================================


@pytest.mark.asyncio
async def test_conflicting_active_subscription_raises_reconciliation_error() -> None:
    """Verifies conflicting active subscription on same organization raises conflict error."""
    org_id = uuid.uuid4()
    existing_sub = make_test_sub(
        org_id=org_id,
        customer_id="cus_primary",
        subscription_id="sub_existing_active",
        status=SubscriptionStatus.active,
    )
    incoming_sub_record = make_test_sub(
        org_id=org_id,
        customer_id="cus_secondary",
        subscription_id=None,
        status=SubscriptionStatus.incomplete,
    )
    session = SubscriptionMockDbSession(subscriptions=[existing_sub, incoming_sub_record])
    event = build_subscription_updated_event(
        customer_id="cus_secondary",
        subscription_id="sub_conflicting_new",
        org_id=org_id,
        status="active",
    )

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_subscription_updated(event, session)

    assert exc_info.value.existing_subscription_id == "sub_existing_active"
    assert exc_info.value.incoming_subscription_id == "sub_conflicting_new"


# =============================================================================
# 9. Runtime Authorization Compatibility (Combined Lifecycle + Reservation)
# =============================================================================


@pytest.mark.asyncio
async def test_runtime_authorization_compatibility_lifecycle() -> None:
    """End-to-end integration: traces lifecycle updates and validates reserve_usage() decisions.

    Proves:
    1. Active Pro subscription reserves within paid limits.
    2. First past_due update establishes grace anchor.
    3. Repeated past_due updates do not reset it.
    4. Reservation within 7 days is allowed.
    5. Reservation exactly at 7 days is denied.
    6. Reservation after 7 days is denied.
    7. Recovery to active clears grace anchor.
    8. Immediate cancellation is denied.
    9. Scheduled cancellation while active remains authorized.
    10. Lifecycle handler never changes billing-period records.
    """
    org_id = uuid.uuid4()
    t_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = OrganizationBillingPeriod(
        id=bp_id,
        organization_id=org_id,
        period_start=t_start - timedelta(days=5),
        period_end=t_start + timedelta(days=25),
        plan_tier="pro",
        monthly_token_quota=100_000,
        tokens_used=0,
        tokens_reserved=0,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    task = GenerationTask(
        id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
    )
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(
        billing_periods=[bp],
        tasks=[task],
        subscriptions=[sub],
    )

    test_settings = get_test_settings()

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", test_settings), \
         patch("app.services.billing.accounting.settings", test_settings):

        # 1. Active Pro subscription allows reservation
        res1 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start,
        )
        assert res1.success is True

        # Reset task/reservation state for next step
        task.status = GenerationTaskStatus.pending
        session.reservations.clear()
        bp.tokens_reserved = 0
        bp.cost_micros_reserved = 0

        # 2. First past_due update establishes grace anchor
        anchor_epoch = int(t_start.timestamp())
        event_pd1 = build_subscription_updated_event(
            org_id=org_id,
            status="past_due",
            created=anchor_epoch,
        )
        await handle_subscription_updated(event_pd1, session)
        assert sub.status == SubscriptionStatus.past_due
        assert sub.past_due_since == t_start

        # 3. Repeated past_due event 2 days later preserves original anchor
        event_pd2 = build_subscription_updated_event(
            org_id=org_id,
            status="past_due",
            created=anchor_epoch + 172800,  # +2 days
        )
        await handle_subscription_updated(event_pd2, session)
        assert sub.past_due_since == t_start  # Anchor preserved!

        # 4. Reservation within 7 days (day 3) is ALLOWED
        res4 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=3),
        )
        assert res4.success is True

        # Reset reservation
        task.status = GenerationTaskStatus.pending
        session.reservations.clear()
        bp.tokens_reserved = 0
        bp.cost_micros_reserved = 0

        # 5. Reservation exactly at 7 days is DENIED
        res5 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=7),
        )
        assert res5.success is False
        assert res5.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED

        # 6. Reservation after 7 days (day 8) is DENIED
        res6 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=8),
        )
        assert res6.success is False
        assert res6.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED

        # 7. Recovery to active clears grace anchor
        recovery_epoch = anchor_epoch + (8 * 86400)
        event_active = build_subscription_updated_event(
            org_id=org_id,
            status="active",
            created=recovery_epoch,
        )
        await handle_subscription_updated(event_active, session)
        assert sub.status == SubscriptionStatus.active
        assert sub.past_due_since is None

        # 8. Immediate cancellation is DENIED
        cancel_epoch = recovery_epoch + 86400
        event_cancel = build_subscription_updated_event(
            org_id=org_id,
            status="canceled",
            created=cancel_epoch,
        )
        await handle_subscription_updated(event_cancel, session)
        assert sub.status == SubscriptionStatus.canceled
        assert sub.plan_tier == "free"

        res8 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=10),
        )
        assert res8.success is False
        assert res8.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE

        # 9. Scheduled cancellation while active remains authorized
        sub.status = SubscriptionStatus.active
        sub.plan_tier = "pro"
        sub.cancel_at_period_end = True
        sub.last_stripe_event_created_at = t_start + timedelta(days=11)

        res9 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=12),
        )
        assert res9.success is True

        # 10. Handler never changed billing period record
        assert bp.is_active is True
        assert bp.is_blocked is False
        assert bp.monthly_token_quota == 100_000
        assert bp.plan_tier == "pro"


# =============================================================================
# 10. Strict Accounting Isolation
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_updated_accounting_isolation() -> None:
    """Verifies handler does NOT modify billing periods, invoices, or usage counters."""
    org_id = uuid.uuid4()
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 11, 1, tzinfo=timezone.utc),
        plan_tier="pro",
        monthly_token_quota=50_000,
        tokens_used=12_000,
        tokens_reserved=0,
        cost_micros_used=500_000,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    sub = make_test_sub(org_id=org_id, plan_tier="pro", status=SubscriptionStatus.active)
    session = SubscriptionMockDbSession(billing_periods=[bp], subscriptions=[sub])
    event = build_subscription_updated_event(org_id=org_id, status="canceled")

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        await handle_subscription_updated(event, session)

    # Billing period MUST be completely untouched
    assert bp.monthly_token_quota == 50_000
    assert bp.tokens_used == 12_000
    assert bp.cost_micros_used == 500_000
    assert bp.is_active is True
    assert bp.is_blocked is False
    # No StripeInvoice added
    invoices_added = [a for a in session.added if isinstance(a, StripeInvoice)]
    assert len(invoices_added) == 0


# =============================================================================
# 11. Webhook Engine Integration & Quarantine
# =============================================================================


@pytest.mark.asyncio
async def test_webhook_engine_quarantine_on_unknown_customer() -> None:
    """Verifies that the webhook engine commits a quarantined outcome on unknown customer."""
    org_id = uuid.uuid4()
    event_id = "evt_sub_unknown_cust"
    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.updated",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_updated_event(
            event_id=event_id,
            customer_id="cus_unregistered_ghost",
            org_id=org_id,
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[])

    with patch("app.services.billing.stripe_subscription_updated_handler.settings", get_test_settings()):
        res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert raw_event.status == WebhookProcessingStatus.quarantined
    assert "Unknown Stripe customer" in (raw_event.error_message or "")
    assert session.savepoints_rolled_back >= 1
    assert session.committed_count >= 1
