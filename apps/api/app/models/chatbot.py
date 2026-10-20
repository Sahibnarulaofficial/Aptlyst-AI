"""SQLAlchemy models for chatbots."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if False:  # TYPE_CHECKING
    from app.models.organization import Organization


class ChatbotStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    inactive = "inactive"


class Chatbot(Base):
    __tablename__ = "chatbots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    welcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True, default="#3b82f6")
    status: Mapped[ChatbotStatus] = mapped_column(
        Enum(ChatbotStatus, name="chatbot_status"),
        nullable=False,
        default=ChatbotStatus.draft,
        server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship(back_populates="chatbots")

    __table_args__ = (
        Index("ix_chatbots_org_created_at", "organization_id", "created_at"),
    )
