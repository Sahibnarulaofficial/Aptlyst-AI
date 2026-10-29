"""Comprehensive test suite for Phase 2B Stage 2B: Transactional Usage Accounting.

Verifies:
1. Pre-inference usage reservation (`reserve_usage`):
   - Atomicity and accurate deduction from monthly token quota and hard spending cap.
   - Enforces FOR UPDATE row-level locking on OrganizationBillingPeriod.
   - Rejects exhausted quota, exhausted spending cap, and 64-bit integer overflow.
   - Rejects inactive, blocked, or expired billing periods.
   - Rejects cross-tenant ownership mismatches (organization vs billing period vs task).
   - Enforces single active reservation invariant per generation task.
   - Rejects non-pending generation tasks.
   - Rejects invalid inputs (booleans, non-UUIDs, negative rates/tokens).
2. Post-inference settlement (`settle_reservation`):
   - Consistent deadlock-safe locking order: OrganizationBillingPeriod FIRST, UsageReservation SECOND.
   - Exact reconciliation of reserved headroom vs actual consumption.
   - Releases unused reserved headroom back to billing period.
   - Creates immutable LLMUsageLog for regulatory and billing audits.
   - Idempotent replay: identical settlement replays return success without double-charging.
   - Conflicting replay: rejects divergent usage on already settled reservations.
   - Turn limit enforcement: rejects actual usage exceeding reservation or model ceiling.
   - Ambiguity quarantine: rejects zero-settlement of tasks in reconciliation_needed or with dispatched/ambiguous attempts.
3. Pre-dispatch release (`release_reservation`):
   - Safely releases all reserved tokens and cost for un-dispatched tasks.
   - Ambiguity quarantine: refuses release if task or attempts are in dispatched or ambiguous state.
   - Idempotent replay on already released reservations.
   - Rejects release of already settled reservations.
4. Transaction rollback semantics and non-negative counter safeguards.

Note:
These unit tests utilize an in-memory async mock session test double and synchronous
coroutine runner (`run_async`) to verify algorithmic correctness, state transitions,
integer arithmetic, and query lock semantics without requiring or connecting to a live
PostgreSQL database or requiring an OS network event loop. Live PostgreSQL concurrency,
foreign key cascades, partial unique indexes, and actual OS-level row locks remain unverified
without a live PostgreSQL instance.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar
import uuid

import pytest

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
import app.models.subscription  # noqa: F401
from app.services.billing.accounting import (
    AccountingErrorCode,
    ReservationResult,
    SettlementResult,
    release_reservation,
    reserve_usage,
    settle_reservation,
)
from app.services.billing.pricing import (
    MAX_MICROS_LIMIT,
    MOCK_GPT_4O_MINI_PRICING,
    ModelPricing,
)

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to immediate completion without requiring an OS event loop.

    This ensures complete test isolation and deterministic in-memory execution.
    """
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise RuntimeError(f"Coroutine {coro} yielded unexpectedly before completing")


# ============================================================================
# Test Doubles / In-Memory Mock Session
# ============================================================================


class MockResult:
    """Mock result object simulating SQLAlchemy's ChunkedIteratorResult."""

    def __init__(self, item: Any = None, items: list[Any] | None = None) -> None:
        self._item = item
        self._items = items if items is not None else ([item] if item is not None else [])

    def scalar_one_or_none(self) -> Any:
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
    """Deterministic in-memory async session test double.

    Emulates SQLAlchemy's execute, add, flush, and rollback mechanics while tracking
    executed statements and their lock modes (_for_update_arg).
    """

    def __init__(
        self,
        *,
        billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] | None = None,
        tasks: dict[uuid.UUID, GenerationTask] | None = None,
        reservations: dict[uuid.UUID, UsageReservation] | None = None,
        usage_logs: dict[uuid.UUID, LLMUsageLog] | None = None,
        attempts: list[GenerationTaskAttempt] | None = None,
    ) -> None:
        self.billing_periods = billing_periods or {}
        self.tasks = tasks or {}
        self.reservations = reservations or {}
        self.usage_logs = usage_logs or {}
        self.attempts = attempts or []
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

    async def flush(self) -> None:
        self.flushed = True

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

        # Query: select(UsageReservation.billing_period_id).where(UsageReservation.id == r_id)
        if name == "billing_period_id" and entity is UsageReservation:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.reservations:
                    return MockResult(self.reservations[v].billing_period_id)
            return MockResult(None)

        # Query: select(OrganizationBillingPeriod).where(...)
        if entity is OrganizationBillingPeriod:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.billing_periods:
                    return MockResult(self.billing_periods[v])
            return MockResult(None)

        # Query: select(GenerationTask).where(...)
        if entity is GenerationTask:
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.tasks:
                    return MockResult(self.tasks[v])
            return MockResult(None)

        # Query: select(UsageReservation)...
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

        # Query: select(LLMUsageLog).where(...)
        if entity is LLMUsageLog:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            log = next((l for l in self.usage_logs.values() if l.generation_task_id == t_id), None)
            return MockResult(log)

        # Query: select(GenerationTaskAttempt).where(...)
        if entity is GenerationTaskAttempt:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            matched = [att for att in self.attempts if att.generation_task_id == t_id]
            return MockResult(items=matched)

        return MockResult(None)


# ============================================================================
# Fixtures & Helpers
# ============================================================================


def make_test_billing_period(
    *,
    period_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    monthly_token_quota: int = 100_000,
    hard_spending_cap_micros: int | None = 1_000_000,  # $1.00 USD
    tokens_used: int = 0,
    tokens_reserved: int = 0,
    cost_micros_used: int = 0,
    cost_micros_reserved: int = 0,
    is_active: bool = True,
    is_blocked: bool = False,
    blocked_reason: str | None = None,
    now: datetime | None = None,
) -> OrganizationBillingPeriod:
    ref_time = now or datetime.now(timezone.utc)
    return OrganizationBillingPeriod(
        id=period_id or uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        period_start=ref_time - timedelta(days=5),
        period_end=ref_time + timedelta(days=25),
        monthly_token_quota=monthly_token_quota,
        hard_spending_cap_micros=hard_spending_cap_micros,
        tokens_used=tokens_used,
        tokens_reserved=tokens_reserved,
        cost_micros_used=cost_micros_used,
        cost_micros_reserved=cost_micros_reserved,
        is_active=is_active,
        is_blocked=is_blocked,
        blocked_reason=blocked_reason,
    )


def make_test_task(
    *,
    task_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    period_id: uuid.UUID | None = None,
    status: GenerationTaskStatus = GenerationTaskStatus.pending,
    lease_token: uuid.UUID | None = None,
) -> GenerationTask:
    return GenerationTask(
        id=task_id or uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        billing_period_id=period_id or uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        visitor_message_id=uuid.uuid4(),
        status=status,
        lease_token=lease_token or uuid.uuid4(),
    )


# ============================================================================
# 1. reserve_usage Tests
# ============================================================================


def test_reserve_usage_success() -> None:
    """Verifies successful quota reservation, period counter deduction, and reservation creation."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
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
    assert res.reservation.status == ReservationStatus.active
    assert res.reservation.organization_id == org_id
    assert res.reservation.billing_period_id == bp_id
    assert res.reservation.generation_task_id == t_id
    assert res.tokens_reserved == MOCK_GPT_4O_MINI_PRICING.max_turn_tokens
    assert res.cost_micros_reserved > 0

    # Verify billing period counters updated
    assert bp.tokens_reserved == res.tokens_reserved
    assert bp.cost_micros_reserved == res.cost_micros_reserved
    assert bp.tokens_used == 0
    assert bp.cost_micros_used == 0
    assert session.flushed is True

    # Verify row locking: OrganizationBillingPeriod locked with_for_update
    bp_query = next(q for q in session.executed_queries if q["entity"] is OrganizationBillingPeriod)
    assert bp_query["for_update"] is True


def test_reserve_usage_custom_ceilings() -> None:
    """Verifies custom turn ceilings are respected during reservation."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            custom_max_input_tokens=1_000,
            custom_max_output_tokens=500,
            now=now,
        )
    )

    assert res.success is True
    assert res.tokens_reserved == 1_500
    assert bp.tokens_reserved == 1_500


def test_reserve_usage_quota_exhausted() -> None:
    """Verifies QUOTA_EXHAUSTED when committed + requested tokens exceeds monthly quota."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Monthly quota is 5,000; mock ceiling requires 5,000 tokens, but 1,000 already used
    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        monthly_token_quota=5_000,
        tokens_used=1_000,
        tokens_reserved=0,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
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

    assert res.success is False
    assert res.error_code == AccountingErrorCode.QUOTA_EXHAUSTED
    assert res.reservation is None
    assert bp.tokens_reserved == 0  # untouched


def test_reserve_usage_spending_cap_exhausted() -> None:
    """Verifies SPENDING_CAP_EXHAUSTED when committed + requested cost exceeds hard cap."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Hard cap is 1,000 micros ($0.001); mock pricing ceiling cost is 3,000 micros
    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        hard_spending_cap_micros=1_000,
        cost_micros_used=500,
        cost_micros_reserved=0,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
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

    assert res.success is False
    assert res.error_code == AccountingErrorCode.SPENDING_CAP_EXHAUSTED
    assert res.reservation is None
    assert bp.cost_micros_reserved == 0  # untouched


def test_reserve_usage_ownership_mismatch() -> None:
    """Verifies INVALID_OWNERSHIP across org, task, and billing period combinations."""
    org_id_1 = uuid.uuid4()
    org_id_2 = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Case 1: Billing period belongs to org_id_2, but request specifies org_id_1
    bp_other = make_test_billing_period(period_id=bp_id, org_id=org_id_2, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id_1, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp_other}, tasks={t_id: task})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id_1,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_OWNERSHIP

    # Case 2: Task belongs to org_id_2, but request specifies org_id_1
    bp_same = make_test_billing_period(period_id=bp_id, org_id=org_id_1, now=now)
    task_other = make_test_task(task_id=t_id, org_id=org_id_2, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp_same}, tasks={t_id: task_other})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id_1,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_OWNERSHIP

    # Case 3: Task bound to different billing period
    different_bp_id = uuid.uuid4()
    task_diff_bp = make_test_task(task_id=t_id, org_id=org_id_1, period_id=different_bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp_same}, tasks={t_id: task_diff_bp})

    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id_1,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_OWNERSHIP


def test_reserve_usage_duplicate_active_reservation() -> None:
    """Verifies DUPLICATE_ACTIVE_RESERVATION when task already has an active reservation."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    existing_res = UsageReservation(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        lease_token=task.lease_token,
        tokens_reserved=5_000,
        cost_micros_reserved=1_000,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={existing_res.id: existing_res},
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
    assert res.error_code == AccountingErrorCode.DUPLICATE_ACTIVE_RESERVATION
    assert res.details.get("existing_reservation_id") == str(existing_res.id)


def test_reserve_usage_invalid_inputs_and_overflow() -> None:
    """Verifies rejection of malformed inputs, non-UUIDs, booleans, and integer overflow."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    session = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task})

    # Non-UUID string
    res = run_async(
        reserve_usage(
            session,
            organization_id="not-a-uuid",  # type: ignore[arg-type]
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_INPUT

    # Boolean masquerading as int / UUID
    res = run_async(
        reserve_usage(
            session,
            organization_id=True,  # type: ignore[arg-type]
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_INPUT

    # Non-ModelPricing object
    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing="invalid",  # type: ignore[arg-type]
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_INPUT

    # Negative custom ceiling
    res = run_async(
        reserve_usage(
            session,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            custom_max_input_tokens=-10,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_INPUT

    # Overflowing reserved counters
    bp_overflow = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        monthly_token_quota=MAX_MICROS_LIMIT + 1_000_000,
        tokens_reserved=MAX_MICROS_LIMIT - 10,
        hard_spending_cap_micros=None,
        now=now,
    )
    session_overflow = MockAsyncSession(billing_periods={bp_id: bp_overflow}, tasks={t_id: task})
    res_overflow = run_async(
        reserve_usage(
            session_overflow,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res_overflow.success is False
    assert res_overflow.error_code == AccountingErrorCode.INTEGER_OVERFLOW


def test_reserve_usage_inactive_blocked_expired_period() -> None:
    """Verifies rejection of inactive, blocked, or expired billing periods."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)

    # Inactive period
    bp_inactive = make_test_billing_period(period_id=bp_id, org_id=org_id, is_active=False, now=now)
    session = MockAsyncSession(billing_periods={bp_id: bp_inactive}, tasks={t_id: task})
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
    assert res.error_code == AccountingErrorCode.BILLING_PERIOD_INACTIVE

    # Blocked period
    bp_blocked = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        is_blocked=True,
        blocked_reason="Payment failure",
        now=now,
    )
    session = MockAsyncSession(billing_periods={bp_id: bp_blocked}, tasks={t_id: task})
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
    assert res.error_code == AccountingErrorCode.BILLING_PERIOD_BLOCKED
    assert "Payment failure" in (res.message or "")

    # Expired period (now > period_end)
    bp_expired = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    bp_expired.period_end = now - timedelta(hours=1)
    session = MockAsyncSession(billing_periods={bp_id: bp_expired}, tasks={t_id: task})
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
    assert res.error_code == AccountingErrorCode.BILLING_PERIOD_EXPIRED


def test_reserve_usage_task_not_pending_or_missing() -> None:
    """Verifies rejection if generation task is missing or in a non-pending state."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)

    # Missing task
    session_no_task = MockAsyncSession(billing_periods={bp_id: bp}, tasks={})
    res = run_async(
        reserve_usage(
            session_no_task,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.TASK_NOT_FOUND

    # Task completed already
    task_completed = make_test_task(
        task_id=t_id,
        org_id=org_id,
        period_id=bp_id,
        status=GenerationTaskStatus.completed,
    )
    session_completed = MockAsyncSession(billing_periods={bp_id: bp}, tasks={t_id: task_completed})
    res = run_async(
        reserve_usage(
            session_completed,
            organization_id=org_id,
            task_id=t_id,
            billing_period_id=bp_id,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_TASK_STATE


# ============================================================================
# 2. settle_reservation Tests
# ============================================================================


def test_settle_reservation_success() -> None:
    """Verifies successful settlement, headroom release, status transition, and audit log creation."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # 5,000 tokens reserved, 3,000 micros reserved
    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        tokens_reserved=5_000,
        cost_micros_reserved=3_000,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        lease_token=task.lease_token,
        tokens_reserved=5_000,
        cost_micros_reserved=3_000,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    # Settle with actual usage: 1,500 tokens (1,000 prompt + 500 completion), 450 micros
    res = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_500,
            actual_cost_micros=450,
            prompt_tokens=1_000,
            completion_tokens=500,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            latency_ms=320,
            now=now,
        )
    )

    assert res.success is True
    assert res.is_idempotent_replay is False
    assert res.actual_tokens == 1_500
    assert res.actual_cost_micros == 450
    assert res.tokens_released == 3_500  # 5,000 - 1,500
    assert res.cost_micros_released == 2_550  # 3,000 - 450

    # Verify billing period counters
    assert bp.tokens_reserved == 0
    assert bp.cost_micros_reserved == 0
    assert bp.tokens_used == 1_500
    assert bp.cost_micros_used == 450

    # Verify reservation status
    assert reservation.status == ReservationStatus.settled
    assert reservation.settled_at == now

    # Verify LLMUsageLog created and persisted
    assert res.usage_log is not None
    assert res.usage_log.organization_id == org_id
    assert res.usage_log.billing_period_id == bp_id
    assert res.usage_log.generation_task_id == t_id
    assert res.usage_log.prompt_tokens == 1_000
    assert res.usage_log.completion_tokens == 500
    assert res.usage_log.total_tokens == 1_500
    assert res.usage_log.cost_micros == 450
    assert res.usage_log.latency_ms == 320
    assert session.flushed is True


def test_settle_reservation_idempotent_replay() -> None:
    """Verifies that replaying an identical settlement returns success without double-charging."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        tokens_used=1_500,
        cost_micros_used=450,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=3_000,
        status=ReservationStatus.settled,
        settled_at=now - timedelta(minutes=5),
    )
    existing_log = LLMUsageLog(
        id=log_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        generation_task_id=t_id,
        prompt_tokens=1_000,
        completion_tokens=500,
        total_tokens=1_500,
        cost_micros=450,
        provider="mock",
        model="mock-gpt-4o-mini",
        pricing_version="dev-mock-2026-v1",
        latency_ms=250,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        usage_logs={log_id: existing_log},
    )

    # Replay identical settlement
    res = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_500,
            actual_cost_micros=450,
            now=now,
        )
    )

    assert res.success is True
    assert res.is_idempotent_replay is True
    assert res.usage_log == existing_log
    # Billing period counters must NOT be incremented again
    assert bp.tokens_used == 1_500
    assert bp.cost_micros_used == 450


def test_settle_reservation_conflicting_replay() -> None:
    """Verifies that replaying a settlement with differing values fails with CONFLICTING_SETTLEMENT."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=3_000,
        status=ReservationStatus.settled,
        settled_at=now - timedelta(minutes=5),
    )
    existing_log = LLMUsageLog(
        id=log_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        generation_task_id=t_id,
        total_tokens=1_500,
        cost_micros=450,
        provider="mock",
        model="mock-gpt-4o-mini",
        pricing_version="dev-mock-2026-v1",
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        usage_logs={log_id: existing_log},
    )

    # Attempt to settle with divergent token count (3,000 instead of 1,500)
    res = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=3_000,
            actual_cost_micros=450,
            now=now,
        )
    )

    assert res.success is False
    assert res.error_code == AccountingErrorCode.CONFLICTING_SETTLEMENT
    assert res.details.get("settled_tokens") == 1_500
    assert res.details.get("attempted_tokens") == 3_000


def test_settle_reservation_exceeds_turn_limits() -> None:
    """Verifies RECONCILIATION_REQUIRED when actual usage exceeds reservation, preserving liability."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    # Case 1: Actual tokens exceed reserved tokens -> RECONCILIATION_REQUIRED and task quarantined
    res_tokens = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=6_000,
            actual_cost_micros=1_000,
            now=now,
        )
    )
    assert res_tokens.success is False
    assert res_tokens.error_code == AccountingErrorCode.RECONCILIATION_REQUIRED
    # Invariant 1: Liability preserved, reservation remains active, task quarantined
    assert reservation.status == ReservationStatus.active
    assert bp.tokens_reserved == 5_000
    assert bp.cost_micros_reserved == 1_500
    assert task.status == GenerationTaskStatus.reconciliation_needed
    assert "Settlement exceeded reservation" in (task.last_error or "")

    # Reset task status for Case 2
    task.status = GenerationTaskStatus.pending
    # Case 2: Actual cost exceeds reserved cost -> RECONCILIATION_REQUIRED and task quarantined
    res_cost = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=4_000,
            actual_cost_micros=2_000,
            now=now,
        )
    )
    assert res_cost.success is False
    assert res_cost.error_code == AccountingErrorCode.RECONCILIATION_REQUIRED
    assert reservation.status == ReservationStatus.active
    assert bp.cost_micros_reserved == 1_500
    assert task.status == GenerationTaskStatus.reconciliation_needed

    # Case 3: Actual tokens exceed model ceiling (even if within reservation) -> EXCEEDS_TURN_LIMITS
    tight_pricing = ModelPricing(
        model_name="tight-mock",
        pricing_version="v1",
        input_rate_micros_per_million=150_000,
        output_rate_micros_per_million=600_000,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
    )
    res_ceiling = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=2_500,  # exceeds tight_pricing.max_turn_tokens = 2,000
            actual_cost_micros=1_000,
            pricing=tight_pricing,
            now=now,
        )
    )
    assert res_ceiling.success is False
    assert res_ceiling.error_code == AccountingErrorCode.EXCEEDS_TURN_LIMITS


def test_settle_reservation_prompt_completion_mismatch() -> None:
    """Verifies INVALID_INPUT when prompt_tokens + completion_tokens != actual_tokens."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    res = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_000,
            actual_cost_micros=300,
            prompt_tokens=600,
            completion_tokens=500,  # sum is 1,100 != 1,000
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_INPUT


# ============================================================================
# 3. Ambiguity & Quarantine Tests
# ============================================================================


def test_settle_and_release_ambiguous_quarantine() -> None:
    """Verifies that tasks with ambiguous or dispatched attempts cannot be zero-settled or released."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )

    # Attempt 1 is in DISPATCHED status
    attempt_dispatched = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        attempt_number=1,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.dispatched,
        dispatched_at=now - timedelta(seconds=30),
    )

    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[attempt_dispatched],
    )

    # 1. Zero-settle must be rejected
    res_settle = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=0,
            actual_cost_micros=0,
            now=now,
        )
    )
    assert res_settle.success is False
    assert res_settle.error_code == AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE
    assert "dispatched" in (res_settle.message or "")

    # 2. Release must be rejected
    res_release = run_async(
        release_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            now=now,
        )
    )
    assert res_release.success is False
    assert res_release.error_code == AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE
    assert "dispatched" in (res_release.message or "")

    # 3. Task in reconciliation_needed state must also be rejected
    task.status = GenerationTaskStatus.reconciliation_needed
    session_recon = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[],  # no attempts, but task status is reconciliation_needed
    )
    res_recon = run_async(
        release_reservation(
            session_recon,
            reservation_id=r_id,
            organization_id=org_id,
            now=now,
        )
    )
    assert res_recon.success is False
    assert res_recon.error_code == AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE


# ============================================================================
# 4. release_reservation Tests
# ============================================================================


def test_release_reservation_clean_success() -> None:
    """Verifies clean release of un-dispatched reservation, returning all reserved headroom."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        tokens_reserved=10_000,
        cost_micros_reserved=3_000,
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=10_000,
        cost_micros_reserved=3_000,
        status=ReservationStatus.active,
    )
    # Attempt failed before dispatch: clean to release
    attempt_failed = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        attempt_number=1,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.failed,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[attempt_failed],
    )

    res = run_async(
        release_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            now=now,
        )
    )

    assert res.success is True
    assert res.is_idempotent_replay is False
    assert res.tokens_released == 10_000
    assert res.cost_micros_released == 3_000
    assert bp.tokens_reserved == 0
    assert bp.cost_micros_reserved == 0
    assert reservation.status == ReservationStatus.released
    assert reservation.settled_at == now
    assert session.flushed is True

    # Replay of release must be idempotent
    res_replay = run_async(
        release_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            now=now,
        )
    )
    assert res_replay.success is True
    assert res_replay.is_idempotent_replay is True


def test_release_reservation_invalid_states() -> None:
    """Verifies that already settled reservations cannot be released."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation_settled = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=10_000,
        cost_micros_reserved=3_000,
        status=ReservationStatus.settled,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation_settled},
    )

    res = run_async(
        release_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            now=now,
        )
    )
    assert res.success is False
    assert res.error_code == AccountingErrorCode.INVALID_TASK_STATE


# ============================================================================
# 5. Row-Lock Ordering & Deadlock Freedom Tests
# ============================================================================


def test_deadlock_safe_lock_ordering() -> None:
    """Verifies that settle_reservation locks OrganizationBillingPeriod FIRST, UsageReservation SECOND."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, tokens_reserved=5_000, cost_micros_reserved=1_500, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_000,
            actual_cost_micros=300,
            now=now,
        )
    )

    # Filter queries executed with FOR UPDATE
    locked_queries = [q for q in session.executed_queries if q["for_update"] is True]
    assert len(locked_queries) >= 2

    # Invariant: OrganizationBillingPeriod must be locked BEFORE UsageReservation
    first_locked_entity = locked_queries[0]["entity"]
    second_locked_entity = locked_queries[1]["entity"]
    assert first_locked_entity is OrganizationBillingPeriod
    assert second_locked_entity is UsageReservation


# ============================================================================
# 6. Session Rollback Behavior Tests
# ============================================================================


def test_accounting_session_rollback_behavior() -> None:
    """Verifies that an error encountered during reservation allows clean rollback."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, monthly_token_quota=100, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
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

    assert res.success is False
    assert res.error_code == AccountingErrorCode.QUOTA_EXHAUSTED

    # Rollback invoked by caller on failure
    run_async(session.rollback())
    assert session.rolled_back is True
    assert bp.tokens_reserved == 0


# ============================================================================
# 7. Focused Invariant Audit Tests
# ============================================================================


def test_settle_reservation_zero_usage_replay_conflict() -> None:
    """Verifies that settling with 0 tokens cannot be replayed with positive tokens (Invariant 5)."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, tokens_reserved=5_000, cost_micros_reserved=1_500, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    # Failed attempt: safe to zero-settle
    attempt_failed = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        attempt_number=1,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.failed,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[attempt_failed],
    )

    # Initial zero settlement
    res_zero = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=0,
            actual_cost_micros=0,
            now=now,
        )
    )
    assert res_zero.success is True
    assert reservation.status == ReservationStatus.settled

    # Replay with identical 0 tokens succeeds as idempotent
    res_zero_replay = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=0,
            actual_cost_micros=0,
            now=now,
        )
    )
    assert res_zero_replay.success is True
    assert res_zero_replay.is_idempotent_replay is True

    # Replay with 500 tokens MUST be rejected as CONFLICTING_SETTLEMENT
    res_conflict = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=500,
            actual_cost_micros=150,
            now=now,
        )
    )
    assert res_conflict.success is False
    assert res_conflict.error_code == AccountingErrorCode.CONFLICTING_SETTLEMENT


def test_settle_reservation_token_composition_guards() -> None:
    """Verifies that individual prompt/completion tokens cannot exceed total actual tokens (Invariant 3)."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, tokens_reserved=5_000, cost_micros_reserved=1_500, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    # Prompt tokens alone exceeding actual tokens
    res_p_overflow = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_000,
            actual_cost_micros=300,
            prompt_tokens=1_200,
            now=now,
        )
    )
    assert res_p_overflow.success is False
    assert res_p_overflow.error_code == AccountingErrorCode.INVALID_INPUT

    # Completion tokens alone exceeding actual tokens
    res_c_overflow = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=1_000,
            actual_cost_micros=300,
            completion_tokens=1_200,
            now=now,
        )
    )
    assert res_c_overflow.success is False
    assert res_c_overflow.error_code == AccountingErrorCode.INVALID_INPUT

    # Prompt tokens exceeding model pricing max_input_tokens
    tight_pricing = ModelPricing(
        model_name="tight-mock",
        pricing_version="v1",
        input_rate_micros_per_million=150_000,
        output_rate_micros_per_million=600_000,
        max_input_tokens=500,
        max_output_tokens=2_000,
    )
    res_input_ceiling = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=800,
            actual_cost_micros=300,
            prompt_tokens=600,  # exceeds tight_pricing.max_input_tokens = 500
            completion_tokens=200,
            pricing=tight_pricing,
            now=now,
        )
    )
    assert res_input_ceiling.success is False
    assert res_input_ceiling.error_code == AccountingErrorCode.EXCEEDS_TURN_LIMITS


def test_release_reservation_quarantines_claimed_and_completed_attempts() -> None:
    """Verifies that attempts in claimed, completed, or stale_received block release (Invariant 6)."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id, tokens_reserved=5_000, cost_micros_reserved=1_500, now=now)
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )

    # Case A: Attempt in claimed status
    att_claimed = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        attempt_number=1,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.claimed,
    )
    session_claimed = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[att_claimed],
    )
    res_claimed = run_async(release_reservation(session_claimed, reservation_id=r_id, organization_id=org_id, now=now))
    assert res_claimed.success is False
    assert res_claimed.error_code == AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE

    # Case B: Attempt in completed status
    att_completed = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=t_id,
        attempt_number=1,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.completed,
    )
    session_completed = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task},
        reservations={r_id: reservation},
        attempts=[att_completed],
    )
    res_completed = run_async(release_reservation(session_completed, reservation_id=r_id, organization_id=org_id, now=now))
    assert res_completed.success is False
    assert res_completed.error_code == AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE


def test_ownership_validation_task_mismatch_in_settle_and_release() -> None:
    """Verifies that mismatched task organization fails with INVALID_OWNERSHIP (Invariant 7)."""
    org_id_1 = uuid.uuid4()
    org_id_2 = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    bp = make_test_billing_period(period_id=bp_id, org_id=org_id_1, tokens_reserved=5_000, cost_micros_reserved=1_500, now=now)
    # Task belongs to org_id_2
    task_mismatch = make_test_task(task_id=t_id, org_id=org_id_2, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id_1,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp},
        tasks={t_id: task_mismatch},
        reservations={r_id: reservation},
    )

    # Settle check
    res_settle = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id_1,
            actual_tokens=1_000,
            actual_cost_micros=300,
            now=now,
        )
    )
    assert res_settle.success is False
    assert res_settle.error_code == AccountingErrorCode.INVALID_OWNERSHIP

    # Release check
    res_release = run_async(
        release_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id_1,
            now=now,
        )
    )
    assert res_release.success is False
    assert res_release.error_code == AccountingErrorCode.INVALID_OWNERSHIP


def test_counter_underflow_prevention_on_corrupted_period() -> None:
    """Verifies that inconsistent period reserved counters fail cleanly rather than underflow (Invariant 2)."""
    org_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    t_id = uuid.uuid4()
    r_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Billing period reserved counters are less than the reservation being settled/released
    bp_corrupt = make_test_billing_period(
        period_id=bp_id,
        org_id=org_id,
        tokens_reserved=1_000,  # Reservation has 5,000!
        cost_micros_reserved=500,  # Reservation has 1,500!
        now=now,
    )
    task = make_test_task(task_id=t_id, org_id=org_id, period_id=bp_id)
    reservation = UsageReservation(
        id=r_id,
        generation_task_id=t_id,
        organization_id=org_id,
        billing_period_id=bp_id,
        tokens_reserved=5_000,
        cost_micros_reserved=1_500,
        status=ReservationStatus.active,
    )
    session = MockAsyncSession(
        billing_periods={bp_id: bp_corrupt},
        tasks={t_id: task},
        reservations={r_id: reservation},
    )

    res_settle = run_async(
        settle_reservation(
            session,
            reservation_id=r_id,
            organization_id=org_id,
            actual_tokens=500,
            actual_cost_micros=150,
            now=now,
        )
    )
    assert res_settle.success is False
    assert res_settle.error_code == AccountingErrorCode.INVALID_TASK_STATE
    assert "less than reservation being settled" in (res_settle.message or "")
