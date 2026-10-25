"""Public chatbots API router for unauthenticated widget and embed runtimes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.visitor_session import (
    VisitorSession,
    create_visitor_token,
    get_verified_visitor_session,
)
from app.db.session import get_db
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import (
    Conversation,
    ConversationStatus,
    Message,
    MessageSenderType,
)
from app.schemas.chatbot import PublicChatbotConfig
from app.schemas.conversation import (
    ConversationCreate,
    ConversationPublicOut,
    MessageOut,
    VisitorMessageCreate,
    VisitorMessageResponse,
)

router = APIRouter(prefix="/public/chatbots", tags=["public-chatbots"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

MAX_VISITOR_PAYLOAD_BYTES = 16 * 1024  # 16 KB


async def _check_visitor_payload_size(request: Request) -> None:
    """Enforce request body size limit for public visitor endpoints (<= 16 KB)."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_VISITOR_PAYLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Payload exceeds maximum allowed size of 16 KB",
                )
        except ValueError:
            pass

    body_bytes = await request.body()
    if len(body_bytes) > MAX_VISITOR_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Payload exceeds maximum allowed size of 16 KB",
        )


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


@router.post(
    "/{bot_id}/conversations",
    response_model=ConversationPublicOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_visitor_conversation(
    bot_id: uuid.UUID,
    request: Request,
    db: DbSession,
    body: ConversationCreate | None = None,
) -> ConversationPublicOut:
    """Initialize a persistent conversation for an anonymous visitor.

    Requires an active chatbot. Returns 404 if the chatbot does not exist,
    is in draft mode, or is inactive.
    Issues a cryptographically signed HMAC-SHA256 session token granting
    access only to this conversation and chatbot.
    """
    await _check_visitor_payload_size(request)
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

    conv_id = uuid.uuid4()
    requested_visitor_id = body.visitor_id if body else None
    token, visitor_id = create_visitor_token(
        bot_id=bot.id,
        conversation_id=conv_id,
        visitor_id=requested_visitor_id,
    )

    conv = Conversation(
        id=conv_id,
        organization_id=bot.organization_id,
        chatbot_id=bot.id,
        visitor_id=visitor_id,
        status=ConversationStatus.active,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    return ConversationPublicOut(
        id=conv.id,
        chatbot_id=conv.chatbot_id,
        visitor_id=conv.visitor_id,
        status=conv.status,
        session_token=token,
        created_at=conv.created_at,
    )


@router.post(
    "/{bot_id}/conversations/{conversation_id}/messages",
    response_model=VisitorMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_visitor_message(
    bot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: VisitorMessageCreate,
    request: Request,
    response: Response,
    db: DbSession,
    session: Annotated[VisitorSession, Depends(get_verified_visitor_session)],
) -> VisitorMessageResponse:
    """Submit a message to a persistent visitor conversation.

    Security & Validation:
    - Verified visitor session token matching bot_id and conversation_id.
    - Active chatbot check (404 if bot is not active).
    - Payload size limit enforced (<= 16 KB; returns 413 if exceeded).
    - Message content length validated (1-2000 chars; returns 422 if invalid).
    - Idempotency: if client_message_id is supplied and already recorded for
      this conversation, the existing message is returned safely.

    Explicit API Contract:
    AI assistant replies are not generated in Milestone 1. The 'assistant_reply'
    field is always null in this milestone.
    """
    # Enforce request payload size limit (<= 16 KB)
    await _check_visitor_payload_size(request)

    # Verify active chatbot
    bot_result = await db.execute(
        select(Chatbot).where(
            Chatbot.id == bot_id,
            Chatbot.status == ChatbotStatus.active,
        )
    )
    bot = bot_result.scalar_one_or_none()
    if not bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chatbot not found",
        )

    # Verify conversation existence and matching bot
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.chatbot_id == bot_id,
        )
    )
    conv = conv_result.scalar_one_or_none()
    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    if conv.status != ConversationStatus.active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conversation is no longer active",
        )

    # Idempotency check: if client_message_id is provided, check for existing message
    if body.client_message_id:
        existing_result = await db.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.client_message_id == body.client_message_id,
            )
        )
        existing_msg = existing_result.scalar_one_or_none()
        if existing_msg:
            # Idempotent replay: return existing message with 200 OK
            response.status_code = status.HTTP_200_OK
            return VisitorMessageResponse(
                message=MessageOut.model_validate(existing_msg),
                assistant_reply=None,
            )

    # Insert message
    msg = Message(
        conversation_id=conversation_id,
        sender_type=MessageSenderType.visitor,
        content=body.content,
        client_message_id=body.client_message_id,
    )
    db.add(msg)
    try:
        await db.commit()
        await db.refresh(msg)
    except IntegrityError:
        await db.rollback()
        # Handle concurrent submission with identical client_message_id
        if body.client_message_id:
            existing_result = await db.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.client_message_id == body.client_message_id,
                )
            )
            existing_msg = existing_result.scalar_one_or_none()
            if existing_msg:
                response.status_code = status.HTTP_200_OK
                return VisitorMessageResponse(
                    message=MessageOut.model_validate(existing_msg),
                    assistant_reply=None,
                )
        raise

    return VisitorMessageResponse(
        message=MessageOut.model_validate(msg),
        assistant_reply=None,
    )


@router.get(
    "/{bot_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
async def list_conversation_messages(
    bot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: DbSession,
    session: Annotated[VisitorSession, Depends(get_verified_visitor_session)],
) -> list[MessageOut]:
    """Retrieve the full chronological message transcript for a visitor conversation.

    Requires a valid signed visitor session token matching bot_id and conversation_id.
    """
    # Verify active chatbot
    bot_result = await db.execute(
        select(Chatbot).where(
            Chatbot.id == bot_id,
            Chatbot.status == ChatbotStatus.active,
        )
    )
    if not bot_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chatbot not found",
        )

    # Verify conversation exists
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.chatbot_id == bot_id,
        )
    )
    if not conv_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    return [MessageOut.model_validate(m) for m in messages]
