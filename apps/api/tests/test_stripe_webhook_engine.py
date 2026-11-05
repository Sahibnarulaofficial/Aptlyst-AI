"""Comprehensive Unit Tests for the Stripe Webhook Processing Engine.

Covers Stage 4.3B Step 3B.9B.1 requirements:
1. Pending event can be claimed.
2. Processed event cannot be claimed again.
3. Quarantined event cannot be claimed.
4. Failed event follows the defined retry policy (retryable vs max attempts).
5. Two workers cannot claim the same event simultaneously (concurrency mutual exclusion).
6. Unknown event type becomes quarantined.
7. Successful handler transitions the event to processed.
8. Retryable handler failure is recorded safely (savepoint rollback of domain changes, status='failed', attempts incremented).
9. Permanent handler failure is quarantined immediately.
10. Database failure during outcome recording does not falsely mark the event processed.
11. Worker interruption recovers without permanently stranding the event.
12. Attempts increment correctly across retryable, permanent, and max-retry failures.
13. Sensitive information is scrubbed from persisted error messages.
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

from datetime import datetime, timezone
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
    ClaimResult,
    ClaimStatus,
    ProcessResult,
    ProcessingOutcome,
    WebhookDispatcher,
    WebhookEnginePersistenceError,
    WebhookPermanentError,
    WebhookRetryableError,
    claim_next_webhook_event,
    claim_webhook_event_by_id,
    execute_claimed_webhook_event,
    is_webhook_event_eligible,
    process_webhook_event,
    record_webhook_outcome,
    sanitize_error_message,
)


# =============================================================================
# Test Doubles & Engine Mocks
# =============================================================================


class EngineMockSavepoint:
    """Simulates SQLAlchemy async database savepoint (begin_nested)."""

    def __init__(self, session: "EngineMockDbSession") -> None:
        self.session = session
        self.domain_snapshot: list[str] = []
        self.event_snapshots: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "EngineMockSavepoint":
        self.domain_snapshot = list(self.session.domain_mutations)
        self.event_snapshots = {
            eid: {
                "status": ev.status,
                "attempts": ev.attempts,
                "error_message": ev.error_message,
                "processed_at": ev.processed_at,
            }
            for eid, ev in self.session.events.items()
        }
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            # Rollback domain mutations and restore event states prior to savepoint
            self.session.domain_mutations = list(self.domain_snapshot)
            for eid, snapshot in self.event_snapshots.items():
                ev = self.session.events.get(eid)
                if ev:
                    for attr, val in snapshot.items():
                        setattr(ev, attr, val)
            self.session.savepoints_rolled_back += 1
            return False  # Propagate exception to outer transaction
        self.session.savepoints_committed += 1
        return False


class EngineMockScalarResult:
    """Simulates SQLAlchemy execution result for scalar_one_or_none."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def scalar_one_or_none(self) -> Any:
        return self._item


class EngineMockDbSession:
    """In-memory async session test double for engine concurrency and transaction testing."""

    def __init__(self, initial_events: list[StripeWebhookEvent] | None = None) -> None:
        self.events: dict[str, StripeWebhookEvent] = {
            e.id: e for e in (initial_events or [])
        }
        self.domain_mutations: list[str] = []
        self.committed_count = 0
        self.rolled_back_count = 0
        self.savepoints_committed = 0
        self.savepoints_rolled_back = 0
        self.fail_commit_with: Exception | None = None
        self.locked_event_ids: set[str] = set()

    async def execute(self, stmt: Any) -> EngineMockScalarResult:
        target_event = None
        where_clauses = getattr(stmt, "_where_criteria", ())
        for criterion in where_clauses:
            if hasattr(criterion, "right") and hasattr(criterion.right, "value"):
                val = criterion.right.value
                if isinstance(val, str) and val in self.events:
                    target_event = self.events[val]
                    break

        if target_event is None:
            # Queue claim simulation: find first pending or failed event with attempts < max
            for ev in sorted(self.events.values(), key=lambda x: x.created_at):
                if ev.status in (WebhookProcessingStatus.pending, WebhookProcessingStatus.failed):
                    if ev.attempts < MAX_WEBHOOK_ATTEMPTS:
                        target_event = ev
                        break

        if target_event and target_event.id in self.locked_event_ids:
            raise RuntimeError(f"Lock contention: event '{target_event.id}' is already locked by another worker")

        return EngineMockScalarResult(target_event)
        params = getattr(stmt, "compile", lambda: None)().params if hasattr(stmt, "compile") else {}
        param_id = params.get("id_1") or params.get("id")

        if param_id and param_id in self.events:
            target_event = self.events[param_id]
        else:
            # Queue claim simulation: find first pending or failed event with attempts < max and retry eligibility
            for ev in sorted(self.events.values(), key=lambda x: x.created_at):
                if is_webhook_event_eligible(ev, max_attempts=MAX_WEBHOOK_ATTEMPTS):
                    target_event = ev
                    break

        if target_event and target_event.id in self.locked_event_ids:
            raise RuntimeError(f"Lock contention: event '{target_event.id}' is already locked by another worker")

        return EngineMockScalarResult(target_event)

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is StripeWebhookEvent:
            return self.events.get(pk)
        return None

    def begin_nested(self) -> EngineMockSavepoint:
        return EngineMockSavepoint(self)

    async def commit(self) -> None:
        if self.fail_commit_with:
            raise self.fail_commit_with
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


def make_test_event(
    event_id: str = "evt_engine_test_1",
    event_type: str = "invoice.paid",
    status: WebhookProcessingStatus = WebhookProcessingStatus.pending,
    attempts: int = 1,
    error_message: str | None = None,
) -> StripeWebhookEvent:
    """Helper to instantiate test StripeWebhookEvent."""
    return StripeWebhookEvent(
        id=event_id,
        event_type=event_type,
        status=status,
        attempts=attempts,
        payload={"id": event_id, "type": event_type, "data": {"object": {"id": "in_test"}}},
        error_message=error_message,
        created_at=datetime.now(timezone.utc),
    )


# =============================================================================
# 1. Event Claiming & Concurrency Tests (Requirements 1 - 5)
# =============================================================================


@pytest.mark.asyncio
async def test_pending_event_can_be_claimed() -> None:
    """Requirement 1: Pending event is claimed under lock and returned as eligible."""
    event = make_test_event(status=WebhookProcessingStatus.pending, attempts=1)
    db = EngineMockDbSession([event])

    claim = await claim_webhook_event_by_id(db, event.id)

    assert claim.status == ClaimStatus.CLAIMED
    assert claim.is_claimed is True
    assert claim.event is not None
    assert claim.event.id == event.id
    assert claim.event.status == WebhookProcessingStatus.pending


@pytest.mark.asyncio
async def test_processed_event_cannot_be_claimed_again() -> None:
    """Requirement 2: Processed event cannot be claimed again; returns ALREADY_PROCESSED."""
    event = make_test_event(status=WebhookProcessingStatus.processed, attempts=1)
    db = EngineMockDbSession([event])

    claim = await claim_webhook_event_by_id(db, event.id)

    assert claim.status == ClaimStatus.ALREADY_PROCESSED
    assert claim.is_claimed is False
    assert claim.event.status == WebhookProcessingStatus.processed


@pytest.mark.asyncio
async def test_quarantined_event_cannot_be_claimed() -> None:
    """Requirement 3: Quarantined event cannot be claimed; returns QUARANTINED."""
    event = make_test_event(
        status=WebhookProcessingStatus.quarantined,
        attempts=2,
        error_message="Manual review required",
    )
    db = EngineMockDbSession([event])

    claim = await claim_webhook_event_by_id(db, event.id)

    assert claim.status == ClaimStatus.QUARANTINED
    assert claim.is_claimed is False
    assert claim.event.status == WebhookProcessingStatus.quarantined


@pytest.mark.asyncio
async def test_failed_event_follows_retry_policy() -> None:
    """Requirement 4: Failed event is claimed if attempts < max, or quarantined if attempts >= max."""
    # Case A: attempts < max_attempts -> eligible for retry
    event_retryable = make_test_event(
        event_id="evt_retryable",
        status=WebhookProcessingStatus.failed,
        attempts=2,
    )
    db_a = EngineMockDbSession([event_retryable])
    claim_a = await claim_webhook_event_by_id(db_a, "evt_retryable", max_attempts=5)

    assert claim_a.status == ClaimStatus.CLAIMED
    assert claim_a.is_claimed is True

    # Case B: attempts >= max_attempts -> transitions to quarantined and committed
    event_maxed = make_test_event(
        event_id="evt_maxed",
        status=WebhookProcessingStatus.failed,
        attempts=5,
    )
    db_b = EngineMockDbSession([event_maxed])
    claim_b = await claim_webhook_event_by_id(db_b, "evt_maxed", max_attempts=5)

    assert claim_b.status == ClaimStatus.MAX_ATTEMPTS_EXCEEDED
    assert claim_b.is_claimed is False
    assert event_maxed.status == WebhookProcessingStatus.quarantined
    assert "exceeded maximum retry attempts" in event_maxed.error_message
    assert db_b.committed_count == 1


@pytest.mark.asyncio
async def test_claim_nonexistent_event_returns_not_found() -> None:
    """Verify claiming an unknown event ID safely returns NOT_FOUND."""
    db = EngineMockDbSession([])
    claim = await claim_webhook_event_by_id(db, "evt_does_not_exist")

    assert claim.status == ClaimStatus.NOT_FOUND
    assert claim.is_claimed is False
    assert claim.event is None


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_same_event_simultaneously() -> None:
    """Requirement 5: Row locking blocks concurrent worker from claiming active row.

    Simulates Worker 1 acquiring the lock. When Worker 2 attempts to claim, it encounters
    a lock contention error or blocks until Worker 1 commits and updates the status to 'processed'.
    """
    event_id = "evt_concurrent_lock"
    event = make_test_event(event_id=event_id, status=WebhookProcessingStatus.pending)
    db_worker_1 = EngineMockDbSession([event])
    db_worker_2 = EngineMockDbSession([event])

    # Worker 1 acquires lock
    claim_1 = await claim_webhook_event_by_id(db_worker_1, event_id)
    assert claim_1.is_claimed is True

    # Mark as locked in the shared system
    db_worker_2.locked_event_ids.add(event_id)

    # Worker 2 attempts to claim while locked -> blocked / raises contention
    with pytest.raises(RuntimeError, match="Lock contention"):
        await claim_webhook_event_by_id(db_worker_2, event_id)

    # Worker 1 finishes processing and commits
    await record_webhook_outcome(db_worker_1, event, ProcessingOutcome.SUCCESS)
    assert event.status == WebhookProcessingStatus.processed

    # Worker 2 lock unblocks and re-reads under READ COMMITTED
    db_worker_2.locked_event_ids.remove(event_id)
    claim_2 = await claim_webhook_event_by_id(db_worker_2, event_id)

    # Worker 2 sees already processed, does not re-process
    assert claim_2.status == ClaimStatus.ALREADY_PROCESSED
    assert claim_2.is_claimed is False


@pytest.mark.asyncio
async def test_claim_next_webhook_event_finds_eligible_queue_item() -> None:
    """Verify queue-based worker claim correctly finds oldest eligible event."""
    ev_processed = make_test_event("evt_1", status=WebhookProcessingStatus.processed)
    ev_pending = make_test_event("evt_2", status=WebhookProcessingStatus.pending)
    ev_quarantined = make_test_event("evt_3", status=WebhookProcessingStatus.quarantined)

    db = EngineMockDbSession([ev_processed, ev_pending, ev_quarantined])
    claim = await claim_next_webhook_event(db)

    assert claim.status == ClaimStatus.CLAIMED
    assert claim.event is not None
    assert claim.event.id == "evt_2"


# =============================================================================
# 2. Handler Execution & Outcome Recording Tests (Requirements 6 - 9)
# =============================================================================


@pytest.mark.asyncio
async def test_successful_handler_transitions_to_processed() -> None:
    """Requirement 7: Clean handler completion commits status='processed' and sets processed_at."""
    event = make_test_event(event_id="evt_success_1", status=WebhookProcessingStatus.pending)
    db = EngineMockDbSession([event])

    handler_called = False

    async def mock_handler(payload: dict[str, Any], session: Any) -> None:
        nonlocal handler_called
        handler_called = True
        session.domain_mutations.append("invoice_ledger_updated")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", mock_handler)

    result = await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert result.success is True
    assert result.status == WebhookProcessingStatus.processed
    assert handler_called is True
    assert event.status == WebhookProcessingStatus.processed
    assert event.processed_at is not None
    assert event.error_message is None
    assert db.committed_count == 1
    assert "invoice_ledger_updated" in db.domain_mutations


@pytest.mark.asyncio
async def test_unknown_event_type_becomes_quarantined() -> None:
    """Requirement 6: Unregistered event type is safely quarantined rather than processed."""
    event = make_test_event(event_id="evt_unknown", event_type="unregistered.custom.event")
    db = EngineMockDbSession([event])

    dispatcher = WebhookDispatcher()
    # No handler registered for 'unregistered.custom.event'

    result = await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert event.status == WebhookProcessingStatus.quarantined
    assert "No handler registered" in event.error_message
    assert event.attempts == 2  # Incremented
    assert db.committed_count == 1


@pytest.mark.asyncio
async def test_retryable_handler_failure_is_recorded_safely() -> None:
    """Requirement 8: Retryable failure rolls back domain changes and persists status='failed'."""
    event = make_test_event(event_id="evt_retryable_fail", attempts=1)
    db = EngineMockDbSession([event])

    async def failing_handler(payload: dict[str, Any], session: Any) -> None:
        session.domain_mutations.append("partial_uncommitted_change")
        raise WebhookRetryableError("Transient lock timeout connecting to organization ledger")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", failing_handler)

    result = await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert result.success is False
    assert result.status == WebhookProcessingStatus.failed
    assert event.status == WebhookProcessingStatus.failed
    assert event.attempts == 2  # Incremented from 1 to 2
    assert "Transient lock timeout" in event.error_message
    assert db.committed_count == 1

    # Verify domain mutations were rolled back by savepoint
    assert "partial_uncommitted_change" not in db.domain_mutations
    assert db.savepoints_rolled_back == 1


@pytest.mark.asyncio
async def test_permanent_handler_failure_is_quarantined_immediately() -> None:
    """Requirement 9: Permanent business failure is quarantined immediately regardless of attempts."""
    event = make_test_event(event_id="evt_permanent_fail", attempts=1)
    db = EngineMockDbSession([event])

    async def invalid_data_handler(payload: dict[str, Any], session: Any) -> None:
        session.domain_mutations.append("corrupted_state_must_rollback")
        raise WebhookPermanentError("Invoice payload lacks customer reference; unrecoverable data")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", invalid_data_handler)

    result = await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert event.status == WebhookProcessingStatus.quarantined
    assert event.attempts == 2
    assert "lacks customer reference" in event.error_message
    assert db.committed_count == 1
    assert "corrupted_state_must_rollback" not in db.domain_mutations
    assert db.savepoints_rolled_back == 1


@pytest.mark.asyncio
async def test_unexpected_exception_is_treated_as_retryable() -> None:
    """Verify unexpected non-engine exceptions are treated as retryable by default."""
    event = make_test_event(event_id="evt_unexpected", attempts=1)
    db = EngineMockDbSession([event])

    async def crashing_handler(payload: dict[str, Any], session: Any) -> None:
        raise ValueError("Unexpected parsing anomaly")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", crashing_handler)

    result = await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert result.success is False
    assert result.status == WebhookProcessingStatus.failed
    assert event.status == WebhookProcessingStatus.failed
    assert event.attempts == 2
    assert "Unexpected parsing anomaly" in event.error_message


# =============================================================================
# 3. Crash Recovery & Durability Invariant Tests (Requirements 10 - 12)
# =============================================================================


@pytest.mark.asyncio
async def test_database_failure_during_outcome_recording_does_not_falsely_mark_processed() -> None:
    """Requirement 10: Database write error during outcome recording triggers rollback and raises."""
    event = make_test_event(event_id="evt_db_crash")
    db = EngineMockDbSession([event])
    db.fail_commit_with = RuntimeError("Database disk full")

    async def dummy_handler(payload: dict[str, Any], session: Any) -> None:
        pass

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", dummy_handler)

    with pytest.raises(WebhookEnginePersistenceError, match="Failed to durably commit outcome"):
        await process_webhook_event(db, event.id, dispatcher=dispatcher)

    assert db.rolled_back_count == 1
    # Event in memory should not be committed as processed
    assert db.committed_count == 0


@pytest.mark.asyncio
async def test_worker_interruption_recovers_without_permanently_stranding_event() -> None:
    """Requirement 11: Worker crash before outcome commit rolls back; event remains claimable."""
    event_id = "evt_stranded_recovery"
    event = make_test_event(event_id=event_id, status=WebhookProcessingStatus.pending, attempts=1)
    
    # Worker 1 starts processing
    db_worker_1 = EngineMockDbSession([event])
    claim_1 = await claim_webhook_event_by_id(db_worker_1, event_id)
    assert claim_1.is_claimed is True

    # Worker 1 terminates abruptly (simulated by session rollback / connection drop)
    await db_worker_1.rollback()

    # Worker 2 picks up the event later
    db_worker_2 = EngineMockDbSession([event])
    claim_2 = await claim_webhook_event_by_id(db_worker_2, event_id)

    # Event is fully claimable and not permanently stranded
    assert claim_2.status == ClaimStatus.CLAIMED
    assert claim_2.event.status == WebhookProcessingStatus.pending


@pytest.mark.asyncio
async def test_attempts_increment_correctly_on_retries_and_quarantine() -> None:
    """Requirement 12: Attempts increment monotonically and transition to quarantine at threshold."""
    event = make_test_event(event_id="evt_attempts_test", attempts=3)
    db = EngineMockDbSession([event])

    async def always_failing_handler(payload: dict[str, Any], session: Any) -> None:
        raise WebhookRetryableError("Transient downstream service failure")

    dispatcher = WebhookDispatcher()
    dispatcher.register("invoice.paid", always_failing_handler)

    # Attempt 4 (max is 5) -> remains failed, attempts=4
    res_1 = await process_webhook_event(db, event.id, dispatcher=dispatcher, max_attempts=5)
    assert res_1.status == WebhookProcessingStatus.failed
    assert event.attempts == 4

    # Attempt 5 -> reaches max_attempts threshold -> transitions to quarantined
    res_2 = await process_webhook_event(db, event.id, dispatcher=dispatcher, max_attempts=5)
    assert res_2.status == WebhookProcessingStatus.quarantined
    assert event.status == WebhookProcessingStatus.quarantined
    assert event.attempts == 5
    assert "exceeded max attempts" in event.error_message


# =============================================================================
# 4. Error Sanitization Tests
# =============================================================================


def test_error_sanitization_removes_secrets_and_cards() -> None:
    """Requirement 13: Error strings scrub Stripe keys, webhook secrets, and card numbers."""
    secret_text = (
        "Failed while contacting api with sk_test_51AbC1234567890def and whsec_TopSecretWebhookKey. "
        "Bearer eyJhbGciOiJIUzI1NiJ9.card 4111 2222 3333 4444"
    )
    cleaned = sanitize_error_message(secret_text)

    assert "sk_test" not in cleaned
    assert "whsec_" not in cleaned
    assert "Bearer " not in cleaned
    assert "4111 2222 3333 4444" not in cleaned
    assert "[REDACTED]" in cleaned


def test_error_sanitization_handles_none_and_truncation() -> None:
    """Verify sanitization handles None gracefully and bounds string length."""
    assert sanitize_error_message(None) == ""
    long_msg = "X" * 2000
    cleaned = sanitize_error_message(long_msg, max_chars=100)
    assert len(cleaned) == 100


# =============================================================================
# 5. Dispatcher Unit Tests
# =============================================================================


def test_dispatcher_registration_and_lookup() -> None:
    """Verify dispatcher registers, inspects, and unregisters handlers."""
    dispatcher = WebhookDispatcher()
    assert dispatcher.registered_types == []

    async def dummy(payload: dict[str, Any], session: Any) -> None:
        pass

    dispatcher.register("checkout.session.completed", dummy)
    assert dispatcher.has_handler("checkout.session.completed") is True
    assert dispatcher.get_handler("checkout.session.completed") is dummy
    assert dispatcher.registered_types == ["checkout.session.completed"]

    dispatcher.unregister("checkout.session.completed")
    assert dispatcher.has_handler("checkout.session.completed") is False
    assert dispatcher.get_handler("checkout.session.completed") is None
