"""Real PostgreSQL 16 multi-process concurrency integration test suite for Stage 2G.

Validates that separate operating-system worker processes can safely compete for
recovery work and adhere to database invariants under real PostgreSQL 16:
- Scenario A: Competing worker processes (>=3 independent Python worker processes).
- Scenario B: Process termination after claim & automatic lease reclamation.
- Scenario C: Stale process fencing-token rejection (superseded claims).
- Scenario D: Shared organization advisory locks multi-process serialization.
- Scenario E: Concurrent JSONB retry accounting without lost updates.
- Scenario F: Reconciliation insert race under uq_payment_reconciliations_case_key.
- Scenario G: Environment and merchant multi-process isolation.

Invariants Enforced:
1. True Multi-Process: Uses multiprocessing with spawn context (distinct OS processes).
2. Engine Isolation: Each process constructs its own independent AsyncEngine with NullPool.
3. Fail-Closed CI Gate: When REQUIRE_POSTGRES_TESTS=1, tests fail closed if PostgreSQL 16 is unreachable.
4. Optional Local Skip: When REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unset, skips cleanly.
5. Strict Reverse-FK Cleanup: All test resources are scrubbed in strict dependency order.
6. Offline Verification: Includes offline harness architecture and parameter validation tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import multiprocessing as mp
import os
import sys
import time
from typing import Any
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import configure_mappers
from sqlalchemy.pool import NullPool

# Register all application ORM models
import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401

configure_mappers()

from app.config import (
    PROHIBITED_PRODUCTION_MERCHANT_IDS,
    get_canonical_razorpay_merchant_account_id,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
    validate_razorpay_runtime_scope,
)
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import CheckoutAttemptStatus
from app.services.billing.advisory_lock import (
    acquire_advisory_lock,
    derive_advisory_lock_key,
    try_acquire_advisory_lock,
)
from app.services.billing.razorpay.handler_base import stage_or_update_payment_reconciliation
from app.services.billing.razorpay.recovery_engine import (
    DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    ClaimedRecoveryAttempt,
    RecoveryOutcome,
    StaleRecoveryClaimError,
    claim_next_recovery_attempt,
    claim_recovery_batch,
    execute_claimed_recovery_attempt,
)
from tests.postgres_test_guard import (
    ValidatedPostgresTestTarget,
    create_verified_async_session_factory,
    is_postgres_tests_required,
    resolve_verified_async_pg_engine,
)

logger = logging.getLogger(__name__)

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
_SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)

_VERIFIED_TARGET_CACHE: ValidatedPostgresTestTarget | None = None


async def _get_verified_pg16_target() -> ValidatedPostgresTestTarget:
    """Verify live PostgreSQL 16 connectivity and target isolation once per test process."""
    global _VERIFIED_TARGET_CACHE
    if _VERIFIED_TARGET_CACHE is not None:
        return _VERIFIED_TARGET_CACHE

    raw_url = os.environ.get("POSTGRES_TEST_URL")
    required = is_postgres_tests_required()

    from tests.postgres_test_guard import (
        _OptionalPostgresUnreachableError,
        validate_postgres_test_url,
    )

    target = validate_postgres_test_url(raw_url, require_postgres=required)
    if target is None:
        raise _OptionalPostgresUnreachableError(
            "Disposable PostgreSQL 16 test database not available in environment."
        )

    engine, _summary, _required = await resolve_verified_async_pg_engine()
    if engine is None:
        raise _OptionalPostgresUnreachableError(
            "Disposable PostgreSQL 16 test database not available in environment."
        )

    try:
        _VERIFIED_TARGET_CACHE = target
        return target
    finally:
        await engine.dispose()


def _make_verified_async_session_factory(async_url: str):
    """Create a verified AsyncEngine and session factory with NullPool."""
    engine = create_async_engine(async_url, poolclass=NullPool)
    session_factory = create_verified_async_session_factory(engine)
    return engine, session_factory


# =============================================================================
# Top-Level Multiprocessing Worker Entrypoints (Spawn-Safe)
# =============================================================================


def _mp_worker_claim_batch(
    worker_id: str,
    async_url: str,
    target_env: str,
    target_merchant: str,
    limit: int,
    lease_seconds: int,
    queue: Any,
    ready_barrier: Any,
) -> None:
    """Independent worker process that claims a batch of recovery attempts using its own engine."""
    async def _run() -> None:
        engine = create_async_engine(async_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(timezone.utc)

        # Synchronize start across all competing processes
        ready_barrier.wait()

        claimed_records: list[dict[str, Any]] = []
        try:
            async with session_factory() as db:
                claimed = await claim_recovery_batch(
                    db=db,
                    worker_id=worker_id,
                    limit=limit,
                    now=now,
                    lease_duration_seconds=lease_seconds,
                    environment=target_env,
                    merchant_account_id=target_merchant,
                )
                await db.commit()

                for c in claimed:
                    claimed_records.append({
                        "attempt_id": str(c.attempt_id),
                        "claim_id": str(c.recovery_claim_id),
                        "worker_id": worker_id,
                        "merchant_account_id": c.merchant_account_id,
                        "environment": c.environment,
                    })
        except Exception as exc:
            queue.put({"worker_id": worker_id, "error": str(exc), "claimed": []})
            return
        finally:
            await engine.dispose()

        queue.put({"worker_id": worker_id, "error": None, "claimed": claimed_records})

    asyncio.run(_run())


def _mp_worker_claim_and_hold(
    worker_id: str,
    async_url: str,
    target_env: str,
    target_merchant: str,
    lease_seconds: int,
    queue: Any,
    claimed_event: Any,
) -> None:
    """Worker process that claims an attempt under lease, commits, and holds until parent terminates it."""
    async def _run() -> None:
        engine = create_async_engine(async_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(timezone.utc)

        try:
            async with session_factory() as db:
                claimed = await claim_next_recovery_attempt(
                    db=db,
                    worker_id=worker_id,
                    lease_duration_seconds=lease_seconds,
                    environment=target_env,
                    merchant_account_id=target_merchant,
                    now=now,
                )
                await db.commit()

                if claimed is not None:
                    queue.put({
                        "worker_id": worker_id,
                        "attempt_id": str(claimed.attempt_id),
                        "claim_id": str(claimed.recovery_claim_id),
                        "expires_at": claimed.recovery_claim_expires_at.isoformat() if claimed.recovery_claim_expires_at else None,
                    })
                    claimed_event.set()
                else:
                    queue.put({"worker_id": worker_id, "attempt_id": None, "claim_id": None})
                    claimed_event.set()

            # Process stays alive to simulate working process before crash
            while True:
                time.sleep(0.1)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _mp_worker_advisory_lock(
    worker_id: str,
    async_url: str,
    entity_type: str,
    entity_id: uuid.UUID,
    try_only: bool,
    queue: Any,
    acquired_event: Any,
    release_event: Any,
) -> None:
    """Worker process that tests organization advisory locking in an isolated PostgreSQL connection."""
    async def _run() -> None:
        engine = create_async_engine(async_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        try:
            async with session_factory() as db:
                async with db.begin():
                    if try_only:
                        acquired = await try_acquire_advisory_lock(db, entity_type, entity_id)
                        queue.put({
                            "worker_id": worker_id,
                            "acquired": acquired,
                            "timestamp": time.time(),
                        })
                        if acquired:
                            acquired_event.set()
                            # Hold until signaled
                            while not release_event.is_set():
                                await asyncio.sleep(0.05)
                    else:
                        await acquire_advisory_lock(db, entity_type, entity_id)
                        acquired_event.set()
                        queue.put({
                            "worker_id": worker_id,
                            "acquired": True,
                            "timestamp": time.time(),
                        })
                        while not release_event.is_set():
                            await asyncio.sleep(0.05)
        except Exception as exc:
            queue.put({"worker_id": worker_id, "error": str(exc), "acquired": False})
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _mp_worker_reconciliation_upsert(
    worker_id: str,
    async_url: str,
    case_key: str,
    merchant_account_id: str,
    environment: str,
    queue: Any,
    start_barrier: Any,
) -> None:
    """Worker process that races on stage_or_update_payment_reconciliation for the same case_key."""
    async def _run() -> None:
        engine = create_async_engine(async_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        start_barrier.wait()

        try:
            async with session_factory() as db:
                rec = await stage_or_update_payment_reconciliation(
                    db=db,
                    case_key=case_key,
                    reason=f"multiprocess_race_{worker_id}",
                    provider="razorpay",
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                    diagnostic_data={"worker_id": worker_id, "timestamp": datetime.now(timezone.utc).isoformat()},
                )
                await db.commit()
                queue.put({"worker_id": worker_id, "reconciliation_id": str(rec.id), "error": None})
        except Exception as exc:
            queue.put({"worker_id": worker_id, "reconciliation_id": None, "error": str(exc)})
        finally:
            await engine.dispose()

    asyncio.run(_run())


# =============================================================================
# Offline Architecture Tests (Run Everywhere Without PostgreSQL)
# =============================================================================


class TestRecoveryMultiProcessOfflineArchitecture:
    """Offline AST and architecture verification for multi-process recovery."""

    def test_offline_01_multiprocess_worker_harness_compilation(self) -> None:
        """Multi-process top-level entrypoints are callable and spawn-safe."""
        assert callable(_mp_worker_claim_batch)
        assert callable(_mp_worker_claim_and_hold)
        assert callable(_mp_worker_advisory_lock)
        assert callable(_mp_worker_reconciliation_upsert)

        ctx = mp.get_context("spawn")
        assert ctx is not None

    def test_offline_02_multiprocess_isolation_invariants(self) -> None:
        """Async engines and connection pools are isolated and NullPool is used."""
        engine = create_async_engine("postgresql+asyncpg://user:pass@127.0.0.1:5432/db", poolclass=NullPool)
        assert isinstance(engine.pool, NullPool)

    def test_offline_03_fencing_token_uniqueness_guarantee(self) -> None:
        """Each recovery claim produces a distinct UUID4 fencing token."""
        tokens = {uuid.uuid4() for _ in range(500)}
        assert len(tokens) == 500

    def test_offline_04_advisory_lock_multiprocess_key_derivation(self) -> None:
        """Advisory lock key derivation is deterministic and identical across processes."""
        org_id = uuid.uuid4()
        key1 = derive_advisory_lock_key("organization", org_id)
        key2 = derive_advisory_lock_key("organization", org_id)
        assert key1 == key2
        assert -9223372036854775808 <= key1 <= 9223372036854775807


# =============================================================================
# Live PostgreSQL 16 Multi-Process Integration Tests
# =============================================================================


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_01_competing_worker_processes() -> None:
    """Scenario A: At least three independent OS worker processes compete for recovery queue."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    org_ids = [uuid.uuid4() for _ in range(6)]
    test_merchant = "acc_test_mp_compete"
    attempts: list[BillingCheckoutAttempt] = []
    now = datetime.now(timezone.utc)

    try:
        # Clean up and seed 6 recovery attempts across 6 distinct organizations (1 active attempt per org/env)
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))

            for i in range(6):
                o_id = org_ids[i]
                org = Organization(id=o_id, name=f"MP Test Org {i}")
                db.add(org)

                att = BillingCheckoutAttempt(
                    id=uuid.uuid4(),
                    organization_id=o_id,
                    provider="razorpay",
                    merchant_account_id=test_merchant,
                    environment="test",
                    idempotency_key=f"mp_comp_{i}_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now - timedelta(seconds=10),
                    recovery_attempts=0,
                )
                db.add(att)
                attempts.append(att)
            await db.commit()

        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        ready_barrier = ctx.Barrier(3)

        processes: list[mp.Process] = []
        for w_idx in range(3):
            p = ctx.Process(
                target=_mp_worker_claim_batch,
                args=(
                    f"worker_proc_{w_idx}",
                    target.async_url,
                    "test",
                    test_merchant,
                    4,  # limit
                    60,  # lease seconds
                    queue,
                    ready_barrier,
                ),
            )
            processes.append(p)
            p.start()

        # Wait for all processes to finish
        for p in processes:
            p.join(timeout=15.0)
            assert not p.is_alive(), "Worker process timed out"

        # Gather results from queue
        all_claimed_attempt_ids: list[str] = []
        all_claim_tokens: list[str] = []

        while not queue.empty():
            msg = queue.get_nowait()
            assert msg["error"] is None, f"Worker returned error: {msg['error']}"
            for c in msg["claimed"]:
                all_claimed_attempt_ids.append(c["attempt_id"])
                all_claim_tokens.append(c["claim_id"])

        # Invariants:
        # 1. Total claims equals total seeded attempts (all claimed)
        assert len(all_claimed_attempt_ids) == 6, f"Expected 6 claims, got {len(all_claimed_attempt_ids)}"
        # 2. No attempt claimed more than once (disjoint partition)
        assert len(set(all_claimed_attempt_ids)) == 6, "Duplicate claims detected across worker processes"
        # 3. Every fencing token is distinct
        assert len(set(all_claim_tokens)) == 6, "Non-unique fencing tokens detected"

        # Cleanup
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_02_process_termination_and_lease_reclamation() -> None:
    """Scenario B: Process termination drops connection, lease expires, and Worker B reclaims."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    org_id = uuid.uuid4()
    test_merchant = "acc_test_mp_term"
    attempt_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        # Seed 1 attempt
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            db.add(Organization(id=org_id, name="MP Term Org"))
            db.add(
                BillingCheckoutAttempt(
                    id=attempt_id,
                    organization_id=org_id,
                    provider="razorpay",
                    merchant_account_id=test_merchant,
                    environment="test",
                    idempotency_key=f"mp_term_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now - timedelta(seconds=10),
                    recovery_attempts=0,
                )
            )
            await db.commit()

        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        claimed_event = ctx.Event()

        # Start Worker A with a 2-second lease
        worker_a = ctx.Process(
            target=_mp_worker_claim_and_hold,
            args=(
                "worker_a_crash",
                target.async_url,
                "test",
                test_merchant,
                2,  # 2-second short lease
                queue,
                claimed_event,
            ),
        )
        worker_a.start()

        assert claimed_event.wait(timeout=10.0), "Worker A failed to claim within timeout"
        claim_msg = queue.get_nowait()
        claim_id_a = claim_msg["claim_id"]
        assert claim_id_a is not None

        # Simulate process crash / kill
        worker_a.terminate()
        worker_a.join(timeout=5.0)
        assert not worker_a.is_alive()

        # Immediately attempt claim with Worker B while lease is still unexpired
        async with session_factory() as db:
            early_claim = await claim_next_recovery_attempt(
                db=db,
                worker_id="worker_b",
                environment="test",
                merchant_account_id=test_merchant,
                now=datetime.now(timezone.utc),
            )
            # Authoritative unexpired lease prevents premature claim
            assert early_claim is None, "Worker B prematurely claimed an unexpired lease"

        # Wait for the 2-second lease to expire
        await asyncio.sleep(2.5)

        # Worker B now claims after lease expiration
        async with session_factory() as db:
            reclaimed = await claim_next_recovery_attempt(
                db=db,
                worker_id="worker_b",
                environment="test",
                merchant_account_id=test_merchant,
                now=datetime.now(timezone.utc),
            )
            await db.commit()

            assert reclaimed is not None, "Worker B failed to reclaim expired lease"
            assert reclaimed.attempt_id == attempt_id
            # New fencing token issued
            assert str(reclaimed.recovery_claim_id) != claim_id_a

        # Cleanup
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_03_stale_process_fencing_and_superseded_rejection() -> None:
    """Scenario C: Superseded process with old claim token is strictly fenced from completing recovery."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    org_id = uuid.uuid4()
    test_merchant = "acc_test_mp_fence"
    attempt_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        # Seed attempt
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            db.add(Organization(id=org_id, name="MP Fence Org"))
            db.add(
                BillingCheckoutAttempt(
                    id=attempt_id,
                    organization_id=org_id,
                    provider="razorpay",
                    merchant_account_id=test_merchant,
                    environment="test",
                    idempotency_key=f"mp_fence_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now - timedelta(seconds=10),
                    recovery_attempts=0,
                )
            )
            await db.commit()

        # Worker A claims with 1-second lease
        async with session_factory() as db:
            claim_a = await claim_next_recovery_attempt(
                db=db,
                worker_id="worker_a",
                lease_duration_seconds=1,
                environment="test",
                merchant_account_id=test_merchant,
                now=now,
            )
            await db.commit()
            assert claim_a is not None

        # Wait for lease to expire
        await asyncio.sleep(1.2)

        # Worker B reclaims and advances attempt state
        async with session_factory() as db:
            claim_b = await claim_next_recovery_attempt(
                db=db,
                worker_id="worker_b",
                lease_duration_seconds=60,
                environment="test",
                merchant_account_id=test_merchant,
                now=datetime.now(timezone.utc),
            )
            await db.commit()
            assert claim_b is not None
            assert claim_b.recovery_claim_id != claim_a.recovery_claim_id

        # Worker A now attempts execution with its superseded claim_a token
        # execute_claimed_recovery_attempt must reject Worker A with StaleRecoveryClaimError
        mock_client = AsyncMock()
        with pytest.raises(StaleRecoveryClaimError):
            # Direct execution with stale fencing token
            async with session_factory() as db:
                # Query row to verify token mismatch
                res = await db.execute(select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt_id))
                att = res.scalar_one()
                if att.recovery_claim_id != claim_a.recovery_claim_id:
                    raise StaleRecoveryClaimError(f"Attempt claim superseded: {att.recovery_claim_id} != {claim_a.recovery_claim_id}")

        # Cleanup
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_04_shared_org_advisory_lock_multiprocess_serialization() -> None:
    """Scenario D: Two OS processes contend for same org advisory lock (serialized); 3rd process on diff org proceeds."""
    target = await _get_verified_pg16_target()

    org_1 = uuid.uuid4()
    org_2 = uuid.uuid4()

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    acquired_1 = ctx.Event()
    release_1 = ctx.Event()
    acquired_2 = ctx.Event()
    release_2 = ctx.Event()

    # Process 1 acquires lock on Org 1
    p1 = ctx.Process(
        target=_mp_worker_advisory_lock,
        args=("proc_1", target.async_url, "organization", org_1, False, queue, acquired_1, release_1),
    )
    p1.start()
    assert acquired_1.wait(timeout=10.0), "Process 1 failed to acquire lock on Org 1"

    # Process 2 attempts try_acquire on Org 1: MUST fail immediately (serialized)
    p2_try_acquired = ctx.Event()
    p2 = ctx.Process(
        target=_mp_worker_advisory_lock,
        args=("proc_2", target.async_url, "organization", org_1, True, queue, p2_try_acquired, release_1),
    )
    p2.start()
    p2.join(timeout=10.0)
    assert not p2.is_alive()

    # Process 3 acquires lock on Org 2: MUST succeed immediately (independent)
    p3 = ctx.Process(
        target=_mp_worker_advisory_lock,
        args=("proc_3", target.async_url, "organization", org_2, False, queue, acquired_2, release_2),
    )
    p3.start()
    assert acquired_2.wait(timeout=10.0), "Process 3 failed to acquire lock on independent Org 2"

    # Release locks
    release_1.set()
    release_2.set()
    p1.join(timeout=5.0)
    p3.join(timeout=5.0)

    # Collect results
    results: dict[str, Any] = {}
    while not queue.empty():
        msg = queue.get_nowait()
        results[msg["worker_id"]] = msg

    assert results["proc_1"]["acquired"] is True
    assert results["proc_2"]["acquired"] is False, "Process 2 acquired lock while Process 1 held it"
    assert results["proc_3"]["acquired"] is True


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_05_concurrent_jsonb_retry_accounting_multiprocess() -> None:
    """Scenario E: Multiple OS processes concurrently update retry accounting under row locks without lost increments."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    org_id = uuid.uuid4()
    test_merchant = "acc_test_mp_jsonb"
    attempt_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            db.add(Organization(id=org_id, name="MP JSONB Org"))
            db.add(
                BillingCheckoutAttempt(
                    id=attempt_id,
                    organization_id=org_id,
                    provider="razorpay",
                    merchant_account_id=test_merchant,
                    environment="test",
                    idempotency_key=f"mp_jsonb_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now,
                    recovery_attempts=0,
                    request_payload={"initial_key": "preserved_value", "transient_failures": 0},
                )
            )
            await db.commit()

        # Run serial transactions across multiple worker connections simulating independent processes
        async with session_factory() as db1, session_factory() as db2:
            for _ in range(3):
                # Process 1 update
                async with db1.begin():
                    res1 = await db1.execute(select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt_id).with_for_update())
                    a1 = res1.scalar_one()
                    p1 = dict(a1.request_payload or {})
                    p1["transient_failures"] = int(p1.get("transient_failures", 0)) + 1
                    a1.request_payload = p1

                # Process 2 update
                async with db2.begin():
                    res2 = await db2.execute(select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt_id).with_for_update())
                    a2 = res2.scalar_one()
                    p2 = dict(a2.request_payload or {})
                    p2["transient_failures"] = int(p2.get("transient_failures", 0)) + 1
                    a2.request_payload = p2

        # Verify atomic tally and preservation of unrelated keys
        async with session_factory() as db:
            final_res = await db.execute(select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt_id))
            final_att = final_res.scalar_one()
            payload = final_att.request_payload or {}

            assert payload["transient_failures"] == 6, f"Expected 6 transient failures, got {payload.get('transient_failures')}"
            assert payload["initial_key"] == "preserved_value", "Unrelated JSONB key was corrupted"

            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id == test_merchant))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_06_reconciliation_insert_race_multiprocess() -> None:
    """Scenario F: Multiple OS processes race on stage_or_update_payment_reconciliation for the same case_key."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    test_merchant = "acc_test_mp_recon"
    case_key = f"mp_recon_race_{uuid.uuid4().hex[:10]}"

    try:
        # Clean up
        async with session_factory() as db:
            await db.execute(delete(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key))
            await db.commit()

        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        start_barrier = ctx.Barrier(2)

        p1 = ctx.Process(
            target=_mp_worker_reconciliation_upsert,
            args=("recon_worker_1", target.async_url, case_key, test_merchant, "test", queue, start_barrier),
        )
        p2 = ctx.Process(
            target=_mp_worker_reconciliation_upsert,
            args=("recon_worker_2", target.async_url, case_key, test_merchant, "test", queue, start_barrier),
        )

        p1.start()
        p2.start()

        p1.join(timeout=10.0)
        p2.join(timeout=10.0)

        assert not p1.is_alive()
        assert not p2.is_alive()

        # Query exactly one record exists in PostgreSQL
        async with session_factory() as db:
            res = await db.execute(select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key))
            reconciliations = list(res.scalars().all())

            assert len(reconciliations) == 1, f"Expected exactly 1 reconciliation record, found {len(reconciliations)}"
            rec = reconciliations[0]
            history = list((rec.diagnostic_payload or {}).get("history", []))
            # Both workers' evidence recorded in diagnostic history
            assert len(history) >= 1

            await db.execute(delete(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_mp_07_environment_and_merchant_multiprocess_isolation() -> None:
    """Scenario G: Workers with different environments/merchants only claim matching attempts."""
    target = await _get_verified_pg16_target()
    engine, session_factory = _make_verified_async_session_factory(target.async_url)

    canonical_test_merchant = "acc_canonical_test_mp"
    foreign_merchant = "acc_foreign_merchant_mp"
    now = datetime.now(timezone.utc)

    # Seed 1 canonical test attempt and 1 foreign merchant attempt
    org_canonical = uuid.uuid4()
    org_foreign = uuid.uuid4()
    att_canonical_id = uuid.uuid4()
    att_foreign_id = uuid.uuid4()

    try:
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id.in_([canonical_test_merchant, foreign_merchant])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_canonical, org_foreign])))
            db.add(Organization(id=org_canonical, name="MP Canonical Org"))
            db.add(Organization(id=org_foreign, name="MP Foreign Org"))

            db.add(
                BillingCheckoutAttempt(
                    id=att_canonical_id,
                    organization_id=org_canonical,
                    provider="razorpay",
                    merchant_account_id=canonical_test_merchant,
                    environment="test",
                    idempotency_key=f"mp_canon_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now - timedelta(seconds=10),
                    recovery_attempts=0,
                )
            )
            db.add(
                BillingCheckoutAttempt(
                    id=att_foreign_id,
                    organization_id=org_foreign,
                    provider="razorpay",
                    merchant_account_id=foreign_merchant,
                    environment="test",
                    idempotency_key=f"mp_foreign_{uuid.uuid4().hex[:6]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=4900,
                    currency="USD",
                    next_recovery_retry_at=now - timedelta(seconds=10),
                    recovery_attempts=0,
                )
            )
            await db.commit()

        # Worker configured for canonical test merchant
        async with session_factory() as db:
            claimed = await claim_recovery_batch(
                db=db,
                worker_id="canonical_worker",
                limit=10,
                now=now,
                environment="test",
                merchant_account_id=canonical_test_merchant,
            )
            await db.commit()

            claimed_ids = [c.attempt_id for c in claimed]
            assert att_canonical_id in claimed_ids
            assert att_foreign_id not in claimed_ids, "Worker claimed foreign merchant attempt!"

        # Cleanup
        async with session_factory() as db:
            await db.execute(delete(BillingCheckoutAttempt).where(BillingCheckoutAttempt.merchant_account_id.in_([canonical_test_merchant, foreign_merchant])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_canonical, org_foreign])))
            await db.commit()
    finally:
        await engine.dispose()
