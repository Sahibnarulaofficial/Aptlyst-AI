"""Tests for the FastAPI health endpoints and DB infrastructure."""

import logging
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from app.db.session import get_db
from app.main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:  # type: ignore[misc]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.anyio
async def test_app_starts(client: AsyncClient) -> None:
    """Verify the FastAPI application is running and /health responds."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_health_db_connected(client: AsyncClient) -> None:
    """Verify /health/db returns 200 when database connectivity succeeds."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    async def _override_db():
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    try:
        response = await client.get("/health/db")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_health_db_when_no_db(client: AsyncClient) -> None:
    """/health/db should return stable generic 503 JSON without raw exception text."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        side_effect=ConnectionRefusedError("Simulated DB connection failure")
    )

    async def _override_db():
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    try:
        response = await client.get("/health/db")
        assert response.status_code == 503
        data = response.json()
        assert data == {"status": "error", "database": "unavailable"}
        assert "Simulated DB connection failure" not in response.text
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_health_db_never_exposes_credentials_or_internal_hosts_and_logs_sanitized(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulated DB errors containing DSN credentials/hosts stay out of HTTP responses and logs scrub secrets."""
    raw_exc = RuntimeError(
        "SQLAlchemy OperationalError connecting to "
        "postgresql+asyncpg://postgres:SuperSecretPass99@internal-db.prod.local:5432/aptlyst_prod "
        "[parameters: ('rzp_live_secretvalue123',)]"
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=raw_exc)

    async def _override_db():
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with caplog.at_level(logging.WARNING, logger="app.api.health"):
            response = await client.get("/health/db")

        assert response.status_code == 503
        assert response.json() == {"status": "error", "database": "unavailable"}
        body = response.text
        for sensitive in (
            "SuperSecretPass99",
            "internal-db.prod.local",
            "postgresql+asyncpg://",
            "SQLAlchemy",
            "rzp_live_secretvalue123",
            "parameters",
        ):
            assert sensitive not in body

        assert any("Database health check failed (RuntimeError):" in rec.message for rec in caplog.records)
        joined_logs = "\n".join(rec.message for rec in caplog.records)
        assert "SuperSecretPass99" not in joined_logs
        assert "rzp_live_secretvalue123" not in joined_logs
        assert "[REDACTED]" in joined_logs
    finally:
        app.dependency_overrides.pop(get_db, None)
