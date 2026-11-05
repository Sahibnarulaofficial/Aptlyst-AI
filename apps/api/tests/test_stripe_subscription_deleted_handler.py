"""Comprehensive Unit & Integration Tests for Stripe Subscription Deleted Webhook Handler.

Event: customer.subscription.deleted

Validates:
1. Dispatcher Registration:
   - customer.subscription.deleted is registered in default_webhook_dispatcher.
2. Authoritative Fresh Cancellation:
   - Sets status = SubscriptionStatus.canceled.
   - Resets plan_tier = "free".
   - Resets cancel_at_period_end = False.
   - Sets canceled_at from canceled_at, ended_at, or event creation timestamp fallback.
   - Sets last_stripe_event_created_at = event_timestamp.
   - Does not commit from within the handler.
3. Past-Due Handling & Audit Trail:
   - Transitioning a past_due subscription preserves past_due_since intact.
4. Idempotent Duplicate Deletion:
   - Duplicate deletion with equal timestamp when already canceled is a safe acknowledgment.
   - Backfills canceled_at if currently None; preserves existing canceled_at if already populated.
   - Terminal incomplete_expired state acknowledged as safe no-op.
5. Monotonic Timestamp Ordering & Stale Event Protection:
   - Strictly older event (event_timestamp < last_stripe_event_created_at) is a complete no-op.
   - Does not touch status, plan_tier, canceled_at, updated_at, or last_stripe_event_created_at.
6. Same-Second / Equal-Timestamp Collisions:
   - Equal timestamp on non-terminal status (active, past_due) raises WebhookReconciliationConflictError.
   - Subscription record is not mutated.
7. Subscription Identity & Historical Superseded Lookup:
   - Incoming sub_id != current local sub_id:
     * Completed StripeCheckoutAttempt exists -> safely acknowledged as no-op. Active sub untouched.
     * No completed attempt -> raises WebhookReconciliationConflictError(reason="unknown_subscription_deleted").
     * Uncompleted attempt (open, failed) -> raises conflict error.
   - Unbound customer (local stripe_subscription_id is None) -> raises
     WebhookReconciliationConflictError(reason="unbound_customer_subscription_deleted").
8. Strict Identity & Metadata Validation:
   - Unknown customer fails closed (WebhookPermanentError).
   - Metadata organization_id mismatch fails closed (WebhookPermanentError).
   - Malformed metadata organization UUID fails closed (WebhookPermanentError).
   - Valid matching metadata organization UUID succeeds.
9. Structural & Payload Validations:
   - Non-matching event type fails closed.
   - Missing or non-sub_ ID fails closed.
   - Missing or non-cus_ ID fails closed.
   - Status not "canceled" fails closed.
   - Invalid period dates (end < start) fails closed.
   - Non-dict data.object fails closed.
10. Strict Accounting Isolation:
    - Does NOT create or mutate OrganizationBillingPeriod rows.
    - Does NOT create or mutate StripeInvoice rows.
    - Does NOT mutate usage counters, tokens, or spending caps.
11. Runtime Authorization Compatibility:
    - Verifies reserve_usage() immediately fails closed with SUBSCRIPTION_INACTIVE.
12. Webhook Engine Integration & Quarantine:
    - End-to-end processing with process_webhook_event():
      * Success marks event processed.
      * Conflict errors (equal timestamp on active, unbound customer, unknown sub) stage
        BillingReconciliation in pending_review and quarantine the event.
      * Permanent errors quarantine the event.
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
from app.services.billing.stripe_subscription_deleted_handler import handle_subscription_deleted
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
# Mock Database Session for Deletion Tests
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
                "updated_at": s.updated_at,
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
                    s.updated_at = snap["updated_at"]
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
    """In-memory AsyncSession double tailored for subscription lifecycle and deletion tests."""

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
                (
                    r for r in self.reconciliations
                    if (ev_id is None or r.event_id == ev_id) and (inc_id is None or r.incoming_subscription_id == inc_id)
                ),
                None,
            )
            return SubscriptionMockScalarResult(match)

        # 3. StripeCheckoutAttempt lookup (Historical superseded query)
        if any("stripe_checkout_attempts" in str(c) for c in where_clauses) or "stripe_checkout_attempts" in str(stmt):
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
            return SubscriptionMockScalarResult(match_attempt)

        # 4. OrganizationBillingPeriod queries
        if any("organization_billing_periods" in str(c) for c in where_clauses) or "organization_billing_periods" in str(stmt):
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.billing_periods:
                        return SubscriptionMockScalarResult(self.billing_periods[v])
                    for bp in self.billing_periods.values():
                        if bp.organization_id == v:
                            return SubscriptionMockScalarResult(bp)
            return SubscriptionMockScalarResult(None)

        # 5. GenerationTask queries
        if any("generation_tasks" in str(c) for c in where_clauses) or "generation_tasks" in str(stmt):
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.tasks:
                        return SubscriptionMockScalarResult(self.tasks[v])
            return SubscriptionMockScalarResult(None)

        # 6. UsageReservation queries
        if any("usage_reservations" in str(c) for c in where_clauses) or "usage_reservations" in str(stmt):
            if ReservationStatus.active in params.values():
                t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
                active_res = next(
                    (
                        r for r in self.reservations.values()
                        if r.generation_task_id == t_id and r.status == ReservationStatus.active
                    ),
                    None,
                )
                return SubscriptionMockScalarResult(active_res)
            return SubscriptionMockScalarResult(None)

        # 7. Subscription queries
        if any("subscriptions" in str(c) for c in where_clauses) or "subscriptions" in str(stmt):
            # Query by stripe_customer_id
            cust_id = params.get("stripe_customer_id")
            if cust_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

            # Query by stripe_subscription_id
            sub_id = params.get("stripe_subscription_id")
            if sub_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
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
# Payload Builder Fixtures
# =============================================================================


def build_subscription_deleted_event(
    *,
    event_id: str = "evt_sub_deleted_1",
    subscription_id: str = "sub_test_12345",
    customer_id: str = "cus_test_12345",
    status: str = "canceled",
    current_period_start: int | None = 1730000000,
    current_period_end: int | None = 1732592000,
    canceled_at: int | None = 1730050000,
    ended_at: int | None = None,
    cancel_at_period_end: bool = False,
    org_id: uuid.UUID | None = None,
    created: int = 1730050000,
    omit_canceled_at: bool = False,
    event_type: str = "customer.subscription.deleted",
    sub_data_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a valid Stripe customer.subscription.deleted event dictionary."""
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
        "ended_at": ended_at,
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_test_item_1",
                    "price": {"id": TEST_PRO_PRICE_ID, "object": "price"},
                }
            ],
        },
        "metadata": metadata,
    }
    if not omit_canceled_at:
        sub_dict["canceled_at"] = canceled_at

    if sub_data_override:
        sub_dict.update(sub_data_override)

    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {
            "object": sub_dict,
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
    canceled_at: datetime | None = None,
    cancel_at_period_end: bool = False,
    last_event_created_at: datetime | None = None,
) -> Subscription:
    """Create in-memory Subscription model fixture."""
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
        cancel_at_period_end=cancel_at_period_end,
        canceled_at=canceled_at,
        past_due_since=past_due_since,
        last_stripe_event_created_at=last_event_created_at,
    )


# =============================================================================
# 1. Dispatcher Registration Tests
# =============================================================================


def test_dispatcher_registers_subscription_deleted() -> None:
    """Verifies customer.subscription.deleted is registered in default dispatcher."""
    handler = default_webhook_dispatcher.get_handler("customer.subscription.deleted")
    assert handler is not None
    assert handler is handle_subscription_deleted


# =============================================================================
# 2. Authoritative Fresh Cancellation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_active_pro_fresh() -> None:
    """Verifies that a valid deletion event transitions active Pro sub to canceled and resets plan_tier to free."""
    org_id = uuid.uuid4()
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        cancel_at_period_end=True,
        last_event_created_at=datetime.fromtimestamp(1730040000, tz=timezone.utc),
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event_created_ts = 1730050000
    event_canceled_ts = 1730049900
    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_created_ts,
        canceled_at=event_canceled_ts,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert sub.cancel_at_period_end is False
    assert sub.canceled_at == datetime.fromtimestamp(event_canceled_ts, tz=timezone.utc)
    assert sub.last_stripe_event_created_at == datetime.fromtimestamp(event_created_ts, tz=timezone.utc)
    assert sub.updated_at is not None
    # Handler never commits directly; leaves commit to outer webhook engine
    assert session.committed_count == 0


@pytest.mark.asyncio
async def test_subscription_deleted_with_ended_at_fallback() -> None:
    """Verifies that if canceled_at is None, ended_at is used as fallback for canceled_at."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, status=SubscriptionStatus.active, plan_tier="pro")
    session = SubscriptionMockDbSession(subscriptions=[sub])

    ended_ts = 1730055000
    event = build_subscription_deleted_event(
        org_id=org_id,
        canceled_at=None,
        ended_at=ended_ts,
        created=1730056000,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert sub.canceled_at == datetime.fromtimestamp(ended_ts, tz=timezone.utc)


@pytest.mark.asyncio
async def test_subscription_deleted_fallback_to_event_created() -> None:
    """Verifies that if neither canceled_at nor ended_at is present, canceled_at falls back to event timestamp."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id, status=SubscriptionStatus.active, plan_tier="pro")
    session = SubscriptionMockDbSession(subscriptions=[sub])

    created_ts = 1730056000
    event = build_subscription_deleted_event(
        org_id=org_id,
        canceled_at=None,
        ended_at=None,
        created=created_ts,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert sub.canceled_at == datetime.fromtimestamp(created_ts, tz=timezone.utc)


# =============================================================================
# 3. Past-Due Handling & Audit Trail
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_preserves_past_due_since() -> None:
    """Verifies that transitioning a past_due subscription strictly preserves past_due_since for audit trail."""
    org_id = uuid.uuid4()
    original_past_due_anchor = datetime.fromtimestamp(1730040000, tz=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=original_past_due_anchor,
        last_event_created_at=original_past_due_anchor,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=1730050000,
        canceled_at=1730050000,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    # Crucial guarantee: past_due_since is NEVER cleared upon cancellation
    assert sub.past_due_since == original_past_due_anchor


# =============================================================================
# 4. Idempotent Duplicate Deletion
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_duplicate_equal_timestamp_backfills_missing_canceled_at() -> None:
    """Verifies that an equal-timestamp duplicate deletion backfills canceled_at if currently None."""
    org_id = uuid.uuid4()
    event_ts = 1730050000
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=None,
        last_event_created_at=datetime.fromtimestamp(event_ts, tz=timezone.utc),
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_ts,
        canceled_at=event_ts,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.canceled_at == datetime.fromtimestamp(event_ts, tz=timezone.utc)


@pytest.mark.asyncio
async def test_subscription_deleted_duplicate_equal_timestamp_already_has_canceled_at() -> None:
    """Verifies that an equal-timestamp duplicate deletion when canceled_at is already populated is a safe no-op."""
    org_id = uuid.uuid4()
    event_ts = 1730050000
    existing_canceled_at = datetime.fromtimestamp(1730040000, tz=timezone.utc)
    original_updated_at = datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=existing_canceled_at,
        last_event_created_at=datetime.fromtimestamp(event_ts, tz=timezone.utc),
    )
    sub.updated_at = original_updated_at
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_ts,
        canceled_at=event_ts,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled
    assert sub.canceled_at == existing_canceled_at
    assert sub.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_subscription_deleted_duplicate_incomplete_expired_noop() -> None:
    """Verifies that a deletion event for an already incomplete_expired subscription is a clean no-op."""
    org_id = uuid.uuid4()
    event_ts = 1730050000
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.incomplete_expired,
        plan_tier="free",
        last_event_created_at=datetime.fromtimestamp(event_ts, tz=timezone.utc),
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_ts,
    )

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.incomplete_expired


# =============================================================================
# 5. Stale / Older Event Protection
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_strictly_older_is_complete_noop() -> None:
    """Verifies that a strictly older deletion event (event_timestamp < last_event) is a complete no-op."""
    org_id = uuid.uuid4()
    newer_ts = datetime(2026, 10, 27, 15, 0, 0, tzinfo=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        canceled_at=None,
        last_event_created_at=newer_ts,
    )
    original_updated_at = datetime(2026, 10, 27, 15, 0, 0, tzinfo=timezone.utc)
    sub.updated_at = original_updated_at
    session = SubscriptionMockDbSession(subscriptions=[sub])

    older_created_ts = int(newer_ts.timestamp()) - 3600  # 1 hour older
    event = build_subscription_deleted_event(
        org_id=org_id,
        created=older_created_ts,
        canceled_at=older_created_ts,
    )

    await handle_subscription_deleted(event, session)

    # Completely untouched
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.canceled_at is None
    assert sub.last_stripe_event_created_at == newer_ts
    assert sub.updated_at == original_updated_at


# =============================================================================
# 6. Same-Second / Equal-Timestamp Collisions
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_equal_timestamp_on_active_raises_conflict() -> None:
    """Verifies that equal timestamp on active status raises WebhookReconciliationConflictError."""
    org_id = uuid.uuid4()
    event_ts = 1730050000
    event_dt = datetime.fromtimestamp(event_ts, tz=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_event_created_at=event_dt,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_ts,
    )

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert exc_info.value.reason == "equal_timestamp_deletion_collision"
    assert exc_info.value.organization_id == org_id
    # Record must not be mutated
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"


@pytest.mark.asyncio
async def test_subscription_deleted_equal_timestamp_on_past_due_raises_conflict() -> None:
    """Verifies that equal timestamp on past_due status raises WebhookReconciliationConflictError."""
    org_id = uuid.uuid4()
    event_ts = 1730050000
    event_dt = datetime.fromtimestamp(event_ts, tz=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=event_dt,
        last_event_created_at=event_dt,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        org_id=org_id,
        created=event_ts,
    )

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert exc_info.value.reason == "equal_timestamp_deletion_collision"
    assert sub.status == SubscriptionStatus.past_due


# =============================================================================
# 7. Subscription Identity & Historical Superseded Lookup
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_superseded_historical_is_safe_noop() -> None:
    """Verifies that deletion of a historical superseded subscription with completed checkout attempt is a no-op."""
    org_id = uuid.uuid4()
    active_sub_id = "sub_new_active_999"
    old_sub_id = "sub_old_superseded_111"

    sub = make_test_sub(
        org_id=org_id,
        subscription_id=active_sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_event_created_at=datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    # Historical completed attempt for the old subscription
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_subscription_id=old_sub_id,
        status=CheckoutAttemptStatus.complete,
    )

    session = SubscriptionMockDbSession(
        subscriptions=[sub],
        attempts=[attempt],
    )

    # Deletion arrives for old_sub_id
    event = build_subscription_deleted_event(
        subscription_id=old_sub_id,
        org_id=org_id,
        created=1730060000,
    )

    await handle_subscription_deleted(event, session)

    # Active subscription sub_new must NOT be canceled by old subscription deletion
    assert sub.stripe_subscription_id == active_sub_id
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.canceled_at is None


@pytest.mark.asyncio
async def test_subscription_deleted_unknown_subscription_raises_conflict() -> None:
    """Verifies that an unknown subscription deletion for a known customer raises WebhookReconciliationConflictError."""
    org_id = uuid.uuid4()
    sub = make_test_sub(
        org_id=org_id,
        subscription_id="sub_current_123",
        status=SubscriptionStatus.active,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub], attempts=[])

    event = build_subscription_deleted_event(
        subscription_id="sub_foreign_ghost_999",
        org_id=org_id,
        created=1730060000,
    )

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert exc_info.value.reason == "unknown_subscription_deleted"
    assert exc_info.value.organization_id == org_id
    assert exc_info.value.existing_subscription_id == "sub_current_123"
    assert exc_info.value.incoming_subscription_id == "sub_foreign_ghost_999"


@pytest.mark.asyncio
async def test_subscription_deleted_uncompleted_attempt_is_not_historical() -> None:
    """Verifies that an uncompleted checkout attempt (e.g. open/failed) does NOT qualify as historical."""
    org_id = uuid.uuid4()
    sub = make_test_sub(
        org_id=org_id,
        subscription_id="sub_current_123",
        status=SubscriptionStatus.active,
    )
    # Attempt exists but status is open (not complete)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_subscription_id="sub_old_open_456",
        status=CheckoutAttemptStatus.open,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub], attempts=[attempt])

    event = build_subscription_deleted_event(
        subscription_id="sub_old_open_456",
        org_id=org_id,
        created=1730060000,
    )

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert exc_info.value.reason == "unknown_subscription_deleted"


@pytest.mark.asyncio
async def test_subscription_deleted_unbound_customer_raises_conflict() -> None:
    """Verifies that deletion for a customer whose local subscription has no stripe_subscription_id raises conflict."""
    org_id = uuid.uuid4()
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=None,  # Unbound customer
        status=SubscriptionStatus.incomplete,
    )
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(
        subscription_id="sub_incoming_789",
        org_id=org_id,
        created=1730060000,
    )

    with pytest.raises(WebhookReconciliationConflictError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert exc_info.value.reason == "unbound_customer_subscription_deleted"
    assert exc_info.value.organization_id == org_id


# =============================================================================
# 8. Strict Identity & Metadata Validation
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_unknown_customer_fails_closed() -> None:
    """Verifies that an unknown customer fails closed with WebhookPermanentError."""
    session = SubscriptionMockDbSession(subscriptions=[])
    event = build_subscription_deleted_event(customer_id="cus_unknown_999")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Unknown Stripe customer" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_metadata_org_mismatch_fails_closed() -> None:
    """Verifies that metadata organization_id mismatch with local customer fails closed."""
    org_id_local = uuid.uuid4()
    org_id_fraud = uuid.uuid4()
    sub = make_test_sub(org_id=org_id_local)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(org_id=org_id_fraud)

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Organization metadata mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_metadata_org_malformed_uuid_fails_closed() -> None:
    """Verifies that malformed organization_id in metadata fails closed."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event()
    event["data"]["object"]["metadata"] = {"organization_id": "not-a-valid-uuid"}

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Invalid organization UUID in metadata" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_valid_metadata_org_matches() -> None:
    """Verifies that matching organization metadata succeeds."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])

    event = build_subscription_deleted_event(org_id=org_id)

    await handle_subscription_deleted(event, session)

    assert sub.status == SubscriptionStatus.canceled


# =============================================================================
# 9. Structural & Payload Validations
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_wrong_event_type_fails() -> None:
    """Verifies that unexpected event type raises WebhookPermanentError."""
    session = SubscriptionMockDbSession()
    event = build_subscription_deleted_event(event_type="customer.subscription.updated")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Unexpected event type" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_missing_sub_id_fails() -> None:
    """Verifies that missing or invalid subscription ID raises WebhookPermanentError."""
    session = SubscriptionMockDbSession()
    event = build_subscription_deleted_event(subscription_id="not_a_sub_id")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Missing or invalid Stripe subscription ID" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_missing_cus_id_fails() -> None:
    """Verifies that missing or invalid customer ID raises WebhookPermanentError."""
    session = SubscriptionMockDbSession()
    event = build_subscription_deleted_event(customer_id="bad_customer")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Missing or invalid Stripe customer ID" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_status_not_canceled_fails() -> None:
    """Verifies that deletion event with status other than 'canceled' raises WebhookPermanentError."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event = build_subscription_deleted_event(org_id=org_id, status="active")

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "Invalid subscription status 'active'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_invalid_period_dates_fails() -> None:
    """Verifies that current_period_end < current_period_start raises WebhookPermanentError."""
    org_id = uuid.uuid4()
    sub = make_test_sub(org_id=org_id)
    session = SubscriptionMockDbSession(subscriptions=[sub])
    event = build_subscription_deleted_event(
        org_id=org_id,
        current_period_start=1732592000,
        current_period_end=1730000000,
    )

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "current_period_end must not be before current_period_start" in str(exc_info.value)


@pytest.mark.asyncio
async def test_subscription_deleted_missing_data_object_fails() -> None:
    """Verifies that malformed payload missing data.object raises WebhookPermanentError."""
    session = SubscriptionMockDbSession()
    event = {"id": "evt_test", "type": "customer.subscription.deleted", "data": {}}

    with pytest.raises(WebhookPermanentError) as exc_info:
        await handle_subscription_deleted(event, session)

    assert "missing or invalid 'data.object'" in str(exc_info.value)


# =============================================================================
# 10. Strict Accounting Isolation
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_strict_accounting_isolation() -> None:
    """Verifies deletion handler does NOT mutate OrganizationBillingPeriod or create StripeInvoice rows."""
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
    event = build_subscription_deleted_event(org_id=org_id)

    await handle_subscription_deleted(event, session)

    # Billing period MUST be completely untouched
    assert bp.monthly_token_quota == 50_000
    assert bp.tokens_used == 12_000
    assert bp.cost_micros_used == 500_000
    assert bp.is_active is True
    assert bp.is_blocked is False
    assert bp.plan_tier == "pro"

    # No StripeInvoice added
    invoices_added = [a for a in session.added if isinstance(a, StripeInvoice)]
    assert len(invoices_added) == 0


# =============================================================================
# 11. Runtime Authorization Compatibility
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_deleted_runtime_auth_fails_closed() -> None:
    """Proves that deletion transitions the subscription so reserve_usage() immediately denies authorization."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    t_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)

    bp = OrganizationBillingPeriod(
        id=bp_id,
        organization_id=org_id,
        period_start=t_start,
        period_end=t_start + timedelta(days=30),
        plan_tier="pro",
        monthly_token_quota=100_000,
        tokens_used=10_000,
        tokens_reserved=0,
        cost_micros_used=100_000,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_event_created_at=t_start,
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

    session = SubscriptionMockDbSession(
        billing_periods=[bp],
        subscriptions=[sub],
        tasks=[task],
    )

    # 1. Active Pro subscription authorizes usage reservation
    with patch("app.services.billing.accounting.settings", get_test_settings()):
        res1 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=5),
        )
        assert res1.success is True

        # Settle or clear reservation for next check
        session.reservations.clear()

        # 2. Deletion event arrives
        del_epoch = int((t_start + timedelta(days=6)).timestamp())
        event = build_subscription_deleted_event(
            org_id=org_id,
            created=del_epoch,
            canceled_at=del_epoch,
        )
        await handle_subscription_deleted(event, session)

        assert sub.status == SubscriptionStatus.canceled
        assert sub.plan_tier == "free"

        # 3. Reservation immediately fails closed with SUBSCRIPTION_INACTIVE
        res2 = await reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=t_start + timedelta(days=7),
        )
        assert res2.success is False
        assert res2.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE


# =============================================================================
# 12. Webhook Engine Integration & Quarantine
# =============================================================================


@pytest.mark.asyncio
async def test_webhook_engine_process_subscription_deleted_success() -> None:
    """Verifies that the webhook engine end-to-end executes deletion and transitions event to processed."""
    org_id = uuid.uuid4()
    event_id = "evt_sub_del_engine_success"
    sub = make_test_sub(org_id=org_id, status=SubscriptionStatus.active, plan_tier="pro")

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.deleted",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_deleted_event(
            event_id=event_id,
            org_id=org_id,
            created=1730060000,
            canceled_at=1730060000,
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[sub])

    res = await process_webhook_event(session, event_id)

    assert res.success is True
    assert res.status == WebhookProcessingStatus.processed
    assert raw_event.status == WebhookProcessingStatus.processed
    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert session.committed_count >= 1


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_equal_timestamp_collision() -> None:
    """Verifies webhook engine catches equal timestamp conflict, stages reconciliation, and quarantines."""
    org_id = uuid.uuid4()
    event_id = "evt_sub_del_collision"
    event_ts = 1730060000
    event_dt = datetime.fromtimestamp(event_ts, tz=timezone.utc)
    sub = make_test_sub(
        org_id=org_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_event_created_at=event_dt,
    )

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.deleted",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_deleted_event(
            event_id=event_id,
            org_id=org_id,
            created=event_ts,
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[sub])

    res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert raw_event.status == WebhookProcessingStatus.quarantined
    assert len(session.reconciliations) == 1
    rec = session.reconciliations[0]
    assert rec.organization_id == org_id
    assert rec.event_id == event_id
    assert rec.reason == "equal_timestamp_deletion_collision"
    assert rec.status == ReconciliationStatus.pending_review
    # Active subscription was NOT modified
    assert sub.status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_unbound_customer() -> None:
    """Verifies webhook engine catches unbound customer conflict, stages reconciliation, and quarantines."""
    org_id = uuid.uuid4()
    event_id = "evt_sub_del_unbound"
    sub = make_test_sub(
        org_id=org_id,
        subscription_id=None,
        status=SubscriptionStatus.incomplete,
    )

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.deleted",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_deleted_event(
            event_id=event_id,
            org_id=org_id,
            created=1730060000,
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[sub])

    res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert len(session.reconciliations) == 1
    rec = session.reconciliations[0]
    assert rec.organization_id == org_id
    assert rec.reason == "unbound_customer_subscription_deleted"
    assert rec.status == ReconciliationStatus.pending_review


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_unknown_subscription() -> None:
    """Verifies webhook engine catches unknown subscription conflict, stages reconciliation, and quarantines."""
    org_id = uuid.uuid4()
    event_id = "evt_sub_del_unknown_sub"
    sub = make_test_sub(
        org_id=org_id,
        subscription_id="sub_current_123",
        status=SubscriptionStatus.active,
    )

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.deleted",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_deleted_event(
            event_id=event_id,
            subscription_id="sub_alien_888",
            org_id=org_id,
            created=1730060000,
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[sub])

    res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert len(session.reconciliations) == 1
    rec = session.reconciliations[0]
    assert rec.organization_id == org_id
    assert rec.reason == "unknown_subscription_deleted"
    assert rec.status == ReconciliationStatus.pending_review


@pytest.mark.asyncio
async def test_webhook_engine_quarantines_unknown_customer_permanent_error() -> None:
    """Verifies webhook engine quarantines permanent error on unknown customer."""
    event_id = "evt_sub_del_unknown_cust"
    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.deleted",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_subscription_deleted_event(
            event_id=event_id,
            customer_id="cus_unregistered_ghost",
        ),
    )
    session = SubscriptionMockDbSession(events=[raw_event], subscriptions=[])

    res = await process_webhook_event(session, event_id)

    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert raw_event.status == WebhookProcessingStatus.quarantined
    assert "Unknown Stripe customer" in (raw_event.error_message or "")
    assert session.savepoints_rolled_back >= 1
    assert session.committed_count >= 1
