"""Pydantic schemas for conversations and messages."""

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.conversation import ConversationStatus, MessageSenderType


class GenerationStatus(str, enum.Enum):
    """Status of AI assistant response generation for a submitted visitor message."""

    completed = "completed"
    failed = "failed"
    skipped = "skipped"


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

    @field_validator("client_message_id")
    @classmethod
    def validate_client_message_id(cls, v: str | None) -> str | None:
        """Reserve 'reply:' prefix for system assistant correlation keys."""
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            return None
        if trimmed.lower().startswith("reply:"):
            raise ValueError(
                "client_message_id cannot use the reserved 'reply:' prefix."
            )
        return trimmed


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

    API Contract (Milestone 3):
    - message: The persisted visitor message (always saved even if AI generation fails).
    - assistant_reply: Text content of the generated reply, or null if skipped/failed.
    - assistant_message: Full persisted assistant message record, or null if skipped/failed.
    - generation_status: Status of the assistant reply generation: 'completed', 'failed', or 'skipped'.
    - generation_error: Non-fatal error explanation if generation failed (visitor message is preserved).
    """

    message: MessageOut
    assistant_reply: str | None = Field(
        default=None,
        description="Text content of the generated assistant reply, or null if skipped/failed.",
    )
    assistant_message: MessageOut | None = Field(
        default=None,
        description="Full persisted assistant message record, or null if generation was skipped/failed.",
    )
    generation_status: GenerationStatus = Field(
        default=GenerationStatus.completed,
        description="Status of assistant reply generation: 'completed', 'failed', or 'skipped'.",
    )
    generation_error: str | None = Field(
        default=None,
        description="Non-fatal error detail when generation fails; visitor message is preserved.",
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
