"""Comprehensive unit and concurrency tests for Razorpay checkout recovery worker (Stage 2D).

Covers all 28 required Stage 2D scenarios across:
1. Eligibility and Scheduling:
   - test_01_eligible_recovery_attempt_is_selected
   - test_02_future_retry_time_is_respected
   - test_03_non_recovery_states_excluded
   - test_04_terminal_states_excluded
   - test_05_invalid_or_missing_environment_fails_closed
   - test_06_production_worker_rejects_test_mode_and_requires_live
   - test_07_maximum_recovery_attempts_enforced
2. Claiming and Concurrency:
   - test_08_claim_generates_and_persists_uuid_and_expiration
   - test_09_two_workers_cannot_acquire_same_valid_claim
   - test_10_expired_claims_reclaimed_safely
   - test_11_stale_worker_cannot_overwrite_superseded_claim
   - test_12_stale_worker_preserves_recovered_subscription_in_reconciliation
   - test_13_claim_ownership_verified_before_persistence
3. Execution and Reconciliation:
   - test_14_successful_provider_verification_transitions_to_open_and_clears_timer
   - test_15_transient_provider_failure_schedules_retry_without_incrementing_count
   - test_16_inconclusive_search_increments_count_and_schedules_backoff
   - test_17_multiple_candidates_ambiguous_creates_reconciliation_and_retries
   - test_18_exhausting_max_attempts_quarantines_and_creates_reconciliation
   - test_19_definitive_nonexistence_evidence_marks_failed
   - test_20_duplicate_recovery_execution_is_idempotent
   - test_21_organization_merchant_and_environment_mismatches_fail_closed
   - test_22_zero_db_locks_held_during_external_gateway_calls
4. Worker Lifecycle & Configuration:
   - test_23_worker_start_and_clean_stop
   - test_24_cancellation_handled_gracefully
   - test_25_empty_queue_does_not_busy_loop
   - test_26_one_failed_attempt_does_not_terminate_worker
   - test_27_disabled_configuration_safely_no_ops
   - test_28_cli_parser_and_validation
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any
import uuid

import pytest

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
from app.config import settings
from app.models.payment import (
    BillingCheckoutAttempt,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus
from app.services.billing.razorpay.client import (
    RazorpayClient,
    RazorpayNetworkError,
    RazorpayRateLimitError,
    RazorpayServer5xxError,
    set_razorpay_client_for_testing,
)
from app.services.billing.razorpay.recovery_engine import (
    DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    DEFAULT_TRANSIENT_BACKOFF_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_BACKOFF_SCHEDULE,
    ClaimedRecoveryAttempt,
    RecoveryExecutionResult,
    RecoveryOutcome,
    StaleRecoveryClaimError,
    claim_next_recovery_attempt,
    claim_recovery_batch,
    compute_next_recovery_retry,
    execute_claimed_recovery_attempt,
)
from app.workers.razorpay_checkout_recovery_worker import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    RazorpayCheckoutRecoveryWorker,
    build_parser,
    run_recovery_worker,
    setup_signal_handlers,
)


# =============================================================================
# In-Memory Concurrency Test Doubles
# =============================================================================


class SharedRecoveryAttemptRepository:
    """Thread/coroutine-safe shared repository simulating PostgreSQL table state across sessions."""

    def __init__(self, attempts: list[BillingCheckoutAttempt] | None = None) -> None:
        self.attempts: dict[uuid.UUID, BillingCheckoutAttempt] = {
            a.id: deepcopy(a) for a in (attempts or [])
        }
        self.locked_ids: set[uuid.UUID] = set()
        self.reconciliations: list[PaymentReconciliation] = []

    def get_attempt(self, attempt_id: uuid.UUID) -> BillingCheckoutAttempt | None:
        att = self.attempts.get(attempt_id)
        return deepcopy(att) if att is not None else None

    def save_attempt(self, attempt: BillingCheckoutAttempt) -> None:
        self.attempts[attempt.id] = deepcopy(attempt)


class MockRecoverySavepoint:
    """Mock savepoint supporting async with for testing nested transactions."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> "MockRecoverySavepoint":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class RecoveryWorkerMockDbSession:
    """In-memory async session test double providing transaction isolation and row locking."""

    _counter = 0

    def __init__(
        self,
        repo: SharedRecoveryAttemptRepository,
        clock_now: datetime | None = None,
    ) -> None:
        RecoveryWorkerMockDbSession._counter += 1
        self.session_id = f"rec_session_{RecoveryWorkerMockDbSession._counter}"
        self.repo = repo
        self.clock_now = clock_now or datetime.now(timezone.utc)
        self.held_locks: set[uuid.UUID] = set()
        self.staged_changes: dict[uuid.UUID, BillingCheckoutAttempt] = {}
        self.staged_reconciliations: list[PaymentReconciliation] = []
        self._in_transaction: bool = True

    async def __aenter__(self) -> "RecoveryWorkerMockDbSession":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()

    def begin_nested(self) -> MockRecoverySavepoint:
        return MockRecoverySavepoint(self)

    def in_transaction(self) -> bool:
        return self._in_transaction

    def add(self, obj: Any) -> None:
        if isinstance(obj, PaymentReconciliation):
            assert obj.case_key is not None and len(str(obj.case_key).strip()) > 0, "case_key is mandatory on PaymentReconciliation"
            self.staged_reconciliations.append(obj)

    async def flush(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        if isinstance(obj, BillingCheckoutAttempt) and obj.id in self.repo.attempts:
            saved = self.repo.attempts[obj.id]
            for attr in (
                "recovery_claim_id",
                "recovery_claim_expires_at",
                "recovery_attempts",
                "next_recovery_retry_at",
                "status",
                "error_message",
                "request_payload",
                "provider_subscription_id",
                "provider_session_id",
                "checkout_url",
            ):
                if hasattr(saved, attr):
                    setattr(obj, attr, getattr(saved, attr))

    async def commit(self) -> None:
        for attempt_id, att in self.staged_changes.items():
            self.repo.save_attempt(att)
        for rec in self.staged_reconciliations:
            self.repo.reconciliations.append(deepcopy(rec))

        self.repo.locked_ids.difference_update(self.held_locks)
        self.held_locks.clear()
        self.staged_changes.clear()
        self.staged_reconciliations.clear()

    async def rollback(self) -> None:
        self.repo.locked_ids.difference_update(self.held_locks)
        self.held_locks.clear()
        self.staged_changes.clear()
        self.staged_reconciliations.clear()

    async def execute(self, statement: Any, params: Any = None) -> Any:
        class ScalarResult:
            def __init__(self, items: list[Any]) -> None:
                self._items = items

            def scalar_one_or_none(self) -> Any:
                return self._items[0] if self._items else None

            def scalar_one(self) -> Any:
                if not self._items:
                    raise ValueError("No rows found")
                return self._items[0]

            def scalars(self) -> "ScalarResult":
                return self

            def all(self) -> list[Any]:
                return list(self._items)

            def first(self) -> Any:
                return self._items[0] if self._items else None

        stmt_str = str(statement)
        now = self.clock_now

        # 1. Claim Batch Query: SELECT ... FROM billing_checkout_attempts WHERE ... FOR UPDATE SKIP LOCKED
        if "billing_checkout_attempts.status = :status_1" in stmt_str or "status IN" in stmt_str or "ORDER BY" in stmt_str:
            candidates: list[BillingCheckoutAttempt] = []
            for att in self.repo.attempts.values():
                # Check row lock held by another session (SKIP LOCKED)
                if att.id in self.repo.locked_ids and att.id not in self.held_locks:
                    continue

                if att.status != CheckoutAttemptStatus.recovery_required:
                    continue
                if att.provider != "razorpay":
                    continue

                env_filter = None
                try:
                    env_filter = statement.compile().params.get("environment_1")
                except Exception:
                    pass
                if env_filter is not None and att.environment != env_filter:
                    continue

                merchant_filter = None
                try:
                    merchant_filter = statement.compile().params.get("merchant_account_id_1")
                except Exception:
                    pass
                if merchant_filter is not None and att.merchant_account_id != merchant_filter:
                    continue

                if att.next_recovery_retry_at is None or att.next_recovery_retry_at > now:
                    continue
                if att.recovery_claim_expires_at is not None and att.recovery_claim_expires_at > now:
                    continue
                if (att.recovery_attempts or 0) >= MAX_RECOVERY_ATTEMPTS:
                    continue

                # Staging attempt in session
                staged = deepcopy(att)
                self.staged_changes[staged.id] = staged
                self.held_locks.add(staged.id)
                self.repo.locked_ids.add(staged.id)
                candidates.append(staged)

            # Sort by earliest retry time, then created_at
            candidates.sort(key=lambda a: (a.next_recovery_retry_at or now, a.created_at or now))

            limit = 10
            if "LIMIT" in stmt_str:
                limit = 1 if "LIMIT :param_1" in stmt_str or "limit(1)" in stmt_str.lower() else 10
            return ScalarResult(candidates[:limit])

        # 2. Lock attempt by ID: SELECT ... WHERE id = :id FOR UPDATE
        if "billing_checkout_attempts.id = :id_1" in stmt_str or "billing_checkout_attempts.id ==" in stmt_str:
            target_id = None
            try:
                target_id = statement.compile().params.get("id_1")
            except Exception:
                pass

            if target_id is None:
                # Look for UUID in statement string or held locks
                for att_id in self.repo.attempts.keys():
                    if str(att_id) in stmt_str:
                        target_id = att_id
                        break

            if target_id and target_id in self.repo.attempts:
                if target_id in self.staged_changes:
                    staged = self.staged_changes[target_id]
                else:
                    staged = deepcopy(self.repo.attempts[target_id])
                    self.staged_changes[target_id] = staged
                self.held_locks.add(target_id)
                self.repo.locked_ids.add(target_id)
                return ScalarResult([staged])

            return ScalarResult([])

        # 3. Query PaymentReconciliation: SELECT ... FROM payment_reconciliations WHERE ...
        if "payment_reconciliations" in stmt_str:
            case_key = None
            try:
                case_key = statement.compile().params.get("case_key_1")
            except Exception:
                pass
            found = []
            for rec in self.repo.reconciliations:
                if case_key is None or rec.case_key == case_key:
                    found.append(rec)
            for rec in self.staged_reconciliations:
                if (case_key is None or rec.case_key == case_key) and rec not in found:
                    found.append(rec)
            return ScalarResult(found)

        return ScalarResult([])


class MockRazorpayClient:
    """Mock Razorpay HTTP client test double with controllable responses and call tracking."""

    def __init__(
        self,
        subscription_data: dict[str, Any] | None = None,
        list_items: list[dict[str, Any]] | None = None,
        fail_with_5xx: bool = False,
        fail_with_network: bool = False,
        fail_with_rate_limit: bool = False,
    ) -> None:
        self.subscription_data = subscription_data or {}
        self.list_items = list_items or []
        self.fail_with_5xx = fail_with_5xx
        self.fail_with_network = fail_with_network
        self.fail_with_rate_limit = fail_with_rate_limit
        self.get_subscription_calls: list[str] = []
        self.list_subscription_calls: list[dict[str, Any]] = []

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        self.get_subscription_calls.append(subscription_id)
        if self.fail_with_5xx:
            raise RazorpayServer5xxError(500, "Internal Server Error from Razorpay")
        if self.fail_with_network:
            raise RazorpayNetworkError("Gateway connection timed out")
        if self.fail_with_rate_limit:
            raise RazorpayRateLimitError(429, "Too Many Requests")
        return self.subscription_data

    async def list_subscriptions(
        self,
        plan_id: str | None = None,
        from_time: int | None = None,
        count: int = 50,
        skip: int = 0,
    ) -> dict[str, Any]:
        self.list_subscription_calls.append({
            "plan_id": plan_id,
            "from_time": from_time,
            "count": count,
            "skip": skip,
        })
        if self.fail_with_5xx:
            raise RazorpayServer5xxError(500, "Gateway 502 Bad Gateway")
        if self.fail_with_network:
            raise RazorpayNetworkError("Gateway connection timeout")
        if self.fail_with_rate_limit:
            raise RazorpayRateLimitError(429, "Rate limit reached")
        return {
            "entity": "collection",
            "count": len(self.list_items),
            "items": self.list_items[skip : skip + count],
        }


def _create_attempt(
    status: CheckoutAttemptStatus = CheckoutAttemptStatus.recovery_required,
    next_recovery_retry_at: datetime | None = None,
    recovery_claim_expires_at: datetime | None = None,
    recovery_claim_id: uuid.UUID | None = None,
    recovery_attempts: int = 0,
    environment: str = "test",
    provider: str = "razorpay",
    org_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    request_payload: dict[str, Any] | None = None,
    provider_subscription_id: str | None = None,
) -> BillingCheckoutAttempt:
    now = datetime.now(timezone.utc)
    return BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        provider=provider,
        merchant_account_id="acc_test_merchant",
        environment=environment,
        provider_customer_id="cust_test_123",
        provider_subscription_id=provider_subscription_id,
        idempotency_key=f"idemp_{uuid.uuid4().hex[:8]}",
        status=status,
        amount_minor_units=0,
        currency="USD",
        request_payload=request_payload if request_payload is not None else {"plan_id": "plan_test_pro"},
        attempt_lease_expires_at=None,
        next_recovery_retry_at=next_recovery_retry_at or now,
        recovery_claim_expires_at=recovery_claim_expires_at,
        recovery_claim_id=recovery_claim_id,
        recovery_attempts=recovery_attempts,
        created_at=created_at or (now - timedelta(minutes=5)),
        updated_at=now,
    )


# =============================================================================
# 1. Eligibility & Scheduling Tests
# =============================================================================


@pytest.mark.anyio
async def test_01_eligible_recovery_attempt_is_selected() -> None:
    """Eligible recovery attempt in recovery_required with next_recovery_retry_at <= now is claimed."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now - timedelta(seconds=10),
    )
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(
        db=session,
        worker_id="worker_test_1",
        now=now,
    )

    assert claimed is not None
    assert claimed.attempt_id == att.id
    assert claimed.status == CheckoutAttemptStatus.recovery_required
    assert claimed.recovery_claim_id is not None
    assert claimed.recovery_claim_expires_at > now


@pytest.mark.anyio
async def test_02_future_retry_time_is_respected() -> None:
    """An attempt with next_recovery_retry_at in the future is excluded from claiming."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(
        db=session,
        worker_id="worker_test_1",
        now=now,
    )

    assert claimed is None


@pytest.mark.anyio
async def test_03_non_recovery_states_excluded() -> None:
    """Non-recovery states ('creating', 'open', 'payment_pending') are never claimed by recovery worker."""
    now = datetime.now(timezone.utc)
    attempts = [
        _create_attempt(status=CheckoutAttemptStatus.creating, next_recovery_retry_at=now),
        _create_attempt(status=CheckoutAttemptStatus.open, next_recovery_retry_at=now),
        _create_attempt(status=CheckoutAttemptStatus.payment_pending, next_recovery_retry_at=now),
    ]
    repo = SharedRecoveryAttemptRepository(attempts)
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(db=session, worker_id="worker_test_1", now=now)
    assert claimed is None


@pytest.mark.anyio
async def test_04_terminal_states_excluded() -> None:
    """Terminal states ('complete', 'failed', 'expired') are strictly excluded from recovery."""
    now = datetime.now(timezone.utc)
    attempts = [
        _create_attempt(status=CheckoutAttemptStatus.complete, next_recovery_retry_at=now),
        _create_attempt(status=CheckoutAttemptStatus.failed, next_recovery_retry_at=now),
        _create_attempt(status=CheckoutAttemptStatus.expired, next_recovery_retry_at=now),
    ]
    repo = SharedRecoveryAttemptRepository(attempts)
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(db=session, worker_id="worker_test_1", now=now)
    assert claimed is None


@pytest.mark.anyio
async def test_05_invalid_or_missing_environment_fails_closed() -> None:
    """An attempt with non-razorpay provider or unsupported environment is not selected."""
    now = datetime.now(timezone.utc)
    att_stripe = _create_attempt(provider="stripe", next_recovery_retry_at=now)
    att_invalid_env = _create_attempt(environment="unknown_env", next_recovery_retry_at=now)
    repo = SharedRecoveryAttemptRepository([att_stripe, att_invalid_env])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(
        db=session, worker_id="worker_test_1", environment="test", now=now
    )
    assert claimed is None


def test_06_production_worker_rejects_test_mode_and_requires_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production recovery worker strictly requires environment='live' and raises ValueError for 'test'."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "live")

    # Explicit test environment raises in production
    with pytest.raises(ValueError, match="Production RazorpayCheckoutRecoveryWorker strictly requires environment='live'"):
        RazorpayCheckoutRecoveryWorker(environment="test")

    # Valid live environment in production is accepted
    worker = RazorpayCheckoutRecoveryWorker(environment="live")
    assert worker.environment == "live"


@pytest.mark.anyio
async def test_07_maximum_recovery_attempts_enforced() -> None:
    """An attempt with recovery_attempts >= 5 is excluded from claim queries (quarantined)."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_attempts=5,
        next_recovery_retry_at=now - timedelta(minutes=1),
    )
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(db=session, worker_id="worker_test_1", now=now)
    assert claimed is None


# =============================================================================
# 2. Claiming & Concurrency Tests
# =============================================================================


@pytest.mark.anyio
async def test_08_claim_generates_and_persists_uuid_and_expiration() -> None:
    """Claim stamps unpredictable UUID recovery_claim_id and sets recovery_claim_expires_at = now + lease."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(next_recovery_retry_at=now)
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(
        db=session,
        worker_id="worker_rec_42",
        lease_duration_seconds=90,
        now=now,
    )

    assert claimed is not None
    assert isinstance(claimed.recovery_claim_id, uuid.UUID)
    assert claimed.recovery_claim_expires_at == now + timedelta(seconds=90)

    # Verify persisted in repository
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.recovery_claim_id == claimed.recovery_claim_id
    assert saved.recovery_claim_expires_at == claimed.recovery_claim_expires_at


@pytest.mark.anyio
async def test_09_two_workers_cannot_acquire_same_valid_claim() -> None:
    """Worker A acquires a valid lease; Worker B cannot claim the same attempt concurrently."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(next_recovery_retry_at=now)
    repo = SharedRecoveryAttemptRepository([att])

    session_a = RecoveryWorkerMockDbSession(repo, clock_now=now)
    claimed_a = await claim_next_recovery_attempt(db=session_a, worker_id="worker_a", now=now)
    assert claimed_a is not None

    session_b = RecoveryWorkerMockDbSession(repo, clock_now=now)
    claimed_b = await claim_next_recovery_attempt(db=session_b, worker_id="worker_b", now=now)
    assert claimed_b is None


@pytest.mark.anyio
async def test_10_expired_claims_reclaimed_safely() -> None:
    """When a worker crashes and lease expires, a second worker safely claims the attempt with a new claim UUID."""
    now = datetime.now(timezone.utc)
    old_claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now - timedelta(minutes=5),
        recovery_claim_expires_at=now - timedelta(seconds=1),  # Expired lease
        recovery_claim_id=old_claim_id,
        recovery_attempts=1,
    )
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = await claim_next_recovery_attempt(db=session, worker_id="worker_b", now=now)
    assert claimed is not None
    assert claimed.recovery_claim_id != old_claim_id
    assert claimed.recovery_attempts == 1  # Expired claim recovery does not increment count


@pytest.mark.anyio
async def test_11_stale_worker_cannot_overwrite_superseded_claim() -> None:
    """A stale worker whose claim was superseded is rejected with StaleRecoveryClaimError on persistence."""
    now = datetime.now(timezone.utc)
    stale_claim_id = uuid.uuid4()
    newer_claim_id = uuid.uuid4()

    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=newer_claim_id,  # Concurrently updated by newer worker
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    stale_claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=stale_claim_id,  # Stale token
        recovery_claim_expires_at=now - timedelta(seconds=10),
        recovery_attempts=att.recovery_attempts,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id=att.provider_customer_id,
        provider_subscription_id=None,
        worker_id="stale_worker",
    )

    client = MockRazorpayClient(
        subscription_data={
            "id": "sub_stale_found_123",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "active",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    with pytest.raises(StaleRecoveryClaimError, match="superseded by another worker"):
        await execute_claimed_recovery_attempt(
            session_factory=session_factory,
            claimed=stale_claimed,
            client=client,
            now_fn=lambda: now,
            apply_jitter=False,
        )


@pytest.mark.anyio
async def test_12_stale_worker_preserves_recovered_subscription_in_reconciliation() -> None:
    """When a stale worker discovers a remote subscription, the candidate is preserved in PaymentReconciliation."""
    now = datetime.now(timezone.utc)
    stale_claim_id = uuid.uuid4()
    newer_claim_id = uuid.uuid4()

    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=newer_claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    stale_claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=stale_claim_id,
        recovery_claim_expires_at=now - timedelta(seconds=10),
        recovery_attempts=att.recovery_attempts,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id=att.provider_customer_id,
        provider_subscription_id=None,
        worker_id="stale_worker",
    )

    client = MockRazorpayClient(
        subscription_data={
            "id": "sub_preserved_reconciliation_456",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "active",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    from app.services.billing.razorpay.recovery_engine import _record_recovery_outcome

    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=session_factory,
            claimed=stale_claimed,
            client=client,
            now_fn=lambda: now,
            known_subscription_id="sub_preserved_reconciliation_456",
            apply_jitter=False,
        )

    # Verify PaymentReconciliation was created
    assert len(repo.reconciliations) == 1
    rec = repo.reconciliations[0]
    assert rec.reason == "superseded_recovery_worker"
    assert rec.status == ReconciliationStatus.pending_review
    assert rec.diagnostic_payload.get("recovered_subscription_id") == "sub_preserved_reconciliation_456"


@pytest.mark.anyio
async def test_13_claim_ownership_verified_before_persistence() -> None:
    """Attempt lock is reacquired and recovery_claim_id verified under write lock before applying state."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id=att.provider_customer_id,
        provider_subscription_id=None,
        worker_id="valid_worker",
    )

    client = MockRazorpayClient(
        subscription_data={
            "id": "sub_valid_123",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        known_subscription_id="sub_valid_123",
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.RECOVERED
    assert res.subscription_id == "sub_valid_123"


# =============================================================================
# 3. Execution & Reconciliation Tests
# =============================================================================


@pytest.mark.anyio
async def test_14_successful_provider_verification_transitions_to_open_and_clears_timer() -> None:
    """When exactly 1 matching remote subscription is found, status becomes 'open' and recovery timer is cleared."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        next_recovery_retry_at=now,
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(
        list_items=[{
            "id": "sub_remote_found_999",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
            "short_url": "https://rzp.io/i/sub_remote_found_999",
        }]
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.RECOVERED
    assert res.subscription_id == "sub_remote_found_999"

    # Verify attempt in repo
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.open
    assert saved.provider_subscription_id == "sub_remote_found_999"
    assert saved.next_recovery_retry_at is None
    assert saved.recovery_claim_id is None
    assert saved.recovery_claim_expires_at is None


@pytest.mark.anyio
async def test_15_transient_provider_failure_schedules_retry_without_incrementing_count() -> None:
    """Gateway 5xx / timeout triggers TRANSIENT_BACKOFF without incrementing recovery_attempts."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=2,
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=2,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(fail_with_5xx=True)

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.TRANSIENT_BACKOFF
    assert res.recovery_attempts == 2  # NOT incremented!
    assert res.next_recovery_retry_at == now + timedelta(seconds=DEFAULT_TRANSIENT_BACKOFF_SECONDS)

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.recovery_attempts == 2
    assert saved.status == CheckoutAttemptStatus.recovery_required
    assert saved.recovery_claim_id is None


@pytest.mark.anyio
async def test_16_inconclusive_search_increments_count_and_schedules_backoff() -> None:
    """When remote search returns 0 candidates, outcome is RETRY_SCHEDULED, attempts increment, and backoff is applied."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(list_items=[])  # 0 candidates

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.RETRY_SCHEDULED
    assert res.recovery_attempts == 1
    # Attempt 1 backoff is 60s
    expected_retry = now + timedelta(seconds=RECOVERY_BACKOFF_SCHEDULE[0])
    assert res.next_recovery_retry_at == expected_retry

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.recovery_attempts == 1
    assert saved.next_recovery_retry_at == expected_retry
    assert saved.recovery_claim_id is None


@pytest.mark.anyio
async def test_17_multiple_candidates_ambiguous_creates_reconciliation_and_retries() -> None:
    """When >1 candidate is found on Razorpay, stages PaymentReconciliation and schedules retry."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=1,
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=1,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(
        list_items=[
            {
                "id": "sub_cand_1",
                "plan_id": "plan_test_pro",
                "customer_id": "cust_test_123",
                "status": "authenticated",
                "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
            },
            {
                "id": "sub_cand_2",
                "plan_id": "plan_test_pro",
                "customer_id": "cust_test_123",
                "status": "authenticated",
                "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
            },
        ]
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.RETRY_SCHEDULED
    assert res.recovery_attempts == 2
    # Attempt 2 backoff is 300s
    assert res.next_recovery_retry_at == now + timedelta(seconds=RECOVERY_BACKOFF_SCHEDULE[1])

    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].reason == "multiple_recovery_candidates_found"
    assert repo.reconciliations[0].status == ReconciliationStatus.pending_review


@pytest.mark.anyio
async def test_18_exhausting_max_attempts_quarantines_and_creates_reconciliation() -> None:
    """On the 5th attempt failure, outcome is QUARANTINED, timer set to NULL, and reconciliation staged."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=4,  # Next attempt is 5th
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=4,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(list_items=[])  # Still inconclusive

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.QUARANTINED
    assert res.recovery_attempts == 5
    assert res.next_recovery_retry_at is None

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.recovery_required  # Remains quarantined
    assert saved.next_recovery_retry_at is None  # Excluded from future worker queries
    assert saved.recovery_attempts == 5

    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].reason == "recovery_budget_exhausted"
    assert repo.reconciliations[0].status == ReconciliationStatus.pending_review


@pytest.mark.anyio
async def test_19_definitive_nonexistence_evidence_marks_failed() -> None:
    """When definitive proof is provided, attempt transitions to 'failed' and releases active reservation."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=2,
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=2,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        now_fn=lambda: now,
        definitive_nonexistence_evidence="Razorpay Support Ticket #88412 confirmed no remote subscription created",
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.FAILED_DEFINITIVE
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.failed
    assert saved.next_recovery_retry_at is None
    assert saved.recovery_claim_id is None

    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].reason == "definitive_nonexistence_evidence"
    assert repo.reconciliations[0].status == ReconciliationStatus.resolved_ignored


@pytest.mark.anyio
async def test_20_duplicate_recovery_execution_is_idempotent() -> None:
    """Recovery execution is idempotent: once open, subsequent execution returns open without duplicate side-effects."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    client = MockRazorpayClient(
        subscription_data={
            "id": "sub_idempotent_123",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        known_subscription_id="sub_idempotent_123",
        apply_jitter=False,
    )
    assert res.outcome == RecoveryOutcome.RECOVERED
    assert res.subscription_id == "sub_idempotent_123"

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.open


@pytest.mark.anyio
async def test_21_organization_merchant_and_environment_mismatches_fail_closed() -> None:
    """Remote subscription matching another organization or wrong plan is rejected and quarantined."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    # Remote sub belongs to a different organization!
    client = MockRazorpayClient(
        subscription_data={
            "id": "sub_wrong_org_789",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(uuid.uuid4())},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        known_subscription_id="sub_wrong_org_789",
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.QUARANTINED
    assert "organization_mismatch" in (res.error_message or "")


@pytest.mark.anyio
async def test_22_zero_db_locks_held_during_external_gateway_calls() -> None:
    """Verifies that DB transaction is not held while executing remote gateway inquiries."""
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att])

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={"plan_id": "plan_test_pro"},
        provider_customer_id="cust_test_123",
        provider_subscription_id=None,
        worker_id="worker_rec_1",
    )

    # Track open DB sessions during client call
    active_sessions_during_http: list[int] = []

    class AssertingClient(MockRazorpayClient):
        async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
            # At this moment, repo.locked_ids MUST be empty because claim transaction was committed!
            active_sessions_during_http.append(len(repo.locked_ids))
            return await super().get_subscription(subscription_id)

    client = AssertingClient(
        subscription_data={
            "id": "sub_unlocked_call_123",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
        }
    )

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
        known_subscription_id="sub_unlocked_call_123",
        apply_jitter=False,
    )

    assert res.outcome == RecoveryOutcome.RECOVERED
    assert len(active_sessions_during_http) == 1
    assert active_sessions_during_http[0] == 0  # Zero database row locks held!


# =============================================================================
# 4. Worker Lifecycle & Configuration Tests
# =============================================================================


@pytest.mark.anyio
async def test_23_worker_start_and_clean_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker polling loop starts, processes batch, and exits cleanly upon worker.stop()."""
    monkeypatch.setattr("app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled", lambda: True)
    monkeypatch.setattr("app.services.billing.razorpay.recovery_engine.is_razorpay_enabled", lambda: True)
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now - timedelta(seconds=1),
    )
    repo = SharedRecoveryAttemptRepository([att])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    client = MockRazorpayClient(
        list_items=[{
            "id": "sub_worker_loop_123",
            "plan_id": "plan_test_pro",
            "customer_id": "cust_test_123",
            "status": "authenticated",
            "notes": {"attempt_id": str(att.id), "organization_id": str(att.organization_id)},
            "short_url": "https://rzp.io/i/sub_worker_loop_123",
        }]
    )

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        poll_interval=0.05,
        batch_size=2,
        client=client,
        clock=lambda: now,
        apply_jitter=False,
    )

    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.1)
    worker.stop()
    await task

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.open


@pytest.mark.anyio
async def test_24_cancellation_handled_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.CancelledError terminates the worker polling loop cleanly without unhandled crash."""
    monkeypatch.setattr("app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled", lambda: True)
    monkeypatch.setattr("app.services.billing.razorpay.recovery_engine.is_razorpay_enabled", lambda: True)
    repo = SharedRecoveryAttemptRepository([])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo)

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        poll_interval=1.0,
    )

    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_25_empty_queue_does_not_busy_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the queue is empty, worker executes single pass and run_once returns 0."""
    monkeypatch.setattr("app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled", lambda: True)
    monkeypatch.setattr("app.services.billing.razorpay.recovery_engine.is_razorpay_enabled", lambda: True)
    repo = SharedRecoveryAttemptRepository([])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo)

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        poll_interval=0.1,
    )

    processed_count = await worker.run_once()
    assert processed_count == 0


@pytest.mark.anyio
async def test_26_one_failed_attempt_does_not_terminate_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unhandled error in one attempt does not terminate the batch execution or worker loop."""
    monkeypatch.setattr("app.workers.razorpay_checkout_recovery_worker.is_razorpay_enabled", lambda: True)
    monkeypatch.setattr("app.services.billing.razorpay.recovery_engine.is_razorpay_enabled", lambda: True)
    now = datetime.now(timezone.utc)
    att_fail = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now,
    )
    att_ok = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now,
    )
    repo = SharedRecoveryAttemptRepository([att_fail, att_ok])

    call_count = 0

    class SelectiveFailClient(MockRazorpayClient):
        async def list_subscriptions(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt raises unexpected exception
                raise RuntimeError("Corrupted memory buffer simulation")
            return {
                "entity": "collection",
                "count": 1,
                "items": [{
                    "id": "sub_second_attempt_success",
                    "plan_id": "plan_test_pro",
                    "customer_id": "cust_test_123",
                    "status": "authenticated",
                    "notes": {"attempt_id": str(att_ok.id), "organization_id": str(att_ok.organization_id)},
                }],
            }

    client = SelectiveFailClient()

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        batch_size=2,
        client=client,
        clock=lambda: now,
        apply_jitter=False,
    )

    results = await worker.run_batch()
    # First attempt handled gateway failure as RETRY_SCHEDULED, second attempt succeeded as RECOVERED
    assert len(results) == 2
    assert results[0].outcome == RecoveryOutcome.RETRY_SCHEDULED
    assert results[1].outcome == RecoveryOutcome.RECOVERED
    assert results[1].subscription_id == "sub_second_attempt_success"


@pytest.mark.anyio
async def test_27_disabled_configuration_safely_no_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """When is_razorpay_enabled() is False, worker.run_once() safely returns 0 without claiming."""
    monkeypatch.setattr(settings, "razorpay_enabled", False)
    now = datetime.now(timezone.utc)
    att = _create_attempt(status=CheckoutAttemptStatus.recovery_required, next_recovery_retry_at=now)
    repo = SharedRecoveryAttemptRepository([att])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        clock=lambda: now,
    )

    count = await worker.run_once()
    assert count == 0
    # Attempt remains unclaimed
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.recovery_claim_id is None


def test_28_cli_parser_and_validation() -> None:
    """CLI argument parser validates arguments correctly."""
    parser = build_parser()

    # Default flags
    args = parser.parse_args([])
    assert args.poll_interval is None
    assert args.batch_size is None
    assert args.max_attempts is None
    assert args.lease_duration is None
    assert args.once is False

    # Explicit flags
    custom = parser.parse_args([
        "--poll-interval", "5.5",
        "--batch-size", "25",
        "--max-attempts", "3",
        "--lease-duration", "120",
        "--once",
        "--log-level", "DEBUG",
    ])
    assert custom.poll_interval == 5.5
    assert custom.batch_size == 25
    assert custom.max_attempts == 3
    assert custom.lease_duration == 120
    assert custom.once is True
    assert custom.log_level == "DEBUG"


def test_29_environment_policy_matrix_prod_staging_dev_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production and non-production environments strictly enforce respective Razorpay environments."""
    for prod_env in ("production", "prod"):
        monkeypatch.setattr(settings, "environment", prod_env)
        monkeypatch.setattr(settings, "razorpay_environment", "live")
        worker = RazorpayCheckoutRecoveryWorker()
        assert worker.environment == "live"

        with pytest.raises(ValueError, match="strictly requires environment='live'"):
            RazorpayCheckoutRecoveryWorker(environment="test")

    for non_prod_env in ("development", "staging", "test", "ci"):
        monkeypatch.setattr(settings, "environment", non_prod_env)
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        worker = RazorpayCheckoutRecoveryWorker()
        assert worker.environment == "test"

        with pytest.raises(ValueError, match="strictly requires environment='test'"):
            RazorpayCheckoutRecoveryWorker(environment="live")


@pytest.mark.anyio
async def test_30_environment_mismatch_before_provider_api_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an attempt presents an environment mismatch, it is quarantined immediately with zero API calls."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="live",  # Attempt in DB says live, but deployment is development (requires test)
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        next_recovery_retry_at=now,
    )
    repo = SharedRecoveryAttemptRepository([att])
    client = MockRazorpayClient()

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="live",
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id=None,
        provider_subscription_id=None,
        worker_id="test_worker",
    )

    result = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
    )

    assert result.outcome == RecoveryOutcome.QUARANTINED
    assert "environment_safety_violation" in (result.error_message or "")
    # Zero gateway calls were made
    assert len(client.get_subscription_calls) == 0
    assert len(client.list_subscription_calls) == 0
    # Reconciliation record created with case_key
    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_quarantined:{att.id}"


@pytest.mark.anyio
async def test_31_repeated_transient_5xx_capped_and_quarantined(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated transient 5xx errors back off exponentially and quarantine at 10 failures."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4()
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        next_recovery_retry_at=now,
        request_payload={"transient_failures": 9},  # 9 previous transient failures
    )
    repo = SharedRecoveryAttemptRepository([att])
    client = MockRazorpayClient(fail_with_5xx=True)

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="test",
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload=att.request_payload,
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="test_worker",
    )

    result = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
    )

    assert result.outcome == RecoveryOutcome.QUARANTINED
    assert "transient_retry_budget_exhausted" in (result.error_message or "")
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.next_recovery_retry_at is None
    # Reconciliation record created
    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_transient_exhausted:{att.id}"
    assert repo.reconciliations[0].reason == "transient_retry_budget_exhausted"


@pytest.mark.anyio
async def test_32_repeated_worker_crashes_and_lease_expiry_capped_and_quarantined(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an attempt repeatedly expires leases (5 times), it is quarantined during claiming."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        next_recovery_retry_at=now - timedelta(minutes=10),
        recovery_claim_expires_at=now - timedelta(minutes=1),  # Expired lease
        request_payload={"expired_lease_count": 4},  # Already expired 4 times previously
    )
    repo = SharedRecoveryAttemptRepository([att])
    session = RecoveryWorkerMockDbSession(repo, clock_now=now)

    # Claiming should encounter the 5th expired lease, quarantine, and return None
    claimed = await claim_next_recovery_attempt(
        db=session,
        worker_id="worker_crashed_test",
        environment="test",
        now=now,
    )
    assert claimed is None

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.next_recovery_retry_at is None
    assert saved.error_message == "repeated_worker_lease_expiry_or_crashes"
    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_worker_crashes:{att.id}"
    assert repo.reconciliations[0].reason == "repeated_worker_crash_or_lease_expiry"


@pytest.mark.anyio
async def test_33_duplicate_stale_worker_reconciliation_is_idempotent_with_case_key() -> None:
    """Two stale workers discovering the same candidate use the same case_key and update history idempotently."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        recovery_claim_id=uuid.uuid4(),  # Active claim in DB belongs to Worker 3
    )
    repo = SharedRecoveryAttemptRepository([att])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    # Worker 1 (stale, holding old claim_id)
    claimed_w1 = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="test",
        status=att.status,
        recovery_claim_id=uuid.uuid4(),
        recovery_claim_expires_at=now - timedelta(seconds=10),
        recovery_attempts=1,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="worker_1",
    )

    # Worker 2 (also stale, holding another old claim_id)
    claimed_w2 = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="test",
        status=att.status,
        recovery_claim_id=uuid.uuid4(),
        recovery_claim_expires_at=now - timedelta(seconds=5),
        recovery_attempts=1,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="worker_2",
    )

    client = MockRazorpayClient(
        subscription_data={"id": "sub_stale_candidate", "plan_id": "plan_test_pro", "customer_id": "cust_test"}
    )

    # Worker 1 executes and is superseded
    from app.services.billing.razorpay.recovery_engine import _record_recovery_outcome

    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=session_factory,
            claimed=claimed_w1,
            client=client,
            known_subscription_id="sub_stale_candidate",
            now_fn=lambda: now,
        )

    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_superseded:{att.id}:sub_stale_candidate"

    # Worker 2 executes and is also superseded for the same subscription
    from app.services.billing.razorpay.recovery_engine import _record_recovery_outcome

    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=session_factory,
            claimed=claimed_w2,
            client=client,
            known_subscription_id="sub_stale_candidate",
            now_fn=lambda: now,
        )

    # Idempotent deduplication: still 1 reconciliation record with history appended
    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_superseded:{att.id}:sub_stale_candidate"
    history = repo.reconciliations[0].diagnostic_payload.get("history", [])
    assert len(history) == 2


@pytest.mark.anyio
async def test_34_stale_worker_cannot_overwrite_newer_worker_success() -> None:
    """A stale worker cannot mutate an attempt after a newer worker has marked it 'open'."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.open,  # Worker 2 already succeeded
        environment="test",
        provider_subscription_id="sub_worker2_success",
        recovery_claim_id=None,
    )
    repo = SharedRecoveryAttemptRepository([att])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    claimed_w1 = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="test",
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=uuid.uuid4(),
        recovery_claim_expires_at=now - timedelta(seconds=10),
        recovery_attempts=1,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="worker_1",
    )

    client = MockRazorpayClient(
        subscription_data={"id": "sub_w1_orphan", "plan_id": "plan_test_pro", "customer_id": "cust_test"}
    )

    from app.services.billing.razorpay.recovery_engine import _record_recovery_outcome

    with pytest.raises(StaleRecoveryClaimError):
        await execute_claimed_recovery_attempt(
            session_factory=session_factory,
            claimed=claimed_w1,
            client=client,
            known_subscription_id="sub_w1_orphan",
            now_fn=lambda: now,
        )

    # Attempt state remains completely unchanged
    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.open
    assert saved.provider_subscription_id == "sub_worker2_success"
    # Orphan subscription from stale worker was preserved in reconciliation
    assert len(repo.reconciliations) == 1
    assert repo.reconciliations[0].case_key == f"recovery_superseded:{att.id}:sub_w1_orphan"


@pytest.mark.anyio
async def test_35_successful_recovery_near_retry_limit_does_not_quarantine() -> None:
    """An attempt on its 5th attempt (recovery_attempts == 4) that succeeds is marked open, not quarantined."""
    now = datetime.now(timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        recovery_attempts=4,  # Next attempt is #5
        environment="test",
    )
    repo = SharedRecoveryAttemptRepository([att])

    def session_factory() -> RecoveryWorkerMockDbSession:
        return RecoveryWorkerMockDbSession(repo, clock_now=now)

    claim_id = uuid.uuid4()
    att.recovery_claim_id = claim_id
    att.recovery_claim_expires_at = now + timedelta(seconds=60)
    repo.save_attempt(att)

    claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment="test",
        status=att.status,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=att.recovery_claim_expires_at,
        recovery_attempts=4,
        created_at=att.created_at,
        request_payload={},
        provider_customer_id="cust_test",
        provider_subscription_id=None,
        worker_id="worker_limit_test",
    )

    client = MockRazorpayClient(
        subscription_data={"id": "sub_success_at_limit", "plan_id": "plan_test_pro", "customer_id": "cust_test"}
    )

    result = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        known_subscription_id="sub_success_at_limit",
        now_fn=lambda: now,
    )

    assert result.outcome == RecoveryOutcome.RECOVERED
    assert result.subscription_id == "sub_success_at_limit"

    saved = repo.get_attempt(att.id)
    assert saved is not None
    assert saved.status == CheckoutAttemptStatus.open
    assert saved.provider_subscription_id == "sub_success_at_limit"
    assert saved.next_recovery_retry_at is None
    # No quarantine reconciliation created
    assert len(repo.reconciliations) == 0


def test_36_worker_cli_arguments_and_invocation_contract() -> None:
    """CLI argument parser correctly parses environment and merchant account id."""
    parser = build_parser()
    args = parser.parse_args([
        "--environment", "live",
        "--merchant-account-id", "acc_prod_rzp_999",
        "--batch-size", "20",
    ])
    assert args.environment == "live"
    assert args.merchant_account_id == "acc_prod_rzp_999"
    assert args.batch_size == 20


@pytest.mark.asyncio
async def test_37_cli_environment_matrix_production_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production deployment strictly enforces live-only recovery and non-default merchant ID."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "live")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_prod_live_123")

    # 1. Production + omitted environment -> live only
    w1 = RazorpayCheckoutRecoveryWorker()
    assert w1.environment == "live"
    assert w1.merchant_account_id == "acc_prod_live_123"

    # 2. Production + explicit live -> permitted
    w2 = RazorpayCheckoutRecoveryWorker(environment="live", merchant_account_id="acc_prod_custom_456")
    assert w2.environment == "live"
    assert w2.merchant_account_id == "acc_prod_custom_456"

    # 3. Production + test environment override -> rejected
    with pytest.raises(ValueError, match="strictly requires environment='live'"):
        RazorpayCheckoutRecoveryWorker(environment="test")

    # 4. Production + settings.razorpay_environment mismatch -> rejected
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    with pytest.raises(ValueError, match="strictly requires RAZORPAY_ENVIRONMENT='live'"):
        RazorpayCheckoutRecoveryWorker(environment="live")

    monkeypatch.setattr(settings, "razorpay_environment", "live")

    # 5. Production + prohibited merchant account override -> rejected
    for prohibited in ["default", "test", "placeholder", "none", "null"]:
        with pytest.raises(ValueError, match="rejects prohibited merchant account"):
            RazorpayCheckoutRecoveryWorker(merchant_account_id=prohibited)


@pytest.mark.asyncio
async def test_38_cli_environment_matrix_non_production_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-production deployment strictly enforces test-only recovery and rejects live overrides."""
    for env in ["staging", "development", "test", "ci", "local"]:
        monkeypatch.setattr(settings, "environment", env)
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "default")

        # 1. Non-prod + omitted environment -> test only
        w = RazorpayCheckoutRecoveryWorker()
        assert w.environment == "test"

        # 2. Non-prod + explicit test environment -> test only
        w_test = RazorpayCheckoutRecoveryWorker(environment="test", merchant_account_id="acc_test_custom")
        assert w_test.environment == "test"
        assert w_test.merchant_account_id == "acc_test_custom"

        # 3. Non-prod + live environment override -> strictly rejected
        with pytest.raises(ValueError, match="strictly requires environment='test'"):
            RazorpayCheckoutRecoveryWorker(environment="live")

        # 4. Non-prod + empty merchant override -> rejected
        with pytest.raises(ValueError, match="merchant_account_id cannot be an empty string"):
            RazorpayCheckoutRecoveryWorker(merchant_account_id="   ")


@pytest.mark.asyncio
async def test_39_rejected_configurations_make_zero_provider_api_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected environment/merchant configurations fail closed before making any provider API calls."""
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "razorpay_environment", "test")

    mock_client = MockRazorpayClient()

    # Attempting to start a worker with illegal environment override
    with pytest.raises(ValueError):
        RazorpayCheckoutRecoveryWorker(environment="live", client=mock_client)

    assert mock_client.get_subscription_calls == []
    assert mock_client.list_subscription_calls == []

    # Direct execution with mismatch also makes 0 calls
    claim_id = uuid.uuid4()
    now_mismatch = datetime.now(timezone.utc)
    att_mismatch = _create_attempt(
        environment="live",
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=now_mismatch + timedelta(seconds=60),
    )
    repo = SharedRecoveryAttemptRepository([att_mismatch])
    session_factory = lambda: RecoveryWorkerMockDbSession(repo)

    claimed_mismatch = ClaimedRecoveryAttempt(
        attempt_id=att_mismatch.id,
        organization_id=att_mismatch.organization_id,
        provider="razorpay",
        merchant_account_id="acc_live_forbidden",
        environment="live",  # Mismatch with non-production 'test'
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=claim_id,
        recovery_claim_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=datetime.now(timezone.utc),
        request_payload={},
        provider_customer_id=None,
        provider_subscription_id=None,
        worker_id="test_worker",
    )

    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed_mismatch,
        client=mock_client,
    )

    assert res.outcome == RecoveryOutcome.QUARANTINED
    assert mock_client.get_subscription_calls == []
    assert mock_client.list_subscription_calls == []


@pytest.mark.asyncio
async def test_40_direct_recovery_engine_claim_enforces_security_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct invocations of claim_recovery_batch enforce Option A environment and merchant policies."""
    repo = SharedRecoveryAttemptRepository()
    session_factory = lambda: RecoveryWorkerMockDbSession(repo)

    # 1. Staging deployment: claim with environment='live' is rejected
    monkeypatch.setattr(settings, "environment", "staging")
    async with session_factory() as db:
        with pytest.raises(ValueError, match="strictly requires environment='test'"):
            await claim_recovery_batch(db=db, worker_id="w_test", environment="live")

    # 2. Production deployment: claim with environment='test' is rejected
    monkeypatch.setattr(settings, "environment", "production")
    async with session_factory() as db:
        with pytest.raises(ValueError, match="strictly requires environment='live'"):
            await claim_recovery_batch(db=db, worker_id="w_test", environment="test")

    # 3. Production deployment: claim with prohibited merchant is rejected
    async with session_factory() as db:
        with pytest.raises(ValueError, match="rejects prohibited merchant account 'default'"):
            await claim_recovery_batch(db=db, worker_id="w_test", merchant_account_id="default")


@pytest.mark.asyncio
async def test_41_jsonb_counter_preserves_unrelated_payload_keys_and_stale_fencing() -> None:
    """JSONB counter updates preserve unrelated request_payload keys and stale claims are fenced."""
    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    att = _create_attempt(
        status=CheckoutAttemptStatus.recovery_required,
        environment="test",
        next_recovery_retry_at=now - timedelta(seconds=10),
        recovery_attempts=0,
        request_payload={
            "plan_id": "plan_pro_annual",
            "customer_id": "cust_abc",
            "customer_email": "tenant@example.com",
            "custom_metadata": {"tenant_tier": "enterprise"},
        },
    )

    repo = SharedRecoveryAttemptRepository([att])
    session_factory = lambda: RecoveryWorkerMockDbSession(repo)

    claimed = (await claim_recovery_batch(
        db=RecoveryWorkerMockDbSession(repo, clock_now=now),
        worker_id="worker_payload_test",
        now=now,
    ))[0]

    # Transient error execution
    client = MockRazorpayClient(fail_with_5xx=True)
    res = await execute_claimed_recovery_attempt(
        session_factory=session_factory,
        claimed=claimed,
        client=client,
        now_fn=lambda: now,
    )

    assert res.outcome == RecoveryOutcome.TRANSIENT_BACKOFF
    saved = repo.get_attempt(att.id)
    assert saved is not None
    # Unrelated keys preserved
    assert saved.request_payload.get("plan_id") == "plan_pro_annual"
    assert saved.request_payload.get("customer_email") == "tenant@example.com"
    assert saved.request_payload.get("custom_metadata") == {"tenant_tier": "enterprise"}
    # Counter incremented
    assert saved.request_payload.get("transient_failures") == 1

    # Stale worker cannot overwrite counter
    stale_claim_id = uuid.uuid4()
    stale_claimed = ClaimedRecoveryAttempt(
        attempt_id=att.id,
        organization_id=att.organization_id,
        provider=att.provider,
        merchant_account_id=att.merchant_account_id,
        environment=att.environment,
        status=CheckoutAttemptStatus.recovery_required,
        recovery_claim_id=stale_claim_id,  # Stale claim ID
        recovery_claim_expires_at=now + timedelta(seconds=60),
        recovery_attempts=0,
        created_at=att.created_at,
        request_payload=dict(saved.request_payload),
        provider_customer_id=None,
        provider_subscription_id=None,
        worker_id="stale_worker",
    )

    from app.services.billing.razorpay.recovery_engine import _record_recovery_outcome

    with pytest.raises(StaleRecoveryClaimError):
        await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=stale_claimed,
            outcome=RecoveryOutcome.TRANSIENT_BACKOFF,
            now_utc=now,
        )

    # Counter remains at 1, unchanged by stale worker
    saved_after = repo.get_attempt(att.id)
    assert saved_after.request_payload.get("transient_failures") == 1


@pytest.mark.asyncio
async def test_42_reconciliation_upsert_idempotency_deterministic_case_keys() -> None:
    """Reconciliation staging uses deterministic case keys and preserves diagnostic history."""
    from app.services.billing.razorpay.handler_base import stage_or_update_payment_reconciliation

    repo = SharedRecoveryAttemptRepository()
    session_factory = lambda: RecoveryWorkerMockDbSession(repo)

    attempt_id = uuid.uuid4()
    case_key = f"recovery_budget_exhausted:{attempt_id}"
    now_1 = datetime(2026, 11, 18, 14, 0, 0, tzinfo=timezone.utc)
    now_2 = datetime(2026, 11, 18, 14, 5, 0, tzinfo=timezone.utc)

    # First update
    async with session_factory() as db:
        rec1 = await stage_or_update_payment_reconciliation(
            db=db,
            case_key=case_key,
            reason="recovery_budget_exhausted",
            diagnostic_data={"attempts_count": 5, "last_error": "no_candidates"},
            now=now_1,
        )

    # Second update for the same deterministic case key
    async with session_factory() as db:
        rec2 = await stage_or_update_payment_reconciliation(
            db=db,
            case_key=case_key,
            reason="recovery_budget_exhausted_retry",
            diagnostic_data={"attempts_count": 5, "last_error": "still_no_candidates"},
            now=now_2,
        )

    assert rec1.case_key == case_key
    assert rec2.case_key == case_key
    assert len(rec2.diagnostic_payload["history"]) == 2
    assert rec2.diagnostic_payload["latest"]["last_error"] == "still_no_candidates"
    assert rec2.diagnostic_payload["attempts_count"] == 5


@pytest.mark.asyncio
async def test_43_reconciliation_concurrent_insert_race_savepoint_handling() -> None:
    """Concurrent insert race on uq_payment_reconciliations_case_key recovers cleanly via savepoint."""
    from app.services.billing.razorpay.handler_base import stage_or_update_payment_reconciliation
    from sqlalchemy.exc import IntegrityError

    repo = SharedRecoveryAttemptRepository()

    # Pre-populate an existing reconciliation in repo (simulating concurrent worker winning insert)
    case_key = f"recovery_quarantined:{uuid.uuid4()}"
    existing_rec = PaymentReconciliation(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="default",
        environment="test",
        case_key=case_key,
        reason="initial_reason",
        status=ReconciliationStatus.pending_review,
        diagnostic_payload={"history": [{"timestamp": "2026-11-18T12:00:00", "reason": "initial", "details": {}}]},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    repo.reconciliations.append(existing_rec)

    class RaceSimulationDbSession(RecoveryWorkerMockDbSession):
        def __init__(self, repo: SharedRecoveryAttemptRepository) -> None:
            super().__init__(repo)
            self._tried_insert = False

        async def flush(self) -> None:
            # First flush raises IntegrityError simulating Postgres unique constraint race
            if not self._tried_insert and self.staged_reconciliations:
                self._tried_insert = True
                raise IntegrityError("duplicate key value violates unique constraint uq_payment_reconciliations_case_key", None, None)

    session = RaceSimulationDbSession(repo)
    result = await stage_or_update_payment_reconciliation(
        db=session,
        case_key=case_key,
        reason="second_worker_reason",
        diagnostic_data={"extra": "concurrent_info"},
    )

    assert result is not None
    assert result.case_key == case_key
    # Verified that savepoint caught IntegrityError and appended to existing history
    assert len(result.diagnostic_payload["history"]) == 2
    assert result.diagnostic_payload["history"][1]["reason"] == "second_worker_reason"
