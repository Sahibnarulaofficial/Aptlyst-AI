"""SQLAlchemy models for usage accounting, billing periods, and audit logs."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if False:  # TYPE_CHECKING
    from app.models.chatbot import Chatbot
    from app.models.conversation import Conversation, Message
    from app.models.generation import GenerationTask
    from app.models.organization import Organization


class ReservationStatus(str, enum.Enum):
    """Status of an in-flight token/cost reservation."""

    active = "active"
    settled = "settled"
    released = "released"


class OrganizationBillingPeriod(Base):
    """Period-specific token and monetary accounting record for an organization.

    Guarantees:
    1. Partial Unique Index: at most ONE active billing period per organization.
    2. Historical Immutability: unlimited inactive periods (is_active=False) retained for audits.
    3. Micro-Dollar Precision: monetary amounts stored as 64-bit integer micro-dollars ($1e-6 USD).
    4. Non-Negative Counters: tokens and costs are guarded by database check constraints.
    """

    __tablename__ = "organization_billing_periods"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    plan_tier: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="free",
        server_default="free",
    )
    monthly_token_quota: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    tokens_used: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    tokens_reserved: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    hard_spending_cap_micros: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    cost_micros_used: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    cost_micros_reserved: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    blocked_reason: Mapped[str | None] = mapped_column(
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
    organization: Mapped["Organization"] = relationship(
        back_populates="billing_periods"
    )
    generation_tasks: Mapped[list["GenerationTask"]] = relationship(
        back_populates="billing_period"
    )
    reservations: Mapped[list["UsageReservation"]] = relationship(
        back_populates="billing_period"
    )
    usage_logs: Mapped[list["LLMUsageLog"]] = relationship(
        back_populates="billing_period"
    )

    __table_args__ = (
        CheckConstraint(
            "monthly_token_quota >= 0",
            name="ck_billing_periods_quota_non_negative",
        ),
        CheckConstraint(
            "hard_spending_cap_micros IS NULL OR hard_spending_cap_micros >= 0",
            name="ck_billing_periods_spending_cap_non_negative",
        ),
        CheckConstraint(
            "tokens_used >= 0 AND tokens_reserved >= 0",
            name="ck_billing_periods_tokens_non_negative",
        ),
        CheckConstraint(
            "cost_micros_used >= 0 AND cost_micros_reserved >= 0",
            name="ck_billing_periods_cost_non_negative",
        ),
        CheckConstraint(
            "period_end > period_start",
            name="ck_billing_periods_period_order",
        ),
        Index(
            "ix_billing_periods_org_dates",
            "organization_id",
            "period_start",
            "period_end",
        ),
        # Partial unique index: at most one active period per organization
        Index(
            "uq_org_active_billing_period",
            "organization_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )


class UsageReservation(Base):
    """Pre-inference quota and monetary reservation for an in-flight generation task.

    Guarantees:
    1. Exactly one active reservation per generation task and lease token.
    2. Prevents double-settlement or double-release via conditional state transitions.
    3. Retains association with the original billing period across renewals.
    """

    __tablename__ = "usage_reservations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    generation_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    lease_token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    tokens_reserved: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    cost_micros_reserved: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status"),
        nullable=False,
        default=ReservationStatus.active,
        server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    generation_task: Mapped["GenerationTask"] = relationship(
        back_populates="reservations"
    )
    organization: Mapped["Organization"] = relationship(
        back_populates="usage_reservations"
    )
    billing_period: Mapped["OrganizationBillingPeriod"] = relationship(
        back_populates="reservations"
    )

    __table_args__ = (
        CheckConstraint(
            "tokens_reserved >= 0 AND cost_micros_reserved >= 0",
            name="ck_usage_reservations_tokens_cost",
        ),
        UniqueConstraint(
            "generation_task_id",
            "lease_token",
            name="uq_reservation_task_lease",
        ),
        Index(
            "uq_task_active_reservation",
            "generation_task_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_usage_reservations_org_status", "organization_id", "status"),
        Index("ix_usage_reservations_period_status", "billing_period_id", "status"),
    )


class LLMUsageLog(Base):
    """Immutable audit record for every completed AI inference turn.

    Retention Policy:
    Conversational and chatbot deletions use ON DELETE SET NULL to preserve
    financial and regulatory billing history indefinitely, while organization
    deletions are restricted (ON DELETE RESTRICT).
    """

    __tablename__ = "llm_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
    chatbot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chatbots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    visitor_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    generation_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    pricing_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    cost_micros: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="usage_logs"
    )
    billing_period: Mapped["OrganizationBillingPeriod"] = relationship(
        back_populates="usage_logs"
    )
    chatbot: Mapped["Chatbot | None"] = relationship(
        back_populates="usage_logs"
    )
    conversation: Mapped["Conversation | None"] = relationship(
        back_populates="usage_logs"
    )
    visitor_message: Mapped["Message | None"] = relationship(
        back_populates="usage_logs"
    )
    generation_task: Mapped["GenerationTask | None"] = relationship(
        back_populates="usage_logs"
    )

    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0 AND cost_micros >= 0 AND latency_ms >= 0",
            name="ck_usage_logs_non_negative",
        ),
        Index("ix_llm_usage_logs_org_created", "organization_id", "created_at"),
        Index("ix_llm_usage_logs_period_created", "billing_period_id", "created_at"),
    )
