"""PostgreSQL 16 integration tests for Stage 2K-C:
1. Real PG16 heartbeat ownership loss halts worker loop without overwriting replacement heartbeat.
2. Real PG16 bounded heartbeat retention cleanup preserves active/recent rows and is idempotent.
3. Real PG16 concurrent cleanup via FOR UPDATE SKIP LOCKED + read-only health/alert evaluation.

Safety:
- Uses tests.postgres_test_guard (never reads `.env` or production DATABASE_URL).
- Skips locally only when REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unavailable.
- Fails closed when REQUIRE_POSTGRES_TESTS=1.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
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

from app.models import register_all_models
from app.models.payment import BillingWorkerHeartbeat
from app.services.billing.razorpay.monitoring import (
    WORKER_TYPE_WEBHOOK,
    WorkerHeartbeatTracker,
    cleanup_stale_worker_heartbeats,
    collect_billing_health_snapshot,
)
from app.workers.razorpay_webhook_worker import RazorpayWebhookWorker
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    _OptionalPostgresUnreachableError,
    is_postgres_tests_required,
    resolve_verified_pg_engine,
)

register_all_models()

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
@pytest.mark.asyncio
async def test_pg16_stage2kc_01_ownership_loss_halts_worker_and_preserves_replacement(
    pg16_urls,
) -> None:
    """Verify real PG16 fencing triggers worker halt and protects replacement heartbeat."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    shared_worker_id = f"pg16_2kc_shared_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    replacement = WorkerHeartbeatTracker(
        worker_id=shared_worker_id,
        worker_type=WORKER_TYPE_WEBHOOK,
        deployment_environment="staging",
        environment="test",
        merchant_account_id="acc_test_pg16",
        stale_takeover_seconds=90,
        started_at=now,
    )
    stale_worker = RazorpayWebhookWorker(
        session_factory=session_factory,
        worker_id=shared_worker_id,
        environment="test",
        poll_interval=0.05,
        clock=lambda: now,
    )

    try:
        # Replacement holds an active unexpired heartbeat row
        assert (
            await replacement.record_heartbeat(
                session_factory,
                status="running",
                now=now,
                force=True,
                processed_delta=3,
            )
            is True
        )

        # Superseded worker starts against the same worker_id: startup heartbeat is fenced out,
        # ownership_lost becomes True, worker stops immediately without writing "stopped".
        await stale_worker.start()
        assert stale_worker.heartbeat_tracker.ownership_lost is True

        async with session_factory() as db:
            row = await db.get(BillingWorkerHeartbeat, shared_worker_id)
            assert row is not None
            assert row.instance_token == replacement.instance_token
            assert row.status == "running"
            assert row.processed_count == 3
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM billing_worker_heartbeats WHERE worker_id = :wid"),
                {"wid": shared_worker_id},
            )
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2kc_02_heartbeat_retention_preserves_active_and_recent_rows(
    pg16_urls,
) -> None:
    """Verify bounded PG16 cleanup deletes only expired rows, preserves active/recent rows, and is idempotent."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    prefix = f"pg16_ret_{uuid.uuid4().hex[:8]}"
    ids = {
        "expired_stopped": f"{prefix}_exp_stopped",
        "recent_stopped": f"{prefix}_rec_stopped",
        "expired_abandoned": f"{prefix}_exp_abandoned",
        "recent_abandoned": f"{prefix}_rec_abandoned",
        "active_running": f"{prefix}_active",
        "self_old": f"{prefix}_self_old",
    }

    rows = [
        BillingWorkerHeartbeat(
            worker_id=ids["expired_stopped"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="stopped",
            started_at=now - timedelta(days=10),
            last_heartbeat_at=now - timedelta(days=8),
        ),
        BillingWorkerHeartbeat(
            worker_id=ids["recent_stopped"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="stopped",
            started_at=now - timedelta(days=3),
            last_heartbeat_at=now - timedelta(days=2),
        ),
        BillingWorkerHeartbeat(
            worker_id=ids["expired_abandoned"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="error",
            started_at=now - timedelta(days=40),
            last_heartbeat_at=now - timedelta(days=35),
        ),
        BillingWorkerHeartbeat(
            worker_id=ids["recent_abandoned"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="running",
            started_at=now - timedelta(days=12),
            last_heartbeat_at=now - timedelta(days=10),
        ),
        BillingWorkerHeartbeat(
            worker_id=ids["active_running"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="running",
            started_at=now - timedelta(minutes=5),
            last_heartbeat_at=now - timedelta(seconds=10),
        ),
        BillingWorkerHeartbeat(
            worker_id=ids["self_old"],
            instance_token=uuid.uuid4(),
            worker_type=WORKER_TYPE_WEBHOOK,
            deployment_environment="staging",
            environment="test",
            merchant_account_id="acc_test_pg16",
            status="stopped",
            started_at=now - timedelta(days=15),
            last_heartbeat_at=now - timedelta(days=14),
        ),
    ]

    try:
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

        async with session_factory() as db:
            deleted = await cleanup_stale_worker_heartbeats(
                db,
                now=now,
                exclude_worker_id=ids["self_old"],
            )
            assert deleted == 2

            # Idempotent repeated run deletes 0 additional rows
            deleted_again = await cleanup_stale_worker_heartbeats(
                db,
                now=now,
                exclude_worker_id=ids["self_old"],
            )
            assert deleted_again == 0

            remaining = {
                wid
                for wid, _ in [
                    (wid, await db.get(BillingWorkerHeartbeat, wid))
                    for wid in ids.values()
                ]
                if _ is not None
            }
            assert remaining == {
                ids["recent_stopped"],
                ids["recent_abandoned"],
                ids["active_running"],
                ids["self_old"],
            }
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM billing_worker_heartbeats WHERE worker_id = ANY(:wids)"),
                {"wids": list(ids.values())},
            )
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2kc_03_concurrent_cleanup_and_monitoring_snapshot(
    pg16_urls,
) -> None:
    """Verify concurrent PG16 cleanup (SKIP LOCKED) and read-only health alert snapshot."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    prefix = f"pg16_cc_{uuid.uuid4().hex[:8]}"
    old_ids = [f"{prefix}_old_{i}" for i in range(6)]
    active_id = f"{prefix}_active"

    try:
        async with session_factory() as db:
            for wid in old_ids:
                db.add(
                    BillingWorkerHeartbeat(
                        worker_id=wid,
                        instance_token=uuid.uuid4(),
                        worker_type=WORKER_TYPE_WEBHOOK,
                        deployment_environment="staging",
                        environment="test",
                        merchant_account_id="acc_test_pg16",
                        status="stopped",
                        started_at=now - timedelta(days=12),
                        last_heartbeat_at=now - timedelta(days=9),
                    )
                )
            db.add(
                BillingWorkerHeartbeat(
                    worker_id=active_id,
                    instance_token=uuid.uuid4(),
                    worker_type=WORKER_TYPE_WEBHOOK,
                    deployment_environment="staging",
                    environment="test",
                    merchant_account_id="acc_test_pg16",
                    status="running",
                    started_at=now - timedelta(minutes=1),
                    last_heartbeat_at=now - timedelta(seconds=5),
                )
            )
            await db.commit()

        async def _run_cleanup() -> int:
            async with session_factory() as db:
                return await cleanup_stale_worker_heartbeats(db, now=now, batch_size=3)

        counts = await asyncio.gather(_run_cleanup(), _run_cleanup())
        # Concurrent SKIP LOCKED batches partition expired rows cleanly without overlap
        assert sum(counts) <= 6
        assert sum(counts) >= 3

        # Clean up any remainder from batch_size=3 and confirm zero left + active intact
        async with session_factory() as db:
            await cleanup_stale_worker_heartbeats(db, now=now, batch_size=10)
            snap = await collect_billing_health_snapshot(
                db=db,
                environment="test",
                now=now,
                emit_alert_logs=False,
            )
            worker_ids = {w.worker_id for w in snap.workers}
            assert active_id in worker_ids
            assert not any(oid in worker_ids for oid in old_ids)
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM billing_worker_heartbeats WHERE worker_id = ANY(:wids)"),
                {"wids": [*old_ids, active_id]},
            )
            await db.commit()
        await async_engine.dispose()
