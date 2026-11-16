"""Comprehensive unit and concurrency tests for Razorpay webhook worker and lease ownership.

Covers all 14 required lease ownership, fencing, concurrency, and lifecycle scenarios:
1. Successful claim and lease assignment.
2. Two workers competing for the same event.
3. Different workers claiming different events.
4. Unexpired lease cannot be stolen.
5. Expired lease can be recovered.
6. Stale worker cannot complete after lease replacement.
7. Stale worker cannot increment attempts or quarantine.
8. Valid owner can mark an event processed.
9. Valid owner can schedule a retry.
10. Fifth failed attempt quarantines.
11. Lease recovery does not increment attempts.
12. Worker crash and recovery end-to-end behavior.
13. Lease fields are cleared on completion.
14. Provider/account/environment scoping remains intact.
15. Offline Alembic migration DDL and metadata verification.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import subprocess
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

from typing import Any
import uuid

import pytest

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    ReconciliationStatus,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.razorpay.webhook_engine import (
    DEFAULT_LEASE_DURATION_SECONDS,
    MAX_WEBHOOK_ATTEMPTS,
    ClaimedWebhookEvent,
    ProcessingOutcome,
    RazorpayWebhookDispatcher,
    RazorpayWebhookPermanentError,
    RazorpayWebhookReconciliationConflictError,
    RazorpayWebhookRetryableError,
    StaleLeaseError,
    claim_next_razorpay_webhook_event,
    execute_claimed_razorpay_webhook_event,
    get_retry_backoff_seconds,
    record_razorpay_webhook_outcome,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message
from unittest.mock import AsyncMock, patch
from app.workers.razorpay_webhook_worker import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    RazorpayWebhookWorker,
    build_parser,
    main,
    run_worker,
    setup_signal_handlers,
)


# =============================================================================
# In-Memory Concurrency Test Doubles
# =============================================================================


class SharedRazorpayEventRepository:
    """Thread/coroutine-safe shared storage simulating PostgreSQL table state across sessions."""

    def __init__(self, events: list[BillingWebhookEvent] | None = None) -> None:
        self.events: dict[uuid.UUID, BillingWebhookEvent] = {
            e.id: deepcopy(e) for e in (events or [])
        }
        self.locked_ids: set[uuid.UUID] = set()
        self.reconciliations: dict[str, PaymentReconciliation] = {}
        self.domain_objects: list[Any] = []
        self.customer_provisions: dict[str, Any] = {}
        self.checkout_attempts: dict[str, Any] = {}

    def get_event(self, event_id: uuid.UUID) -> BillingWebhookEvent | None:
        ev = self.events.get(event_id)
        return deepcopy(ev) if ev is not None else None

    def save_event(self, event: BillingWebhookEvent) -> None:
        self.events[event.id] = deepcopy(event)


class MockSavepoint:
    """In-memory savepoint test double supporting nested transactions."""

    def __init__(self, session: "RazorpayWorkerMockDbSession") -> None:
        self.session = session
        self.staged_snapshot: dict[uuid.UUID, BillingWebhookEvent] = {}
        self.domain_snapshot: list[Any] = []
        self.reconciliation_snapshot: list[PaymentReconciliation] = []

    async def __aenter__(self) -> "MockSavepoint":
        self.staged_snapshot = deepcopy(self.session.staged_changes)
        self.domain_snapshot = list(self.session.staged_domain_objects)
        self.reconciliation_snapshot = list(self.session.staged_reconciliations)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()

    async def rollback(self) -> None:
        # Revert attributes of events to preserve object identity in session.staged_changes
        for eid, snap in self.staged_snapshot.items():
            if eid in self.session.staged_changes:
                cur = self.session.staged_changes[eid]
                cur.status = snap.status
                cur.attempts = snap.attempts
                cur.next_retry_at = snap.next_retry_at
                cur.lease_token = snap.lease_token
                cur.locked_by = snap.locked_by
                cur.lease_expires_at = snap.lease_expires_at
                cur.error_message = snap.error_message
                cur.quarantined_at = snap.quarantined_at
                cur.quarantine_reason = snap.quarantine_reason
        for eid in list(self.session.staged_changes.keys()):
            if eid not in self.staged_snapshot:
                del self.session.staged_changes[eid]

        self.session.staged_domain_objects = list(self.domain_snapshot)
        self.session.staged_reconciliations = list(self.reconciliation_snapshot)

    async def commit(self) -> None:
        pass


class RazorpayWorkerMockDbSession:
    """In-memory async session test double providing transaction isolation and row locking."""

    _counter = 0

    def __init__(
        self,
        repo: SharedRazorpayEventRepository,
        clock_now: datetime | None = None,
    ) -> None:
        RazorpayWorkerMockDbSession._counter += 1
        self.session_id = f"session_{RazorpayWorkerMockDbSession._counter}"
        self.repo = repo
        self.clock_now = clock_now or datetime.now(timezone.utc)
        self.held_locks: set[uuid.UUID] = set()
        self.staged_changes: dict[uuid.UUID, BillingWebhookEvent] = {}
        self.staged_domain_objects: list[Any] = []
        self.staged_reconciliations: list[PaymentReconciliation] = []

    async def __aenter__(self) -> "RazorpayWorkerMockDbSession":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.rollback()

    def begin_nested(self) -> MockSavepoint:
        return MockSavepoint(self)

    def add(self, obj: Any) -> None:
        self.staged_domain_objects.append(obj)

    async def flush(self) -> None:
        pass

    async def execute(self, statement: Any) -> Any:
        class ScalarResult:
            def __init__(self, item: Any) -> None:
                self._item = item

            def scalar_one_or_none(self) -> Any:
                return self._item

            def scalar_one(self) -> Any:
                return self._item

            def scalars(self) -> "ScalarResult":
                return self

            def all(self) -> list[Any]:
                return [self._item] if self._item is not None else []

            def first(self) -> Any:
                return self._item

        stmt_str = str(statement)
        now = self.clock_now

        # Reconciliation Upsert query
        if "payment_reconciliations" in stmt_str:
            params: dict[str, Any] = {}
            try:
                params = statement.compile().params
            except Exception:
                pass
            case_key = params.get("case_key", "default_case_key")
            reason = params.get("reason", "unknown_conflict")
            rec_id = params.get("id", uuid.uuid4())
            existing = self.repo.reconciliations.get(case_key)
            if existing is not None:
                existing.updated_at = self.clock_now
                existing.reason = reason
                existing.status = ReconciliationStatus.pending_review
                self.staged_reconciliations.append(existing)
                return ScalarResult(existing)
            else:
                new_rec = PaymentReconciliation(
                    id=rec_id if isinstance(rec_id, uuid.UUID) else uuid.uuid4(),
                    provider=params.get("provider", "razorpay"),
                    merchant_account_id=params.get("merchant_account_id", "default"),
                    environment=params.get("environment", "test"),
                    case_key=case_key,
                    reason=reason,
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload=params.get("diagnostic_payload", {}),
                    created_at=self.clock_now,
                    updated_at=self.clock_now,
                )
                self.staged_reconciliations.append(new_rec)
                return ScalarResult(new_rec)

        # Race retry check: BillingCustomerProvision
        if "billing_customer_provisions" in stmt_str:
            params = {}
            try:
                params = statement.compile().params
            except Exception:
                pass
            cust_id = params.get("provider_customer_id_1")
            prov = self.repo.customer_provisions.get(cust_id) if cust_id else None
            return ScalarResult(prov)

        # Race retry check: BillingCheckoutAttempt
        if "billing_checkout_attempts" in stmt_str:
            params = {}
            try:
                params = statement.compile().params
            except Exception:
                pass
            sub_id = params.get("provider_subscription_id_1")
            cust_id = params.get("provider_customer_id_1")
            att = None
            if sub_id and sub_id in self.repo.checkout_attempts:
                att = self.repo.checkout_attempts[sub_id]
            elif cust_id and cust_id in self.repo.checkout_attempts:
                att = self.repo.checkout_attempts[cust_id]
            return ScalarResult(att)

        # 1. Claim query: SELECT ... FROM billing_webhook_events WHERE ... ORDER BY received_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED
        if "ORDER BY" in stmt_str and "LIMIT" in stmt_str:
            params = {}
            try:
                params = statement.compile().params
            except Exception:
                pass

            provider_filter = params.get("provider_1", "razorpay")
            environment_filter = params.get("environment_1")

            candidates = []
            for ev in self.repo.events.values():
                # Check active row lock by another in-flight transaction
                if ev.id in self.repo.locked_ids and ev.id not in self.held_locks:
                    continue  # SKIP LOCKED

                # Verify provider
                if ev.provider != provider_filter:
                    continue
                # Verify environment if filtered
                if environment_filter is not None and ev.environment != environment_filter:
                    continue
                # Signature verification required
                if not ev.signature_verified:
                    continue
                # Eligible status
                if ev.status not in (WebhookProcessingStatus.pending, WebhookProcessingStatus.failed):
                    continue
                # Max attempts threshold
                if ev.attempts >= MAX_WEBHOOK_ATTEMPTS:
                    continue
                # Next retry check
                if ev.next_retry_at is not None and ev.next_retry_at > now:
                    continue
                # Lease expiration check (unexpired lease cannot be claimed)
                if ev.lease_expires_at is not None and ev.lease_expires_at > now:
                    continue

                candidates.append(ev)

            if not candidates:
                return ScalarResult(None)

            # Sort FIFO by received_at
            candidates.sort(key=lambda x: x.received_at)
            selected = candidates[0]

            # Acquire lock
            self.repo.locked_ids.add(selected.id)
            self.held_locks.add(selected.id)

            # Return working copy
            working_copy = deepcopy(selected)
            self.staged_changes[working_copy.id] = working_copy
            return ScalarResult(working_copy)

        # 2. Row lookup by ID: SELECT ... WHERE id = :id_1 ... WITH FOR UPDATE
        params = {}
        try:
            params = statement.compile().params
        except Exception:
            pass

        target_id = params.get("id_1")
        if target_id is None:
            # Fallback parsing
            for k, v in params.items():
                if isinstance(v, uuid.UUID):
                    target_id = v
                    break

        if target_id is not None:
            raw_ev = self.repo.events.get(target_id)
            if raw_ev is None:
                return ScalarResult(None)

            # Check locking
            if raw_ev.id in self.repo.locked_ids and raw_ev.id not in self.held_locks:
                # In real Postgres with_for_update blocks; in unit tests raise concurrency conflict
                raise RuntimeError("Row is locked by another active session.")

            self.repo.locked_ids.add(raw_ev.id)
            self.held_locks.add(raw_ev.id)

            working_copy = deepcopy(self.staged_changes.get(raw_ev.id, raw_ev))
            self.staged_changes[working_copy.id] = working_copy
            return ScalarResult(working_copy)

        return ScalarResult(None)

    async def commit(self) -> None:
        """Persist staged changes to shared repository and release held locks."""
        for eid, modified in self.staged_changes.items():
            self.repo.save_event(modified)
        self.staged_changes.clear()
        for rec in self.staged_reconciliations:
            self.repo.reconciliations[rec.case_key] = rec
        self.staged_reconciliations.clear()
        for obj in self.staged_domain_objects:
            self.repo.domain_objects.append(obj)
        self.staged_domain_objects.clear()
        for lid in list(self.held_locks):
            self.repo.locked_ids.discard(lid)
        self.held_locks.clear()

    async def rollback(self) -> None:
        """Discard staged changes and release held locks."""
        self.staged_changes.clear()
        self.staged_reconciliations.clear()
        self.staged_domain_objects.clear()
        for lid in list(self.held_locks):
            self.repo.locked_ids.discard(lid)
        self.held_locks.clear()

    async def refresh(self, obj: Any) -> None:
        pass


def make_test_event(
    provider_event_id: str = "evt_rzp_test_001",
    status: WebhookProcessingStatus = WebhookProcessingStatus.pending,
    attempts: int = 0,
    event_type: str = "subscription.charged",
    provider: str = "razorpay",
    merchant_account_id: str = "acc_TEST_123",
    environment: str = "test",
    next_retry_at: datetime | None = None,
    lease_token: uuid.UUID | None = None,
    locked_by: str | None = None,
    lease_expires_at: datetime | None = None,
    received_at: datetime | None = None,
) -> BillingWebhookEvent:
    now = datetime.now(timezone.utc)
    return BillingWebhookEvent(
        id=uuid.uuid4(),
        provider=provider,
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_event_id=provider_event_id,
        event_type=event_type,
        status=status,
        attempts=attempts,
        payload={"id": provider_event_id, "event": event_type, "data": {}},
        signature_verified=True,
        next_retry_at=next_retry_at,
        lease_token=lease_token,
        locked_by=locked_by,
        lease_expires_at=lease_expires_at,
        received_at=received_at or now,
    )


# =============================================================================
# Test Scenarios
# =============================================================================


@pytest.mark.asyncio
async def test_1_successful_claim_and_lease_assignment() -> None:
    """1. Successful claim assigns fresh lease_token, locked_by, lease_expires_at and commits."""
    event = make_test_event("evt_1", status=WebhookProcessingStatus.pending, attempts=0)
    repo = SharedRazorpayEventRepository([event])
    now = datetime.now(timezone.utc)

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        claimed = await claim_next_razorpay_webhook_event(
            db=db,
            worker_id="worker_alpha",
            lease_duration_seconds=60,
            now=now,
        )

    assert claimed is not None
    assert claimed.event_id == event.id
    assert claimed.locked_by == "worker_alpha"
    assert claimed.lease_token is not None
    assert claimed.lease_expires_at == now + timedelta(seconds=60)
    assert claimed.attempts == 0

    # Verify persisted lease in repository
    persisted = repo.get_event(event.id)
    assert persisted is not None
    assert persisted.lease_token == claimed.lease_token
    assert persisted.locked_by == "worker_alpha"
    assert persisted.lease_expires_at == now + timedelta(seconds=60)
    # Ensure row lock was released upon commit
    assert event.id not in repo.locked_ids


@pytest.mark.asyncio
async def test_2_two_workers_competing_for_same_event() -> None:
    """2. Two concurrent workers compete for a single event; exactly one wins the lease."""
    event = make_test_event("evt_single")
    repo = SharedRazorpayEventRepository([event])
    now = datetime.now(timezone.utc)

    # Worker 1 claims
    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db1:
        c1 = await claim_next_razorpay_webhook_event(db=db1, worker_id="worker_1", now=now)

    # Worker 2 attempts to claim while Worker 1 holds active lease
    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db2:
        c2 = await claim_next_razorpay_webhook_event(db=db2, worker_id="worker_2", now=now)

    assert c1 is not None
    assert c1.locked_by == "worker_1"
    assert c2 is None  # Worker 2 receives nothing because lease is active


@pytest.mark.asyncio
async def test_3_different_workers_claiming_different_events() -> None:
    """3. Different workers claim distinct events independently without collision."""
    t0 = datetime.now(timezone.utc)
    ev1 = make_test_event("evt_1", received_at=t0)
    ev2 = make_test_event("evt_2", received_at=t0 + timedelta(seconds=1))
    repo = SharedRazorpayEventRepository([ev1, ev2])

    async with RazorpayWorkerMockDbSession(repo, clock_now=t0) as db1:
        c1 = await claim_next_razorpay_webhook_event(db=db1, worker_id="worker_1", now=t0)

    async with RazorpayWorkerMockDbSession(repo, clock_now=t0) as db2:
        c2 = await claim_next_razorpay_webhook_event(db=db2, worker_id="worker_2", now=t0)

    assert c1 is not None and c2 is not None
    assert c1.event_id == ev1.id
    assert c2.event_id == ev2.id
    assert c1.lease_token != c2.lease_token
    assert c1.locked_by == "worker_1"
    assert c2.locked_by == "worker_2"


@pytest.mark.asyncio
async def test_4_unexpired_lease_cannot_be_stolen() -> None:
    """4. An unexpired lease cannot be claimed by another worker."""
    now = datetime.now(timezone.utc)
    active_lease = make_test_event(
        "evt_leased",
        lease_token=uuid.uuid4(),
        locked_by="worker_orig",
        lease_expires_at=now + timedelta(seconds=45),
    )
    repo = SharedRazorpayEventRepository([active_lease])

    # 20 seconds later (lease still has 25s remaining)
    query_time = now + timedelta(seconds=20)
    async with RazorpayWorkerMockDbSession(repo, clock_now=query_time) as db:
        claimed = await claim_next_razorpay_webhook_event(
            db=db,
            worker_id="worker_thief",
            now=query_time,
        )

    assert claimed is None
    persisted = repo.get_event(active_lease.id)
    assert persisted is not None
    assert persisted.locked_by == "worker_orig"


@pytest.mark.asyncio
async def test_5_expired_lease_can_be_recovered() -> None:
    """5. An expired lease becomes eligible for recovery by a new worker."""
    now = datetime.now(timezone.utc)
    old_token = uuid.uuid4()
    expired_event = make_test_event(
        "evt_expired",
        lease_token=old_token,
        locked_by="worker_dead",
        lease_expires_at=now - timedelta(seconds=5),  # Expired 5 seconds ago
        attempts=1,
    )
    repo = SharedRazorpayEventRepository([expired_event])

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        claimed = await claim_next_razorpay_webhook_event(
            db=db,
            worker_id="worker_recovery",
            now=now,
        )

    assert claimed is not None
    assert claimed.event_id == expired_event.id
    assert claimed.locked_by == "worker_recovery"
    assert claimed.lease_token != old_token
    assert claimed.attempts == 1  # Attempts preserved, NOT incremented


@pytest.mark.asyncio
async def test_6_stale_worker_cannot_complete_after_lease_replacement() -> None:
    """6. A stale worker whose lease expired and was replaced cannot record outcome."""
    now = datetime.now(timezone.utc)
    worker_1_token = uuid.uuid4()
    worker_2_token = uuid.uuid4()

    event = make_test_event(
        "evt_fenced",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        lease_token=worker_2_token,  # Replaced by Worker 2
        locked_by="worker_2",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    # Stale ClaimedWebhookEvent referencing old Worker 1
    stale_claim = ClaimedWebhookEvent(
        event_id=event.id,
        provider="razorpay",
        merchant_account_id="acc_TEST_123",
        environment="test",
        provider_event_id=event.provider_event_id,
        event_type="subscription.charged",
        payload={},
        attempts=0,
        lease_token=worker_1_token,  # Stale token
        locked_by="worker_1",
        lease_expires_at=now - timedelta(seconds=10),
        received_at=event.received_at,
    )

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        with pytest.raises(StaleLeaseError) as exc_info:
            await record_razorpay_webhook_outcome(
                db=db,
                claimed=stale_claim,
                outcome=ProcessingOutcome.SUCCESS,
                now=now,
            )

    assert "lost lease ownership" in str(exc_info.value)

    # Verify event in DB is unchanged (still owned by worker_2)
    persisted = repo.get_event(event.id)
    assert persisted is not None
    assert persisted.status == WebhookProcessingStatus.pending
    assert persisted.lease_token == worker_2_token
    assert persisted.locked_by == "worker_2"


@pytest.mark.asyncio
async def test_7_stale_worker_cannot_increment_attempts_or_quarantine() -> None:
    """7. A stale worker attempting failure recording cannot increment attempts or trigger quarantine."""
    now = datetime.now(timezone.utc)
    stale_token = uuid.uuid4()
    active_token = uuid.uuid4()

    event = make_test_event(
        "evt_stale_fail",
        status=WebhookProcessingStatus.failed,
        attempts=4,
        lease_token=active_token,
        locked_by="worker_current",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    stale_claim = ClaimedWebhookEvent(
        event_id=event.id,
        provider="razorpay",
        merchant_account_id="acc_TEST_123",
        environment="test",
        provider_event_id=event.provider_event_id,
        event_type="subscription.charged",
        payload={},
        attempts=4,
        lease_token=stale_token,
        locked_by="worker_stale",
        lease_expires_at=now - timedelta(seconds=5),
        received_at=event.received_at,
    )

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        with pytest.raises(StaleLeaseError):
            await record_razorpay_webhook_outcome(
                db=db,
                claimed=stale_claim,
                outcome=ProcessingOutcome.RETRYABLE_FAILURE,
                error="Stale error",
                max_attempts=5,
                now=now,
            )

    persisted = repo.get_event(event.id)
    assert persisted is not None
    assert persisted.attempts == 4  # NOT incremented to 5
    assert persisted.status == WebhookProcessingStatus.failed  # NOT quarantined


@pytest.mark.asyncio
async def test_8_valid_owner_can_mark_event_processed() -> None:
    """8. Valid lease owner successfully transitions event to processed and clears lease fields."""
    now = datetime.now(timezone.utc)
    token = uuid.uuid4()
    event = make_test_event(
        "evt_success",
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    claimed = ClaimedWebhookEvent(
        event_id=event.id,
        provider="razorpay",
        merchant_account_id="acc_TEST_123",
        environment="test",
        provider_event_id=event.provider_event_id,
        event_type="subscription.charged",
        payload={},
        attempts=0,
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
        received_at=event.received_at,
    )

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        updated = await record_razorpay_webhook_outcome(
            db=db,
            claimed=claimed,
            outcome=ProcessingOutcome.SUCCESS,
            now=now,
        )

    assert updated.status == WebhookProcessingStatus.processed
    assert updated.processed_at == now
    assert updated.error_message is None
    assert updated.lease_token is None
    assert updated.locked_by is None
    assert updated.lease_expires_at is None


@pytest.mark.asyncio
async def test_9_valid_owner_can_schedule_retry() -> None:
    """9. Valid owner records retryable failure: attempts increment to 1 and backoff is 5 seconds."""
    now = datetime.now(timezone.utc)
    token = uuid.uuid4()
    event = make_test_event(
        "evt_retry",
        attempts=0,
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    claimed = ClaimedWebhookEvent(
        event_id=event.id,
        provider="razorpay",
        merchant_account_id="acc_TEST_123",
        environment="test",
        provider_event_id=event.provider_event_id,
        event_type="subscription.charged",
        payload={},
        attempts=0,
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
        received_at=event.received_at,
    )

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        updated = await record_razorpay_webhook_outcome(
            db=db,
            claimed=claimed,
            outcome=ProcessingOutcome.RETRYABLE_FAILURE,
            error="Downstream network timeout",
            now=now,
        )

    assert updated.attempts == 1
    assert updated.status == WebhookProcessingStatus.failed
    assert updated.next_retry_at == now + timedelta(seconds=5)  # 5s backoff for 1st failure
    assert "Downstream network timeout" in (updated.error_message or "")
    # Lease cleared
    assert updated.lease_token is None
    assert updated.locked_by is None
    assert updated.lease_expires_at is None


@pytest.mark.asyncio
async def test_10_fifth_failed_attempt_quarantines() -> None:
    """10. Fifth failed attempt triggers quarantine and clears lease fields."""
    now = datetime.now(timezone.utc)
    token = uuid.uuid4()
    event = make_test_event(
        "evt_quarantine",
        status=WebhookProcessingStatus.failed,
        attempts=4,
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    claimed = ClaimedWebhookEvent(
        event_id=event.id,
        provider="razorpay",
        merchant_account_id="acc_TEST_123",
        environment="test",
        provider_event_id=event.provider_event_id,
        event_type="subscription.charged",
        payload={},
        attempts=4,
        lease_token=token,
        locked_by="worker_1",
        lease_expires_at=now + timedelta(seconds=60),
        received_at=event.received_at,
    )

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        updated = await record_razorpay_webhook_outcome(
            db=db,
            claimed=claimed,
            outcome=ProcessingOutcome.RETRYABLE_FAILURE,
            error="Permanent gateway degradation",
            max_attempts=5,
            now=now,
        )

    assert updated.attempts == 5
    assert updated.status == WebhookProcessingStatus.quarantined
    assert updated.quarantined_at == now
    assert "exceeded max attempts" in (updated.quarantine_reason or "").lower()
    assert updated.lease_token is None
    assert updated.locked_by is None
    assert updated.lease_expires_at is None


@pytest.mark.asyncio
async def test_11_lease_recovery_does_not_increment_attempts() -> None:
    """11. Re-claiming an expired lease does not increment attempt count."""
    now = datetime.now(timezone.utc)
    event = make_test_event(
        "evt_recov_attempts",
        status=WebhookProcessingStatus.failed,
        attempts=2,
        lease_token=uuid.uuid4(),
        locked_by="worker_dead",
        lease_expires_at=now - timedelta(seconds=10),
    )
    repo = SharedRazorpayEventRepository([event])

    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        claimed = await claim_next_razorpay_webhook_event(
            db=db,
            worker_id="worker_alive",
            now=now,
        )

    assert claimed is not None
    assert claimed.attempts == 2
    persisted = repo.get_event(event.id)
    assert persisted is not None
    assert persisted.attempts == 2


@pytest.mark.asyncio
async def test_12_worker_crash_and_recovery_end_to_end() -> None:
    """12. Worker crashes mid-processing; subsequent worker cleanly recovers and completes the event."""
    t0 = datetime.now(timezone.utc)
    event = make_test_event("evt_crash", status=WebhookProcessingStatus.pending, attempts=0, received_at=t0)
    repo = SharedRazorpayEventRepository([event])

    dispatcher = RazorpayWebhookDispatcher()

    # Worker 1: claims and crashes (simulated by not recording outcome)
    worker_1 = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=t0),
        dispatcher=dispatcher,
        worker_id="worker_1",
        lease_duration=30,
        clock=lambda: t0,
    )
    claimed_1 = await worker_1.claim_one()
    assert claimed_1 is not None
    assert repo.get_event(event.id).locked_by == "worker_1"

    # Time advances 40 seconds past lease expiry
    t1 = t0 + timedelta(seconds=40)

    # Worker 2: registers handler and processes
    async def sample_handler(payload: dict[str, Any], db: Any) -> None:
        pass  # Success

    dispatcher.register("subscription.charged", sample_handler)

    worker_2 = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=t1),
        dispatcher=dispatcher,
        worker_id="worker_2",
        lease_duration=30,
        clock=lambda: t1,
    )

    result_2 = await worker_2.process_one()
    assert result_2 is not None
    assert result_2.success is True
    assert result_2.status == WebhookProcessingStatus.processed
    assert result_2.attempts == 0

    final_event = repo.get_event(event.id)
    assert final_event is not None
    assert final_event.status == WebhookProcessingStatus.processed
    assert final_event.lease_token is None
    assert final_event.locked_by is None


@pytest.mark.asyncio
async def test_13_lease_fields_are_cleared_on_completion() -> None:
    """13. All completion outcomes (SUCCESS, FAILED, QUARANTINED) clear lease fields atomically."""
    now = datetime.now(timezone.utc)

    # Case A: Success
    ev_a = make_test_event("evt_a", lease_token=uuid.uuid4(), locked_by="w", lease_expires_at=now + timedelta(seconds=60))
    # Case B: Failure
    ev_b = make_test_event("evt_b", attempts=1, lease_token=uuid.uuid4(), locked_by="w", lease_expires_at=now + timedelta(seconds=60))
    # Case C: Quarantine (attempts=4 -> 5)
    ev_c = make_test_event("evt_c", attempts=4, lease_token=uuid.uuid4(), locked_by="w", lease_expires_at=now + timedelta(seconds=60))

    repo = SharedRazorpayEventRepository([ev_a, ev_b, ev_c])

    for ev, outcome in [(ev_a, ProcessingOutcome.SUCCESS), (ev_b, ProcessingOutcome.RETRYABLE_FAILURE), (ev_c, ProcessingOutcome.RETRYABLE_FAILURE)]:
        claim = ClaimedWebhookEvent(
            event_id=ev.id,
            provider="razorpay",
            merchant_account_id=ev.merchant_account_id,
            environment=ev.environment,
            provider_event_id=ev.provider_event_id,
            event_type=ev.event_type,
            payload={},
            attempts=ev.attempts,
            lease_token=ev.lease_token,
            locked_by=ev.locked_by,
            lease_expires_at=ev.lease_expires_at,
            received_at=ev.received_at,
        )
        async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
            updated = await record_razorpay_webhook_outcome(db=db, claimed=claim, outcome=outcome, now=now)
            assert updated.lease_token is None
            assert updated.locked_by is None
            assert updated.lease_expires_at is None


@pytest.mark.asyncio
async def test_14_provider_account_environment_scoping_intact() -> None:
    """14. Worker strictly respects provider and environment scoping filters."""
    now = datetime.now(timezone.utc)
    ev_stripe = make_test_event("evt_stripe", provider="stripe", environment="live")
    ev_live = make_test_event("evt_rzp_live", provider="razorpay", environment="live")
    ev_test = make_test_event("evt_rzp_test", provider="razorpay", environment="test")

    repo = SharedRazorpayEventRepository([ev_stripe, ev_live, ev_test])

    # Worker configured for provider='razorpay' and environment='test'
    async with RazorpayWorkerMockDbSession(repo, clock_now=now) as db:
        claimed = await claim_next_razorpay_webhook_event(
            db=db,
            worker_id="worker_scoped",
            provider="razorpay",
            environment="test",
            now=now,
        )

    assert claimed is not None
    assert claimed.provider == "razorpay"
    assert claimed.environment == "test"
    assert claimed.provider_event_id == "evt_rzp_test"


def test_15_offline_alembic_ddl_and_metadata() -> None:
    """15. Offline Alembic DDL generation validates revision d1e2f3a4b5c6 upgrade and downgrade."""
    api_dir = Path(__file__).resolve().parent.parent
    migration_file = (
        api_dir
        / "alembic"
        / "versions"
        / "20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py"
    )
    assert migration_file.exists(), f"Migration file not found at {migration_file}"

    spec = importlib.util.spec_from_file_location("migration_d1e2f3a4b5c6", migration_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "d1e2f3a4b5c6"
    assert migration.down_revision == "c1d2e3f4a5b7"

    # Offline upgrade DDL check
    res_up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "c1d2e3f4a5b7:d1e2f3a4b5c6", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res_up.returncode == 0, f"Alembic upgrade --sql failed:\n{res_up.stderr}"
    sql_up = res_up.stdout
    assert "ALTER TABLE billing_webhook_events ADD COLUMN lease_token UUID" in sql_up
    assert "ALTER TABLE billing_webhook_events ADD COLUMN lease_expires_at TIMESTAMP WITH TIME ZONE" in sql_up
    assert "ALTER TABLE billing_webhook_events ADD COLUMN locked_by VARCHAR(64)" in sql_up
    assert "CREATE INDEX ix_billing_webhook_events_claim ON billing_webhook_events" in sql_up
    assert "ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY" in sql_up

    # Offline downgrade DDL check
    res_down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "d1e2f3a4b5c6:c1d2e3f4a5b7", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res_down.returncode == 0, f"Alembic downgrade --sql failed:\n{res_down.stderr}"
    sql_down = res_down.stdout
    assert "DROP INDEX ix_billing_webhook_events_claim" in sql_down
    assert "ALTER TABLE billing_webhook_events DROP COLUMN locked_by" in sql_down
    assert "ALTER TABLE billing_webhook_events DROP COLUMN lease_expires_at" in sql_down
    assert "ALTER TABLE billing_webhook_events DROP COLUMN lease_token" in sql_down


# =============================================================================
# CLI and Lifecycle Tests (Step 5.2D.2.2)
# =============================================================================


def test_16_cli_argument_parsing_and_worker_propagation() -> None:
    """16. CLI parser correctly parses valid flags and propagates them to worker instance."""
    parser = build_parser()
    args = parser.parse_args([
        "--poll-interval", "4.5",
        "--batch-size", "25",
        "--max-attempts", "8",
        "--once",
        "--log-level", "DEBUG",
    ])

    assert args.poll_interval == 4.5
    assert args.batch_size == 25
    assert args.max_attempts == 8
    assert args.once is True
    assert args.log_level == "DEBUG"

    worker = RazorpayWebhookWorker(
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
        max_attempts=args.max_attempts,
    )
    assert worker.poll_interval == 4.5
    assert worker.batch_size == 25
    assert worker.max_attempts == 8


def test_17_cli_default_maximum_attempts_remains_five() -> None:
    """17. Default maximum attempts is strictly 5 and defaults are preserved when flags omitted."""
    parser = build_parser()
    args = parser.parse_args([])

    assert args.poll_interval is None
    assert args.batch_size is None
    assert args.max_attempts is None
    assert args.once is False
    assert args.log_level == "INFO"

    worker = RazorpayWebhookWorker()
    assert worker.max_attempts == 5
    assert worker.poll_interval == DEFAULT_POLL_INTERVAL_SECONDS
    assert worker.batch_size == DEFAULT_BATCH_SIZE


def test_18_cli_invalid_argument_rejection() -> None:
    """18. Invalid CLI arguments and programmatic arguments are strictly rejected."""
    # CLI level rejection via main()
    for bad_args in [
        ["--poll-interval", "0"],
        ["--poll-interval", "-2.5"],
        ["--batch-size", "0"],
        ["--batch-size", "-10"],
        ["--max-attempts", "0"],
        ["--max-attempts", "-3"],
        ["--log-level", "INVALID_LEVEL"],
    ]:
        with pytest.raises(SystemExit) as exc_info:
            main(bad_args)
        assert exc_info.value.code != 0

    # Programmatic level rejection via RazorpayWebhookWorker.__init__
    with pytest.raises(ValueError, match="poll_interval must be greater than 0"):
        RazorpayWebhookWorker(poll_interval=0)
    with pytest.raises(ValueError, match="poll_interval must be greater than 0"):
        RazorpayWebhookWorker(poll_interval=-1.0)
    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        RazorpayWebhookWorker(batch_size=0)
    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        RazorpayWebhookWorker(max_attempts=0)


def test_19_cli_help_flag_no_connections() -> None:
    """19. --help flag displays usage and exits cleanly with 0 without opening DB connections."""
    parser = build_parser()
    help_text = parser.format_help()
    assert "--poll-interval" in help_text
    assert "--batch-size" in help_text
    assert "--max-attempts" in help_text
    assert "--once" in help_text
    assert "--log-level" in help_text

    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


@pytest.mark.asyncio
async def test_20_worker_run_once_execution() -> None:
    """20. --once mode processes available events in a single batch pass and exits with code 0."""
    now = datetime.now(timezone.utc)
    ev1 = make_test_event("evt_run_once_1", event_type="payment.captured", received_at=now)
    ev2 = make_test_event("evt_run_once_2", event_type="payment.captured", received_at=now + timedelta(seconds=1))
    repo = SharedRazorpayEventRepository([ev1, ev2])

    dispatcher = RazorpayWebhookDispatcher()

    async def success_handler(payload: dict[str, Any], db: Any) -> None:
        pass

    dispatcher.register("payment.captured", success_handler)

    exit_code = await run_worker(
        poll_interval=1.0,
        batch_size=10,
        run_once_only=True,
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
    )

    assert exit_code == 0
    saved_ev1 = repo.get_event(ev1.id)
    saved_ev2 = repo.get_event(ev2.id)
    assert saved_ev1 is not None and saved_ev1.status == WebhookProcessingStatus.processed
    assert saved_ev2 is not None and saved_ev2.status == WebhookProcessingStatus.processed
    assert saved_ev1.lease_token is None
    assert saved_ev2.lease_token is None


@pytest.mark.asyncio
async def test_21_signal_triggered_graceful_shutdown() -> None:
    """21. Signal handlers trigger graceful stop, avoid duplicate registration, and drain cleanly."""
    worker = RazorpayWebhookWorker(poll_interval=0.1)

    # Initial registration
    setup_signal_handlers(worker)
    assert getattr(worker, "_signal_handlers_registered", False) is True

    # Duplicate registration should be safely ignored
    setup_signal_handlers(worker)
    assert getattr(worker, "_signal_handlers_registered", False) is True

    # Start worker and trigger stop after brief delay
    async def stop_after_delay() -> None:
        await asyncio.sleep(0.05)
        worker.stop()

    stop_task = asyncio.create_task(stop_after_delay())
    await worker.start()
    await stop_task

    assert worker._stop_event.is_set() is True


@pytest.mark.asyncio
async def test_22_worker_startup_and_cleanup_engine_disposal() -> None:
    """22. Worker disposes default database engine in finally block upon shutdown."""
    mock_engine = AsyncMock()

    with patch("app.db.session.engine", mock_engine), \
         patch("app.workers.razorpay_webhook_worker.RazorpayWebhookWorker.run_once", new_callable=AsyncMock) as mock_run_once:
        mock_run_once.return_value = 0

        exit_code = await run_worker(run_once_only=True, session_factory=None)
        assert exit_code == 0
        mock_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_23_worker_polling_loop_tolerates_transient_exceptions() -> None:
    """23. Unhandled loop exceptions are caught and logged without terminating worker polling loop."""
    worker = RazorpayWebhookWorker(poll_interval=0.05)
    call_count = 0

    async def flaky_run_batch(max_count: int | None = None) -> list[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Simulated transient network glitch")
        worker.stop()
        return []

    worker.run_batch = flaky_run_batch  # type: ignore[assignment]

    await worker.start()

    assert call_count >= 2
    assert worker._stop_event.is_set() is True


# =============================================================================
# Mock Worker Transaction Engine & Hardening Tests (MOCK-WRK-01..10)
# =============================================================================


@pytest.mark.asyncio
async def test_mock_wrk_01_atomic_success() -> None:
    """MOCK-WRK-01: Atomic success commits domain changes and marks event processed in single tx."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_01", received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    created_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="razorpay",
        provider_subscription_id="sub_wrk_01",
        status=SubscriptionStatus.active,
    )

    async def success_handler(payload: dict[str, Any], db: Any) -> None:
        db.add(created_sub)

    dispatcher.register("subscription.charged", success_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_01",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is True
    assert result.status == WebhookProcessingStatus.processed
    assert result.attempts == 0

    # Domain mutations committed
    assert created_sub in repo.domain_objects

    # Event committed as processed and lease cleared
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.processed
    assert saved_event.lease_token is None
    assert saved_event.locked_by is None
    assert saved_event.lease_expires_at is None


@pytest.mark.asyncio
async def test_mock_wrk_02_conflict_error_savepoint_rollback_and_reconciliation_upsert() -> None:
    """MOCK-WRK-02: Conflict error rolls back savepoint, upserts PaymentReconciliation, quarantines event."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_02", received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    aborted_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="razorpay",
        provider_subscription_id="sub_wrk_02",
        status=SubscriptionStatus.active,
    )

    async def conflict_handler(payload: dict[str, Any], db: Any) -> None:
        db.add(aborted_sub)
        raise RazorpayWebhookReconciliationConflictError(
            "Metadata tenant mismatch: notes claim org A, but subscription belongs to org B",
            case_key="metadata_mismatch:sub_wrk_02",
            reason="metadata_tenant_mismatch",
            diagnostic_payload={"subscription_id": "sub_wrk_02"},
        )

    dispatcher.register("subscription.charged", conflict_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_02",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert result.reconciliation_upserted is True
    assert result.failure_category == "reconciliation_conflict"

    # Domain mutations were rolled back!
    assert aborted_sub not in repo.domain_objects

    # Reconciliation case was upserted in outer transaction
    assert "metadata_mismatch:sub_wrk_02" in repo.reconciliations
    rec = repo.reconciliations["metadata_mismatch:sub_wrk_02"]
    assert rec.reason == "metadata_tenant_mismatch"
    assert rec.status == ReconciliationStatus.pending_review

    # Event committed as quarantined and lease cleared
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.quarantined
    assert saved_event.lease_token is None
    assert saved_event.locked_by is None


@pytest.mark.asyncio
async def test_mock_wrk_03_ed02_unbound_customer_race_retry() -> None:
    """MOCK-WRK-03: ED-02 unbound customer schedules single 2s race retry when in-flight record exists."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_03", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    # Pre-populate qualifying customer provision in repo
    repo.customer_provisions["cust_racing_123"] = BillingCustomerProvision(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=event.merchant_account_id,
        environment=event.environment,
        provider_customer_id="cust_racing_123",
        status=CustomerProvisionStatus.resolved,
    )

    async def unbound_handler(payload: dict[str, Any], db: Any) -> None:
        raise RazorpayWebhookReconciliationConflictError(
            "Unbound customer entity: cust_racing_123",
            case_key="unbound_entity:cust_racing_123",
            reason="unbound_provider_customer",
            diagnostic_payload={"customer_id": "cust_racing_123"},
        )

    dispatcher.register("subscription.charged", unbound_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_03",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.failed
    assert result.attempts == 1
    assert result.failure_category == "concurrent_binding_race"

    # No reconciliation record created during race retry
    assert len(repo.reconciliations) == 0

    # Event scheduled with 2s delay
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.failed
    assert saved_event.attempts == 1
    assert saved_event.next_retry_at == now + timedelta(seconds=2)
    assert saved_event.lease_token is None


@pytest.mark.asyncio
async def test_mock_wrk_04_ed01_unexpected_internal_error_savepoint_rollback() -> None:
    """MOCK-WRK-04: Unexpected internal error rolls back savepoint and schedules exponential retry."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_04", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    aborted_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="razorpay",
        provider_subscription_id="sub_wrk_04",
        status=SubscriptionStatus.active,
    )

    async def faulty_handler(payload: dict[str, Any], db: Any) -> None:
        db.add(aborted_sub)
        raise RuntimeError("Unexpected internal crash in domain business logic")

    dispatcher.register("subscription.charged", faulty_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_04",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.failed
    assert result.attempts == 1
    assert result.failure_category == "unexpected_internal_error"

    # Domain mutations rolled back
    assert aborted_sub not in repo.domain_objects

    # Event scheduled with attempt 1 backoff (5 seconds)
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.failed
    assert saved_event.attempts == 1
    assert saved_event.next_retry_at == now + timedelta(seconds=5)
    assert saved_event.lease_token is None


@pytest.mark.asyncio
async def test_mock_wrk_05_permanent_error_quarantines_without_retry() -> None:
    """MOCK-WRK-05: Permanent error triggers immediate quarantine on first attempt without retrying."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_05", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    async def perm_handler(payload: dict[str, Any], db: Any) -> None:
        raise RazorpayWebhookPermanentError("Malformed invoice structure: negative amount")

    dispatcher.register("subscription.charged", perm_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_05",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert result.attempts == 1
    assert result.failure_category == "permanent_failure"

    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.quarantined
    assert saved_event.next_retry_at is None
    assert saved_event.lease_token is None


@pytest.mark.asyncio
async def test_mock_wrk_06_unknown_event_type_quarantines() -> None:
    """MOCK-WRK-06: Unregistered event type immediately quarantines without attempting retry."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_06", event_type="unregistered.custom.event", received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()  # Empty dispatcher

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_06",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert result.failure_category == "unknown_event"

    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.quarantined
    assert saved_event.next_retry_at is None
    assert saved_event.lease_token is None


@pytest.mark.asyncio
async def test_mock_wrk_07_ed05_cancelled_error_propagation() -> None:
    """MOCK-WRK-07: ED-05 CancelledError rolls back transaction without outcome writes and propagates."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_07", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    async def cancelling_handler(payload: dict[str, Any], db: Any) -> None:
        raise asyncio.CancelledError("Worker cancelled externally")

    dispatcher.register("subscription.charged", cancelling_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_07",
        clock=lambda: now,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.process_one()

    # Verify event was NOT updated with failure or attempts
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.pending
    assert saved_event.attempts == 0
    # Lease token remains intact for automatic recovery after expiry
    assert saved_event.lease_token is not None


@pytest.mark.asyncio
async def test_mock_wrk_08_ed06_logging_sanitization(caplog: pytest.LogCaptureFixture) -> None:
    """MOCK-WRK-08: ED-06 error messages are sanitized before database persistence and logging."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_08", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    async def leaky_handler(payload: dict[str, Any], db: Any) -> None:
        raise RuntimeError(
            "API failure with key_secret=mySuperSecret999 and webhook_secret=whsec_TopSecret123"
        )

    dispatcher.register("subscription.charged", leaky_handler)

    worker = RazorpayWebhookWorker(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_08",
        clock=lambda: now,
    )

    with caplog.at_level("WARNING"):
        result = await worker.process_one()

    assert result is not None
    assert result.success is False

    # Check database persisted error message
    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert "mySuperSecret999" not in (saved_event.error_message or "")
    assert "whsec_TopSecret123" not in (saved_event.error_message or "")
    assert "[REDACTED]" in (saved_event.error_message or "")

    # Check captured logs
    for record in caplog.records:
        assert "mySuperSecret999" not in record.message
        assert "whsec_TopSecret123" not in record.message


@pytest.mark.asyncio
async def test_mock_wrk_09_stale_lease_rejection() -> None:
    """MOCK-WRK-09: Stale worker loses lease before execution; rejected without mutations."""
    now = datetime.now(timezone.utc)
    worker_1_token = uuid.uuid4()
    worker_2_token = uuid.uuid4()

    # Event in repo already superseded by worker_2
    event = make_test_event(
        "evt_wrk_09",
        lease_token=worker_2_token,
        locked_by="worker_2",
        lease_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRazorpayEventRepository([event])

    stale_claim = ClaimedWebhookEvent(
        event_id=event.id,
        provider=event.provider,
        merchant_account_id=event.merchant_account_id,
        environment=event.environment,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        payload={},
        attempts=0,
        lease_token=worker_1_token,  # Stale token
        locked_by="worker_1",
        lease_expires_at=now - timedelta(seconds=10),
        received_at=event.received_at,
    )

    async def dummy_handler(payload: dict[str, Any], db: Any) -> None:
        pass

    result = await execute_claimed_razorpay_webhook_event(
        session_factory=lambda: RazorpayWorkerMockDbSession(repo, clock_now=now),
        claimed=stale_claim,
        handler=dummy_handler,
        now_fn=lambda: now,
    )

    assert result.success is False
    assert result.lease_lost is True
    assert result.failure_category == "stale_lease"

    # DB state unchanged
    persisted = repo.get_event(event.id)
    assert persisted is not None
    assert persisted.lease_token == worker_2_token
    assert persisted.locked_by == "worker_2"


@pytest.mark.asyncio
async def test_mock_wrk_10_commit_failure_fallback_handling() -> None:
    """MOCK-WRK-10: Commit failure triggers fenced fallback transaction to record failure."""
    now = datetime.now(timezone.utc)
    event = make_test_event("evt_wrk_10", attempts=0, received_at=now)
    repo = SharedRazorpayEventRepository([event])
    dispatcher = RazorpayWebhookDispatcher()

    async def simple_handler(payload: dict[str, Any], db: Any) -> None:
        pass

    dispatcher.register("subscription.charged", simple_handler)

    commit_count = 0

    class FailingCommitMockDbSession(RazorpayWorkerMockDbSession):
        async def commit(self) -> None:
            nonlocal commit_count
            commit_count += 1
            if commit_count == 2:
                raise RuntimeError("PostgreSQL connection severed during outer commit")
            await super().commit()

    worker = RazorpayWebhookWorker(
        session_factory=lambda: FailingCommitMockDbSession(repo, clock_now=now),
        dispatcher=dispatcher,
        worker_id="worker_wrk_10",
        clock=lambda: now,
    )

    result = await worker.process_one()
    assert result is not None
    assert result.success is False
    assert result.status == WebhookProcessingStatus.failed
    assert result.failure_category == "commit_failure"
    assert result.attempts == 1

    saved_event = repo.get_event(event.id)
    assert saved_event is not None
    assert saved_event.status == WebhookProcessingStatus.failed
    assert saved_event.attempts == 1
    assert "connection severed" in (saved_event.error_message or "").lower()
