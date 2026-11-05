"""Unit Tests for the Durable Stripe Webhook Processing Worker.

Covers Step 4.6B requirements:
1. Worker claims and processes a pending event.
2. Duplicate event delivery handling.
3. Multiple workers competing for the same event (concurrency mutual exclusion).
4. Transient processing failure and bounded retry.
5. Quarantined events not being repeatedly processed.
6. Graceful worker shutdown via stop signal.
7. Event recovery after worker restart (abandoned uncommitted lease recovery).
8. Configurable batch size and single-pass execution.
9. Unknown event type quarantine without crashing worker.
"""

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
from datetime import datetime, timedelta, timezone
from typing import Any
import pytest

import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
from app.models.subscription import StripeWebhookEvent, WebhookProcessingStatus
from app.services.billing.stripe_webhook_engine import (
    MAX_WEBHOOK_ATTEMPTS,
    WEBHOOK_RETRY_BACKOFF_SCHEDULE,
    ClaimResult,
    ClaimStatus,
    WebhookDispatcher,
    WebhookPermanentError,
    WebhookRetryableError,
    get_retry_backoff_seconds,
    is_webhook_event_eligible,
)
from app.workers.stripe_webhook_worker import StripeWebhookWorker


# =============================================================================
# In-Memory Concurrency Test Doubles
# =============================================================================


class WorkerMockSavepoint:
    """Simulates SQLAlchemy async savepoint (begin_nested)."""

    def __init__(self, session: "WorkerMockDbSession") -> None:
        self.session = session
        self.event_snapshots: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "WorkerMockSavepoint":
        self.event_snapshots = {
            eid: {
                "status": ev.status,
                "attempts": ev.attempts,
                "error_message": ev.error_message,
                "processed_at": ev.processed_at,
            }
            for eid, ev in self.session.repo.events.items()
        }
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            # Rollback event states prior to savepoint
            for eid, snapshot in self.event_snapshots.items():
                ev = self.session.repo.events.get(eid)
                if ev:
                    for attr, val in snapshot.items():
                        setattr(ev, attr, val)
            return False  # Propagate exception to outer transaction
        return False


class WorkerMockScalarResult:
    """Simulates SQLAlchemy scalar_one_or_none result."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def scalar_one_or_none(self) -> Any:
        return self._item


class SharedEventRepository:
    """Shared database storage accessible across multiple competing worker sessions."""

    def __init__(self, events: list[StripeWebhookEvent] | None = None) -> None:
        self.events: dict[str, StripeWebhookEvent] = {
            e.id: e for e in (events or [])
        }
        self.locked_ids: set[str] = set()

    def claim_next(
        self,
        session_id: str,
        max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
        now: datetime | None = None,
    ) -> StripeWebhookEvent | None:
        for ev in sorted(self.events.values(), key=lambda x: x.created_at):
            if ev.id in self.locked_ids:
                continue
            if is_webhook_event_eligible(ev, now=now, max_attempts=max_attempts):
                self.locked_ids.add(ev.id)
                return ev
        return None

    def release_lock(self, event_id: str) -> None:
        self.locked_ids.discard(event_id)


class WorkerMockDbSession:
    """In-memory async session test double for worker testing."""

    _counter = 0

    def __init__(
        self,
        repo: SharedEventRepository,
        clock_now: datetime | None = None,
    ) -> None:
        WorkerMockDbSession._counter += 1
        self.session_id = f"session_{WorkerMockDbSession._counter}"
        self.repo = repo
        self.clock_now = clock_now
        self.held_locks: set[str] = set()
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "WorkerMockDbSession":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self.committed:
            await self.rollback()

    async def execute(self, stmt: Any) -> WorkerMockScalarResult:
        where_clauses = getattr(stmt, "_where_criteria", ())
        target_event = None

        # Check if querying a specific event by ID
        for criterion in where_clauses:
            if hasattr(criterion, "right") and hasattr(criterion.right, "value"):
                val = criterion.right.value
                if isinstance(val, str) and val in self.repo.events:
                    target_event = self.repo.events[val]
                    break

        if target_event is None:
            # Queue claim query (SELECT ... FOR UPDATE SKIP LOCKED)
            target_event = self.repo.claim_next(self.session_id, now=self.clock_now)
            if target_event:
                self.held_locks.add(target_event.id)
        else:
            if target_event.id not in self.repo.locked_ids:
                self.repo.locked_ids.add(target_event.id)
                self.held_locks.add(target_event.id)

        return WorkerMockScalarResult(target_event)

    def begin_nested(self) -> WorkerMockSavepoint:
        return WorkerMockSavepoint(self)

    async def commit(self) -> None:
        self.committed = True
        for eid in list(self.held_locks):
            self.repo.release_lock(eid)
        self.held_locks.clear()

    async def rollback(self) -> None:
        self.rolled_back = True
        for eid in list(self.held_locks):
            self.repo.release_lock(eid)
        self.held_locks.clear()

    async def refresh(self, instance: Any) -> None:
        pass


def make_test_event(
    event_id: str = "evt_test_1",
    event_type: str = "invoice.paid",
    status: WebhookProcessingStatus = WebhookProcessingStatus.pending,
    attempts: int = 1,
    payload: dict[str, Any] | None = None,
    processed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> StripeWebhookEvent:
    """Helper to instantiate an in-memory StripeWebhookEvent."""
    return StripeWebhookEvent(
        id=event_id,
        event_type=event_type,
        status=status,
        attempts=attempts,
        payload=payload or {"id": event_id, "type": event_type, "data": {}},
        created_at=created_at or datetime.now(timezone.utc),
        processed_at=processed_at,
    )


# =============================================================================
# Test Suite
# =============================================================================


@pytest.mark.asyncio
async def test_worker_claims_and_processes_pending_event() -> None:
    """Verify worker picks up a pending event, calls handler, and marks it processed."""
    event = make_test_event(event_id="evt_pending_1", event_type="invoice.paid")
    repo = SharedEventRepository([event])

    processed_payloads: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any], db: Any) -> None:
        processed_payloads.append(payload)

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
        batch_size=5,
    )

    result = await worker.process_one()

    assert result is not None
    assert result.success is True
    assert result.status == WebhookProcessingStatus.processed
    assert result.event_id == "evt_pending_1"
    assert len(processed_payloads) == 1
    assert event.status == WebhookProcessingStatus.processed
    assert event.processed_at is not None
    assert event.error_message is None


@pytest.mark.asyncio
async def test_worker_handles_duplicate_or_already_processed_event() -> None:
    """Verify already processed events are never picked up or processed again."""
    event = make_test_event(
        event_id="evt_processed_1",
        status=WebhookProcessingStatus.processed,
        attempts=1,
    )
    repo = SharedEventRepository([event])

    dispatcher = WebhookDispatcher()
    call_count = 0

    async def handler(payload: dict[str, Any], db: Any) -> None:
        nonlocal call_count
        call_count += 1

    dispatcher.register("invoice.paid", handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    result = await worker.process_one()

    # Nothing to claim because queue has no pending/failed events
    assert result is None
    assert call_count == 0
    assert event.status == WebhookProcessingStatus.processed


@pytest.mark.asyncio
async def test_multiple_workers_competing_for_same_event() -> None:
    """Verify multiple workers cannot claim the same event simultaneously."""
    event = make_test_event(event_id="evt_contested_1")
    repo = SharedEventRepository([event])

    execution_count = 0

    async def slow_handler(payload: dict[str, Any], db: Any) -> None:
        nonlocal execution_count
        execution_count += 1
        await asyncio.sleep(0.05)

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", slow_handler)

    worker_1 = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )
    worker_2 = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    # Launch both workers concurrently on the single event
    res_1, res_2 = await asyncio.gather(
        worker_1.process_one(),
        worker_2.process_one(),
    )

    # Exactly one worker claims the event; the competing worker receives None
    outcomes = [res_1, res_2]
    claimed_results = [r for r in outcomes if r is not None]
    unclaimed_results = [r for r in outcomes if r is None]

    assert len(claimed_results) == 1
    assert len(unclaimed_results) == 1
    assert claimed_results[0].success is True
    assert execution_count == 1
    assert event.status == WebhookProcessingStatus.processed


@pytest.mark.asyncio
async def test_transient_processing_failure_and_retry() -> None:
    """Verify transient error marks event failed and increments attempts for subsequent retry."""
    event = make_test_event(event_id="evt_retry_1", attempts=1)
    repo = SharedEventRepository([event])

    attempts_made = 0

    async def flaky_handler(payload: dict[str, Any], db: Any) -> None:
        nonlocal attempts_made
        attempts_made += 1
        if attempts_made == 1:
            raise WebhookRetryableError("Temporary payment gateway timeout")
        # Succeed on retry
        return

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", flaky_handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
        max_attempts=3,
    )

    # First attempt: failure
    res_1 = await worker.process_one()
    assert res_1 is not None
    assert res_1.success is False
    assert res_1.status == WebhookProcessingStatus.failed
    assert event.status == WebhookProcessingStatus.failed
    assert event.attempts == 2

    # Fast-forward time past retry backoff delay to make event eligible for retry
    event.processed_at = datetime.now(timezone.utc) - timedelta(seconds=20)

    # Second attempt: recovery
    res_2 = await worker.process_one()
    assert res_2 is not None
    assert res_2.success is True
    assert res_2.status == WebhookProcessingStatus.processed
    assert event.status == WebhookProcessingStatus.processed
    assert event.attempts == 2
    assert attempts_made == 2


@pytest.mark.asyncio
async def test_quarantined_events_not_repeatedly_processed() -> None:
    """Verify permanent failure immediately quarantines and is not re-processed."""
    event = make_test_event(event_id="evt_perm_1", attempts=1)
    repo = SharedEventRepository([event])

    call_count = 0

    async def failing_handler(payload: dict[str, Any], db: Any) -> None:
        nonlocal call_count
        call_count += 1
        raise WebhookPermanentError("Unrecoverable data mismatch")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", failing_handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    # First run quarantines
    res = await worker.process_one()
    assert res is not None
    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert event.status == WebhookProcessingStatus.quarantined
    assert call_count == 1

    # Second run ignores quarantined event
    res_again = await worker.process_one()
    assert res_again is None
    assert call_count == 1


@pytest.mark.asyncio
async def test_graceful_worker_shutdown() -> None:
    """Verify stop() cleanly terminates worker loop without dropping in-flight work."""
    repo = SharedEventRepository([])
    dispatcher = WebhookDispatcher()

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.05,
    )

    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.02)
    assert worker.is_running is True

    worker.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert worker.is_running is False


@pytest.mark.asyncio
async def test_event_recovery_after_worker_restart() -> None:
    """Verify crash or uncommitted transaction releases row lock so restarted worker recovers."""
    event = make_test_event(event_id="evt_crash_1")
    repo = SharedEventRepository([event])

    dispatcher = WebhookDispatcher()

    # Simulate crashed worker: claims event, but aborts session without commit
    async with WorkerMockDbSession(repo) as db:
        claim_res = await db.execute(None)
        assert claim_res.scalar_one_or_none() is not None
        assert "evt_crash_1" in repo.locked_ids
        # Worker crashes here: context manager exits without commit, triggering rollback

    # Upon session rollback, lock is released and event remains pending
    assert "evt_crash_1" not in repo.locked_ids
    assert event.status == WebhookProcessingStatus.pending

    # Restarted worker picks up and processes the abandoned event
    async def recover_handler(payload: dict[str, Any], db: Any) -> None:
        pass

    dispatcher.register("invoice.paid", recover_handler)

    new_worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    result = await new_worker.process_one()
    assert result is not None
    assert result.success is True
    assert event.status == WebhookProcessingStatus.processed


@pytest.mark.asyncio
async def test_worker_run_once_batch_limit() -> None:
    """Verify run_once processes up to batch_size and leaves remaining events queued."""
    events = [make_test_event(event_id=f"evt_batch_{i}") for i in range(5)]
    repo = SharedEventRepository(events)

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", lambda p, d: asyncio.sleep(0))

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
        batch_size=3,
    )

    processed_count = await worker.run_once()
    assert processed_count == 3

    processed_events = [e for e in repo.events.values() if e.status == WebhookProcessingStatus.processed]
    pending_events = [e for e in repo.events.values() if e.status == WebhookProcessingStatus.pending]

    assert len(processed_events) == 3
    assert len(pending_events) == 2


@pytest.mark.asyncio
async def test_worker_unknown_event_type_quarantined() -> None:
    """Verify events with unregistered event types are safely quarantined without crashing."""
    event = make_test_event(event_id="evt_unknown_1", event_type="unregistered.custom.event")
    repo = SharedEventRepository([event])

    empty_dispatcher = WebhookDispatcher()

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=empty_dispatcher,
        poll_interval=0.01,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert event.status == WebhookProcessingStatus.quarantined
    assert "no handler registered" in (event.error_message or "").lower()


# =============================================================================
# Step 4.6B.2: Bounded Webhook Retry Backoff Tests
# =============================================================================


@pytest.mark.asyncio
async def test_pending_events_immediately_eligible() -> None:
    """Requirement: Pending events must remain immediately eligible without delay."""
    # Pending event with attempts=1 and processed_at=None
    event = make_test_event(
        event_id="evt_pending_immediate",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        processed_at=None,
    )
    repo = SharedEventRepository([event])

    # Direct helper verification: immediately eligible
    assert is_webhook_event_eligible(event) is True

    processed_payloads: list[dict[str, Any]] = []

    async def test_handler(payload: dict[str, Any], db: Any) -> None:
        processed_payloads.append(payload)

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", test_handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    # Worker claims and processes immediately on first attempt
    res = await worker.process_one()
    assert res is not None
    assert res.success is True
    assert res.status == WebhookProcessingStatus.processed
    assert len(processed_payloads) == 1
    assert event.status == WebhookProcessingStatus.processed
    assert event.processed_at is not None


@pytest.mark.asyncio
async def test_failed_events_ineligible_before_retry_delay() -> None:
    """Requirement: Failed events are ineligible for claiming before the retry backoff delay elapses."""
    now = datetime.now(timezone.utc)
    # Event failed 2 seconds ago on attempt 1 (requires 5s backoff delay)
    event = make_test_event(
        event_id="evt_ineligible_early",
        status=WebhookProcessingStatus.failed,
        attempts=1,
        processed_at=now,
    )
    repo = SharedEventRepository([event])

    # Check 2 seconds later (ineligible: 2s < 5s)
    check_time = now + timedelta(seconds=2)
    assert is_webhook_event_eligible(event, now=check_time) is False

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", lambda p, d: asyncio.sleep(0))

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo, clock_now=check_time),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    # Worker attempts to claim; must receive None because event is in backoff window
    claim_result = await worker.process_one()
    assert claim_result is None
    assert event.status == WebhookProcessingStatus.failed
    assert event.id not in repo.locked_ids


@pytest.mark.asyncio
async def test_failed_events_eligible_after_retry_delay() -> None:
    """Requirement: Failed events become eligible for claiming once the backoff delay elapses."""
    now = datetime.now(timezone.utc)
    # Event failed 6 seconds ago on attempt 1 (requires 5s backoff delay)
    event = make_test_event(
        event_id="evt_eligible_after_delay",
        status=WebhookProcessingStatus.failed,
        attempts=1,
        processed_at=now - timedelta(seconds=6),
    )
    repo = SharedEventRepository([event])

    # Check at current time: 6s elapsed >= 5s required delay -> eligible
    assert is_webhook_event_eligible(event, now=now) is True

    processed_calls: list[str] = []

    async def recover_handler(payload: dict[str, Any], db: Any) -> None:
        processed_calls.append(payload["id"])

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", recover_handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        poll_interval=0.01,
    )

    # Worker claims and processes the eligible event
    claim_result = await worker.process_one()
    assert claim_result is not None
    assert claim_result.success is True
    assert claim_result.status == WebhookProcessingStatus.processed
    assert len(processed_calls) == 1
    assert event.status == WebhookProcessingStatus.processed


def test_increasing_retry_delays_exponential_backoff() -> None:
    """Requirement: Increasing retry delays follow bounded schedule (5s, 15s, 45s, 120s)."""
    assert get_retry_backoff_seconds(1) == 5
    assert get_retry_backoff_seconds(2) == 15
    assert get_retry_backoff_seconds(3) == 45
    assert get_retry_backoff_seconds(4) == 120
    assert get_retry_backoff_seconds(5) == 120

    base_time = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

    # Attempt 1: 5s delay
    ev1 = make_test_event("e1", status=WebhookProcessingStatus.failed, attempts=1, processed_at=base_time)
    assert is_webhook_event_eligible(ev1, now=base_time + timedelta(seconds=4.9)) is False
    assert is_webhook_event_eligible(ev1, now=base_time + timedelta(seconds=5.0)) is True

    # Attempt 2: 15s delay
    ev2 = make_test_event("e2", status=WebhookProcessingStatus.failed, attempts=2, processed_at=base_time)
    assert is_webhook_event_eligible(ev2, now=base_time + timedelta(seconds=14.9)) is False
    assert is_webhook_event_eligible(ev2, now=base_time + timedelta(seconds=15.0)) is True

    # Attempt 3: 45s delay
    ev3 = make_test_event("e3", status=WebhookProcessingStatus.failed, attempts=3, processed_at=base_time)
    assert is_webhook_event_eligible(ev3, now=base_time + timedelta(seconds=44.9)) is False
    assert is_webhook_event_eligible(ev3, now=base_time + timedelta(seconds=45.0)) is True

    # Attempt 4: 120s delay
    ev4 = make_test_event("e4", status=WebhookProcessingStatus.failed, attempts=4, processed_at=base_time)
    assert is_webhook_event_eligible(ev4, now=base_time + timedelta(seconds=119.9)) is False
    assert is_webhook_event_eligible(ev4, now=base_time + timedelta(seconds=120.0)) is True


@pytest.mark.asyncio
async def test_maximum_attempts_quarantine() -> None:
    """Requirement: Attempt 5 failure transitions event to quarantined, never retried."""
    now = datetime.now(timezone.utc)

    # Event already quarantined
    quarantined_ev = make_test_event(
        "evt_quar",
        status=WebhookProcessingStatus.quarantined,
        attempts=5,
        processed_at=now - timedelta(days=30),
    )
    # Even after 30 days, quarantined event is never eligible
    assert is_webhook_event_eligible(quarantined_ev, now=now) is False

    # Event with attempts >= max_attempts and status=failed is also ineligible
    failed_max_ev = make_test_event(
        "evt_max_fail",
        status=WebhookProcessingStatus.failed,
        attempts=5,
        processed_at=now - timedelta(days=30),
    )
    assert is_webhook_event_eligible(failed_max_ev, now=now, max_attempts=5) is False

    # Verify execution of 5th attempt failure triggers quarantine
    ev_attempt_4 = make_test_event(
        "evt_quarantine_run",
        status=WebhookProcessingStatus.failed,
        attempts=4,
        processed_at=now - timedelta(seconds=200),
    )
    repo = SharedEventRepository([ev_attempt_4])

    async def fail_handler(payload: dict[str, Any], db: Any) -> None:
        raise WebhookRetryableError("Persistent downstream outage")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", fail_handler)

    worker = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        max_attempts=5,
    )

    # Attempt 5: fails and triggers quarantine threshold
    res = await worker.process_one()
    assert res is not None
    assert res.success is False
    assert res.status == WebhookProcessingStatus.quarantined
    assert ev_attempt_4.status == WebhookProcessingStatus.quarantined
    assert ev_attempt_4.attempts == 5
    assert "exceeded max attempts" in (ev_attempt_4.error_message or "").lower()

    # Subsequent worker run skips quarantined event
    res_next = await worker.process_one()
    assert res_next is None


@pytest.mark.asyncio
async def test_multiple_worker_contention_with_retry_backoff() -> None:
    """Requirement: Multiple workers use SELECT FOR UPDATE SKIP LOCKED with retry backoff."""
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(seconds=100)

    # ev1: failed 2s ago, attempts=1 (needs 5s delay -> INELIGIBLE)
    ev1 = make_test_event(
        "evt_ineligible_1",
        status=WebhookProcessingStatus.failed,
        attempts=1,
        processed_at=now - timedelta(seconds=2),
        created_at=t0,
    )
    # ev2: failed 20s ago, attempts=2 (needs 15s delay -> ELIGIBLE!)
    ev2 = make_test_event(
        "evt_eligible_2",
        status=WebhookProcessingStatus.failed,
        attempts=2,
        processed_at=now - timedelta(seconds=20),
        created_at=t0 + timedelta(seconds=1),
    )
    # ev3: pending (needs 0s delay -> ELIGIBLE!)
    ev3 = make_test_event(
        "evt_eligible_3",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        processed_at=None,
        created_at=t0 + timedelta(seconds=2),
    )

    repo = SharedEventRepository([ev1, ev2, ev3])

    claimed_events: list[str] = []

    async def slow_handler(payload: dict[str, Any], db: Any) -> None:
        claimed_events.append(payload["id"])
        await asyncio.sleep(0.05)

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", slow_handler)

    worker_a = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
    )
    worker_b = StripeWebhookWorker(
        session_factory=lambda: WorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
    )

    # Launch both workers concurrently
    res_a, res_b = await asyncio.gather(
        worker_a.process_one(),
        worker_b.process_one(),
    )

    # Both workers claimed an event
    assert res_a is not None
    assert res_b is not None
    assert res_a.success is True
    assert res_b.success is True

    # Mutually exclusive: one worker got ev2, other got ev3
    processed_ids = {res_a.event_id, res_b.event_id}
    assert processed_ids == {"evt_eligible_2", "evt_eligible_3"}

    # ev1 was NEVER claimed because it is in its 5s backoff window
    assert ev1.status == WebhookProcessingStatus.failed
    assert "evt_ineligible_1" not in processed_ids
    assert "evt_ineligible_1" not in repo.locked_ids
