"""Pydantic schemas for conversations and messages."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import ConversationStatus, MessageSenderType


class ConversationCreate(BaseModel):
    """Payload to initiate a new anonymous visitor conversation."""

    visitor_id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional client-generated visitor identifier. Generated server-side if omitted.",
    )


class ConversationPublicOut(BaseModel):
    """Public representation of an initialized visitor conversation."""

    id: uuid.UUID
    chatbot_id: uuid.UUID
    visitor_id: str
    status: ConversationStatus
    session_token: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VisitorMessageCreate(BaseModel):
    """Payload for submitting a visitor message."""

    content: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Text content of the visitor message (1-2000 characters).",
    )
    client_message_id: str | None = Field(
        default=None,
        max_length=64,
        description="Client-generated unique ID for idempotent message submission.",
    )


class MessageOut(BaseModel):
    """Schema for a persisted conversation message."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_type: MessageSenderType
    content: str
    client_message_id: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VisitorMessageResponse(BaseModel):
    """Response returned upon submitting a visitor message.

    Explicit API Contract:
    AI assistant replies are not generated in Milestone 1. The 'assistant_reply'
    field is always null in this milestone, indicating successful message persistence
    without synchronous LLM invocation.
    """

    message: MessageOut
    assistant_reply: str | None = Field(
        default=None,
        description="Explicit API contract: AI replies are not generated in Milestone 1.",
    )


class DashboardConversationOut(BaseModel):
    """Dashboard view of a conversation for authenticated organization members."""

    id: uuid.UUID
    chatbot_id: uuid.UUID
    visitor_id: str
    status: ConversationStatus
    messages_count: int = 0
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
