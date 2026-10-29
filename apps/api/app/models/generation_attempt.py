"""SQLAlchemy model for durable generation attempts and provider execution tracking."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if False:  # TYPE_CHECKING
    from app.models.generation import GenerationTask


class GenerationAttemptStatus(str, enum.Enum):
    """Lifecycle status of a generation task provider attempt."""

    claimed = "claimed"
    dispatched = "dispatched"
    completed = "completed"
    failed = "failed"
    ambiguous = "ambiguous"
    stale_received = "stale_received"


class GenerationTaskAttempt(Base):
    """Durable attempt record tracking each provider execution attempt for a generation task.

    Retention & Audit Policy:
    Conversational and generation task deletions use ON DELETE SET NULL to preserve
    provider dispatch and execution history indefinitely, matching the financial audit
    retention policy of LLMUsageLog. Attempts are never cascaded away.

    Guarantees:
    1. Uniqueness: at most one attempt per attempt_number and lease_token while attached to a task.
    2. Lease binding: each attempt records the lease token that initiated it.
    3. Audit trail: preserves provider request ID, tokens, latency, cost, and errors.
    4. Ambiguity isolation: provider timeouts/drops quarantine without losing state.
    """

    __tablename__ = "generation_task_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    generation_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    lease_token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    status: Mapped[GenerationAttemptStatus] = mapped_column(
        Enum(GenerationAttemptStatus, name="generation_attempt_status"),
        nullable=False,
        default=GenerationAttemptStatus.claimed,
        server_default="claimed",
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    completion_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    cost_micros: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    response_content: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    error_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
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
    generation_task: Mapped["GenerationTask | None"] = relationship(
        back_populates="attempts_list"
    )

    __table_args__ = (
        Index(
            "uq_attempt_task_number",
            "generation_task_id",
            "attempt_number",
            unique=True,
            postgresql_where=text("generation_task_id IS NOT NULL"),
        ),
        Index(
            "uq_attempt_task_lease",
            "generation_task_id",
            "lease_token",
            unique=True,
            postgresql_where=text("generation_task_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) AND "
            "(completion_tokens IS NULL OR completion_tokens >= 0) AND "
            "(cost_micros IS NULL OR cost_micros >= 0) AND "
            "(latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_attempt_tokens_cost",
        ),
        CheckConstraint("attempt_number > 0", name="ck_attempt_number_positive"),
        Index("ix_generation_attempts_task_status", "generation_task_id", "status"),
        Index("ix_generation_attempts_provider_req", "provider_request_id"),
    )
