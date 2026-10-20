"""Tests for chatbot management, validation, role authorization, and tenant isolation."""

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import AsyncSessionLocal
from app.main import app
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.organization import MemberRole, Organization, OrganizationMember
from tests.test_auth import TEST_JWT_SECRET, make_token


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.anyio
async def test_create_and_list_chatbots_scoped_to_organization(client: AsyncClient) -> None:
    """Verifies that an organization member can create and list chatbots scoped to their org."""
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
                json={"name": "Chatbot Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # 2. List chatbots initially -> empty
            res_list_empty = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res_list_empty.status_code == 200
            assert res_list_empty.json() == []

            # 3. Create chatbot
            res_create = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Support Bot",
                    "description": "Helps customer support",
                    "website_url": "https://example.com",
                    "welcome_message": "Hello!",
                    "instructions": "Be friendly and brief.",
                    "primary_color": "#2563eb",
                    "status": "active",
                },
            )
            assert res_create.status_code == 201
            bot_data = res_create.json()
            assert bot_data["name"] == "Support Bot"
            assert bot_data["organization_id"] == str(org_id)
            assert bot_data["status"] == "active"
            assert bot_data["conversations_count"] == 0
            assert bot_data["leads_count"] == 0
            bot_id = bot_data["id"]

            # 4. List chatbots -> 1 item
            res_list = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res_list.status_code == 200
            bots = res_list.json()
            assert len(bots) == 1
            assert bots[0]["id"] == bot_id
            assert bots[0]["name"] == "Support Bot"

            # 5. Get by ID
            res_get = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res_get.status_code == 200
            assert res_get.json()["id"] == bot_id
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_chatbot_validation_rules(client: AsyncClient) -> None:
    """Verifies that invalid payloads (empty name, bad URL, bad hex) are rejected with 422."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Setup org
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Validation Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # Empty name (after strip)
            res_bad_name = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "   "},
            )
            assert res_bad_name.status_code == 422

            # Bad website URL
            res_bad_url = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Valid Name", "website_url": "ftp://not-allowed"},
            )
            assert res_bad_url.status_code == 422

            # Bad hex color
            res_bad_color = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Valid Name", "primary_color": "not-a-color"},
            )
            assert res_bad_color.status_code == 422
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_chatbot_cross_tenant_isolation(client: AsyncClient) -> None:
    """Verifies tenant isolation: User B cannot list, create, or read chatbots in User A's org."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    token_a = make_token(user_id=str(user_a))
    token_b = make_token(user_id=str(user_b))
    org_a_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # User A sets up org and bot
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Org A"},
            )
            assert res_org.status_code == 201
            org_a_id = uuid.UUID(res_org.json()["id"])

            res_bot = await client.post(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Org A Bot"},
            )
            assert res_bot.status_code == 201
            bot_a_id = res_bot.json()["id"]

            # User B attempts to list Org A's bots -> 404
            res_b_list = await client.get(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert res_b_list.status_code == 404
            assert res_b_list.json()["detail"] == "Not found"

            # User B attempts to read Bot A -> 404
            res_b_get = await client.get(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_a_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert res_b_get.status_code == 404
            assert res_b_get.json()["detail"] == "Not found"

            # User B attempts to create bot in Org A -> 404
            res_b_create = await client.post(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Infiltrator Bot"},
            )
            assert res_b_create.status_code == 404
            assert res_b_create.json()["detail"] == "Not found"
    finally:
        if org_a_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_a_id))
                await session.commit()


@pytest.mark.anyio
async def test_chatbot_cross_org_id_tampering(client: AsyncClient) -> None:
    """Verifies that requesting a Bot from Org A through Org B URL path returns 404 even if user belongs to Org B."""
    user = uuid.uuid4()
    token = make_token(user_id=str(user))
    org_1_id = None
    org_2_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Setup Org 1
            res_org1 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Org 1"},
            )
            assert res_org1.status_code == 201
            org_1_id = uuid.UUID(res_org1.json()["id"])

            # Setup Bot 1 in Org 1
            res_bot1 = await client.post(
                f"/api/v1/organizations/{org_1_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Org 1 Bot"},
            )
            bot_1_id = res_bot1.json()["id"]

            # Directly create Org 2 and add user as member to simulate belonging to two orgs
            async with AsyncSessionLocal() as session:
                org2 = Organization(name="Org 2")
                session.add(org2)
                await session.flush()
                org_2_id = org2.id

                session.add(
                    OrganizationMember(
                        organization_id=org_2_id,
                        user_id=user,
                        role=MemberRole.admin,
                    )
                )
                await session.commit()

            # Attempt to get Bot 1 using Org 2 path -> Must return 404
            res_tamper = await client.get(
                f"/api/v1/organizations/{org_2_id}/chatbots/{bot_1_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res_tamper.status_code == 404
            assert res_tamper.json()["detail"] == "Not found"
    finally:
        async with AsyncSessionLocal() as session:
            if org_1_id:
                await session.execute(delete(Organization).where(Organization.id == org_1_id))
            if org_2_id:
                await session.execute(delete(Organization).where(Organization.id == org_2_id))
            await session.commit()


@pytest.mark.anyio
async def test_chatbot_unauthenticated_requests_rejected(client: AsyncClient) -> None:
    """Verifies that missing or invalid authorization tokens are rejected with 401/403."""
    random_org = uuid.uuid4()

    # Missing token -> 403 Forbidden (FastAPI HTTPBearer)
    res_no_auth = await client.get(f"/api/v1/organizations/{random_org}/chatbots")
    assert res_no_auth.status_code == 403

    # Invalid token -> 401 Unauthorized
    res_bad_token = await client.get(
        f"/api/v1/organizations/{random_org}/chatbots",
        headers={"Authorization": "Bearer invalid.token.payload"},
    )
    assert res_bad_token.status_code == 401
