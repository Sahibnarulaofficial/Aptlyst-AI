"""Chatbots API router."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.organizations import _require_org_role
from app.auth.dependencies import AuthenticatedUser
from app.db.session import get_db
from app.models.chatbot import Chatbot
from app.models.organization import MemberRole
from app.schemas.chatbot import ChatbotCreate, ChatbotOut, ChatbotUpdate

router = APIRouter(prefix="/organizations/{org_id}/chatbots", tags=["chatbots"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[ChatbotOut])
async def list_chatbots(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> list[ChatbotOut]:
    """List all chatbots belonging to the specified organization.

    Requires membership in the organization. Returns 404 for non-members.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.member)

    result = await db.execute(
        select(Chatbot)
        .where(Chatbot.organization_id == org_id)
        .order_by(Chatbot.created_at.desc())
    )
    chatbots = result.scalars().all()

    return [
        ChatbotOut(
            id=bot.id,
            organization_id=bot.organization_id,
            name=bot.name,
            description=bot.description,
            website_url=bot.website_url,
            welcome_message=bot.welcome_message,
            instructions=bot.instructions,
            primary_color=bot.primary_color,
            status=bot.status,
            conversations_count=0,
            leads_count=0,
            last_active=bot.created_at,
            created_at=bot.created_at,
            updated_at=bot.updated_at,
        )
        for bot in chatbots
    ]


@router.post("", response_model=ChatbotOut, status_code=status.HTTP_201_CREATED)
async def create_chatbot(
    org_id: uuid.UUID,
    body: ChatbotCreate,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> ChatbotOut:
    """Create a new chatbot scoped to the specified organization.

    Requires member role or higher in the organization. Returns 404 for non-members.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.member)

    bot = Chatbot(
        organization_id=org_id,
        name=body.name,
        description=body.description,
        website_url=body.website_url,
        welcome_message=body.welcome_message,
        instructions=body.instructions,
        primary_color=body.primary_color,
        status=body.status,
    )
    db.add(bot)
    await db.commit()
    await db.refresh(bot)

    return ChatbotOut(
        id=bot.id,
        organization_id=bot.organization_id,
        name=bot.name,
        description=bot.description,
        website_url=bot.website_url,
        welcome_message=bot.welcome_message,
        instructions=bot.instructions,
        primary_color=bot.primary_color,
        status=bot.status,
        conversations_count=0,
        leads_count=0,
        last_active=bot.created_at,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


@router.get("/{bot_id}", response_model=ChatbotOut)
async def get_chatbot(
    org_id: uuid.UUID,
    bot_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> ChatbotOut:
    """Get a single chatbot by ID scoped to the specified organization.

    Returns 404 if the user is not a member or if the chatbot does not belong to org_id.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.member)

    result = await db.execute(
        select(Chatbot).where(
            Chatbot.id == bot_id,
            Chatbot.organization_id == org_id,
        )
    )
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return ChatbotOut(
        id=bot.id,
        organization_id=bot.organization_id,
        name=bot.name,
        description=bot.description,
        website_url=bot.website_url,
        welcome_message=bot.welcome_message,
        instructions=bot.instructions,
        primary_color=bot.primary_color,
        status=bot.status,
        conversations_count=0,
        leads_count=0,
        last_active=bot.created_at,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


@router.patch("/{bot_id}", response_model=ChatbotOut)
async def update_chatbot(
    org_id: uuid.UUID,
    bot_id: uuid.UUID,
    body: ChatbotUpdate,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> ChatbotOut:
    """Update a chatbot scoped to the specified organization.

    Requires member role or higher in the organization. Returns 404 if not found or non-member.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.member)

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided for update",
        )

    result = await db.execute(
        select(Chatbot).where(
            Chatbot.id == bot_id,
            Chatbot.organization_id == org_id,
        )
    )
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    for field, value in update_data.items():
        setattr(bot, field, value)

    await db.commit()
    await db.refresh(bot)

    return ChatbotOut(
        id=bot.id,
        organization_id=bot.organization_id,
        name=bot.name,
        description=bot.description,
        website_url=bot.website_url,
        welcome_message=bot.welcome_message,
        instructions=bot.instructions,
        primary_color=bot.primary_color,
        status=bot.status,
        conversations_count=0,
        leads_count=0,
        last_active=bot.created_at,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )
