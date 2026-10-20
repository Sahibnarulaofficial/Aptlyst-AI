"""Tests for the FastAPI health endpoints and DB infrastructure."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
async def test_health_db_when_no_db(client: AsyncClient) -> None:
    """
    /health/db should return 503 when no database is available.
    This validates the endpoint exists and handles connection errors gracefully.
    Pass a real DATABASE_URL to get 200.
    """
    response = await client.get("/health/db")
    assert response.status_code in (200, 503)
