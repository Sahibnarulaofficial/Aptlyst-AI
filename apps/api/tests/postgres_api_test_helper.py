"""Shared PostgreSQL 16 test guard, session isolation, and tenant cleanup helpers for Stage 2K-D2."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import configure_mappers

from app.db.session import get_db
from app.main import app
from app.models import register_all_models
from app.models.billing import LLMUsageLog, OrganizationBillingPeriod, UsageReservation
from app.models.generation import GenerationTask
from app.models.generation_attempt import GenerationTaskAttempt
from app.models.organization import Organization
from app.security.rate_limiter import _in_memory_limiter
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    _OptionalPostgresUnreachableError,
    assert_async_session_local_isolated,
    create_verified_async_session_factory,
    is_postgres_tests_required,
    resolve_verified_async_pg_engine,
    validate_postgres_test_url,
)

register_all_models()
configure_mappers()

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)


async def cleanup_test_organizations(
    session_factory: Callable[[], AsyncSession],
    organization_ids: Sequence[uuid.UUID | None],
) -> None:
    """Delete test organizations and dependent billing/generation rows in reverse FK order.

    All deletes are strictly scoped to ``organization_ids``. Safe if setup failed before
    creating all rows or if ``organization_ids`` is empty.
    """
    valid_ids = list(dict.fromkeys(oid for oid in organization_ids if oid is not None))
    if not valid_ids:
        return

    async with session_factory() as session:
        task_ids_subq = select(GenerationTask.id).where(
            GenerationTask.organization_id.in_(valid_ids)
        )
        await session.execute(
            delete(GenerationTaskAttempt).where(
                GenerationTaskAttempt.generation_task_id.in_(task_ids_subq)
            )
        )
        await session.execute(
            delete(LLMUsageLog).where(LLMUsageLog.organization_id.in_(valid_ids))
        )
        await session.execute(
            delete(UsageReservation).where(UsageReservation.organization_id.in_(valid_ids))
        )
        await session.execute(
            delete(GenerationTask).where(GenerationTask.organization_id.in_(valid_ids))
        )
        await session.execute(
            delete(OrganizationBillingPeriod).where(
                OrganizationBillingPeriod.organization_id.in_(valid_ids)
            )
        )
        await session.execute(
            delete(Organization).where(Organization.id.in_(valid_ids))
        )
        await session.commit()


async def verified_pg_api_session_factory() -> AsyncGenerator[Callable[[], AsyncSession], None]:
    """Resolve an isolated PG16 AsyncSession factory and bind FastAPI ``get_db`` to it."""
    raw_url = os.environ.get("POSTGRES_TEST_URL")
    required = is_postgres_tests_required()

    try:
        engine, _summary, _ = await resolve_verified_async_pg_engine()
    except _OptionalPostgresUnreachableError as exc:
        pytest.skip(str(exc))
    except PostgresTestConfigError as exc:
        pytest.fail(str(exc), pytrace=False)

    if engine is None or not (raw_url or "").strip():
        if required:
            pytest.fail(
                "REQUIRE_POSTGRES_TESTS=1 is enabled, but verified PostgreSQL 16 engine is unavailable.",
                pytrace=False,
            )
        pytest.skip("Disposable PostgreSQL 16 database not available.")

    target = validate_postgres_test_url(raw_url, require_postgres=required)
    assert target is not None
    if required:
        assert_async_session_local_isolated(target)

    session_factory = create_verified_async_session_factory(engine)
    previous_override = app.dependency_overrides.get(get_db)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    _in_memory_limiter.clear()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield session_factory
    finally:
        if previous_override is not None:
            app.dependency_overrides[get_db] = previous_override
        else:
            app.dependency_overrides.pop(get_db, None)
        _in_memory_limiter.clear()
        await engine.dispose()
