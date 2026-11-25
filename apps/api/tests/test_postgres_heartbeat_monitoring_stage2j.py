"""PostgreSQL 16 integration tests for Stage 2J/2J-V worker heartbeats, monitoring, and migration a4b5c6d7e8f3.

Validates against real PostgreSQL 16 (never SQLite):
- Isolated alembic_version & billing_worker_heartbeats table, indexes, RLS, and role privileges.
- Real INSERT ... ON CONFLICT (worker_id) DO UPDATE heartbeat upserts and RETURNING fencing.
- Concurrent heartbeat writes across distinct worker IDs and competing owners of the same worker_id.
- Stale-instance takeover and rejection of fenced writes from superseded heartbeat owners.
- Real read-only collect_billing_health_snapshot queries and alert evaluation against PostgreSQL 16.
- Reversible Alembic migration a4b5c6d7e8f3 downgrade and clean re-upgrade on disposable DB.

Safety:
- Uses tests.postgres_test_guard (never reads `.env` or production DATABASE_URL).
- Skips locally only when REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unavailable.
- Fails closed when REQUIRE_POSTGRES_TESTS=1.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    BillingWorkerHeartbeat,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus, WebhookProcessingStatus
from app.services.billing.razorpay.monitoring import (
    WORKER_TYPE_RECOVERY,
    WORKER_TYPE_WEBHOOK,
    BillingHealthThresholds,
    WorkerHeartbeatTracker,
    collect_billing_health_snapshot,
)
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    _OptionalPostgresUnreachableError,
    is_postgres_tests_required,
    resolve_verified_pg_engine,
)

API_DIR = Path(__file__).resolve().parent.parent
POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
_SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)


def _to_asyncpg_url(raw_url: str) -> str:
    trimmed = raw_url.strip()
    scheme, rest = trimmed.split("://", 1)
    if scheme.lower() in ("postgresql", "postgres", "postgresql+psycopg2", "postgresql+psycopg"):
        return f"postgresql+asyncpg://{rest}"
    return trimmed


@pytest.fixture(scope="module")
def pg16_urls():
    """Verify PostgreSQL 16 isolation and return (sync_engine, async_url)."""
    try:
        engine, _summary, _required = resolve_verified_pg_engine()
    except _OptionalPostgresUnreachableError as exc:
        pytest.skip(str(exc))
    except PostgresTestConfigError as exc:
        pytest.fail(str(exc), pytrace=False)

    if engine is None or not POSTGRES_TEST_URL:
        pytest.skip("Disposable PostgreSQL 16 database not available.")

    async_url = _to_asyncpg_url(POSTGRES_TEST_URL)
    try:
        yield engine, async_url
    finally:
        engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
def test_pg16_stage2j_01_schema_indexes_rls_and_grants(pg16_urls) -> None:
    """Verify PostgreSQL 16 version, heartbeat table/indexes, RLS, and anon/authenticated revokes."""
    engine, _ = pg16_urls
    with engine.connect() as conn:
        ver_num = conn.execute(text("SHOW server_version_num")).scalar_one()
        assert int(ver_num) >= 160000 and int(ver_num) < 170000

        rls_enabled = conn.execute(
            text(
                "SELECT relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'billing_worker_heartbeats'"
            )
        ).scalar_one()
        assert rls_enabled is True

        idx_names = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'billing_worker_heartbeats'"
                )
            ).fetchall()
        }
        assert "ix_billing_worker_heartbeats_type_env_hb" in idx_names
        assert "ix_billing_worker_heartbeats_status_hb" in idx_names

        privs = conn.execute(
            text(
                "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND table_name = 'billing_worker_heartbeats' "
                "AND grantee IN ('anon', 'authenticated')"
            )
        ).fetchall()
        assert privs == []


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2j_02_heartbeat_upsert_takeover_and_fencing(pg16_urls) -> None:
    """Verify PostgreSQL 16 ON CONFLICT upsert, stale takeover, and old-owner fencing rejection."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    worker_id = f"pg16_hb_fencing_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    tracker_a = WorkerHeartbeatTracker(
        worker_id=worker_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        heartbeat_interval_seconds=30.0,
        stale_takeover_seconds=90,
        started_at=now - timedelta(seconds=200),
    )
    tracker_b = WorkerHeartbeatTracker(
        worker_id=worker_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        heartbeat_interval_seconds=30.0,
        stale_takeover_seconds=90,
        started_at=now,
    )

    try:
        # 1. Old worker writes stale initial heartbeat
        stale_ts = now - timedelta(seconds=120)
        assert await tracker_a.record_heartbeat(session_factory, status="running", now=stale_ts, force=True) is True
        assert tracker_a.ownership_lost is False

        # 2. Replacement worker safely takes over stale row
        assert await tracker_b.record_heartbeat(session_factory, status="running", now=now, force=True) is True
        assert tracker_b.ownership_lost is False

        # 3. Zombie old worker attempts fenced write -> rejected cleanly by PostgreSQL
        assert await tracker_a.record_heartbeat(
            session_factory,
            status="running",
            now=now + timedelta(seconds=5),
            force=True,
        ) is False
        assert tracker_a.ownership_lost is True

        # 4. Verify DB still holds replacement worker's instance_token
        async with session_factory() as db:
            row = await db.get(BillingWorkerHeartbeat, worker_id)
            assert row is not None
            assert row.instance_token == tracker_b.instance_token
            assert row.status == "running"

        # 5. Clean shutdown writes stopped
        assert await tracker_b.record_heartbeat(
            session_factory,
            status="stopped",
            now=now + timedelta(seconds=10),
        ) is True
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM billing_worker_heartbeats WHERE worker_id = :wid"),
                {"wid": worker_id},
            )
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2j_03_concurrent_heartbeats_and_live_health_snapshot(pg16_urls) -> None:
    """Verify concurrent PostgreSQL 16 heartbeat writes and read-only health snapshot/alert evaluation."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    wh_id = f"pg16_wh_{uuid.uuid4().hex[:8]}"
    rec_id = f"pg16_rec_{uuid.uuid4().hex[:8]}"
    shared_id = f"pg16_race_{uuid.uuid4().hex[:8]}"

    wh_tracker = WorkerHeartbeatTracker(
        worker_id=wh_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        started_at=now,
    )
    rec_tracker = WorkerHeartbeatTracker(
        worker_id=rec_id,
        worker_type=WORKER_TYPE_RECOVERY,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        started_at=now,
    )
    race_1 = WorkerHeartbeatTracker(
        worker_id=shared_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        started_at=now,
    )
    race_2 = WorkerHeartbeatTracker(
        worker_id=shared_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        started_at=now,
    )

    try:
        # Distinct concurrent heartbeats both succeed
        res_distinct = await asyncio.gather(
            wh_tracker.record_heartbeat(session_factory, status="running", now=now, force=True, processed_delta=2),
            rec_tracker.record_heartbeat(session_factory, status="running", now=now, force=True, processed_delta=1),
        )
        assert res_distinct == [True, True]

        # Concurrent fresh race on same worker_id -> exactly one owner succeeds, loser is fenced
        res_race = await asyncio.gather(
            race_1.record_heartbeat(session_factory, status="running", now=now, force=True),
            race_2.record_heartbeat(session_factory, status="running", now=now, force=True),
        )
        assert sorted(res_race) == [False, True]

        # Read-only health snapshot against PostgreSQL 16
        async with session_factory() as db:
            report = await collect_billing_health_snapshot(
                db=db,
                environment="test",
                now=now + timedelta(seconds=5),
                thresholds=BillingHealthThresholds(heartbeat_stale_seconds=90),
                emit_alert_logs=False,
            )
        worker_ids = {w.worker_id for w in report.workers}
        assert {wh_id, rec_id, shared_id}.issubset(worker_ids)
        assert not any(a.code in ("NO_ACTIVE_WEBHOOK_WORKER", "NO_ACTIVE_RECOVERY_WORKER") for a in report.alerts)
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM billing_worker_heartbeats WHERE worker_id = ANY(:wids)"),
                {"wids": [wh_id, rec_id, shared_id]},
            )
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
def test_pg16_stage2j_04_migration_downgrade_and_reupgrade(pg16_urls) -> None:
    """Verify isolated downgrade a4b5c6d7e8f3 -> f3a4b5c6d7e2 and clean re-upgrade on disposable PG16 DB."""
    engine, async_url = pg16_urls
    mig_env = {
        **os.environ,
        "APTLYST_PROCESS_ROLE": "migration",
        "ENVIRONMENT": "test",
        "DATABASE_URL": async_url,
    }
    down_res = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "downgrade", "f3a4b5c6d7e2"],
        cwd=str(API_DIR),
        env=mig_env,
        capture_output=True,
        text=True,
    )
    assert down_res.returncode == 0, f"Downgrade failed:\n{down_res.stderr}"

    with engine.connect() as conn:
        exists_after_down = conn.execute(
            text("SELECT to_regclass('public.billing_worker_heartbeats')")
        ).scalar_one()
        assert exists_after_down is None

    up_res = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
        cwd=str(API_DIR),
        env=mig_env,
        capture_output=True,
        text=True,
    )
    assert up_res.returncode == 0, f"Re-upgrade failed:\n{up_res.stderr}"

    with engine.connect() as conn:
        exists_after_up = conn.execute(
            text("SELECT to_regclass('public.billing_worker_heartbeats')")
        ).scalar_one()
        assert exists_after_up == "billing_worker_heartbeats"
