"""Conversation context builder enforcing strict tenant isolation and input bounding."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import Chatbot
from app.models.conversation import Message, MessageSenderType
from app.services.llm.base import LLMMessage


async def build_conversation_context(
    db: AsyncSession,
    chatbot: Chatbot,
    conversation_id: uuid.UUID,
    current_message: Message,
    max_context_messages: int = 10,
) -> list[LLMMessage]:
    """Assemble chronological conversation history for LLM prompt context.

    Security & Isolation Guarantees:
    1. Tenant Isolation: Messages are queried exclusively by conversation_id.
       Cross-conversation or cross-tenant messages are never included.
    2. Role Separation: Chatbot instructions are retained exclusively as system
       instructions. Visitor messages are strictly assigned role='user'.
    3. Input Bounding: History is bounded to max_context_messages turns.
    4. Current Message: The current visitor message is always included as the final turn.

    Args:
        db: Active AsyncSession.
        chatbot: Authenticated Chatbot model instance.
        conversation_id: UUID of the persistent conversation.
        current_message: The newly persisted visitor Message record.
        max_context_messages: Maximum total messages to include in the prompt context.

    Returns:
        list[LLMMessage]: Chronological context turns ready for provider consumption.
    """
    # Reserve 1 slot for current message
    history_limit = max(0, max_context_messages - 1)

    history_messages: list[Message] = []
    if history_limit > 0:
        # Fetch the most recent messages preceding current_message within this conversation
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id != current_message.id,
            )
            .order_by(Message.created_at.desc())
            .limit(history_limit)
        )
        result = await db.execute(stmt)
        # Reverse to restore chronological order (oldest -> newest)
        history_messages = list(reversed(result.scalars().all()))

    context: list[LLMMessage] = []

    # Map previous history turns
    for msg in history_messages:
        if msg.sender_type == MessageSenderType.visitor:
            role = "user"
        elif msg.sender_type == MessageSenderType.assistant:
            role = "assistant"
        elif msg.sender_type == MessageSenderType.system:
            role = "system"
        else:
            continue

        context.append(LLMMessage(role=role, content=msg.content))

    # Append current visitor message as the latest turn
    context.append(LLMMessage(role="user", content=current_message.content))

    return context
