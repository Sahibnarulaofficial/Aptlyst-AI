"""Tests for organization management, role authorization, tenant isolation, and concurrency."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.organization import MemberRole, OrganizationMember
from tests.postgres_api_test_helper import (
    SKIP_IF_OPTIONAL_POSTGRES,
    cleanup_test_organizations,
    verified_pg_api_session_factory,
)
from tests.test_auth import TEST_JWT_SECRET, make_token

pytestmark = [
    pytest.mark.pg_api_integration,
    SKIP_IF_OPTIONAL_POSTGRES,
    pytest.mark.anyio,
]


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


@pytest.mark.anyio
async def test_setup_organization_creates_org_and_owner(
    client: AsyncClient, pg_session_factory
) -> None:
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
            response = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Acme Corp"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Acme Corp"
        org_id = uuid.UUID(data["id"])

        # Verify DB records
        async with pg_session_factory() as session:
            member = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id == user_id,
                )
            )
            assert member is not None
            assert member.role == MemberRole.owner
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_id])


@pytest.mark.anyio
async def test_setup_organization_free_plan_limit_returns_403(
    client: AsyncClient, pg_session_factory
) -> None:
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
            r1 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "First Org"},
            )
            assert r1.status_code == 201
            org_id = uuid.UUID(r1.json()["id"])

            r2 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Second Org"},
            )
            assert r2.status_code == 403
            assert "Free plan is limited to 1 organization" in r2.json()["detail"]
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_id])


@pytest.mark.anyio
async def test_setup_organization_paid_plan_allows_multiple_orgs(
    client: AsyncClient, pg_session_factory
) -> None:
    """Paid users (e.g. Pro) can create multiple owned organizations."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id), app_metadata={"plan": "pro"})
    created_org_ids: list[uuid.UUID] = []

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            r1 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Paid Org One"},
            )
            assert r1.status_code == 201
            created_org_ids.append(uuid.UUID(r1.json()["id"]))

            r2 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Paid Org Two"},
            )
            assert r2.status_code == 201
            created_org_ids.append(uuid.UUID(r2.json()["id"]))

            r3 = await client.post(
                "/api/v1/organizations",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Paid Org Three"},
            )
            assert r3.status_code == 201
            created_org_ids.append(uuid.UUID(r3.json()["id"]))

        async with pg_session_factory() as session:
            members = (
                await session.scalars(
                    select(OrganizationMember).where(
                        OrganizationMember.user_id == user_id,
                        OrganizationMember.role == MemberRole.owner,
                    )
                )
            ).all()
            assert len(members) == 3
    finally:
        await cleanup_test_organizations(pg_session_factory, created_org_ids)


@pytest.mark.anyio
async def test_free_user_non_owner_memberships_do_not_count_towards_limit(
    client: AsyncClient, pg_session_factory
) -> None:
    """Non-owner memberships (admin, member) do not count toward the owned organization limit."""
    owner_user_id = uuid.uuid4()
    free_user_id = uuid.uuid4()
    owner_token = make_token(user_id=str(owner_user_id))
    free_token = make_token(user_id=str(free_user_id))
    created_org_ids: list[uuid.UUID] = []

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # 1. First user creates an org
            r_owner = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {owner_token}"},
                json={"name": "Owner Org"},
            )
            assert r_owner.status_code == 201
            org1_id = uuid.UUID(r_owner.json()["id"])
            created_org_ids.append(org1_id)

            # 2. Add free_user as a regular member to Org 1
            async with pg_session_factory() as session:
                session.add(
                    OrganizationMember(
                        organization_id=org1_id,
                        user_id=free_user_id,
                        role=MemberRole.member,
                    )
                )
                await session.commit()

            # 3. Free user creates their 1 allowed owned organization -> should succeed (201)
            r_free_1 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {free_token}"},
                json={"name": "Free User Org"},
            )
            assert r_free_1.status_code == 201
            created_org_ids.append(uuid.UUID(r_free_1.json()["id"]))

            # 4. Free user attempts second owned org -> must fail with 403
            r_free_2 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {free_token}"},
                json={"name": "Free User Org Two"},
            )
            assert r_free_2.status_code == 403
            assert "Free plan is limited to 1 organization" in r_free_2.json()["detail"]
    finally:
        await cleanup_test_organizations(pg_session_factory, created_org_ids)


@pytest.mark.anyio
async def test_frontend_payload_cannot_spoof_paid_plan(
    client: AsyncClient, pg_session_factory
) -> None:
    """Extra fields in the request body (e.g. plan='pro', is_paid=True) are ignored."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            r1 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Legit Org"},
            )
            assert r1.status_code == 201
            org_id = uuid.UUID(r1.json()["id"])

            # Attempt to bypass limit by sending spoofed plan fields in request payload
            r2 = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Spoofed Org",
                    "plan": "pro",
                    "is_paid": True,
                },
            )
            assert r2.status_code == 403
            assert "Free plan is limited to 1 organization" in r2.json()["detail"]
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_id])


@pytest.mark.anyio
async def test_setup_organization_concurrency_race_condition(
    client: AsyncClient, pg_session_factory
) -> None:
    """Tests that concurrent setup requests for the same user cannot create duplicate orgs."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    created_org_ids: list[uuid.UUID] = []

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Send two setup requests concurrently for the exact same user
            async def run_setup(name: str):
                return await client.post(
                    "/api/v1/organizations/setup",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"name": name},
                )

            res1, res2 = await asyncio.gather(
                run_setup("Concurrent Org A"),
                run_setup("Concurrent Org B"),
            )

        for r in (res1, res2):
            if r.status_code == 201:
                created_org_ids.append(uuid.UUID(r.json()["id"]))

        status_codes = [res1.status_code, res2.status_code]
        assert 201 in status_codes, f"Expected at least one 201, got {status_codes}"
        assert 403 in status_codes, f"Expected at least one 403, got {status_codes}"

        # Verify only 1 org owned in database
        async with pg_session_factory() as session:
            members = (
                await session.scalars(
                    select(OrganizationMember).where(
                        OrganizationMember.user_id == user_id,
                        OrganizationMember.role == MemberRole.owner,
                    )
                )
            ).all()
            assert len(members) == 1
    finally:
        await cleanup_test_organizations(pg_session_factory, created_org_ids)


@pytest.mark.anyio
async def test_organization_tenant_isolation(
    client: AsyncClient, pg_session_factory
) -> None:
    """Verifies that User B cannot list or get User A's organization."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    token_a = make_token(user_id=str(user_a))
    token_b = make_token(user_id=str(user_b))
    org_a_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # User A sets up org
            res = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "Org A"},
            )
            assert res.status_code == 201
            org_a_id = uuid.UUID(res.json()["id"])

            # User B lists organizations: must be empty
            list_res = await client.get(
                "/api/v1/organizations",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert list_res.status_code == 200
            assert list_res.json() == []

            # User B tries to get Org A: must return 404
            get_res = await client.get(
                f"/api/v1/organizations/{org_a_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert get_res.status_code == 404
            assert get_res.json()["detail"] == "Not found"
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_a_id])


@pytest.mark.anyio
async def test_update_organization_role_enforcement(
    client: AsyncClient, pg_session_factory
) -> None:
    """Verifies role permissions: owner/admin can update, member gets 403, non-member gets 404."""
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()

    token_owner = make_token(user_id=str(owner_id))
    token_member = make_token(user_id=str(member_id))
    token_outsider = make_token(user_id=str(outsider_id))

    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # 1. Owner creates org
            res = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {token_owner}"},
                json={"name": "Original Name"},
            )
            assert res.status_code == 201
            org_id = uuid.UUID(res.json()["id"])

            # Add member_id as MemberRole.member
            async with pg_session_factory() as session:
                session.add(
                    OrganizationMember(
                        organization_id=org_id,
                        user_id=member_id,
                        role=MemberRole.member,
                    )
                )
                await session.commit()

            # 2. Member tries to update org -> 403 Forbidden
            member_patch = await client.patch(
                f"/api/v1/organizations/{org_id}",
                headers={"Authorization": f"Bearer {token_member}"},
                json={"name": "Member Renamed"},
            )
            assert member_patch.status_code == 403
            assert member_patch.json()["detail"] == "Insufficient role"

            # 3. Outsider tries to update org -> 404 Not Found
            outsider_patch = await client.patch(
                f"/api/v1/organizations/{org_id}",
                headers={"Authorization": f"Bearer {token_outsider}"},
                json={"name": "Outsider Renamed"},
            )
            assert outsider_patch.status_code == 404
            assert outsider_patch.json()["detail"] == "Not found"

            # 4. Owner updates org -> 200 OK
            owner_patch = await client.patch(
                f"/api/v1/organizations/{org_id}",
                headers={"Authorization": f"Bearer {token_owner}"},
                json={"name": "Updated By Owner"},
            )
            assert owner_patch.status_code == 200
            assert owner_patch.json()["name"] == "Updated By Owner"
    finally:
        await cleanup_test_organizations(pg_session_factory, [org_id])
