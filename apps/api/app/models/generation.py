"""SQLAlchemy models for AI generation task coordination and concurrency leases."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if False:  # TYPE_CHECKING
    from app.models.billing import LLMUsageLog, OrganizationBillingPeriod, UsageReservation
    from app.models.conversation import Conversation, Message
    from app.models.organization import Organization


class GenerationTaskStatus(str, enum.Enum):
    """Lifecycle status of an assistant reply generation task."""

    pending = "pending"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    reconciliation_needed = "reconciliation_needed"


class GenerationTask(Base):
    """Durable generation coordination record preventing duplicate inference.

    Guarantees:
    1. Uniqueness: exactly one generation task per visitor message (1:1 relationship).
    2. Concurrency Fencing: workers must match lease_token and lease_version to finalize.
    3. Reversible Failover: expired leases can be atomically acquired by another worker.
    4. Period Association: irrevocably tied to the billing period active at request time.
    """

    __tablename__ = "generation_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    visitor_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    billing_period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_billing_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[GenerationTaskStatus] = mapped_column(
        Enum(GenerationTaskStatus, name="generation_task_status"),
        nullable=False,
        default=GenerationTaskStatus.pending,
        server_default="pending",
    )
    lease_token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    lease_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship(
        back_populates="generation_tasks"
    )
    visitor_message: Mapped["Message"] = relationship(
        back_populates="generation_task"
    )
    organization: Mapped["Organization"] = relationship(
        back_populates="generation_tasks"
    )
    billing_period: Mapped["OrganizationBillingPeriod"] = relationship(
        back_populates="generation_tasks"
    )
    reservations: Mapped[list["UsageReservation"]] = relationship(
        back_populates="generation_task",
        cascade="all, delete-orphan",
    )
    usage_logs: Mapped[list["LLMUsageLog"]] = relationship(
        back_populates="generation_task"
    )

    __table_args__ = (
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_generation_tasks_attempts",
        ),
        Index("ix_generation_tasks_conv_status", "conversation_id", "status"),
        Index("ix_generation_tasks_lease_recovery", "status", "lease_expires_at"),
        Index("ix_generation_tasks_org_period", "organization_id", "billing_period_id"),
    )
