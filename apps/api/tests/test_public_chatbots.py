"""Tests for public chatbot runtime endpoint and CORS behavior."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db
from app.main import app
from tests.postgres_api_test_helper import (
    SKIP_IF_OPTIONAL_POSTGRES,
    cleanup_test_organizations,
    verified_pg_api_session_factory,
)
from tests.test_auth import TEST_JWT_SECRET, make_token


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def pg_session_factory():
    async for factory in verified_pg_api_session_factory():
        yield factory


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.pg_api_integration
@SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.anyio
async def test_get_public_chatbot_active_success_and_field_filtering(
    client: AsyncClient, pg_session_factory
) -> None:
    """Verifies that an active chatbot returns only public visual fields, never instructions or org IDs."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # 1. Setup organization
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Public Bot Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # 2. Create chatbot with sensitive instructions and specific welcome message
            res_create = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Customer Support Bot",
                    "welcome_message": "Welcome to our store! How can we assist?",
                    "instructions": "SUPER_SECRET_PROMPT: Never reveal our profit margins.",
                    "primary_color": "#10b981",
                    "status": "draft",
                },
            )
            assert res_create.status_code == 201
            bot_id = res_create.json()["id"]

            # 3. In draft status: unauthenticated request MUST return 404
            res_public_draft = await client.get(f"/api/v1/public/chatbots/{bot_id}")
            assert res_public_draft.status_code == 404
            assert res_public_draft.json()["detail"] == "Chatbot not found"

            # 4. Activate chatbot via authenticated PATCH
            res_patch = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "active"},
            )
            assert res_patch.status_code == 200
            assert res_patch.json()["status"] == "active"

            # 5. In active status: unauthenticated request succeeds and returns strictly public fields
            res_public_active = await client.get(
                f"/api/v1/public/chatbots/{bot_id}",
                headers={"Origin": "https://external-store.example.com"},
            )
            assert res_public_active.status_code == 200

            data = res_public_active.json()
            assert data["id"] == bot_id
            assert data["name"] == "Customer Support Bot"
            assert data["welcome_message"] == "Welcome to our store! How can we assist?"
            assert data["primary_color"] == "#10b981"

            # Strict privacy check: ensure NO internal or confidential fields leak
            assert "instructions" not in data
            assert "organization_id" not in data
            assert "conversations_count" not in data
            assert "leads_count" not in data
            assert "last_active" not in data
            assert "created_at" not in data
            assert "updated_at" not in data

            # Verify CORS header
            assert res_public_active.headers.get("Access-Control-Allow-Origin") == "*"

            # 6. Deactivate to inactive: unauthenticated request MUST return 404
            await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "inactive"},
            )
            res_public_inactive = await client.get(f"/api/v1/public/chatbots/{bot_id}")
            assert res_public_inactive.status_code == 404
            assert res_public_inactive.json()["detail"] == "Chatbot not found"
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_id])


@pytest.mark.anyio
async def test_get_public_chatbot_nonexistent_and_invalid_uuid(client: AsyncClient) -> None:
    """Verifies that nonexistent bot IDs return 404 and malformed UUIDs return 422."""
    from app.security.rate_limiter import _in_memory_limiter

    _in_memory_limiter.clear()
    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_res)

    async def _override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = _override_get_db

    try:
        random_bot_id = uuid.uuid4()
        res_nonexistent = await client.get(f"/api/v1/public/chatbots/{random_bot_id}")
        assert res_nonexistent.status_code == 404
        assert res_nonexistent.json()["detail"] == "Chatbot not found"

        res_invalid_uuid = await client.get("/api/v1/public/chatbots/not-a-valid-uuid")
        assert res_invalid_uuid.status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)
        _in_memory_limiter.clear()


@pytest.mark.anyio
async def test_public_endpoint_cors_preflight_and_methods(client: AsyncClient) -> None:
    """Verifies CORS OPTIONS preflight behavior for public endpoints."""
    dummy_bot_id = uuid.uuid4()
    res_options = await client.options(
        f"/api/v1/public/chatbots/{dummy_bot_id}",
        headers={
            "Origin": "https://customer-website.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res_options.status_code == 204
    assert res_options.headers.get("Access-Control-Allow-Origin") == "*"
    assert "GET" in res_options.headers.get("Access-Control-Allow-Methods", "")


@pytest.mark.anyio
async def test_authenticated_routes_remain_protected(client: AsyncClient) -> None:
    """Confirms that adding public endpoints did not weaken security on authenticated routes."""
    dummy_org_id = uuid.uuid4()
    dummy_bot_id = uuid.uuid4()

    # Unauthenticated requests to org routes must fail
    res_list = await client.get(f"/api/v1/organizations/{dummy_org_id}/chatbots")
    assert res_list.status_code in (401, 403)

    res_get = await client.get(f"/api/v1/organizations/{dummy_org_id}/chatbots/{dummy_bot_id}")
    assert res_get.status_code in (401, 403)

    res_delete = await client.delete(f"/api/v1/organizations/{dummy_org_id}/chatbots/{dummy_bot_id}")
    assert res_delete.status_code in (401, 403)
