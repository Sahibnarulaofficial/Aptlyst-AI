"""Public chatbots API router for unauthenticated widget and embed runtimes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.chatbot import Chatbot, ChatbotStatus
from app.schemas.chatbot import PublicChatbotConfig

router = APIRouter(prefix="/public/chatbots", tags=["public-chatbots"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/{bot_id}", response_model=PublicChatbotConfig)
async def get_public_chatbot(
    bot_id: uuid.UUID,
    db: DbSession,
) -> PublicChatbotConfig:
    """Retrieve public configuration for an active chatbot.

    Unauthenticated endpoint intended for the embeddable website widget.
    Returns 404 if the chatbot does not exist, is in draft mode, or is inactive.
    Never exposes internal instructions, organization IDs, or operational metrics.
    """
    result = await db.execute(
        select(Chatbot).where(
            Chatbot.id == bot_id,
            Chatbot.status == ChatbotStatus.active,
        )
    )
    bot = result.scalar_one_or_none()

    if not bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chatbot not found",
        )

    return PublicChatbotConfig.model_validate(bot)
