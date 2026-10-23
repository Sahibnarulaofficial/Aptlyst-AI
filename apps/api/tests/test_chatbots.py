"""Tests for chatbot management, validation, role authorization, and tenant isolation."""

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

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

    # Unauthenticated PATCH -> 403 Forbidden
    res_no_auth_patch = await client.patch(
        f"/api/v1/organizations/{random_org}/chatbots/{uuid.uuid4()}",
        json={"name": "New Name"},
    )
    assert res_no_auth_patch.status_code == 403

    # Unauthenticated DELETE -> 403 Forbidden
    res_no_auth_del = await client.delete(
        f"/api/v1/organizations/{random_org}/chatbots/{uuid.uuid4()}",
    )
    assert res_no_auth_del.status_code == 403


@pytest.mark.anyio
async def test_update_chatbot_supported_fields_and_partial_updates(client: AsyncClient) -> None:
    """Verifies partial updates, updating each supported field, and explicit null vs omitted."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # 1. Setup organization and initial bot
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Update Test Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            res_create = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Original Name",
                    "description": "Original Description",
                    "website_url": "https://original.com",
                    "welcome_message": "Original Welcome",
                    "instructions": "Original Instructions",
                    "primary_color": "#112233",
                    "status": "draft",
                },
            )
            assert res_create.status_code == 201
            bot_id = res_create.json()["id"]

            # 2. Partial update: only name
            res_patch_name = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Renamed Bot"},
            )
            assert res_patch_name.status_code == 200
            data = res_patch_name.json()
            assert data["name"] == "Renamed Bot"
            assert data["description"] == "Original Description"
            assert data["website_url"] == "https://original.com"
            assert data["status"] == "draft"

            # 3. Partial update: status to active
            res_patch_status = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "active"},
            )
            assert res_patch_status.status_code == 200
            assert res_patch_status.json()["status"] == "active"
            assert res_patch_status.json()["name"] == "Renamed Bot"

            # 4. Partial update: status to inactive
            res_patch_inactive = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "inactive"},
            )
            assert res_patch_inactive.status_code == 200
            assert res_patch_inactive.json()["status"] == "inactive"

            # 5. Update remaining fields: description, instructions, welcome_message, primary_color, website_url
            res_patch_all = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "description": "Updated Description",
                    "website_url": "https://updated.com",
                    "welcome_message": "Updated Welcome",
                    "instructions": "Updated Instructions",
                    "primary_color": "#445566",
                },
            )
            assert res_patch_all.status_code == 200
            all_data = res_patch_all.json()
            assert all_data["description"] == "Updated Description"
            assert all_data["website_url"] == "https://updated.com"
            assert all_data["welcome_message"] == "Updated Welcome"
            assert all_data["instructions"] == "Updated Instructions"
            assert all_data["primary_color"] == "#445566"
            assert all_data["status"] == "inactive"  # omitted, preserved

            # 6. Explicit null resets nullable fields in database
            res_patch_nulls = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "description": None,
                    "website_url": None,
                    "welcome_message": None,
                    "instructions": None,
                },
            )
            assert res_patch_nulls.status_code == 200
            null_data = res_patch_nulls.json()
            assert null_data["description"] is None
            assert null_data["website_url"] is None
            assert null_data["welcome_message"] is None
            assert null_data["instructions"] is None
            assert null_data["name"] == "Renamed Bot"  # omitted, preserved

            # 7. Empty update body rejected with 400
            res_empty_patch = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={},
            )
            assert res_empty_patch.status_code == 400
            assert res_empty_patch.json()["detail"] == "No fields provided for update"
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_update_chatbot_validation_rules(client: AsyncClient) -> None:
    """Verifies that invalid updates (empty name, bad URL, bad hex, bad status, null name) are rejected with 422."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Validation Patch Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            res_bot = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Valid Bot"},
            )
            assert res_bot.status_code == 201
            bot_id = res_bot.json()["id"]

            # Empty name
            res_bad_name = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "   "},
            )
            assert res_bad_name.status_code == 422

            # Null name
            res_null_name = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": None},
            )
            assert res_null_name.status_code == 422

            # Bad URL
            res_bad_url = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"website_url": "javascript:alert(1)"},
            )
            assert res_bad_url.status_code == 422

            # Bad hex color
            res_bad_color = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"primary_color": "blue"},
            )
            assert res_bad_color.status_code == 422

            # Invalid status value
            res_bad_status = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "deleted"},
            )
            assert res_bad_status.status_code == 422

            # Null status
            res_null_status = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": None},
            )
            assert res_null_status.status_code == 422
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_update_chatbot_isolation_and_authorization(client: AsyncClient) -> None:
    """Verifies that non-existent bot returns 404, cross-org access returns 404, and members/admins/owners can update."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    token_a = make_token(user_id=str(user_a))
    token_b = make_token(user_id=str(user_b))
    org_a_id = None
    org_b_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Org A & Bot A
            res_org_a = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Org A Update"},
            )
            assert res_org_a.status_code == 201
            org_a_id = uuid.UUID(res_org_a.json()["id"])

            res_bot_a = await client.post(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Bot A"},
            )
            assert res_bot_a.status_code == 201
            bot_a_id = res_bot_a.json()["id"]

            # 1. Non-existent chatbot in Org A -> 404
            random_bot = uuid.uuid4()
            res_404 = await client.patch(
                f"/api/v1/organizations/{org_a_id}/chatbots/{random_bot}",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Nonexistent"},
            )
            assert res_404.status_code == 404
            assert res_404.json()["detail"] == "Not found"

            # 2. User B (not in Org A) attempts to update Bot A -> 404
            res_b_tamper = await client.patch(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_a_id}",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Attacker Name"},
            )
            assert res_b_tamper.status_code == 404
            assert res_b_tamper.json()["detail"] == "Not found"

            # 3. User B in Org B attempts cross-org tampering with Org B path and Bot A ID -> 404
            res_org_b = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Org B Update"},
            )
            assert res_org_b.status_code == 201
            org_b_id = uuid.UUID(res_org_b.json()["id"])

            res_cross_tamper = await client.patch(
                f"/api/v1/organizations/{org_b_id}/chatbots/{bot_a_id}",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Cross Tamper"},
            )
            assert res_cross_tamper.status_code == 404
            assert res_cross_tamper.json()["detail"] == "Not found"

            # 4. Member role can update: Add user_b to Org A as MemberRole.member
            async with AsyncSessionLocal() as session:
                session.add(
                    OrganizationMember(
                        organization_id=org_a_id,
                        user_id=user_b,
                        role=MemberRole.member,
                    )
                )
                await session.commit()

            res_member_update = await client.patch(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_a_id}",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Updated By Member"},
            )
            assert res_member_update.status_code == 200
            assert res_member_update.json()["name"] == "Updated By Member"
    finally:
        async with AsyncSessionLocal() as session:
            if org_a_id:
                await session.execute(delete(Organization).where(Organization.id == org_a_id))
            if org_b_id:
                await session.execute(delete(Organization).where(Organization.id == org_b_id))
            await session.commit()


@pytest.mark.anyio
async def test_chatbot_color_validation_consistency_and_editing_regression(
    client: AsyncClient,
) -> None:
    """Verifies color validation consistency between create and update, ensuring valid

    chatbots can be edited when changing unrelated fields and submitting color.
    """
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Color Regression Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # 1. Create with 4-character hex shorthand (#123)
            res_shorthand = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Shorthand Hex Bot", "primary_color": "#123"},
            )
            assert res_shorthand.status_code == 201
            bot_shorthand_id = res_shorthand.json()["id"]
            assert res_shorthand.json()["primary_color"] == "#123"

            # 2. Edit unrelated field (description) while form resubmits the existing primary_color="#123"
            res_edit_unrelated = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_shorthand_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Shorthand Hex Bot",
                    "description": "Updated Description Only",
                    "primary_color": "#123",
                },
            )
            assert res_edit_unrelated.status_code == 200
            assert res_edit_unrelated.json()["description"] == "Updated Description Only"
            assert res_edit_unrelated.json()["primary_color"] == "#123"

            # 3. Edit omitting primary_color keeps existing primary_color
            res_omit_color = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_shorthand_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Renamed Shorthand Bot"},
            )
            assert res_omit_color.status_code == 200
            assert res_omit_color.json()["name"] == "Renamed Shorthand Bot"
            assert res_omit_color.json()["primary_color"] == "#123"

            # 4. Update with null or empty string resets to default "#3b82f6" (consistent with create)
            res_null_color = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_shorthand_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"primary_color": None},
            )
            assert res_null_color.status_code == 200
            assert res_null_color.json()["primary_color"] == "#3b82f6"

            res_empty_color = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_shorthand_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"primary_color": "   "},
            )
            assert res_empty_color.status_code == 200
            assert res_empty_color.json()["primary_color"] == "#3b82f6"
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_delete_chatbot_successful_and_cross_tenant_isolation(
    client: AsyncClient,
) -> None:
    """Verifies successful 204 deletion, 404 for deleted or nonexistent bot, and cross-tenant isolation."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    token_a = make_token(user_id=str(user_a))
    token_b = make_token(user_id=str(user_b))
    org_a_id = None
    org_b_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Setup Org A with two bots
            res_org_a = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Org A Delete"},
            )
            assert res_org_a.status_code == 201
            org_a_id = uuid.UUID(res_org_a.json()["id"])

            res_bot_1 = await client.post(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Bot 1"},
            )
            assert res_bot_1.status_code == 201
            bot_1_id = res_bot_1.json()["id"]

            res_bot_2 = await client.post(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Bot 2"},
            )
            assert res_bot_2.status_code == 201
            bot_2_id = res_bot_2.json()["id"]

            # 1. Delete Bot 1 -> 204 No Content
            res_del_1 = await client.delete(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_1_id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_del_1.status_code == 204
            assert res_del_1.content == b""

            # 2. GET on deleted Bot 1 -> 404 Not Found
            res_get_del = await client.get(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_1_id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_get_del.status_code == 404
            assert res_get_del.json()["detail"] == "Not found"

            # 3. Listing chatbots in Org A only returns Bot 2
            res_list = await client.get(
                f"/api/v1/organizations/{org_a_id}/chatbots",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_list.status_code == 200
            bots = res_list.json()
            assert len(bots) == 1
            assert bots[0]["id"] == bot_2_id

            # 4. Subsequent deletion of Bot 1 -> 404 Not Found
            res_del_again = await client.delete(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_1_id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_del_again.status_code == 404

            # 5. Delete nonexistent random bot -> 404 Not Found
            random_bot = uuid.uuid4()
            res_del_random = await client.delete(
                f"/api/v1/organizations/{org_a_id}/chatbots/{random_bot}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_del_random.status_code == 404

            # 6. Cross-tenant isolation: Setup Org B for User B
            res_org_b = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Org B Delete"},
            )
            assert res_org_b.status_code == 201
            org_b_id = uuid.UUID(res_org_b.json()["id"])

            # User B attempts to delete Bot 2 in Org A using Org A path -> 404
            res_cross_a = await client.delete(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_2_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert res_cross_a.status_code == 404

            # User B attempts to delete Bot 2 using Org B path -> 404 (scoped to org)
            res_cross_b = await client.delete(
                f"/api/v1/organizations/{org_b_id}/chatbots/{bot_2_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert res_cross_b.status_code == 404

            # Verify Bot 2 still exists and is untouched in Org A
            res_bot_2_check = await client.get(
                f"/api/v1/organizations/{org_a_id}/chatbots/{bot_2_id}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_bot_2_check.status_code == 200
            assert res_bot_2_check.json()["name"] == "Bot 2"
    finally:
        async with AsyncSessionLocal() as session:
            if org_a_id:
                await session.execute(delete(Organization).where(Organization.id == org_a_id))
            if org_b_id:
                await session.execute(delete(Organization).where(Organization.id == org_b_id))
            await session.commit()


@pytest.mark.anyio
async def test_delete_chatbot_roles_and_transaction_safety(
    client: AsyncClient,
) -> None:
    """Verifies role authorization (member, admin, owner can delete; outsider rejected) and transaction safety."""
    user_owner = uuid.uuid4()
    user_admin = uuid.uuid4()
    user_member = uuid.uuid4()
    user_outsider = uuid.uuid4()

    token_owner = make_token(user_id=str(user_owner))
    token_admin = make_token(user_id=str(user_admin))
    token_member = make_token(user_id=str(user_member))
    token_outsider = make_token(user_id=str(user_outsider))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Setup Org
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_owner}"},
                json={"name": "Role Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # Add admin and member
            async with AsyncSessionLocal() as session:
                session.add(
                    OrganizationMember(
                        organization_id=org_id,
                        user_id=user_admin,
                        role=MemberRole.admin,
                    )
                )
                session.add(
                    OrganizationMember(
                        organization_id=org_id,
                        user_id=user_member,
                        role=MemberRole.member,
                    )
                )
                await session.commit()

            # Create 4 bots
            bot_ids = []
            for i in range(1, 5):
                res_create = await client.post(
                    f"/api/v1/organizations/{org_id}/chatbots",
                    headers={"Authorization": f"Bearer {token_owner}"},
                    json={"name": f"Bot {i}"},
                )
                assert res_create.status_code == 201
                bot_ids.append(res_create.json()["id"])

            # 1. Member deletes Bot 1 -> 204
            res_del_member = await client.delete(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_ids[0]}",
                headers={"Authorization": f"Bearer {token_member}"},
            )
            assert res_del_member.status_code == 204

            # 2. Admin deletes Bot 2 -> 204
            res_del_admin = await client.delete(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_ids[1]}",
                headers={"Authorization": f"Bearer {token_admin}"},
            )
            assert res_del_admin.status_code == 204

            # 3. Owner deletes Bot 3 -> 204
            res_del_owner = await client.delete(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_ids[2]}",
                headers={"Authorization": f"Bearer {token_owner}"},
            )
            assert res_del_owner.status_code == 204

            # 4. Outsider attempts to delete Bot 4 -> 404 Not Found
            res_del_outsider = await client.delete(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_ids[3]}",
                headers={"Authorization": f"Bearer {token_outsider}"},
            )
            assert res_del_outsider.status_code == 404

            # Bot 4 still exists
            res_get_4 = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_ids[3]}",
                headers={"Authorization": f"Bearer {token_owner}"},
            )
            assert res_get_4.status_code == 200

            # 5. Schema check: deleting chatbots leaves organization and members intact
            async with AsyncSessionLocal() as session:
                org_check = await session.get(Organization, org_id)
                assert org_check is not None
                members_result = await session.execute(
                    select(OrganizationMember).where(OrganizationMember.organization_id == org_id)
                )
                assert len(members_result.scalars().all()) == 3
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()
