"""Organizations API router."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.db.session import get_db
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.schemas.organization import (
    OrganizationCreate,
    OrganizationOut,
    OrganizationWithRoleOut,
)

router = APIRouter(prefix="/organizations", tags=["organizations"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _require_org_role(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    minimum_role: MemberRole = MemberRole.member,
) -> OrganizationMember:
    """Return the membership record or raise 404/403.

    Returns 404 regardless of whether the org doesn't exist or the user isn't
    a member, to avoid leaking whether an org exists.
    """
    role_order = [MemberRole.member, MemberRole.admin, MemberRole.owner]

    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    membership = result.scalar_one_or_none()

    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if role_order.index(membership.role) < role_order.index(minimum_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

    return membership


@router.get("", response_model=list[OrganizationWithRoleOut])
async def list_organizations(
    current_user: AuthenticatedUser,
    db: DbSession,
) -> list[OrganizationWithRoleOut]:
    """List all organizations the authenticated user belongs to."""
    result = await db.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == current_user.id)
        .order_by(Organization.created_at)
    )
    rows = result.all()
    return [
        OrganizationWithRoleOut(
            id=org.id,
            name=org.name,
            role=role,
            created_at=org.created_at,
            updated_at=org.updated_at,
        )
        for org, role in rows
    ]


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
@router.post("/setup", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def setup_initial_organization(
    body: OrganizationCreate,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> OrganizationOut:
    """Create an organization and set the authenticated user as owner.

    Business rules:
    - Free plan: maximum 1 owned organization. Attempting to create a second returns 403 Forbidden.
    - Paid plan: can create multiple owned organizations.
    - Non-owner memberships (member, admin) do not count toward this limit.
    """
    # Acquire a transaction-level advisory lock scoped to the user ID to serialize
    # concurrent creation attempts for the same user (preventing TOCTOU races).
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        lock_key = int(current_user.id.int % 2147483647)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": lock_key},
        )

    try:
        # Check count of organizations this user owns (non-owner memberships do not count)
        owned_count_query = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.user_id == current_user.id,
                OrganizationMember.role == MemberRole.owner,
            )
        )
        owned_count = (await db.scalar(owned_count_query)) or 0

        if owned_count >= 1 and not current_user.is_paid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Free plan is limited to 1 organization. Upgrade to create more organizations.",
            )

        org = Organization(name=body.name)
        db.add(org)
        await db.flush()  # obtain org.id before creating membership

        membership = OrganizationMember(
            organization_id=org.id,
            user_id=current_user.id,
            role=MemberRole.owner,
        )
        db.add(membership)
        await db.commit()
        await db.refresh(org)

        return OrganizationOut.model_validate(org)
    except HTTPException:
        raise
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflict creating organization",
        )


@router.get("/{org_id}", response_model=OrganizationOut)
async def get_organization(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> OrganizationOut:
    """Get a single organization. Requires membership."""
    await _require_org_role(org_id, current_user.id, db)

    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return OrganizationOut.model_validate(org)


@router.patch("/{org_id}", response_model=OrganizationOut)
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationCreate,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> OrganizationOut:
    """Update organization name. Requires admin or owner role."""
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    org.name = body.name
    await db.commit()
    await db.refresh(org)

    return OrganizationOut.model_validate(org)
