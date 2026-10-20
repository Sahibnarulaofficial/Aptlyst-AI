"""Tests for organization management, role authorization, tenant isolation, and concurrency."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.main import app
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
async def test_setup_organization_creates_org_and_owner(client: AsyncClient) -> None:
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
        async with AsyncSessionLocal() as session:
            member = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id == user_id,
                )
            )
            assert member is not None
            assert member.role == MemberRole.owner
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_setup_organization_duplicate_returns_409(client: AsyncClient) -> None:
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
            assert r2.status_code == 409
            assert "already owns an organization" in r2.json()["detail"]
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
async def test_setup_organization_concurrency_race_condition(client: AsyncClient) -> None:
    """Tests that concurrent setup requests for the same user cannot create duplicate orgs."""
    user_id = uuid.uuid4()
    token = make_token(user_id=str(user_id))
    created_org_ids = []

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

        status_codes = [res1.status_code, res2.status_code]
        assert 201 in status_codes, f"Expected at least one 201, got {status_codes}"
        assert 409 in status_codes, f"Expected at least one 409, got {status_codes}"

        for r in (res1, res2):
            if r.status_code == 201:
                created_org_ids.append(uuid.UUID(r.json()["id"]))

        # Verify only 1 org owned in database
        async with AsyncSessionLocal() as session:
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
        if created_org_ids:
            async with AsyncSessionLocal() as session:
                for oid in created_org_ids:
                    await session.execute(delete(Organization).where(Organization.id == oid))
                await session.commit()


@pytest.mark.anyio
async def test_organization_tenant_isolation(client: AsyncClient) -> None:
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
        if org_a_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_a_id))
                await session.commit()


@pytest.mark.anyio
async def test_update_organization_role_enforcement(client: AsyncClient) -> None:
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
            async with AsyncSessionLocal() as session:
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
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()
