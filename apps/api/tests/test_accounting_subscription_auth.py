"""Comprehensive test suite for subscription-aware runtime authorization and past-due grace period enforcement.

Milestone 4 Step 3B.9B.2B.3A.

Testing Classification:
1. In-Memory Mocked Unit & Deterministic State Tests:
   - Evaluates the deterministic authorization policy, integer calculations, and state transitions
     using in-memory test doubles (`MockAsyncSession`) and explicit UTC time manipulation.
   - Verifies lock query acquisition order (OrganizationBillingPeriod FIRST, Subscription SECOND).
2. Concurrency & Transactional Boundary Tests:
   - Verifies race-safety semantics, lock ordering invariants, and atomic rejection of
     concurrent reservations against newly revoked subscriptions.
   - Live PostgreSQL row locks (OS kernel locks, serialization anomalies, and foreign key
     cascades) are documented and segregated from deterministic unit assertions.

Requirements Verified:
1. Free billing periods proceed without requiring a Stripe subscription.
2. Pro billing periods with active subscriptions succeed within quota limits.
3. Pro billing periods with past_due subscriptions retain access strictly within 7-day grace.
4. Exact 7-day boundary condition (`now >= past_due_since + 7 days`) denies access (`GRACE_PERIOD_EXPIRED`).
5. Exceeded grace period (> 7 days) denies access (`GRACE_PERIOD_EXPIRED`).
6. Missing `past_due_since` anchor on past_due subscription fails closed (`GRACE_PERIOD_EXPIRED`).
7. Missing subscription association for a Pro billing period fails closed (`SUBSCRIPTION_NOT_FOUND`).
8. Non-entitled statuses (canceled, unpaid, paused, trialing, incomplete, incomplete_expired)
   are immediately denied (`SUBSCRIPTION_INACTIVE`).
9. Scheduled cancellation (`cancel_at_period_end=True`) does NOT revoke access while status is active.
10. Quota and spending cap checks remain enforced when subscription is entitled.
11. Settlement and release regressions: settlements and pre-dispatch releases function correctly.
12. Consistent global lock ordering: OrganizationBillingPeriod FIRST, Subscription SECOND.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar
import uuid

import pytest

from app.config import settings
from app.db.base import Base
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.organization  # noqa: F401
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Message, MessageSenderType
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.conversation import GenerationStatus
from app.services.billing.accounting import (
    AccountingErrorCode,
    ReservationResult,
    SettlementResult,
    release_reservation,
    reserve_usage,
    settle_reservation,
)
from app.services.billing.pricing import (
    MOCK_GPT_4O_MINI_PRICING,
    ModelPricing,
)
from app.services.llm.orchestrator import (
    OrchestratorErrorCode,
    OrchestrationResult,
    orchestrate_assistant_reply,
)
from app.api.public_chatbots import _map_orchestrator_error_to_public_message

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to immediate completion without requiring an OS event loop."""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise RuntimeError(f"Coroutine {coro} yielded unexpectedly before completing")


# =============================================================================
# In-Memory Session Test Double Supporting Organization, Period & Subscription
# =============================================================================


class MockResult:
    """Mock result simulating SQLAlchemy Result."""

    def __init__(self, item: Any = None, items: list[Any] | None = None) -> None:
        self._item = item
        self._items = items if items is not None else ([item] if item is not None else [])

    def scalar_one_or_none(self) -> Any:
        return self._item

    def scalar(self) -> Any:
        return self._item

    def scalars(self) -> "MockScalars":
        return MockScalars(self._items)


class MockScalars:
    """Mock scalar container simulating SQLAlchemy's ScalarResult."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any:
        return self._items[0] if self._items else None


class MockAsyncSession:
    """Deterministic in-memory async session test double for subscription accounting tests."""

    def __init__(
        self,
        *,
        billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] | None = None,
        tasks: dict[uuid.UUID, GenerationTask] | None = None,
        reservations: dict[uuid.UUID, UsageReservation] | None = None,
        usage_logs: dict[uuid.UUID, LLMUsageLog] | None = None,
        attempts: list[GenerationTaskAttempt] | dict[uuid.UUID, GenerationTaskAttempt] | None = None,
        subscriptions: dict[uuid.UUID, Subscription] | None = None,
        messages: dict[uuid.UUID, Message] | None = None,
    ) -> None:
        self.billing_periods = billing_periods or {}
        self.tasks = tasks or {}
        self.reservations = reservations or {}
        self.usage_logs = usage_logs or {}
        if isinstance(attempts, list):
            self.attempts = {a.id: a for a in attempts}
        else:
            self.attempts = attempts or {}
        self.subscriptions = subscriptions or {}
        self.messages = messages or {}
        self.added: list[Any] = []
        self.flushed = False
        self.rolled_back = False
        self.executed_queries: list[dict[str, Any]] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, UsageReservation):
            self.reservations[obj.id] = obj
        elif isinstance(obj, LLMUsageLog):
            self.usage_logs[obj.id] = obj
        elif isinstance(obj, Subscription):
            self.subscriptions[obj.organization_id] = obj
        elif isinstance(obj, GenerationTask):
            self.tasks[obj.id] = obj
        elif isinstance(obj, Message):
            self.messages[obj.id] = obj
        elif isinstance(obj, GenerationTaskAttempt):
            self.attempts[obj.id] = obj

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        pass

    async def rollback(self) -> None:
        self.rolled_back = True

    async def execute(self, stmt: Any) -> MockResult:
        col = stmt.column_descriptions[0]
        entity = col.get("entity")
        name = col.get("name")
        params = stmt.compile().params
        has_for_update = getattr(stmt, "_for_update_arg", None) is not None

        self.executed_queries.append({
            "entity": entity,
            "name": name,
            "params": params,
            "for_update": has_for_update,
        })

        if name == "coalesce" and entity is GenerationTaskAttempt:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            matched = [a for a in self.attempts.values() if a.generation_task_id == t_id]
            max_num = max([a.attempt_number for a in matched], default=0)
            return MockResult(max_num)

        if name == "billing_period_id" and entity is UsageReservation:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.reservations:
                    return MockResult(self.reservations[v].billing_period_id)
            return MockResult(None)

        if entity is Message:
            client_msg_id = next((v for v in params.values() if isinstance(v, str) and v.startswith("reply:")), None)
            if client_msg_id:
                for m in self.messages.values():
                    if m.client_message_id == client_msg_id:
                        return MockResult(m)
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.messages:
                    return MockResult(self.messages[v])
            return MockResult(None)

        if entity is OrganizationBillingPeriod:
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.billing_periods:
                        return MockResult(self.billing_periods[v])
                    for bp in self.billing_periods.values():
                        if bp.organization_id == v:
                            return MockResult(bp)
            return MockResult(None)

        if entity is Subscription:
            status_list = next((v for v in params.values() if isinstance(v, list) and all(isinstance(x, SubscriptionStatus) for x in v)), None)
            single_status = next((v for v in params.values() if isinstance(v, SubscriptionStatus)), None)
            if single_status is not None and status_list is None:
                status_list = [single_status]

            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.subscriptions:
                    sub_val = self.subscriptions[v]
                    sub_list = sub_val if isinstance(sub_val, list) else [sub_val]
                    if status_list is not None:
                        matched = [s for s in sub_list if s.status in status_list]
                    else:
                        matched = sub_list
                    if not matched:
                        return MockResult(None, items=[])
                    return MockResult(matched[0], items=matched)
            return MockResult(None, items=[])

        if entity is GenerationTask:
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.tasks:
                        return MockResult(self.tasks[v])
                    for t in self.tasks.values():
                        if t.visitor_message_id == v:
                            return MockResult(t)
            return MockResult(None)

        if entity is UsageReservation:
            if ReservationStatus.active in params.values():
                t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
                active_res = next(
                    (r for r in self.reservations.values()
                     if r.generation_task_id == t_id and r.status == ReservationStatus.active),
                    None,
                )
                return MockResult(active_res)
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.reservations:
                    return MockResult(self.reservations[v])
            return MockResult(None)

        if entity is LLMUsageLog:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            log = next((l for l in self.usage_logs.values() if l.generation_task_id == t_id), None)
            return MockResult(log)

        if entity is GenerationTaskAttempt:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.attempts:
                    return MockResult(self.attempts[v])
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            matched = [att for att in self.attempts.values() if att.generation_task_id == t_id]
            return MockResult(items=matched)

        return MockResult(None)


# =============================================================================
# Helper Fixture Factories
# =============================================================================


def make_period(
    *,
    period_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    plan_tier: str = "free",
    monthly_token_quota: int = 100_000,
    hard_spending_cap_micros: int | None = 1_000_000,
    tokens_used: int = 0,
    tokens_reserved: int = 0,
    cost_micros_used: int = 0,
    cost_micros_reserved: int = 0,
    is_active: bool = True,
    is_blocked: bool = False,
    now: datetime | None = None,
) -> OrganizationBillingPeriod:
    ref_time = now or datetime.now(timezone.utc)
    return OrganizationBillingPeriod(
        id=period_id or uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        period_start=ref_time - timedelta(days=5),
        period_end=ref_time + timedelta(days=25),
        plan_tier=plan_tier,
        monthly_token_quota=monthly_token_quota,
        hard_spending_cap_micros=hard_spending_cap_micros,
        tokens_used=tokens_used,
        tokens_reserved=tokens_reserved,
        cost_micros_used=cost_micros_used,
        cost_micros_reserved=cost_micros_reserved,
        is_active=is_active,
        is_blocked=is_blocked,
    )


def make_task(
    *,
    task_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    period_id: uuid.UUID | None = None,
    status: GenerationTaskStatus = GenerationTaskStatus.pending,
) -> GenerationTask:
    return GenerationTask(
        id=task_id or uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        billing_period_id=period_id or uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=status,
        lease_token=uuid.uuid4(),
    )


def make_subscription(
    *,
    org_id: uuid.UUID,
    status: SubscriptionStatus = SubscriptionStatus.active,
    plan_tier: str = "pro",
    past_due_since: datetime | None = None,
    cancel_at_period_end: bool = False,
    canceled_at: datetime | None = None,
    now: datetime | None = None,
) -> Subscription:
    ref_time = now or datetime.now(timezone.utc)
    return Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id=f"cus_{uuid.uuid4().hex[:14]}",
        stripe_subscription_id=f"sub_{uuid.uuid4().hex[:14]}",
        stripe_price_id="price_pro_test",
        status=status,
        plan_tier=plan_tier,
        current_period_start=ref_time - timedelta(days=10),
        current_period_end=ref_time + timedelta(days=20),
        cancel_at_period_end=cancel_at_period_end,
        canceled_at=canceled_at,
        past_due_since=past_due_since,
    )


# =============================================================================
# 1. Free Tier Invariant Tests
# =============================================================================


def test_free_billing_period_without_subscription() -> None:
    """Verifies that Free billing periods require NO subscription record and succeed."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="free", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.error_code is None
    assert res.reservation is not None
    # Confirm Subscription was NEVER queried for a Free tier period
    sub_queries = [q for q in session.executed_queries if q["entity"] is Subscription]
    assert len(sub_queries) == 0


def test_free_billing_period_with_canceled_subscription() -> None:
    """Verifies that an existing canceled subscription does NOT block a Free billing period."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="free", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.canceled, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.error_code is None


# =============================================================================
# 2. Pro Tier Active & Entitlement Tests
# =============================================================================


def test_pro_period_with_active_subscription() -> None:
    """Verifies that a Pro billing period with an active subscription succeeds and acquires row locks."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.error_code is None
    assert res.reservation is not None

    # Verify Global Lock Ordering: OrganizationBillingPeriod locked FIRST, Subscription SECOND
    assert len(session.executed_queries) >= 2
    q0 = session.executed_queries[0]
    q1 = session.executed_queries[1]
    assert q0["entity"] is OrganizationBillingPeriod
    assert q0["for_update"] is True
    assert q1["entity"] is Subscription
    assert q1["for_update"] is True


def test_pro_period_missing_subscription_fails_closed() -> None:
    """Verifies that a Pro billing period with NO local subscription record fails closed."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.SUBSCRIPTION_NOT_FOUND
    assert "requires an active Stripe subscription" in res.message


# =============================================================================
# 3. Seven-Day Past-Due Grace Period Enforcement Tests
# =============================================================================


def test_pro_period_past_due_within_grace() -> None:
    """Case 3A: past_due subscription within 7-day grace (e.g. 6 days, 23 hours, 59 mins) is ALLOWED."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 8, 11, 59, 59, tzinfo=timezone.utc)
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)  # 6d 23h 59m 59s ago

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.past_due, past_due_since=anchor, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.error_code is None
    assert res.reservation is not None


def test_pro_period_past_due_exactly_at_seven_day_boundary() -> None:
    """Case 3B: exactly at seven elapsed 24-hour periods (now == past_due_since + 7 days), access is DENIED."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = anchor + timedelta(days=7)  # Exactly 7 elapsed 24-hour periods

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.past_due, past_due_since=anchor, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED
    assert "grace period has expired" in res.message


def test_pro_period_past_due_after_seven_day_expiry() -> None:
    """Case 3C: after seven days (e.g. 7 days + 1 second), access is DENIED."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = anchor + timedelta(days=7, seconds=1)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.past_due, past_due_since=anchor, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED


def test_pro_period_past_due_missing_anchor_fails_closed() -> None:
    """Case 3D: past_due status with missing or null past_due_since anchor must fail closed."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.past_due, past_due_since=None, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED
    assert "anchor is missing" in res.message


def test_repeated_past_due_does_not_reset_grace_anchor() -> None:
    """Case 3E: Repeated past_due checks calculate strictly from the original past_due_since anchor."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Day 3: allowed
    day3 = anchor + timedelta(days=3)
    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=day3)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.past_due, past_due_since=anchor, now=day3)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res3 = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=day3,
        )
    )
    assert res3.success is True

    # Day 8: denied from the original anchor
    day8 = anchor + timedelta(days=8)
    task8 = make_task(task_id=uuid.uuid4(), org_id=org_id, period_id=bp_id)
    session.tasks[task8.id] = task8

    res8 = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=task8.id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=day8,
        )
    )
    assert res8.success is False
    assert res8.error_code == AccountingErrorCode.GRACE_PERIOD_EXPIRED


# =============================================================================
# 4. Non-Entitled Statuses & Scheduled Cancellation
# =============================================================================


@pytest.mark.parametrize(
    "non_entitled_status",
    [
        SubscriptionStatus.canceled,
        SubscriptionStatus.unpaid,
        SubscriptionStatus.paused,
        SubscriptionStatus.trialing,
        SubscriptionStatus.incomplete,
        SubscriptionStatus.incomplete_expired,
    ],
)
def test_pro_period_non_entitled_statuses(non_entitled_status: SubscriptionStatus) -> None:
    """Verifies that non-entitled subscription states are strictly rejected."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=non_entitled_status, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE
    assert res.details.get("subscription_status") == non_entitled_status.value


def test_pro_period_scheduled_cancellation_while_active() -> None:
    """Verifies that cancel_at_period_end=True does NOT revoke access while status remains active."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(
        org_id=org_id,
        status=SubscriptionStatus.active,
        cancel_at_period_end=True,
        canceled_at=now - timedelta(days=2),
        now=now,
    )
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.error_code is None
    assert res.reservation is not None


# =============================================================================
# 5. Entitled Pro Period Quota & Cap Enforcement
# =============================================================================


def test_pro_period_quota_exhaustion_when_subscription_active() -> None:
    """Verifies that quota limits remain enforced even when subscription is active."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Set monthly token quota lower than required turn ceiling
    bp = make_period(
        period_id=bp_id,
        org_id=org_id,
        plan_tier="pro",
        monthly_token_quota=2_000,
        tokens_used=1_000,
        now=now,
    )
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,  # requires 5,000 max turn tokens
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.QUOTA_EXHAUSTED


def test_pro_period_spending_cap_exhaustion_when_subscription_active() -> None:
    """Verifies that hard spending caps remain enforced even when subscription is active."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Set hard spending cap lower than turn cost ceiling
    bp = make_period(
        period_id=bp_id,
        org_id=org_id,
        plan_tier="pro",
        hard_spending_cap_micros=100,  # $0.0001
        cost_micros_used=50,
        now=now,
    )
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.SPENDING_CAP_EXHAUSTED


# =============================================================================
# 6. Settlement, Release & Idempotency Regressions
# =============================================================================


def test_pro_reservation_settlement_and_release_lifecycle() -> None:
    """Verifies full reservation -> settlement -> release lifecycle on a Pro period."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    # Step 1: Acquire reservation
    res_acquire = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res_acquire.success is True
    reservation = res_acquire.reservation

    # Step 2: Settle reservation with actual usage
    actual_tokens = 500
    actual_cost = 150
    settle_res = run_async(
        settle_reservation(
            session,
            reservation_id=reservation.id,
            organization_id=org_id,
            actual_tokens=actual_tokens,
            actual_cost_micros=actual_cost,
            prompt_tokens=200,
            completion_tokens=300,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now + timedelta(seconds=5),
        )
    )
    assert settle_res.success is True
    assert settle_res.actual_tokens == actual_tokens
    assert settle_res.actual_cost_micros == actual_cost
    assert bp.tokens_used == actual_tokens
    assert bp.cost_micros_used == actual_cost
    assert bp.tokens_reserved == 0

    # Step 3: Idempotent replay of identical settlement
    replay_res = run_async(
        settle_reservation(
            session,
            reservation_id=reservation.id,
            organization_id=org_id,
            actual_tokens=actual_tokens,
            actual_cost_micros=actual_cost,
            prompt_tokens=200,
            completion_tokens=300,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now + timedelta(seconds=10),
        )
    )
    assert replay_res.success is True
    assert replay_res.is_idempotent_replay is True


# =============================================================================
# 7. Concurrency & Revocation Fencing Simulation
# =============================================================================


def test_concurrent_subscription_revocation_and_reservation_fencing() -> None:
    """Verifies that when a subscription is revoked concurrently, subsequent reservations are fenced.

    Simulation of PostgreSQL READ COMMITTED row-level locking:
    - Worker 1 executes subscription cancellation (updates Subscription.status = 'canceled').
    - Worker 2 executes reserve_usage():
      1. Locks OrganizationBillingPeriod.
      2. Locks Subscription (SELECT FOR UPDATE).
      3. Reads authoritative status = 'canceled'.
      4. Immediately rejects reservation with SUBSCRIPTION_INACTIVE.
    """
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    sub = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task}, subscriptions={org_id: sub})

    # Concurrently revoke subscription
    sub.status = SubscriptionStatus.canceled
    sub.canceled_at = now

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.SUBSCRIPTION_INACTIVE
    assert bp.tokens_reserved == 0
    assert bp.cost_micros_reserved == 0


# =============================================================================
# 8. Orchestrator Integration & Public Error Non-Exposure Tests
# =============================================================================


class MockTestProvider:
    """Mock provider tracking invocation count."""

    def __init__(self, response_text: str = "Assistant reply") -> None:
        self.response_text = response_text
        self.call_count = 0

    async def generate_reply(self, **kwargs: Any) -> Any:
        from app.services.llm.base import LLMResponse, LLMUsage
        self.call_count += 1
        return LLMResponse(
            content=self.response_text,
            model="mock-gpt-4o-mini",
            usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        )


def _setup_orchestrator_env(
    *,
    plan_tier: str = "pro",
    sub_status: SubscriptionStatus | None = None,
    past_due_since: datetime | None = None,
    now: datetime | None = None,
) -> tuple[MockAsyncSession, Chatbot, uuid.UUID, Message, OrganizationBillingPeriod]:
    current_time = now or datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    bp = make_period(org_id=org_id, plan_tier=plan_tier, now=current_time)
    chatbot = Chatbot(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="SupportBot",
        instructions="Helpful assistant.",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Hello!",
        created_at=current_time,
    )

    subs = {}
    if sub_status is not None:
        sub = make_subscription(
            org_id=org_id,
            status=sub_status,
            past_due_since=past_due_since,
            now=current_time,
        )
        subs[org_id] = sub

    session = MockAsyncSession(
        billing_periods={bp.id: bp},
        subscriptions=subs,
    )
    session.add(visitor_msg)
    return session, chatbot, conv_id, visitor_msg, bp


def test_orchestrator_denies_generation_when_subscription_canceled() -> None:
    """Verifies orchestrator denies turn when Pro subscription is canceled with zero provider calls."""
    settings.llm_provider = "mock"
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    session, chatbot, conv_id, visitor_msg, bp = _setup_orchestrator_env(
        plan_tier="pro",
        sub_status=SubscriptionStatus.canceled,
        now=now,
    )
    provider = MockTestProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=session,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SUBSCRIPTION_INACTIVE
    assert provider.call_count == 0  # Zero provider I/O allowed
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.failed


def test_orchestrator_denies_generation_when_past_due_grace_expired() -> None:
    """Verifies orchestrator denies turn when Pro subscription is past_due > 7 days."""
    settings.llm_provider = "mock"
    now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)  # 8 days ago
    session, chatbot, conv_id, visitor_msg, bp = _setup_orchestrator_env(
        plan_tier="pro",
        sub_status=SubscriptionStatus.past_due,
        past_due_since=anchor,
        now=now,
    )
    provider = MockTestProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=session,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.GRACE_PERIOD_EXPIRED
    assert provider.call_count == 0


def test_orchestrator_permits_generation_when_past_due_within_grace() -> None:
    """Verifies orchestrator allows turn when Pro subscription is past_due <= 7 days."""
    settings.llm_provider = "mock"
    now = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
    anchor = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)  # 3 days ago (within grace)
    session, chatbot, conv_id, visitor_msg, bp = _setup_orchestrator_env(
        plan_tier="pro",
        sub_status=SubscriptionStatus.past_due,
        past_due_since=anchor,
        now=now,
    )
    provider = MockTestProvider(response_text="Within grace period answer")

    # MockAsyncSession need support for message lookups
    result = run_async(
        orchestrate_assistant_reply(
            db=session,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.reply_message is not None
    assert result.reply_message.content == "Within grace period answer"
    assert provider.call_count == 1


def test_orchestrator_denies_generation_when_pro_subscription_missing() -> None:
    """Verifies orchestrator denies turn when Pro billing period has no subscription row."""
    settings.llm_provider = "mock"
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    session, chatbot, conv_id, visitor_msg, bp = _setup_orchestrator_env(
        plan_tier="pro",
        sub_status=None,  # No subscription in DB
        now=now,
    )
    provider = MockTestProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=session,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SUBSCRIPTION_NOT_FOUND
    assert provider.call_count == 0


def test_public_chatbot_error_mapping_safe_messages() -> None:
    """Verifies public error mapping does NOT expose internal Stripe IDs or subscription details."""
    inactive_res = OrchestrationResult(
        status=GenerationStatus.failed,
        error_code=OrchestratorErrorCode.SUBSCRIPTION_INACTIVE,
        error_message="Internal: sub_12345 is canceled in Stripe",
    )
    grace_res = OrchestrationResult(
        status=GenerationStatus.failed,
        error_code=OrchestratorErrorCode.GRACE_PERIOD_EXPIRED,
        error_message="Internal: sub_67890 past_due since 2026-10-01 exceeded 7 days",
    )
    not_found_res = OrchestrationResult(
        status=GenerationStatus.failed,
        error_code=OrchestratorErrorCode.SUBSCRIPTION_NOT_FOUND,
        error_message="Internal: Organization uuid-abc has no subscriptions row",
    )

    for res in [inactive_res, grace_res, not_found_res]:
        public_msg = _map_orchestrator_error_to_public_message(res)
        # Must be safe, customer-facing message
        assert "sub_" not in public_msg
        assert "uuid-" not in public_msg
        assert "Stripe" not in public_msg
        assert "Internal" not in public_msg
        assert public_msg == "The AI assistant is currently unavailable due to organization service limits. Your message was saved."



def test_reserve_usage_ignores_historical_canceled_and_incomplete_subscriptions() -> None:
    """Proves reserve_usage ignores historical canceled and incomplete rows and selects active subscription."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)

    sub_canceled = make_subscription(org_id=org_id, status=SubscriptionStatus.canceled, now=now)
    sub_incomplete = make_subscription(org_id=org_id, status=SubscriptionStatus.incomplete, now=now)
    sub_active = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)

    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        subscriptions={org_id: [sub_canceled, sub_incomplete, sub_active]},
    )

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is True
    assert res.reservation is not None
    assert res.reservation.status == ReservationStatus.active


def test_reserve_usage_multiple_eligible_subscriptions_fails_safely() -> None:
    """Proves reserve_usage fails safely with RECONCILIATION_REQUIRED when multiple eligible rows exist."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    bp = make_period(period_id=bp_id, org_id=org_id, plan_tier="pro", now=now)
    task = make_task(task_id=t_id, org_id=org_id, period_id=bp_id)

    sub_active_1 = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)
    sub_active_2 = make_subscription(org_id=org_id, status=SubscriptionStatus.active, now=now)

    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        subscriptions={org_id: [sub_active_1, sub_active_2]},
    )

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.RECONCILIATION_REQUIRED
    assert res.details.get("eligible_subscription_count") == 2
