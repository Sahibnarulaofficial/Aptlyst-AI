"""Live PostgreSQL 16 integration test suite for D-2 Stage 1 Razorpay customer provisioning.

Validates:
- Caller-session isolation across unflushed ORM, dirty ORM, deleted ORM, flushed writes,
  Core SQL DML, active `async with caller_db.begin():` blocks, identity-map preservation,
  and default `AsyncSessionLocal` fallback safety (`PG-CUST-01` through `PG-CUST-07`).
- Real PostgreSQL 16 `FOR UPDATE` row-lock serialization, `READ COMMITTED` snapshot wakeup,
  HTTP boundary connection hygiene (`0` idle-in-transaction sessions), unique constraint
  and partial unique index enforcement (`PG-CUST-08` through `PG-CUST-11`).
- Fail-closed recovery & fencing paths: D-1 corrupted resolved + JSONB reconciliation,
  E-1 independent fallback session, D-6 cross-org `23505` collision recovery with
  `asyncpg` `constraint_name`/`detail` diagnostics, S1-F1 4/5-session secondary integrity
  recovery, stale worker fencing, expired leases, HTTP definite vs ambiguous failures, and
  mid-HTTP organization deletion (`PG-CUST-12` through `PG-CUST-16`).
- Wire-level `asyncio.CancelledError` teardown during blocked `FOR UPDATE` locks and
  secondary recovery (`PG-CUST-17`, `PG-CUST-18`).
- PostgreSQL 16 RLS / `anon` & `authenticated` role isolation and end-to-end
  `create_razorpay_subscription_checkout()` Phase A / Phase B isolation (`PG-CUST-19`, `PG-CUST-20`).

NOTE:
- SQLite MUST NOT be used as a substitute for PostgreSQL 16 integration testing.
- When `REQUIRE_POSTGRES_TESTS=1` (e.g. CI), tests NEVER skip and MUST fail closed unless a
  verified isolated local PostgreSQL 16 test database is reachable via `POSTGRES_TEST_URL`.
- Locally without `REQUIRE_POSTGRES_TESTS`, if `POSTGRES_TEST_URL` is unset or an isolated
  local database is unreachable, tests skip safely without ever touching Supabase or `.env`.
"""

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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from pydantic import SecretStr
import pytest
from sqlalchemy import delete, inspect as sa_inspect, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import configure_mappers

# Register all application ORM models on Base.metadata before mapper configuration
# (mirrors alembic/env.py and tests/test_razorpay_customer.py).
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
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import CheckoutAttemptStatus, Subscription
from app.services.billing.razorpay.checkout import create_razorpay_subscription_checkout
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayServer5xxError,
)
from app.services.billing.razorpay.customer import (
    CORRUPTED_RESOLVED_ERROR_CODE,
    PROVIDER_CUSTOMER_UNIQUE_INDEX,
    RazorpayCustomerError,
    RazorpayCustomerPersistenceError,
    RazorpayCustomerProvisioningInProgressError,
    RazorpayCustomerReconciliationRequiredError,
    get_or_create_razorpay_customer,
)
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    PostgresTestConnectionError,
    ValidatedPostgresTestTarget,
    _OptionalPostgresUnreachableError,
    _default_async_pg_engine_factory,
    assert_async_session_local_isolated,
    create_verified_async_session_factory,
    is_postgres_tests_required,
    resolve_verified_async_pg_engine,
    validate_postgres_test_url,
)


POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
_SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)

_VERIFIED_TARGET_CACHE: ValidatedPostgresTestTarget | None = None


async def _get_verified_pg16_target() -> ValidatedPostgresTestTarget:
    """Verify live PostgreSQL 16 connectivity and database isolation once per process."""
    global _VERIFIED_TARGET_CACHE
    if _VERIFIED_TARGET_CACHE is not None:
        return _VERIFIED_TARGET_CACHE

    raw_url = os.environ.get("POSTGRES_TEST_URL")
    required = is_postgres_tests_required()
    target = validate_postgres_test_url(raw_url, require_postgres=required)
    assert target is not None

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
class Pg16CustomerHarness:
    """Per-test live PostgreSQL 16 harness with tracked reverse-FK and trigger cleanup."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    target: ValidatedPostgresTestTarget
    org_ids: list[uuid.UUID] = field(default_factory=list)
    case_keys: list[str] = field(default_factory=list)
    triggers: list[tuple[str, str, str]] = field(default_factory=list)

    def register_org(self, org_id: uuid.UUID) -> uuid.UUID:
        if org_id not in self.org_ids:
            self.org_ids.append(org_id)
        return org_id

    def register_case_key(self, case_key: str) -> str:
        if case_key not in self.case_keys:
            self.case_keys.append(case_key)
        return case_key

    def register_trigger(self, trigger_name: str, table_name: str, function_name: str) -> None:
        entry = (trigger_name, table_name, function_name)
        if entry not in self.triggers:
            self.triggers.append(entry)

    def new_customer_id(self, tag: str = "cust") -> str:
        return f"cust_pg16_{tag}_{uuid.uuid4().hex[:8]}"

    async def create_org(self, name: str | None = None) -> Organization:
        org = Organization(
            id=uuid.uuid4(),
            name=name or f"PG16 Test Org {uuid.uuid4().hex[:8]}",
        )
        self.register_org(org.id)
        async with self.session_factory() as db:
            db.add(org)
            await db.commit()
        return org


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
    harness = Pg16CustomerHarness(
        engine=engine,
        session_factory=session_factory,
        target=target,
    )

    try:
        yield harness
    finally:
        cleanup_error: Exception | None = None
        try:
            async with engine.begin() as conn:
                for trigger_name, table_name, function_name in reversed(harness.triggers):
                    await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};"))
                    await conn.execute(text(f"DROP FUNCTION IF EXISTS {function_name}();"))

                if harness.org_ids or harness.case_keys:
                    await conn.execute(
                        text(
                            "DELETE FROM payment_reconciliations "
                            "WHERE organization_id = ANY(:org_ids) OR case_key = ANY(:case_keys)"
                        ),
                        {
                            "org_ids": harness.org_ids,
                            "case_keys": harness.case_keys,
                        },
                    )
                if harness.org_ids:
                    await conn.execute(
                        text("DELETE FROM billing_payments WHERE organization_id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM billing_checkout_attempts WHERE organization_id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM billing_customer_provisions WHERE organization_id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM subscriptions WHERE organization_id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM organizations WHERE id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )

                    remaining_provisions = await conn.execute(
                        text(
                            "SELECT count(*) FROM billing_customer_provisions "
                            "WHERE organization_id = ANY(:org_ids)"
                        ),
                        {"org_ids": harness.org_ids},
                    )
                    assert int(remaining_provisions.scalar_one()) == 0

                    remaining_orgs = await conn.execute(
                        text("SELECT count(*) FROM organizations WHERE id = ANY(:org_ids)"),
                        {"org_ids": harness.org_ids},
                    )
                    assert int(remaining_orgs.scalar_one()) == 0

                if harness.org_ids or harness.case_keys:
                    remaining_recons = await conn.execute(
                        text(
                            "SELECT count(*) FROM payment_reconciliations "
                            "WHERE organization_id = ANY(:org_ids) OR case_key = ANY(:case_keys)"
                        ),
                        {
                            "org_ids": harness.org_ids,
                            "case_keys": harness.case_keys,
                        },
                    )
                    assert int(remaining_recons.scalar_one()) == 0
        except Exception as exc:
            cleanup_error = exc
        finally:
            await engine.dispose()

        if cleanup_error is not None:
            raise cleanup_error


async def _wait_for_pg_lock_waiter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    query_fragment: str,
    timeout_seconds: float = 5.0,
) -> int:
    """Poll pg_stat_activity until an active backend running `query_fragment` blocks on a PG lock."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as obs_db:
            res = await obs_db.execute(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND state = 'active' "
                    "AND wait_event_type = 'Lock' "
                    "AND position(:fragment IN query) > 0"
                ),
                {"fragment": query_fragment},
            )
            pid = res.scalar_one_or_none()
            if pid is not None:
                return int(pid)
        await asyncio.sleep(0.03)
    raise AssertionError(
        f"Timed out after {timeout_seconds:.1f}s waiting for PostgreSQL 16 Lock wait on query containing {query_fragment!r}."
    )


async def _count_idle_in_transaction_backends(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Count non-observer sessions currently in 'idle in transaction' on the test database."""
    async with session_factory() as obs_db:
        res = await obs_db.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() "
                "AND state = 'idle in transaction'"
            )
        )
        return int(res.scalar_one())


async def _fetch_provision(
    session_factory: async_sessionmaker[AsyncSession],
    organization_id: uuid.UUID,
) -> BillingCustomerProvision | None:
    async with session_factory() as obs_db:
        res = await obs_db.execute(
            select(BillingCustomerProvision).where(
                BillingCustomerProvision.organization_id == organization_id,
                BillingCustomerProvision.provider == "razorpay",
                BillingCustomerProvision.merchant_account_id == "default",
                BillingCustomerProvision.environment == "test",
            )
        )
        return res.scalar_one_or_none()


async def _fetch_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    case_key: str,
) -> PaymentReconciliation | None:
    async with session_factory() as obs_db:
        res = await obs_db.execute(
            select(PaymentReconciliation).where(
                PaymentReconciliation.provider == "razorpay",
                PaymentReconciliation.merchant_account_id == "default",
                PaymentReconciliation.environment == "test",
                PaymentReconciliation.case_key == case_key,
            )
        )
        return res.scalar_one_or_none()


# =============================================================================
# Group 1: Caller-Session Isolation on Live PostgreSQL 16 (PG-CUST-01..07)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_unflushed_new_orm_objects_untouched(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-01: Unflushed caller_db.new ORM objects stay uncommitted across provisioning."""
    org_target = await pg.create_org("Target Org PG-CUST-01")
    org_unflushed_id = pg.register_org(uuid.uuid4())
    expected_customer_id = pg.new_customer_id("01")
    observed_mid_http: dict[str, Any] = {}

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            async with pg.session_factory() as obs_db:
                row = (
                    await obs_db.execute(
                        select(Organization).where(Organization.id == org_unflushed_id)
                    )
                ).scalar_one_or_none()
                observed_mid_http["unflushed_in_pg"] = row is not None
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        unflushed_org = Organization(id=org_unflushed_id, name="Unflushed Caller Org")
        caller_db.add(unflushed_org)
        assert unflushed_org in caller_db.new

        customer_id = await get_or_create_razorpay_customer(
            organization_id=org_target.id,
            user_email="owner@example.com",
            session_factory=pg.session_factory,
        )

        assert customer_id == expected_customer_id
        assert observed_mid_http == {"unflushed_in_pg": False}
        assert unflushed_org in caller_db.new

        # Verify unflushed_org is still NOT in PostgreSQL while provision IS committed.
        async with pg.session_factory() as obs_db:
            assert (
                await obs_db.execute(
                    select(Organization).where(Organization.id == org_unflushed_id)
                )
            ).scalar_one_or_none() is None

        prov = await _fetch_provision(pg.session_factory, org_target.id)
        assert prov is not None
        assert prov.status == CustomerProvisionStatus.resolved
        assert prov.provider_customer_id == expected_customer_id

        # Caller commits independently afterward.
        await caller_db.commit()

    async with pg.session_factory() as verify_db:
        committed_unflushed = (
            await verify_db.execute(
                select(Organization).where(Organization.id == org_unflushed_id)
            )
        ).scalar_one_or_none()
        assert committed_unflushed is not None
        assert committed_unflushed.name == "Unflushed Caller Org"


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_dirty_orm_objects_untouched(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-02: Dirty ORM attributes in caller_db stay uncommitted; service reads PG16 committed state."""
    committed_name = "Committed PG16 Org Name 02"
    org = await pg.create_org(committed_name)
    expected_customer_id = pg.new_customer_id("02")
    sent_names: list[str] = []

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            sent_names.append(str(kwargs.get("name")))
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        loaded_org = await caller_db.get(Organization, org.id)
        assert loaded_org is not None
        loaded_org.name = "Dirty Uncommitted Caller Name"
        assert loaded_org in caller_db.dirty

        customer_id = await get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=pg.session_factory,
        )

        assert customer_id == expected_customer_id
        assert sent_names == [committed_name]
        assert loaded_org in caller_db.dirty

        await caller_db.rollback()

    async with pg.session_factory() as verify_db:
        db_org = await verify_db.get(Organization, org.id)
        assert db_org is not None
        assert db_org.name == committed_name

    prov = await _fetch_provision(pg.session_factory, org.id)
    assert prov is not None
    assert prov.status == CustomerProvisionStatus.resolved
    assert prov.provider_customer_id == expected_customer_id


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_deleted_orm_objects_untouched(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-03: Pending caller_db.deleted survives internal TX rollback; ON DELETE RESTRICT enforced."""
    org_victim = await pg.create_org("Victim Org 03")
    org_target = await pg.create_org("Target Org 03")
    org_owner = await pg.create_org("Owner Org 03")
    shared_customer_id = pg.new_customer_id("03_shared")
    pg.register_case_key(f"customer_provision:collision:{org_target.id}:{shared_customer_id}")

    async with pg.session_factory() as seed_db:
        seed_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_owner.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=shared_customer_id,
                status=CustomerProvisionStatus.resolved,
            )
        )
        await seed_db.commit()

    class _CollidingClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": shared_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _CollidingClient(),
    )

    async with pg.session_factory() as caller_db:
        victim = await caller_db.get(Organization, org_victim.id)
        assert victim is not None
        await caller_db.delete(victim)
        assert victim in caller_db.deleted

        # Service hits real PG16 23505 IntegrityError and rolls back TX_A2 internally.
        with pytest.raises(RazorpayCustomerPersistenceError):
            await get_or_create_razorpay_customer(
                organization_id=org_target.id,
                session_factory=pg.session_factory,
            )

        # Caller's pending delete must remain intact.
        assert victim in caller_db.deleted
        await caller_db.commit()

    # Verify victim was deleted on caller commit.
    async with pg.session_factory() as verify_db:
        assert await verify_db.get(Organization, org_victim.id) is None

    # Verify real PG16 ON DELETE RESTRICT (23503) on billing_customer_provisions.organization_id.
    async with pg.session_factory() as fk_db:
        with pytest.raises(IntegrityError) as fk_exc:
            await fk_db.execute(
                text("DELETE FROM organizations WHERE id = :org_id"),
                {"org_id": org_owner.id},
            )
        await fk_db.rollback()
        orig = fk_exc.value.orig
        pgcode = getattr(orig, "pgcode", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
        assert pgcode == "23503"


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_previously_flushed_writes_untouched(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-04: Previously flushed ORM writes in caller_db (new/dirty/deleted empty) stay open & uncommitted."""
    org_target = await pg.create_org("Target Org 04")
    org_flushed_id = pg.register_org(uuid.uuid4())
    expected_customer_id = pg.new_customer_id("04")

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        flushed_org = Organization(id=org_flushed_id, name="Flushed Uncommitted Org 04")
        caller_db.add(flushed_org)
        await caller_db.flush()

        assert len(caller_db.new) == 0
        assert len(caller_db.dirty) == 0
        assert len(caller_db.deleted) == 0
        assert caller_db.in_transaction() is True

        customer_id = await get_or_create_razorpay_customer(
            organization_id=org_target.id,
            session_factory=pg.session_factory,
        )
        assert customer_id == expected_customer_id
        assert caller_db.in_transaction() is True

        # Observer still cannot see flushed_org because caller_db has not committed.
        async with pg.session_factory() as obs_db:
            assert await obs_db.get(Organization, org_flushed_id) is None

        await caller_db.commit()

    async with pg.session_factory() as verify_db:
        assert await verify_db.get(Organization, org_flushed_id) is not None


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_core_sql_dml_untouched(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-05: Core SQL insert/update/delete statements on caller_db stay isolated to caller's TX."""
    org_target = await pg.create_org("Original Core Target 05")
    org_core_id = pg.register_org(uuid.uuid4())
    expected_customer_id = pg.new_customer_id("05")

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        await caller_db.execute(
            insert(Organization).values(id=org_core_id, name="Core Inserted Org 05")
        )
        await caller_db.execute(
            update(Organization)
            .where(Organization.id == org_core_id)
            .values(name="Core Updated Org 05")
        )
        assert caller_db.in_transaction() is True
        in_tx_name = (
            await caller_db.execute(
                select(Organization.name).where(Organization.id == org_core_id)
            )
        ).scalar_one()
        assert in_tx_name == "Core Updated Org 05"

        customer_id = await get_or_create_razorpay_customer(
            organization_id=org_target.id,
            session_factory=pg.session_factory,
        )
        assert customer_id == expected_customer_id
        assert caller_db.in_transaction() is True

        # Observer sees neither Core insert nor Core update prior to caller rollback.
        async with pg.session_factory() as obs_db:
            assert await obs_db.get(Organization, org_core_id) is None

        await caller_db.rollback()

    # Rollback reverted caller Core DML while keeping committed provision intact.
    async with pg.session_factory() as verify_db:
        assert await verify_db.get(Organization, org_core_id) is None
        verify_target = await verify_db.get(Organization, org_target.id)
        assert verify_target is not None
        assert verify_target.name == "Original Core Target 05"

    prov = await _fetch_provision(pg.session_factory, org_target.id)
    assert prov is not None
    assert prov.status == CustomerProvisionStatus.resolved
    assert prov.provider_customer_id == expected_customer_id


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_active_begin_block_and_rollback_safety(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-06: Active `async with caller_db.begin():` exits cleanly and isolates caller exceptions."""
    org_1 = await pg.create_org("Begin Commit Org 06")
    org_2 = await pg.create_org("Begin Rollback Org 06")
    org_extra_commit_id = pg.register_org(uuid.uuid4())
    org_extra_abort_id = pg.register_org(uuid.uuid4())
    cust_1 = pg.new_customer_id("06a")
    cust_2 = pg.new_customer_id("06b")
    issued = iter([cust_1, cust_2])

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": next(issued)}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    # Part 1: Clean exit from `async with caller_db.begin():`
    async with pg.session_factory() as caller_db:
        async with caller_db.begin():
            caller_db.add(Organization(id=org_extra_commit_id, name="Committed Inside Begin 06"))
            res_1 = await get_or_create_razorpay_customer(
                organization_id=org_1.id,
                session_factory=pg.session_factory,
            )
            assert res_1 == cust_1

    async with pg.session_factory() as verify_db:
        assert await verify_db.get(Organization, org_extra_commit_id) is not None

    # Part 2: Caller raises after provisioning succeeds inside `async with caller_db.begin():`
    async with pg.session_factory() as caller_db_abort:
        with pytest.raises(RuntimeError, match="caller_abort_06"):
            async with caller_db_abort.begin():
                caller_db_abort.add(Organization(id=org_extra_abort_id, name="Aborted Inside Begin 06"))
                res_2 = await get_or_create_razorpay_customer(
                    organization_id=org_2.id,
                    session_factory=pg.session_factory,
                )
                assert res_2 == cust_2
                raise RuntimeError("caller_abort_06")

    async with pg.session_factory() as verify_db_2:
        assert await verify_db_2.get(Organization, org_extra_abort_id) is None
        prov_2 = await _fetch_provision(pg.session_factory, org_2.id)
        assert prov_2 is not None
        assert prov_2.status == CustomerProvisionStatus.resolved
        assert prov_2.provider_customer_id == cust_2


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_caller_session_identity_map_preserved_and_default_session_local(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-07: Caller identity_map stays unexpired and session_factory=None uses verified AsyncSessionLocal."""
    assert_async_session_local_isolated(pg.target)

    org = await pg.create_org("Identity Map & AsyncSessionLocal Org 07")
    expected_customer_id = pg.new_customer_id("07")

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        loaded_org = await caller_db.get(Organization, org.id)
        assert loaded_org is not None
        assert loaded_org in caller_db.identity_map.values()
        assert sa_inspect(loaded_org).expired is False

        # Pass session_factory=None to exercise the real default AsyncSessionLocal path.
        customer_id = await get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=None,
        )
        assert customer_id == expected_customer_id
        assert loaded_org in caller_db.identity_map.values()
        assert sa_inspect(loaded_org).expired is False
        assert loaded_org.name == org.name


# =============================================================================
# Group 2: Concurrency, Row Locking & Index Uniqueness on PG16 (PG-CUST-08..11)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_concurrency_for_update_lock_queue_and_read_committed_reuse(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-08: Real PG16 FOR UPDATE row lock queues Worker B and READ COMMITTED reuses winner."""
    org = await pg.create_org("Lock Queue Org 08")
    winner_customer_id = pg.new_customer_id("08_winner")
    http_calls = 0

    class _NoHttpExpectedClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal http_calls
            http_calls += 1
            return {"id": pg.new_customer_id("08_unexpected")}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _NoHttpExpectedClient(),
    )

    async with pg.session_factory() as locker_db:
        # Locker acquires real PG16 FOR UPDATE row lock on organizations.
        locked = (
            await locker_db.execute(
                select(Organization).where(Organization.id == org.id).with_for_update()
            )
        ).scalar_one()
        assert locked.id == org.id

        worker_task = asyncio.create_task(
            get_or_create_razorpay_customer(
                organization_id=org.id,
                session_factory=pg.session_factory,
            )
        )

        # Confirm PostgreSQL 16 actually blocked worker_task on the row lock.
        blocked_pid = await _wait_for_pg_lock_waiter(
            pg.session_factory,
            query_fragment="FROM organizations",
        )
        assert blocked_pid > 0

        # Locker commits resolved customer provision and releases the FOR UPDATE lock.
        locker_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=winner_customer_id,
                status=CustomerProvisionStatus.resolved,
            )
        )
        await locker_db.commit()

        reused_id = await asyncio.wait_for(worker_task, timeout=10.0)

    assert reused_id == winner_customer_id
    assert http_calls == 0


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_concurrency_simultaneous_first_time_provisioning_single_http_call(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-09: Simultaneous workers serialize on Organization FOR UPDATE with 0 idle-in-tx during HTTP."""
    org = await pg.create_org("Concurrent Workers Org 09")
    expected_customer_id = pg.new_customer_id("09")
    http_entered = asyncio.Event()
    release_http = asyncio.Event()
    http_calls = 0
    idle_in_tx_during_http: list[int] = []

    class _ControlledClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal http_calls
            http_calls += 1
            idle_count = await _count_idle_in_transaction_backends(pg.session_factory)
            idle_in_tx_during_http.append(idle_count)
            http_entered.set()
            await asyncio.wait_for(release_http.wait(), timeout=10.0)
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _ControlledClient(),
    )

    worker_1 = asyncio.create_task(
        get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=pg.session_factory,
        )
    )
    await asyncio.wait_for(http_entered.wait(), timeout=10.0)

    # While worker_1 is in HTTP (with TX_A1 committed & closed), worker_2 tries to provision.
    with pytest.raises(RazorpayCustomerProvisioningInProgressError):
        await get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=pg.session_factory,
        )

    release_http.set()
    winner_id = await asyncio.wait_for(worker_1, timeout=10.0)

    assert winner_id == expected_customer_id
    assert http_calls == 1
    assert idle_in_tx_during_http == [0]


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_uniqueness_org_provider_and_partial_provider_customer_index(
    pg: Pg16CustomerHarness,
) -> None:
    """PG-CUST-10: PG16 enforces uq_billing_customer_provisions_org_provider and partial provider_customer index."""
    org_a = await pg.create_org("Uniqueness Org A 10")
    org_b = await pg.create_org("Uniqueness Org B 10")
    org_c = await pg.create_org("Uniqueness Org C 10")

    # 1. Partial unique index allows multiple NULL provider_customer_id rows across statuses.
    async with pg.session_factory() as db_nulls:
        db_nulls.add_all(
            [
                BillingCustomerProvision(
                    id=uuid.uuid4(),
                    organization_id=org_a.id,
                    provider="razorpay",
                    merchant_account_id="default",
                    environment="test",
                    provider_customer_id=None,
                    status=CustomerProvisionStatus.pending,
                    claim_operation_id="claim_a",
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                ),
                BillingCustomerProvision(
                    id=uuid.uuid4(),
                    organization_id=org_b.id,
                    provider="razorpay",
                    merchant_account_id="default",
                    environment="test",
                    provider_customer_id=None,
                    status=CustomerProvisionStatus.failed,
                    last_error_code="400",
                ),
                BillingCustomerProvision(
                    id=uuid.uuid4(),
                    organization_id=org_c.id,
                    provider="razorpay",
                    merchant_account_id="default",
                    environment="test",
                    provider_customer_id=None,
                    status=CustomerProvisionStatus.ambiguous,
                    last_error_code="503",
                ),
            ]
        )
        await db_nulls.commit()

    # 2. Duplicate (organization_id, provider, merchant_account_id, environment) hits 23505.
    async with pg.session_factory() as db_dup_org:
        db_dup_org.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_a.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=None,
                status=CustomerProvisionStatus.failed,
            )
        )
        with pytest.raises(IntegrityError) as org_uq_exc:
            await db_dup_org.commit()
        await db_dup_org.rollback()
        cause = getattr(org_uq_exc.value.orig, "__cause__", None)
        assert getattr(cause, "sqlstate", None) == "23505"
        assert getattr(cause, "constraint_name", None) == "uq_billing_customer_provisions_org_provider"

    # 3. Duplicate non-NULL provider_customer_id across orgs hits partial unique index 23505.
    unique_cust = pg.new_customer_id("10_uq")
    async with pg.session_factory() as db_set_cust:
        prov_a = (
            await db_set_cust.execute(
                select(BillingCustomerProvision).where(
                    BillingCustomerProvision.organization_id == org_a.id
                )
            )
        ).scalar_one()
        prov_a.status = CustomerProvisionStatus.resolved
        prov_a.provider_customer_id = unique_cust
        prov_a.claim_operation_id = None
        prov_a.lease_expires_at = None
        await db_set_cust.commit()

    async with pg.session_factory() as db_dup_cust:
        prov_b = (
            await db_dup_cust.execute(
                select(BillingCustomerProvision).where(
                    BillingCustomerProvision.organization_id == org_b.id
                )
            )
        ).scalar_one()
        prov_b.status = CustomerProvisionStatus.resolved
        prov_b.provider_customer_id = unique_cust
        with pytest.raises(IntegrityError) as cust_uq_exc:
            await db_dup_cust.commit()
        await db_dup_cust.rollback()
        cause_cust = getattr(cust_uq_exc.value.orig, "__cause__", None)
        assert getattr(cause_cust, "sqlstate", None) == "23505"
        assert getattr(cause_cust, "constraint_name", None) == PROVIDER_CUSTOMER_UNIQUE_INDEX


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_idempotent_reuse_and_multi_tenant_parallel_provisioning(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-11: Resolved reuse runs 0 HTTP calls / 0 idle-in-tx; distinct orgs provision in parallel."""
    org_existing = await pg.create_org("Existing Resolved Org 11")
    existing_cust = pg.new_customer_id("11_existing")
    async with pg.session_factory() as seed_db:
        seed_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_existing.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=existing_cust,
                status=CustomerProvisionStatus.resolved,
            )
        )
        await seed_db.commit()

    org_t1 = await pg.create_org("Parallel Tenant 1")
    org_t2 = await pg.create_org("Parallel Tenant 2")
    org_t3 = await pg.create_org("Parallel Tenant 3")
    tenant_map = {
        str(org_t1.id): pg.new_customer_id("11_t1"),
        str(org_t2.id): pg.new_customer_id("11_t2"),
        str(org_t3.id): pg.new_customer_id("11_t3"),
    }
    in_flight = 0
    all_three_in_http = asyncio.Event()
    http_calls_made: list[str] = []

    class _ParallelClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal in_flight
            org_key = str(kwargs["notes"]["organization_id"])
            http_calls_made.append(org_key)
            in_flight += 1
            if in_flight >= 3:
                all_three_in_http.set()
            await asyncio.wait_for(all_three_in_http.wait(), timeout=10.0)
            return {"id": tenant_map[org_key]}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _ParallelClient(),
    )

    # 1. Existing resolved org reuses customer twice with zero HTTP calls.
    assert (
        await get_or_create_razorpay_customer(
            organization_id=org_existing.id,
            session_factory=pg.session_factory,
        )
        == existing_cust
    )
    assert (
        await get_or_create_razorpay_customer(
            organization_id=org_existing.id,
            session_factory=pg.session_factory,
        )
        == existing_cust
    )
    assert http_calls_made == []
    assert await _count_idle_in_transaction_backends(pg.session_factory) == 0

    # 2. Three distinct organizations execute concurrently without cross-tenant lock blocking.
    res_1, res_2, res_3 = await asyncio.gather(
        get_or_create_razorpay_customer(organization_id=org_t1.id, session_factory=pg.session_factory),
        get_or_create_razorpay_customer(organization_id=org_t2.id, session_factory=pg.session_factory),
        get_or_create_razorpay_customer(organization_id=org_t3.id, session_factory=pg.session_factory),
    )
    assert res_1 == tenant_map[str(org_t1.id)]
    assert res_2 == tenant_map[str(org_t2.id)]
    assert res_3 == tenant_map[str(org_t3.id)]
    assert sorted(http_calls_made) == sorted(tenant_map.keys())


# =============================================================================
# Group 3: Recovery & Fencing Paths on Live PG16 (PG-CUST-12..16)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d1_corrupted_resolved_fails_closed_with_jsonb_reconciliation(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-12: D-1 corrupted resolved row transitions to ambiguous and persists JSONB reconciliation."""
    org = await pg.create_org("Corrupted Resolved Org 12")
    case_key = pg.register_case_key(f"customer_provision:anomaly:resolved_missing_id:{org.id}")

    async with pg.session_factory() as seed_db:
        seed_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id="   ",
                status=CustomerProvisionStatus.resolved,
            )
        )
        await seed_db.commit()

    def _forbid_client():
        raise AssertionError("Razorpay HTTP client must never be called on D-1 corrupted resolved state")

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        _forbid_client,
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="corrupted 'resolved' state"):
        await get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=pg.session_factory,
        )

    prov = await _fetch_provision(pg.session_factory, org.id)
    assert prov is not None
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE

    recon = await _fetch_reconciliation(pg.session_factory, case_key)
    assert recon is not None
    assert recon.status == ReconciliationStatus.pending_review
    assert recon.reason == CORRUPTED_RESOLVED_ERROR_CODE
    assert isinstance(recon.diagnostic_payload, dict)
    assert recon.diagnostic_payload["latest"]["anomaly_type"] == "whitespace_customer_id"
    assert len(recon.diagnostic_payload["history"]) == 1


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_e1_corrupted_resolved_recon_failure_runs_independent_fallback_tx(
    pg: Pg16CustomerHarness,
) -> None:
    """PG-CUST-13: E-1 runs independent fallback session when TX_A1 reconciliation insert fails on PG16."""
    org = await pg.create_org("E1 Fallback Org 13")
    case_key = pg.register_case_key(f"customer_provision:anomaly:resolved_missing_id:{org.id}")
    fn_name = f"fn_pg16_fail_recon_{uuid.uuid4().hex[:8]}"
    trg_name = f"trg_pg16_fail_recon_{uuid.uuid4().hex[:8]}"
    pg.register_trigger(trg_name, "payment_reconciliations", fn_name)

    async with pg.session_factory() as seed_db:
        seed_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id="",
                status=CustomerProvisionStatus.resolved,
            )
        )
        await seed_db.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.case_key = '{case_key}' THEN
                        RAISE EXCEPTION 'simulated_pg16_recon_failure' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await seed_db.execute(
            text(
                f"""
                CREATE TRIGGER {trg_name}
                BEFORE INSERT ON payment_reconciliations
                FOR EACH ROW EXECUTE FUNCTION {fn_name}();
                """
            )
        )
        await seed_db.commit()

    with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
        await get_or_create_razorpay_customer(
            organization_id=org.id,
            session_factory=pg.session_factory,
        )

    assert exc_info.value.fallback_committed is True
    assert exc_info.value.original_error is not None

    prov = await _fetch_provision(pg.session_factory, org.id)
    assert prov is not None
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE
    assert await _fetch_reconciliation(pg.session_factory, case_key) is None


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d6_cross_org_collision_recovers_durably_without_losing_owner(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-14: Real PG16 23505 on partial customer index recovers D-6 with asyncpg constraint diagnostics."""
    org_owner = await pg.create_org("Owner Org 14")
    org_affected = await pg.create_org("Affected Org 14")
    shared_customer_id = pg.new_customer_id("14_collide")
    collision_case_key = pg.register_case_key(
        f"customer_provision:collision:{org_affected.id}:{shared_customer_id}"
    )

    async with pg.session_factory() as seed_db:
        seed_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_owner.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=shared_customer_id,
                status=CustomerProvisionStatus.resolved,
            )
        )
        await seed_db.commit()

    class _CollidingClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": shared_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _CollidingClient(),
    )

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(
            organization_id=org_affected.id,
            session_factory=pg.session_factory,
        )

    assert exc_info.value.customer_id == shared_customer_id

    # Owner remains untouched & resolved.
    owner_prov = await _fetch_provision(pg.session_factory, org_owner.id)
    assert owner_prov is not None
    assert owner_prov.status == CustomerProvisionStatus.resolved
    assert owner_prov.provider_customer_id == shared_customer_id

    # Affected org fails closed to ambiguous with NULL provider_customer_id.
    affected_prov = await _fetch_provision(pg.session_factory, org_affected.id)
    assert affected_prov is not None
    assert affected_prov.status == CustomerProvisionStatus.ambiguous
    assert affected_prov.provider_customer_id is None
    assert affected_prov.last_error_code == "customer_id_org_collision"

    # Stage A diagnostic fix verified end-to-end on real PG16 asyncpg 23505 exception!
    recon = await _fetch_reconciliation(pg.session_factory, collision_case_key)
    assert recon is not None
    assert recon.status == ReconciliationStatus.pending_review
    assert recon.reason == "customer_id_org_collision"
    latest = recon.diagnostic_payload["latest"]
    assert latest["conflicting_organization_id"] == str(org_owner.id)
    err_details = latest["original_error_details"]
    assert err_details["pgcode"] == "23505"
    assert err_details["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX
    assert err_details["detail"] is not None
    assert shared_customer_id in err_details["detail"]


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_s1_f1_secondary_integrity_and_five_session_fallback_chain(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-15: Real PG16 TX_A2 failure -> recovery_db 23505 -> secondary_db & 5th fallback_db chain."""
    # Part 1: 4-session S1-F1 chain (tx1_db -> tx2_db fail -> recovery_db 23505 -> secondary_db D-6).
    org_owner_1 = await pg.create_org("Owner S1F1 A 15")
    org_affected_1 = await pg.create_org("Affected S1F1 A 15")
    cust_1 = pg.new_customer_id("15a")
    case_key_1 = pg.register_case_key(f"customer_provision:collision:{org_affected_1.id}:{cust_1}")

    fn_tx2_1 = f"fn_pg16_tx2_once_{uuid.uuid4().hex[:8]}"
    trg_tx2_1 = f"trg_pg16_tx2_once_{uuid.uuid4().hex[:8]}"
    pg.register_trigger(trg_tx2_1, "billing_customer_provisions", fn_tx2_1)

    async with pg.session_factory() as setup_db:
        await setup_db.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn_tx2_1}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.organization_id = '{org_affected_1.id}'::uuid
                       AND NEW.status = 'resolved'::customer_provision_status THEN
                        RAISE EXCEPTION 'simulated_pg16_tx2_failure' USING ERRCODE = '40001';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await setup_db.execute(
            text(
                f"""
                CREATE TRIGGER {trg_tx2_1}
                BEFORE UPDATE ON billing_customer_provisions
                FOR EACH ROW EXECUTE FUNCTION {fn_tx2_1}();
                """
            )
        )
        await setup_db.commit()

    class _ClientPart1:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            # Commit conflicting owner while org_affected_1 is in HTTP so recovery_db hits real PG16 23505.
            async with pg.session_factory() as race_db:
                race_db.add(
                    BillingCustomerProvision(
                        id=uuid.uuid4(),
                        organization_id=org_owner_1.id,
                        provider="razorpay",
                        merchant_account_id="default",
                        environment="test",
                        provider_customer_id=cust_1,
                        status=CustomerProvisionStatus.resolved,
                    )
                )
                await race_db.commit()
            return {"id": cust_1}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _ClientPart1(),
    )

    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(
            organization_id=org_affected_1.id,
            session_factory=pg.session_factory,
        )

    prov_1 = await _fetch_provision(pg.session_factory, org_affected_1.id)
    assert prov_1 is not None
    assert prov_1.status == CustomerProvisionStatus.ambiguous
    assert prov_1.provider_customer_id is None
    assert prov_1.last_error_code == "customer_id_org_collision"
    recon_1 = await _fetch_reconciliation(pg.session_factory, case_key_1)
    assert recon_1 is not None
    assert recon_1.diagnostic_payload["latest"]["original_error_details"]["pgcode"] == "23505"

    # Part 2: 5-session S1-F1 chain where secondary_db reconciliation staging ALSO fails -> 5th fallback_db.
    org_owner_2 = await pg.create_org("Owner S1F1 B 15")
    org_affected_2 = await pg.create_org("Affected S1F1 B 15")
    cust_2 = pg.new_customer_id("15b")
    case_key_2 = pg.register_case_key(f"customer_provision:collision:{org_affected_2.id}:{cust_2}")

    fn_tx2_2 = f"fn_pg16_tx2_b_{uuid.uuid4().hex[:8]}"
    trg_tx2_2 = f"trg_pg16_tx2_b_{uuid.uuid4().hex[:8]}"
    fn_recon_2 = f"fn_pg16_recon_b_{uuid.uuid4().hex[:8]}"
    trg_recon_2 = f"trg_pg16_recon_b_{uuid.uuid4().hex[:8]}"
    pg.register_trigger(trg_tx2_2, "billing_customer_provisions", fn_tx2_2)
    pg.register_trigger(trg_recon_2, "payment_reconciliations", fn_recon_2)

    async with pg.session_factory() as setup_db_2:
        await setup_db_2.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn_tx2_2}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.organization_id = '{org_affected_2.id}'::uuid
                       AND NEW.status = 'resolved'::customer_provision_status THEN
                        RAISE EXCEPTION 'simulated_pg16_tx2_failure_b' USING ERRCODE = '40001';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await setup_db_2.execute(
            text(
                f"""
                CREATE TRIGGER {trg_tx2_2}
                BEFORE UPDATE ON billing_customer_provisions
                FOR EACH ROW EXECUTE FUNCTION {fn_tx2_2}();
                """
            )
        )
        await setup_db_2.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn_recon_2}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.case_key = '{case_key_2}' THEN
                        RAISE EXCEPTION 'simulated_pg16_secondary_recon_failure' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await setup_db_2.execute(
            text(
                f"""
                CREATE TRIGGER {trg_recon_2}
                BEFORE INSERT ON payment_reconciliations
                FOR EACH ROW EXECUTE FUNCTION {fn_recon_2}();
                """
            )
        )
        await setup_db_2.commit()

    class _ClientPart2:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            async with pg.session_factory() as race_db:
                race_db.add(
                    BillingCustomerProvision(
                        id=uuid.uuid4(),
                        organization_id=org_owner_2.id,
                        provider="razorpay",
                        merchant_account_id="default",
                        environment="test",
                        provider_customer_id=cust_2,
                        status=CustomerProvisionStatus.resolved,
                    )
                )
                await race_db.commit()
            return {"id": cust_2}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _ClientPart2(),
    )

    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(
            organization_id=org_affected_2.id,
            session_factory=pg.session_factory,
        )

    prov_2 = await _fetch_provision(pg.session_factory, org_affected_2.id)
    assert prov_2 is not None
    assert prov_2.status == CustomerProvisionStatus.ambiguous
    assert prov_2.provider_customer_id is None
    assert prov_2.last_error_code == "reconciliation_persist_failed"
    assert await _fetch_reconciliation(pg.session_factory, case_key_2) is None


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_fencing_stale_worker_expired_lease_and_http_failure_lifecycles(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-16: Stale worker fencing, expired lease, HTTP 400 vs 503, and mid-HTTP org deletion on PG16."""
    # 1. Stale worker claim_operation_id fencing
    org_stale = await pg.create_org("Stale Worker Org 16")
    cust_stale = pg.new_customer_id("16_stale")
    captured_stale_claim: list[str] = []

    class _StaleRaceClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            async with pg.session_factory() as steal_db:
                prov = (
                    await steal_db.execute(
                        select(BillingCustomerProvision).where(
                            BillingCustomerProvision.organization_id == org_stale.id
                        )
                    )
                ).scalar_one()
                assert prov.claim_operation_id is not None
                captured_stale_claim.append(prov.claim_operation_id)
                pg.register_case_key(f"customer_provision:superseded:{prov.claim_operation_id}")
                prov.claim_operation_id = "newer_claim_op_16"
                await steal_db.commit()
            return {"id": cust_stale}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StaleRaceClient(),
    )

    with pytest.raises(RazorpayCustomerError, match="superseded or lost ownership"):
        await get_or_create_razorpay_customer(
            organization_id=org_stale.id,
            session_factory=pg.session_factory,
        )

    stale_prov = await _fetch_provision(pg.session_factory, org_stale.id)
    assert stale_prov is not None
    assert stale_prov.claim_operation_id == "newer_claim_op_16"
    assert stale_prov.status == CustomerProvisionStatus.pending
    stale_recon = await _fetch_reconciliation(
        pg.session_factory,
        f"customer_provision:superseded:{captured_stale_claim[0]}",
    )
    assert stale_recon is not None
    assert stale_recon.reason == "stale_worker_customer_created"
    assert stale_recon.diagnostic_payload["provider_customer_id"] == cust_stale

    # 2. Expired pending lease -> transitions to ambiguous without calling HTTP
    org_expired = await pg.create_org("Expired Lease Org 16")
    async with pg.session_factory() as exp_db:
        exp_db.add(
            BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_expired.id,
                provider="razorpay",
                merchant_account_id="default",
                environment="test",
                provider_customer_id=None,
                status=CustomerProvisionStatus.pending,
                claim_operation_id="expired_op_16",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=120),
            )
        )
        await exp_db.commit()

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="lease expired"):
        await get_or_create_razorpay_customer(
            organization_id=org_expired.id,
            session_factory=pg.session_factory,
        )
    exp_prov = await _fetch_provision(pg.session_factory, org_expired.id)
    assert exp_prov is not None
    assert exp_prov.status == CustomerProvisionStatus.ambiguous
    assert exp_prov.last_error_code == "lease_expired"

    # 3. HTTP 400 definite client failure -> status='failed' (cleanup_db)
    org_400 = await pg.create_org("HTTP 400 Org 16")

    class _Client400:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            raise RazorpayAPIError("Bad request", status_code=400)

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _Client400(),
    )
    with pytest.raises(RazorpayCustomerError):
        await get_or_create_razorpay_customer(
            organization_id=org_400.id,
            session_factory=pg.session_factory,
        )
    prov_400 = await _fetch_provision(pg.session_factory, org_400.id)
    assert prov_400 is not None
    assert prov_400.status == CustomerProvisionStatus.failed
    assert prov_400.last_error_code == "400"

    # 4. HTTP 503 ambiguous server error -> status='ambiguous' (ambig_db)
    org_503 = await pg.create_org("HTTP 503 Org 16")

    class _Client503:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            raise RazorpayServer5xxError("Gateway timeout", status_code=503)

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _Client503(),
    )
    with pytest.raises(Exception):
        await get_or_create_razorpay_customer(
            organization_id=org_503.id,
            session_factory=pg.session_factory,
        )
    prov_503 = await _fetch_provision(pg.session_factory, org_503.id)
    assert prov_503 is not None
    assert prov_503.status == CustomerProvisionStatus.ambiguous
    assert prov_503.last_error_code == "503"

    # 5. Mid-HTTP organization deletion -> organization_id=None orphan reconciliation
    org_deleted = await pg.create_org("Mid-HTTP Deleted Org 16")
    cust_orphan = pg.new_customer_id("16_orphan")
    captured_orphan_claim: list[str] = []

    class _DeleteOrgMidHttpClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            async with pg.session_factory() as del_db:
                prov = (
                    await del_db.execute(
                        select(BillingCustomerProvision).where(
                            BillingCustomerProvision.organization_id == org_deleted.id
                        )
                    )
                ).scalar_one()
                assert prov.claim_operation_id is not None
                captured_orphan_claim.append(prov.claim_operation_id)
                pg.register_case_key(f"customer_provision:superseded:{prov.claim_operation_id}")
                await del_db.execute(
                    delete(BillingCustomerProvision).where(
                        BillingCustomerProvision.organization_id == org_deleted.id
                    )
                )
                await del_db.execute(
                    delete(Organization).where(Organization.id == org_deleted.id)
                )
                await del_db.commit()
            return {"id": cust_orphan}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _DeleteOrgMidHttpClient(),
    )
    with pytest.raises(RazorpayCustomerError, match="became unavailable"):
        await get_or_create_razorpay_customer(
            organization_id=org_deleted.id,
            session_factory=pg.session_factory,
        )
    del_recon = await _fetch_reconciliation(
        pg.session_factory,
        f"customer_provision:superseded:{captured_orphan_claim[0]}",
    )
    assert del_recon is not None
    assert del_recon.organization_id is None
    assert del_recon.reason == "organization_unavailable_after_customer_created"
    assert del_recon.diagnostic_payload["provider_customer_id"] == cust_orphan


# =============================================================================
# Group 4: Wire-Level Cancellation & Socket Cleanup on PG16 (PG-CUST-17..18)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_cancellation_during_blocked_for_update_lock_releases_connection(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-17: task.cancel() during blocked PG16 FOR UPDATE propagates CancelledError and closes socket."""
    org = await pg.create_org("Wire Cancel Lock Org 17")
    expected_customer_id = pg.new_customer_id("17")

    class _StubClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": expected_customer_id}

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: _StubClient(),
    )

    async with pg.session_factory() as caller_db:
        preloaded_org = await caller_db.get(Organization, org.id)
        assert preloaded_org is not None

        async with pg.session_factory() as locker_db:
            await locker_db.execute(
                select(Organization).where(Organization.id == org.id).with_for_update()
            )

            task = asyncio.create_task(
                get_or_create_razorpay_customer(
                    organization_id=org.id,
                    session_factory=pg.session_factory,
                )
            )
            await _wait_for_pg_lock_waiter(
                pg.session_factory,
                query_fragment="FROM organizations",
            )

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            await locker_db.rollback()

        assert await _count_idle_in_transaction_backends(pg.session_factory) == 0
        assert preloaded_org in caller_db.identity_map.values()
        assert sa_inspect(preloaded_org).expired is False

    # Subsequent provisioning after lock release succeeds cleanly.
    resolved_id = await get_or_create_razorpay_customer(
        organization_id=org.id,
        session_factory=pg.session_factory,
    )
    assert resolved_id == expected_customer_id


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_cancellation_during_secondary_recovery_closes_all_sessions(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-18: task.cancel() while recovery_db waits on PG16 lock closes all opened sessions cleanly."""
    org = await pg.create_org("Wire Cancel Recovery Org 18")
    cust_id = pg.new_customer_id("18")
    fn_name = f"fn_pg16_cancel_rec_{uuid.uuid4().hex[:8]}"
    trg_name = f"trg_pg16_cancel_rec_{uuid.uuid4().hex[:8]}"
    pg.register_trigger(trg_name, "billing_customer_provisions", fn_name)

    async with pg.session_factory() as setup_db:
        await setup_db.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.organization_id = '{org.id}'::uuid
                       AND NEW.status = 'resolved'::customer_provision_status THEN
                        RAISE EXCEPTION 'simulated_pg16_tx2_before_cancel' USING ERRCODE = '40001';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await setup_db.execute(
            text(
                f"""
                CREATE TRIGGER {trg_name}
                BEFORE UPDATE ON billing_customer_provisions
                FOR EACH ROW EXECUTE FUNCTION {fn_name}();
                """
            )
        )
        await setup_db.commit()

    locker_cm = pg.session_factory()
    locker_db = await locker_cm.__aenter__()
    try:
        class _ClientHoldLockBeforeRecovery:
            async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
                # Acquire lock before TX_A2 fails so recovery_db blocks on Organization FOR UPDATE.
                await locker_db.execute(
                    select(Organization).where(Organization.id == org.id).with_for_update()
                )
                return {"id": cust_id}

        monkeypatch.setattr(
            "app.services.billing.razorpay.customer.get_razorpay_client",
            lambda: _ClientHoldLockBeforeRecovery(),
        )

        task = asyncio.create_task(
            get_or_create_razorpay_customer(
                organization_id=org.id,
                session_factory=pg.session_factory,
            )
        )
        await _wait_for_pg_lock_waiter(
            pg.session_factory,
            query_fragment="FROM organizations",
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await locker_db.rollback()
        await locker_cm.__aexit__(None, None, None)

    assert await _count_idle_in_transaction_backends(pg.session_factory) == 0


# =============================================================================
# Group 5: Schema, RLS/Roles & End-to-End Checkout Isolation (PG-CUST-19..20)
# =============================================================================


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_schema_rls_and_role_permissions_on_provisioning_tables(
    pg: Pg16CustomerHarness,
) -> None:
    """PG-CUST-19: Verify RLS, indexes, and anon/authenticated permission denials on PG16."""
    async with pg.session_factory() as db:
        roles_rows = (
            await db.execute(
                text(
                    "SELECT rolname, rolcanlogin FROM pg_roles "
                    "WHERE rolname IN ('anon', 'authenticated') ORDER BY rolname"
                )
            )
        ).fetchall()
        assert {str(r[0]): bool(r[1]) for r in roles_rows} == {
            "anon": False,
            "authenticated": False,
        }

        rls_rows = (
            await db.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class "
                    "WHERE relname IN ('billing_customer_provisions', 'payment_reconciliations') "
                    "ORDER BY relname"
                )
            )
        ).fetchall()
        assert {str(r[0]): bool(r[1]) for r in rls_rows} == {
            "billing_customer_provisions": True,
            "payment_reconciliations": True,
        }

        for role_name in ("anon", "authenticated"):
            for table_name in ("billing_customer_provisions", "payment_reconciliations"):
                with pytest.raises(DBAPIError) as perm_exc:
                    await db.execute(text(f"SET LOCAL ROLE {role_name};"))
                    await db.execute(text(f"SELECT 1 FROM {table_name} LIMIT 1;"))
                await db.rollback()
                cause = getattr(perm_exc.value.orig, "__cause__", None)
                pgcode = getattr(perm_exc.value.orig, "pgcode", None) or getattr(cause, "sqlstate", None)
                assert pgcode == "42501"


@_SKIP_IF_OPTIONAL_POSTGRES
async def test_pg16_d2_checkout_phase_a_and_phase_b_end_to_end_isolation(
    pg: Pg16CustomerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG-CUST-20: End-to-end create_razorpay_subscription_checkout Phase A & Phase B on live PG16."""
    org = await pg.create_org("Checkout End-to-End Org 20")
    expected_customer_id = pg.new_customer_id("20_cust")
    expected_sub_id = f"sub_pg16_{uuid.uuid4().hex[:12]}"
    observed_during_sub_http: dict[str, Any] = {}

    class _CheckoutRazorpayClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": expected_customer_id}

        async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
            # Phase A must already be committed in PG16 before subscription HTTP executes.
            prov = await _fetch_provision(pg.session_factory, org.id)
            observed_during_sub_http["provision_status"] = (
                prov.status.value if prov is not None else None
            )
            observed_during_sub_http["provider_customer_id"] = (
                prov.provider_customer_id if prov is not None else None
            )
            observed_during_sub_http["idle_in_tx"] = await _count_idle_in_transaction_backends(
                pg.session_factory
            )
            return {
                "id": expected_sub_id,
                "status": "created",
                "short_url": f"https://rzp.io/i/{uuid.uuid4().hex[:10]}",
            }

    stub_client = _CheckoutRazorpayClient()
    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.get_razorpay_client",
        lambda: stub_client,
    )
    monkeypatch.setattr(
        "app.services.billing.razorpay.checkout.get_razorpay_client",
        lambda: stub_client,
    )

    async with pg.session_factory() as checkout_db:
        # Simulate prior caller read transaction (e.g. RBAC org lookup) before checkout service runs.
        loaded_org = await checkout_db.get(Organization, org.id)
        assert loaded_org is not None
        assert checkout_db.in_transaction() is True

        result = await create_razorpay_subscription_checkout(
            db=checkout_db,
            organization=loaded_org,
            user_email="admin@example.com",
            customer_session_factory=pg.session_factory,
        )

    assert result.organization_id == org.id
    assert result.subscription_id == expected_sub_id
    assert result.status == CheckoutAttemptStatus.open
    assert observed_during_sub_http == {
        "provision_status": "resolved",
        "provider_customer_id": expected_customer_id,
        "idle_in_tx": 0,
    }

    async with pg.session_factory() as verify_db:
        attempt = (
            await verify_db.execute(
                select(BillingCheckoutAttempt).where(
                    BillingCheckoutAttempt.organization_id == org.id
                )
            )
        ).scalar_one_or_none()
        assert attempt is not None
        assert attempt.status == CheckoutAttemptStatus.open
        assert attempt.provider_subscription_id == expected_sub_id
        assert attempt.provider_customer_id == expected_customer_id

        # Customer provisioning + checkout creation NEVER creates or mutates Subscription rows.
        sub_count = (
            await verify_db.execute(
                select(Subscription).where(Subscription.organization_id == org.id)
            )
        ).scalars().all()
        assert sub_count == []
