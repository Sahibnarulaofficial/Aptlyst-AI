"""Tests for the FastAPI health endpoints and DB infrastructure."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

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
    app.dependency_overrides[get_db] = lambda: mock_session
    try:
        response = await client.get("/health/db")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_health_db_when_no_db(client: AsyncClient) -> None:
    """
    /health/db should return 503 when no database is available or connection fails.
    Validates the endpoint handles connection errors gracefully without crashing.
    """
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=ConnectionRefusedError("Simulated DB connection failure"))
    app.dependency_overrides[get_db] = lambda: mock_session
    try:
        response = await client.get("/health/db")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "error"
        assert "Simulated DB connection failure" in data["database"]
    finally:
        app.dependency_overrides.pop(get_db, None)
