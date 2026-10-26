"""SQLAlchemy models for Stripe subscriptions and idempotent webhook processing."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if False:  # TYPE_CHECKING
    from app.models.organization import Organization


class SubscriptionStatus(str, enum.Enum):
    """Normalized Stripe subscription status."""

    incomplete = "incomplete"
    incomplete_expired = "incomplete_expired"
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    unpaid = "unpaid"
    paused = "paused"


class WebhookProcessingStatus(str, enum.Enum):
    """Processing state of an incoming Stripe webhook event."""

    pending = "pending"
    processed = "processed"
    failed = "failed"


class Subscription(Base):
    """Authoritative local subscription record synced via Stripe webhooks.

    Guarantees:
    1. Single Subscription Authority: local database is the source of truth, not transient JWTs.
    2. Unique Stripe Links: customer and subscription IDs are globally unique.
    3. Concurrency Protection: locked via SELECT ... FOR UPDATE during webhook transitions.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stripe_customer_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    stripe_price_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"),
        nullable=False,
        default=SubscriptionStatus.incomplete,
        server_default="incomplete",
    )
    plan_tier: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="free",
        server_default="free",
    )
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_stripe_event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
        back_populates="subscriptions"
    )

    __table_args__ = (
        Index("ix_subscriptions_org_status", "organization_id", "status"),
        Index(
            "uq_org_active_subscription",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'trialing')"),
        ),
    )


class StripeWebhookEvent(Base):
    """Deduplication and audit record for Stripe webhook events.

    Safe Retry Design:
    1. Primary Key on id (stripe_event_id) prevents duplicate concurrent processing.
    2. Status defaults to 'pending' during transaction; transitions to 'processed'
       only upon successful business transaction commit.
    3. Failed processing can be safely retried without permanent lockout.
    """

    __tablename__ = "stripe_webhook_events"

    id: Mapped[str] = mapped_column(
        String(255), primary_key=True
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
    status: Mapped[WebhookProcessingStatus] = mapped_column(
        Enum(WebhookProcessingStatus, name="webhook_processing_status"),
        nullable=False,
        default=WebhookProcessingStatus.pending,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
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
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_stripe_webhook_events_status", "status", "created_at"),
    )
