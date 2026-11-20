"""Live PostgreSQL 16 concurrency and transaction integration test suite for Stage 2E.

Validates billing checkout recovery concurrency and database guarantees across real PostgreSQL 16:
- PG16-REC-01: Schema baseline verification (columns, partial indexes, unique constraints).
- PG16-REC-02: Competing worker claim concurrency under SELECT ... FOR UPDATE SKIP LOCKED.
- PG16-REC-03: FOR UPDATE SKIP LOCKED non-blocking queue partition verification.
- PG16-REC-04: Lease expiration and bounded reclamation with fresh claim UUIDs.
- PG16-REC-05: Stale worker fencing-token rejection (StaleRecoveryClaimError) and reconciliation.
- PG16-REC-06: Atomic JSONB retry-counter updates (transient_failures & expired_lease_count).
- PG16-REC-07: Reconciliation upsert idempotency under uq_payment_reconciliations_case_key races.
- PG16-REC-08: Organization-level canonical advisory locks serialization and domain separation.
- PG16-REC-09: Production versus test environment isolation under ADR-006 Option A.
- PG16-REC-10: Failure injection: abandoned worker lease and automatic reclamation.
- PG16-REC-11: Failure injection: transaction rollback cleanly reverts outcome state.
- PG16-REC-12: Failure injection: connection drop releases PostgreSQL row and advisory locks.

Guarantees & Invariants:
1. Real PostgreSQL 16: Never uses SQLite or Docker mocks for integration tests.
2. Connection Isolation: Uses SQLAlchemy NullPool with distinct database connections for concurrency.
3. Fail-Closed CI Gate: When REQUIRE_POSTGRES_TESTS=1, tests fail closed if PostgreSQL 16 is unreachable.
4. Optional Local Skip: When REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unset, skips cleanly.
5. Strict Reverse-FK Cleanup: All test resources are scrubbed in strict dependency order in finally blocks.
6. Offline Verification: Includes offline query AST and logic verification tests that run everywhere.
"""

from __future__ import annotations

import os
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import SecretStr
import pytest
from sqlalchemy import delete, func, inspect as sa_inspect, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import configure_mappers

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

from app.config import settings
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingPayment,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import CheckoutAttemptStatus, Subscription, SubscriptionStatus
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayClient,
    get_razorpay_client,
)
from app.services.billing.razorpay.handler_base import (
    build_payment_reconciliation_upsert_stmt,
    stage_or_update_payment_reconciliation,
    upsert_payment_reconciliation,
)
from app.services.billing.advisory_lock import (
    acquire_advisory_lock,
    derive_advisory_lock_key,
    try_acquire_advisory_lock,
)
from app.services.billing.razorpay.lock import (
    PG_BIGINT_MAX,
    PG_BIGINT_MIN,
    acquire_razorpay_advisory_lock,
    derive_razorpay_advisory_lock_key,
    try_acquire_razorpay_advisory_lock as try_acquire_razorpay_advisory_lock,
)
from app.services.billing.razorpay.recovery_engine import (
    DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    MAX_EXPIRED_LEASE_RECOVERIES,
    MAX_RECOVERY_ATTEMPTS,
    MAX_TRANSIENT_RECOVERIES,
    ClaimedRecoveryAttempt,
    RecoveryOutcome,
    StaleRecoveryClaimError,
    _record_recovery_outcome,
    claim_next_recovery_attempt,
    claim_recovery_batch,
    compute_next_recovery_retry,
    execute_claimed_recovery_attempt,
)
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    PostgresTestConnectionError,
    ValidatedPostgresTestTarget,
    _OptionalPostgresUnreachableError,
    _default_async_pg_engine_factory,
    create_verified_async_session_factory,
    is_postgres_tests_required,
    resolve_verified_async_pg_engine,
    validate_postgres_test_url,
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
    """Verify live PostgreSQL 16 connectivity and target isolation once per process."""
    global _VERIFIED_TARGET_CACHE
    if _VERIFIED_TARGET_CACHE is not None:
        return _VERIFIED_TARGET_CACHE

    raw_url = os.environ.get("POSTGRES_TEST_URL")
    required = is_postgres_tests_required()
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


@dataclass
class Pg16CheckoutRecoveryHarness:
    """Per-test live PostgreSQL 16 harness with tracked reverse-FK cleanup."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    target: ValidatedPostgresTestTarget

    org_ids: list[uuid.UUID] = field(default_factory=list)
    case_keys: list[str] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)
    sub_ids: list[uuid.UUID] = field(default_factory=list)
    triggers: list[tuple[str, str, str]] = field(default_factory=list)

    def register_org(self, org_id: uuid.UUID) -> uuid.UUID:
        if org_id not in self.org_ids:
            self.org_ids.append(org_id)
        return org_id

    def register_case_key(self, case_key: str) -> str:
        if case_key not in self.case_keys:
            self.case_keys.append(case_key)
        return case_key

    def register_attempt(self, attempt_id: uuid.UUID) -> uuid.UUID:
        if attempt_id not in self.attempt_ids:
            self.attempt_ids.append(attempt_id)
        return attempt_id

    def register_sub(self, sub_id: uuid.UUID) -> uuid.UUID:
        if sub_id not in self.sub_ids:
            self.sub_ids.append(sub_id)
        return sub_id

    def register_trigger(self, trigger_name: str, table_name: str, function_name: str) -> None:
        entry = (trigger_name, table_name, function_name)
        if entry not in self.triggers:
            self.triggers.append(entry)

    async def create_org(self, name: str | None = None) -> Organization:
        org = Organization(
            id=uuid.uuid4(),
            name=name or f"PG16 Recovery Org {uuid.uuid4().hex[:8]}",
        )
        self.register_org(org.id)
        async with self.session_factory() as db:
            db.add(org)
            await db.commit()
        return org

    async def create_subscription(
        self,
        org_id: uuid.UUID,
        provider_sub_id: str | None = None,
        status: SubscriptionStatus = SubscriptionStatus.incomplete,
        environment: str = "test",
    ) -> Subscription:
        sub_id_str = provider_sub_id or f"sub_pg16_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="default",
            environment=environment,
            provider_subscription_id=sub_id_str,
            provider_customer_id=f"cust_pg16_{uuid.uuid4().hex[:8]}",
            status=status,
            plan_tier="pro",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            created_at=now,
            updated_at=now,
        )
        self.register_sub(sub.id)
        async with self.session_factory() as db:
            db.add(sub)
            await db.commit()
        return sub

    async def create_recovery_attempt(
        self,
        org_id: uuid.UUID,
        status: CheckoutAttemptStatus = CheckoutAttemptStatus.recovery_required,
        environment: str = "test",
        merchant_account_id: str = "default",
        next_recovery_retry_at: datetime | None = None,
        recovery_claim_expires_at: datetime | None = None,
        recovery_claim_id: uuid.UUID | None = None,
        recovery_attempts: int = 0,
        request_payload: dict[str, Any] | None = None,
        provider_subscription_id: str | None = None,
    ) -> BillingCheckoutAttempt:
        now = datetime.now(timezone.utc)
        attempt = BillingCheckoutAttempt(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            idempotency_key=f"idem_rec_{uuid.uuid4().hex}",
            status=status,
            amount_minor_units=0,
            currency="INR",
            request_payload=request_payload or {"plan_id": "plan_pg16_pro", "plan_tier": "pro"},
            attempt_lease_expires_at=now + timedelta(seconds=300),
            session_expires_at=now + timedelta(hours=2),
            provider_subscription_id=provider_subscription_id,
            next_recovery_retry_at=next_recovery_retry_at,
            recovery_claim_expires_at=recovery_claim_expires_at,
            recovery_claim_id=recovery_claim_id,
            recovery_attempts=recovery_attempts,
            created_at=now,
            updated_at=now,
        )
        self.register_attempt(attempt.id)
        async with self.session_factory() as db:
            db.add(attempt)
            await db.commit()
        return attempt


@pytest.fixture(autouse=True)
def _configure_razorpay_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure deterministic non-live Razorpay test settings for every test."""
    monkeypatch.setattr(settings, "razorpay_enabled", True)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_pg16_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("rzp_test_pg16_secret"))
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "default")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "plan_pg16_pro")
    monkeypatch.setattr(settings, "razorpay_plan_currency", "USD")


@pytest.fixture
async def pg():
    """Provide a function-scoped verified PostgreSQL 16 engine/session harness with reverse-FK cleanup."""
    try:
        target = await _get_verified_pg16_target()
    except _OptionalPostgresUnreachableError as exc:
        pytest.skip(str(exc))
    except (PostgresTestConfigError, PostgresTestConnectionError) as exc:
        pytest.fail(str(exc), pytrace=False)

    engine = _default_async_pg_engine_factory(target.async_url)
    session_factory = create_verified_async_session_factory(engine)
    harness = Pg16CheckoutRecoveryHarness(
        engine=engine,
        session_factory=session_factory,
        target=target,
    )

    try:
        yield harness
    finally:
        try:
            async with engine.begin() as conn:
                for trigger_name, table_name, function_name in reversed(harness.triggers):
                    await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};"))
                    await conn.execute(text(f"DROP FUNCTION IF EXISTS {function_name}();"))

                if harness.case_keys or harness.org_ids:
                    await conn.execute(
                        text(
                            "DELETE FROM payment_reconciliations "
                            "WHERE case_key = ANY(:keys) OR organization_id = ANY(:orgs)"
                        ),
                        {
                            "keys": harness.case_keys or [""],
                            "orgs": harness.org_ids or [uuid.uuid4()],
                        },
                    )

                if harness.org_ids or harness.attempt_ids:
                    await conn.execute(
                        text(
                            "DELETE FROM billing_payments "
                            "WHERE organization_id = ANY(:orgs) OR checkout_attempt_id = ANY(:attempts)"
                        ),
                        {
                            "orgs": harness.org_ids or [uuid.uuid4()],
                            "attempts": harness.attempt_ids or [uuid.uuid4()],
                        },
                    )
                    await conn.execute(
                        text(
                            "DELETE FROM billing_checkout_attempts "
                            "WHERE organization_id = ANY(:orgs) OR id = ANY(:attempts)"
                        ),
                        {
                            "orgs": harness.org_ids or [uuid.uuid4()],
                            "attempts": harness.attempt_ids or [uuid.uuid4()],
                        },
                    )

                if harness.org_ids:
                    await conn.execute(
                        text("DELETE FROM billing_customer_provisions WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM subscriptions WHERE organization_id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM organizations WHERE id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )

                    remaining_attempts = await conn.execute(
                        text(
                            "SELECT count(*) FROM billing_checkout_attempts "
                            "WHERE organization_id = ANY(:orgs) OR id = ANY(:attempts)"
                        ),
                        {
                            "orgs": harness.org_ids,
                            "attempts": harness.attempt_ids or [uuid.uuid4()],
                        },
                    )
                    assert int(remaining_attempts.scalar_one()) == 0

                    remaining_orgs = await conn.execute(
                        text("SELECT count(*) FROM organizations WHERE id = ANY(:orgs)"),
                        {"orgs": harness.org_ids},
                    )
                    assert int(remaining_orgs.scalar_one()) == 0
        finally:
            await engine.dispose()


# =============================================================================
# Offline Query AST & Architecture Verification Tests (100% Offline)
# =============================================================================


class TestCheckoutRecoveryOfflineArchitecture:
    """Verifies recovery query construction, FOR UPDATE SKIP LOCKED AST, and model invariants offline."""

    def test_offline_01_recovery_query_compilation_and_skip_locked(self) -> None:
        """Verify claim query compiles to PostgreSQL with FOR UPDATE SKIP LOCKED and NULL-safe predicate."""
        from sqlalchemy.dialects import postgresql

        now_utc = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
        stmt = (
            select(BillingCheckoutAttempt)
            .where(
                BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
                BillingCheckoutAttempt.provider == "razorpay",
                BillingCheckoutAttempt.environment == "test",
                BillingCheckoutAttempt.next_recovery_retry_at.is_not(None),
                BillingCheckoutAttempt.next_recovery_retry_at <= now_utc,
                BillingCheckoutAttempt.recovery_attempts < 5,
            )
            .order_by(
                BillingCheckoutAttempt.next_recovery_retry_at.asc(),
                BillingCheckoutAttempt.created_at.asc(),
            )
            .limit(10)
            .with_for_update(skip_locked=True)
        )

        compiled_sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE SKIP LOCKED" in compiled_sql
        assert "billing_checkout_attempts.status =" in compiled_sql
        assert "billing_checkout_attempts.next_recovery_retry_at IS NOT NULL" in compiled_sql
        assert "ORDER BY billing_checkout_attempts.next_recovery_retry_at ASC" in compiled_sql

    def test_offline_02_model_columns_and_constraints_present(self) -> None:
        """Verify ORM model BillingCheckoutAttempt has all Stage 2C recovery attributes."""
        col_names = {c.name for c in BillingCheckoutAttempt.__table__.columns}
        assert "next_recovery_retry_at" in col_names
        assert "recovery_claim_expires_at" in col_names
        assert "recovery_claim_id" in col_names
        assert "recovery_attempts" in col_names

    def test_offline_03_advisory_lock_canonical_key_derivation(self) -> None:
        """Verify advisory lock key derivation produces 64-bit signed integers consistently."""
        test_uuid = uuid.uuid4()
        key_org = derive_advisory_lock_key("organization", test_uuid)
        key_cust = derive_razorpay_advisory_lock_key("customer", "cust_pg16_offline")

        assert PG_BIGINT_MIN <= key_org <= PG_BIGINT_MAX
        assert PG_BIGINT_MIN <= key_cust <= PG_BIGINT_MAX
        assert key_org != key_cust  # Domain separation

    def test_offline_04_backoff_schedule_bounds_and_determinism(self) -> None:
        """Verify exponential backoff schedule bounds and non-jittered determinism."""
        base_time = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)

        t1 = compute_next_recovery_retry(attempt_count=1, now=base_time, apply_jitter=False)
        t2 = compute_next_recovery_retry(attempt_count=2, now=base_time, apply_jitter=False)
        t3 = compute_next_recovery_retry(attempt_count=3, now=base_time, apply_jitter=False)
        t5 = compute_next_recovery_retry(attempt_count=5, now=base_time, apply_jitter=False)

        assert t1 == base_time + timedelta(seconds=60)
        assert t2 == base_time + timedelta(seconds=300)
        assert t3 == base_time + timedelta(seconds=900)
        assert t5 == base_time + timedelta(seconds=3600)
        assert t5 > t3 > t2 > t1


# =============================================================================
# Live PostgreSQL 16 Integration Tests (Guarded by _SKIP_IF_OPTIONAL_POSTGRES)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_01_schema_baseline_and_partial_indexes(pg: Pg16CheckoutRecoveryHarness) -> None:
    """PG16-REC-01: Verify PostgreSQL 16 version, recovery columns, partial indexes, and unique constraints."""
    async with pg.session_factory() as db:
        # 1. Assert PostgreSQL 16.x server version
        ver_res = await db.execute(text("SELECT version();"))
        ver_str = ver_res.scalar_one()
        assert "PostgreSQL 16" in ver_str, f"Expected PostgreSQL 16, got: {ver_str}"

        # 2. Inspect table columns on billing_checkout_attempts
        cols_res = await db.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'billing_checkout_attempts' "
                "AND column_name IN ('next_recovery_retry_at', 'recovery_claim_expires_at', 'recovery_claim_id', 'recovery_attempts')"
            )
        )
        cols_map = {row[0]: row[1] for row in cols_res.all()}
        assert "next_recovery_retry_at" in cols_map
        assert "recovery_claim_expires_at" in cols_map
        assert "recovery_claim_id" in cols_map
        assert "recovery_attempts" in cols_map
        assert "timestamp" in cols_map["next_recovery_retry_at"].lower()
        assert "timestamp" in cols_map["recovery_claim_expires_at"].lower()
        assert cols_map["recovery_claim_id"].lower() in ("uuid", "character varying")
        assert cols_map["recovery_attempts"].lower() in ("integer", "smallint", "bigint")

        # 3. Inspect partial indexes on billing_checkout_attempts
        idx_res = await db.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'billing_checkout_attempts' "
                "AND indexname IN ('ix_bca_recovery_pending', 'ix_bca_stale_creating')"
            )
        )
        idx_defs = {row[0]: row[1] for row in idx_res.all()}
        assert "ix_bca_recovery_pending" in idx_defs
        assert "ix_bca_stale_creating" in idx_defs
        assert "WHERE" in idx_defs["ix_bca_recovery_pending"].upper()

        # 4. Inspect unique constraint on payment_reconciliations
        cons_res = await db.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name = 'payment_reconciliations' "
                "AND constraint_name = 'uq_payment_reconciliations_case_key'"
            )
        )
        cons = cons_res.scalar_one_or_none()
        assert cons == "uq_payment_reconciliations_case_key"


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_02_competing_worker_claims_skip_locked_partition(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-02: 5 concurrent workers race under FOR UPDATE SKIP LOCKED to partition 10 eligible attempts."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    # Seed 10 recovery-eligible attempts
    created_attempts: list[BillingCheckoutAttempt] = []
    for i in range(10):
        att = await pg.create_recovery_attempt(
            org_id=org.id,
            status=CheckoutAttemptStatus.recovery_required,
            next_recovery_retry_at=now_utc - timedelta(seconds=10 + i),
        )
        created_attempts.append(att)

    # Launch 5 concurrent workers with independent sessions
    async def _worker_claim(worker_idx: int) -> list[uuid.UUID]:
        async with pg.session_factory() as db:
            claimed = await claim_recovery_batch(
                db=db,
                worker_id=f"worker_{worker_idx}",
                limit=2,
                lease_duration_seconds=30,
                now=now_utc,
            )
            return [c.attempt_id for c in claimed]

    tasks = [_worker_claim(i) for i in range(5)]
    results = await asyncio.gather(*tasks)

    all_claimed_ids = [att_id for sublist in results for att_id in sublist]
    assert len(all_claimed_ids) == 10, f"Expected exactly 10 claims, got {len(all_claimed_ids)}"
    assert len(set(all_claimed_ids)) == 10, "Duplicate claim detected across concurrent workers!"

    # Verify each row in DB has unique recovery_claim_id and future expiration
    async with pg.session_factory() as db:
        res = await db.execute(
            select(BillingCheckoutAttempt).where(
                BillingCheckoutAttempt.id.in_(all_claimed_ids)
            )
        )
        db_attempts = list(res.scalars().all())
        assert len(db_attempts) == 10
        claim_ids = {a.recovery_claim_id for a in db_attempts}
        assert len(claim_ids) == 10
        assert None not in claim_ids
        for a in db_attempts:
            assert a.recovery_claim_expires_at is not None
            assert a.recovery_claim_expires_at > now_utc


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_03_for_update_skip_locked_non_blocking_concurrency(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-03: Session holding row lock is skipped by concurrent worker without blocking."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    att1 = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=20),
    )
    att2 = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=10),
    )

    barrier = asyncio.Event()
    worker_done = asyncio.Event()
    worker_claimed_ids: list[uuid.UUID] = []

    async def _holding_session() -> None:
        async with pg.session_factory() as db:
            # Lock att1 explicitly with FOR UPDATE
            await db.execute(
                select(BillingCheckoutAttempt)
                .where(BillingCheckoutAttempt.id == att1.id)
                .with_for_update()
            )
            barrier.set()
            # Hold lock until worker completes
            await worker_done.wait()
            await db.rollback()

    async def _concurrent_worker() -> None:
        await barrier.wait()
        async with pg.session_factory() as db:
            # Query with SKIP LOCKED: should skip locked att1 and claim att2 immediately
            claimed = await claim_recovery_batch(
                db=db,
                worker_id="non_blocking_worker",
                limit=10,
                now=now_utc,
            )
            for c in claimed:
                worker_claimed_ids.append(c.attempt_id)
        worker_done.set()

    await asyncio.gather(_holding_session(), _concurrent_worker())

    assert att1.id not in worker_claimed_ids, "Locked att1 was incorrectly claimed!"
    assert att2.id in worker_claimed_ids, "Unlocked att2 should have been claimed without blocking!"


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_04_lease_expiration_and_reclamation(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-04: Expired worker lease allows clean reclamation with fresh UUID and incremented expired_lease_count."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)
    old_claim_id = uuid.uuid4()

    # Seed attempt whose claim expired in the past
    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=60),
        recovery_claim_expires_at=now_utc - timedelta(seconds=10),
        recovery_claim_id=old_claim_id,
        request_payload={"plan_id": "plan_pg16_pro", "expired_lease_count": 0},
    )

    async with pg.session_factory() as db:
        claimed = await claim_recovery_batch(
            db=db,
            worker_id="replacement_worker",
            limit=1,
            now=now_utc,
        )
        assert len(claimed) == 1
        new_claim = claimed[0]
        assert new_claim.attempt_id == att.id
        assert new_claim.recovery_claim_id != old_claim_id

    # Verify DB row updated with fresh claim id and expired_lease_count == 1
    async with pg.session_factory() as db:
        updated = await db.get(BillingCheckoutAttempt, att.id)
        assert updated is not None
        assert updated.recovery_claim_id == new_claim.recovery_claim_id
        assert updated.recovery_claim_expires_at > now_utc
        assert int(updated.request_payload.get("expired_lease_count", 0)) == 1


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_05_stale_worker_fencing_rejection_and_reconciliation(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-05: Superseded stale worker is fenced by UUID token and stages reconciliation without corrupting winning state."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    # Worker A claims attempt
    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=30),
    )

    async with pg.session_factory() as db:
        claimed_a_batch = await claim_recovery_batch(
            db=db,
            worker_id="worker_a",
            limit=1,
            lease_duration_seconds=1,  # Short lease
            now=now_utc,
        )
        claimed_a = claimed_a_batch[0]

    # Time advances; lease expires; Worker B reclaims attempt
    advanced_time = now_utc + timedelta(seconds=5)
    async with pg.session_factory() as db:
        claimed_b_batch = await claim_recovery_batch(
            db=db,
            worker_id="worker_b",
            limit=1,
            lease_duration_seconds=60,
            now=advanced_time,
        )
        claimed_b = claimed_b_batch[0]
        assert claimed_b.recovery_claim_id != claimed_a.recovery_claim_id

    # Worker B successfully finalizes recovery with sub_b
    sub_b_id = f"sub_pg16_winner_{uuid.uuid4().hex[:8]}"
    res_b = await _record_recovery_outcome(
        session_factory=pg.session_factory,
        claimed=claimed_b,
        outcome=RecoveryOutcome.RECOVERED,
        target_sub_id=sub_b_id,
        short_url="https://rzp.io/i/winner",
        now_utc=advanced_time,
    )
    assert res_b.outcome == RecoveryOutcome.RECOVERED

    # Stale Worker A resumes and attempts to finalize with sub_a
    sub_a_id = f"sub_pg16_loser_{uuid.uuid4().hex[:8]}"
    pg.register_case_key(f"recovery_superseded:{att.id}:{sub_a_id}")

    with pytest.raises(StaleRecoveryClaimError):
        await _record_recovery_outcome(
            session_factory=pg.session_factory,
            claimed=claimed_a,
            outcome=RecoveryOutcome.RECOVERED,
            target_sub_id=sub_a_id,
            now_utc=advanced_time + timedelta(seconds=2),
        )

    # Verify attempt in DB was NOT corrupted by stale worker
    async with pg.session_factory() as db:
        final_att = await db.get(BillingCheckoutAttempt, att.id)
        assert final_att is not None
        assert final_att.status == CheckoutAttemptStatus.open
        assert final_att.provider_subscription_id == sub_b_id  # Winner preserved

        # Verify reconciliation case was staged for superseded subscription
        case_key = f"recovery_superseded:{att.id}:{sub_a_id}"
        rec_res = await db.execute(
            select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key)
        )
        rec = rec_res.scalar_one_or_none()
        assert rec is not None
        assert rec.reason == "superseded_recovery_worker"
        assert rec.diagnostic_payload.get("recovered_subscription_id") == sub_a_id


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_06_atomic_jsonb_retry_counter_concurrency(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-06: Atomic JSONB retry counter increments preserve existing keys and quarantine at threshold."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    # Seed attempt near quarantine limit (4 expired leases)
    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=20),
        recovery_claim_expires_at=now_utc - timedelta(seconds=5),
        recovery_claim_id=uuid.uuid4(),
        request_payload={
            "plan_id": "plan_pg16_pro",
            "customer_ref": "cust_12345",
            "expired_lease_count": 4,
            "transient_failures": 3,
        },
    )

    pg.register_case_key(f"recovery_worker_crashes:{att.id}")

    # Worker claims: detects 5th expired lease -> quarantines immediately
    async with pg.session_factory() as db:
        claimed = await claim_recovery_batch(
            db=db,
            worker_id="quarantine_worker",
            limit=1,
            now=now_utc,
        )
        # Quarantined attempt is NOT claimed for execution
        assert len(claimed) == 0

    # Verify attempt quarantined in DB, retry timestamp cleared, and original keys intact
    async with pg.session_factory() as db:
        updated = await db.get(BillingCheckoutAttempt, att.id)
        assert updated is not None
        assert updated.next_recovery_retry_at is None
        assert updated.recovery_claim_id is None
        assert updated.error_message == "repeated_worker_lease_expiry_or_crashes"
        assert updated.request_payload.get("plan_id") == "plan_pg16_pro"
        assert updated.request_payload.get("customer_ref") == "cust_12345"
        assert int(updated.request_payload.get("expired_lease_count", 0)) == 5

        # Verify reconciliation case created
        rec_res = await db.execute(
            select(PaymentReconciliation).where(
                PaymentReconciliation.case_key == f"recovery_worker_crashes:{att.id}"
            )
        )
        rec = rec_res.scalar_one_or_none()
        assert rec is not None
        assert rec.reason == "repeated_worker_crash_or_lease_expiry"


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_07_reconciliation_upsert_idempotency_savepoint_race(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-07: 3 concurrent workers racing on identical case_key handle unique constraint via savepoint rollback."""
    org = await pg.create_org()
    case_key = f"race_test_case_{uuid.uuid4().hex}"
    pg.register_case_key(case_key)
    now_utc = datetime.now(timezone.utc)

    # 3 concurrent workers attempting to stage the exact same reconciliation case
    async def _stage_case(worker_idx: int) -> uuid.UUID:
        async with pg.session_factory() as db:
            rec = await stage_or_update_payment_reconciliation(
                db=db,
                case_key=case_key,
                reason="concurrent_upsert_race",
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                organization_id=org.id,
                diagnostic_data={"worker_index": worker_idx, "ts": now_utc.isoformat()},
                now=now_utc,
            )
            await db.commit()
            return rec.id

    tasks = [_stage_case(i) for i in range(3)]
    results = await asyncio.gather(*tasks)

    # Verify exactly 1 unique record exists in payment_reconciliations
    async with pg.session_factory() as db:
        res = await db.execute(
            select(PaymentReconciliation).where(PaymentReconciliation.case_key == case_key)
        )
        rows = list(res.scalars().all())
        assert len(rows) == 1, f"Expected exactly 1 row, got {len(rows)}"
        assert rows[0].reason == "concurrent_upsert_race"
        assert rows[0].organization_id == org.id


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_08_organization_advisory_lock_serialization(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-08: Canonical organization advisory locks serialize operations for the same org while allowing distinct orgs to proceed concurrently."""
    org1 = await pg.create_org(name="Advisory Org 1")
    org2 = await pg.create_org(name="Advisory Org 2")

    order_log: list[str] = []
    barrier = asyncio.Event()

    async def _task_org1_first() -> None:
        async with pg.session_factory() as db:
            await acquire_advisory_lock(db, "organization", org1.id)
            order_log.append("org1_first_acquired")
            barrier.set()
            await asyncio.sleep(0.15)
            order_log.append("org1_first_releasing")
            await db.commit()

    async def _task_org1_second() -> None:
        await barrier.wait()
        async with pg.session_factory() as db:
            # Must block until org1_first releases
            await acquire_advisory_lock(db, "organization", org1.id)
            order_log.append("org1_second_acquired")
            await db.commit()

    async def _task_org2_concurrent() -> None:
        await barrier.wait()
        async with pg.session_factory() as db:
            # Different org: should acquire immediately without waiting on org1
            await acquire_advisory_lock(db, "organization", org2.id)
            order_log.append("org2_concurrent_acquired")
            await db.commit()

    await asyncio.gather(_task_org1_first(), _task_org1_second(), _task_org2_concurrent())

    # Assert org1_second acquired strictly after org1_first releasing
    idx_first_release = order_log.index("org1_first_releasing")
    idx_second_acquire = order_log.index("org1_second_acquired")
    assert idx_second_acquire > idx_first_release, "Organization advisory lock did not serialize same-tenant tasks!"


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_09_production_isolation_and_billing_invariants(
    pg: Pg16CheckoutRecoveryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG16-REC-09: Environment isolation partitions recovery claims and rejects cross-environment operations."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    # Seed test attempt and live attempt
    test_att = await pg.create_recovery_attempt(
        org_id=org.id,
        environment="test",
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=10),
    )
    live_att = await pg.create_recovery_attempt(
        org_id=org.id,
        environment="live",
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=10),
    )

    # In test mode: worker claims only test attempt
    async with pg.session_factory() as db:
        test_claims = await claim_recovery_batch(
            db=db,
            worker_id="test_worker",
            environment="test",
            limit=10,
            now=now_utc,
        )
        claimed_ids = {c.attempt_id for c in test_claims}
        assert test_att.id in claimed_ids
        assert live_att.id not in claimed_ids

    # Switch to production mode: rejects test merchant account
    monkeypatch.setattr(settings, "environment", "production")
    async with pg.session_factory() as db:
        with pytest.raises(ValueError, match="rejects prohibited merchant account"):
            await claim_recovery_batch(
                db=db,
                worker_id="prod_worker",
                environment="live",
                merchant_account_id="test",
                limit=10,
                now=now_utc,
            )


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_10_controlled_failure_injection_abandoned_lease(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-10: Crashed worker leaves committed lease; other workers skip until lease expiration allows reclamation."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=10),
    )

    # Worker 1 claims attempt and crashes immediately
    async with pg.session_factory() as db:
        c1 = await claim_recovery_batch(
            db=db,
            worker_id="crashed_worker",
            limit=1,
            lease_duration_seconds=5,
            now=now_utc,
        )
        assert len(c1) == 1

    # Worker 2 attempts claim while lease is active: skipped
    async with pg.session_factory() as db:
        c2 = await claim_recovery_batch(
            db=db,
            worker_id="worker_2",
            limit=1,
            now=now_utc + timedelta(seconds=2),
        )
        assert len(c2) == 0

    # Advance time past lease expiration: Worker 2 successfully reclaims
    async with pg.session_factory() as db:
        c3 = await claim_recovery_batch(
            db=db,
            worker_id="worker_2",
            limit=1,
            now=now_utc + timedelta(seconds=6),
        )
        assert len(c3) == 1
        assert c3[0].attempt_id == att.id


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_11_controlled_failure_injection_transaction_rollback(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-11: Transaction rollback during outcome persistence leaves database in clean uncorrupted state."""
    org = await pg.create_org()
    now_utc = datetime.now(timezone.utc)

    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
        next_recovery_retry_at=now_utc - timedelta(seconds=10),
    )

    # Claim attempt
    async with pg.session_factory() as db:
        batch = await claim_recovery_batch(
            db=db,
            worker_id="rollback_worker",
            limit=1,
            now=now_utc,
        )
        claimed = batch[0]

    # Simulate transaction rollback during final outcome persistence
    async with pg.session_factory() as db:
        # Load attempt and apply changes without committing
        stmt = select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == att.id).with_for_update()
        res = await db.execute(stmt)
        obj = res.scalar_one()
        obj.status = CheckoutAttemptStatus.complete
        obj.provider_subscription_id = "sub_uncommitted"
        await db.rollback()  # Explicit rollback

    # Verify attempt in DB remains in recovery_required with claim info intact
    async with pg.session_factory() as db:
        reloaded = await db.get(BillingCheckoutAttempt, att.id)
        assert reloaded is not None
        assert reloaded.status == CheckoutAttemptStatus.recovery_required
        assert reloaded.provider_subscription_id is None


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_recovery_12_controlled_failure_injection_connection_drop(
    pg: Pg16CheckoutRecoveryHarness,
) -> None:
    """PG16-REC-12: Abrupt connection close immediately releases PostgreSQL row and advisory locks without lingering locks."""
    org = await pg.create_org()
    att = await pg.create_recovery_attempt(
        org_id=org.id,
        status=CheckoutAttemptStatus.recovery_required,
    )

    # Connection 1 acquires row lock and advisory lock, then forcibly closes connection
    async with pg.session_factory() as db1:
        await acquire_advisory_lock(db1, "organization", org.id)
        await db1.execute(
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == att.id)
            .with_for_update()
        )
        # Abruptly close underlying connection
        await db1.close()

    # Connection 2 immediately acquires advisory and row lock without blocking or hanging
    async with pg.session_factory() as db2:
        acquired = await try_acquire_advisory_lock(db2, "organization", org.id)
        assert acquired is True

        res = await db2.execute(
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == att.id)
            .with_for_update(nowait=True)
        )
        locked_row = res.scalar_one_or_none()
        assert locked_row is not None
        await db2.rollback()
