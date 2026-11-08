"""SQLAlchemy models for Stripe subscriptions and idempotent webhook processing."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    from app.models.billing import OrganizationBillingPeriod
    from app.models.organization import Organization
    from app.models.payment import BillingInvoice, BillingPayment, PaymentReconciliation


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
    quarantined = "quarantined"


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
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    provider_customer_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    provider_plan_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    environment: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    last_webhook_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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
    past_due_since: Mapped[datetime | None] = mapped_column(
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

    def __init__(self, **kwargs: Any) -> None:
        # Backward compatibility for Stripe legacy callers:
        # If stripe_customer_id is provided and provider-neutral fields are omitted,
        # automatically populate them so existing tests and Stripe services continue to work.
        if "stripe_customer_id" in kwargs and kwargs["stripe_customer_id"]:
            kwargs.setdefault("provider", "stripe")
            kwargs.setdefault("provider_customer_id", kwargs["stripe_customer_id"])
            kwargs.setdefault("merchant_account_id", "default")
            kwargs.setdefault("environment", "legacy")
            if "stripe_subscription_id" in kwargs:
                kwargs.setdefault("provider_subscription_id", kwargs["stripe_subscription_id"])
            if "stripe_price_id" in kwargs:
                kwargs.setdefault("provider_plan_id", kwargs["stripe_price_id"])
            if "last_stripe_event_created_at" in kwargs:
                kwargs.setdefault("last_webhook_event_at", kwargs["last_stripe_event_created_at"])
        super().__init__(**kwargs)

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="subscriptions"
    )
    billing_invoices: Mapped[list["BillingInvoice"]] = relationship(
        back_populates="subscription"
    )
    billing_payments: Mapped[list["BillingPayment"]] = relationship(
        back_populates="subscription"
    )
    payment_reconciliations: Mapped[list["PaymentReconciliation"]] = relationship(
        back_populates="subscription"
    )
    billing_periods: Mapped[list["OrganizationBillingPeriod"]] = relationship(
        back_populates="subscription"
    )

    __table_args__ = (
        Index("ix_subscriptions_org_status", "organization_id", "status"),
        Index(
            "uq_org_active_subscription",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'trialing')"),
        ),
        Index(
            "uq_subscriptions_provider_customer",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_customer_id",
            unique=True,
        ),
        Index(
            "uq_subscriptions_provider_subscription",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_subscription_id",
            unique=True,
            postgresql_where=text("provider_subscription_id IS NOT NULL"),
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


class CheckoutAttemptStatus(str, enum.Enum):
    """Normalized status of an organization checkout session attempt."""

    creating = "creating"
    recovery_required = "recovery_required"
    open = "open"
    payment_pending = "payment_pending"
    complete = "complete"
    expired = "expired"
    failed = "failed"


class ReconciliationStatus(str, enum.Enum):
    """Review and resolution status for a financial billing reconciliation record."""

    pending_review = "pending_review"
    resolved_refunded = "resolved_refunded"
    resolved_ignored = "resolved_ignored"


class StripeCheckoutAttempt(Base):
    """Durable organization-scoped Stripe Checkout Session attempt.

    Guarantees:
    1. Mutual Exclusion: At most one in-flight, pending, or open attempt per organization
       via partial unique index `uq_org_active_checkout_attempt`.
    2. Replay Fidelity: Persists exact `request_payload` for deterministic idempotency replay.
    3. Crash Safety: Leased `creating` state with `recovery_required` transition protects
       against releasing the tenant reservation while upstream Stripe state is ambiguous.
    """

    __tablename__ = "stripe_checkout_attempts"

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
    )
    stripe_session_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checkout_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    status: Mapped[CheckoutAttemptStatus] = mapped_column(
        Enum(CheckoutAttemptStatus, name="checkout_attempt_status"),
        nullable=False,
        default=CheckoutAttemptStatus.creating,
        server_default="creating",
    )
    attempt_lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
    organization: Mapped["Organization"] = relationship(
        back_populates="checkout_attempts"
    )

    __table_args__ = (
        Index("ix_stripe_checkout_attempts_org_created", "organization_id", "created_at"),
        Index("ix_stripe_checkout_attempts_subscription_id", "stripe_subscription_id"),
        Index(
            "uq_org_active_checkout_attempt",
            "organization_id",
            unique=True,
            postgresql_where=text(
                "status IN ('creating', 'recovery_required', 'open', 'payment_pending')"
            ),
        ),
        Index(
            "ix_stripe_checkout_attempts_lease",
            "status",
            "attempt_lease_expires_at",
            postgresql_where=text("status = 'creating'"),
        ),
    )


class BillingReconciliation(Base):
    """Durable audit record for duplicate subscriptions and billing reconciliation cases.

    Guarantees:
    1. Non-Cascading Audit: Referenced webhook events and organizations use ON DELETE RESTRICT
       to guarantee financial audit trails cannot be deleted.
    2. Duplicate Prevention: UniqueConstraint on (event_id, incoming_subscription_id)
       ensures idempotent recording.
    3. Human Verification: Captures administrator resolution lifecycle without automatic
       cancellation or refunds.
    """

    __tablename__ = "billing_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("stripe_webhook_events.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    existing_subscription_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    incoming_subscription_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="duplicate_active_subscription",
        server_default="duplicate_active_subscription",
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        Enum(ReconciliationStatus, name="reconciliation_status"),
        nullable=False,
        default=ReconciliationStatus.pending_review,
        server_default="pending_review",
    )
    diagnostic_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolution_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="billing_reconciliations"
    )
    webhook_event: Mapped["StripeWebhookEvent"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "incoming_subscription_id",
            name="uq_reconciliation_event_sub",
        ),
        Index("ix_billing_reconciliations_org_status", "organization_id", "status"),
    )


class InvoiceProcessingStatus(str, enum.Enum):
    """Processing lifecycle state of an ingested Stripe invoice."""

    pending = "pending"
    processed = "processed"
    ignored = "ignored"
    failed = "failed"


class StripeInvoice(Base):
    """Authoritative ledger of Stripe invoices for renewal idempotency and financial audit.

    Guarantees:
    1. Single Explicit Uniqueness: stripe_invoice_id is guarded by uq_stripe_invoices_invoice_id;
       no redundant secondary index is created.
    2. Event Provenance: raw_event_id is a foreign key to stripe_webhook_events.id with ON DELETE RESTRICT.
    3. Multi-Event Convergence: invoice.paid and invoice.payment_succeeded serialize on
       the same invoice row; only the canonical paid cycle event provisions a billing period.
    4. Flexible Period Association:
       - One invoice can reference at most one billing period (billing_period_id).
       - Multiple invoices may reference the same billing period (non-unique index).
    5. Minor-Unit Precision: amount_paid_minor_units stores integer values matching currency minor units.
    6. Non-Cascading Audit Trail: ON DELETE RESTRICT prevents silent cascading deletion.
    """

    __tablename__ = "stripe_invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    stripe_invoice_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stripe_customer_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    billing_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    invoice_status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    amount_paid_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_status: Mapped[InvoiceProcessingStatus] = mapped_column(
        Enum(InvoiceProcessingStatus, name="invoice_processing_status"),
        nullable=False,
        default=InvoiceProcessingStatus.pending,
        server_default="pending",
    )
    billing_period_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_billing_periods.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_event_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("stripe_webhook_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    diagnostic_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="stripe_invoices"
    )
    billing_period: Mapped["OrganizationBillingPeriod"] = relationship()
    originating_event: Mapped["StripeWebhookEvent | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("stripe_invoice_id", name="uq_stripe_invoices_invoice_id"),
        Index("ix_stripe_invoices_organization_id", "organization_id"),
        Index("ix_stripe_invoices_customer_id", "stripe_customer_id"),
        Index("ix_stripe_invoices_subscription_id", "stripe_subscription_id"),
        Index("ix_stripe_invoices_billing_period_id", "billing_period_id"),
        Index("ix_stripe_invoices_raw_event_id", "raw_event_id"),
        Index("ix_stripe_invoices_org_created", "organization_id", "created_at"),
        Index("ix_stripe_invoices_status", "processing_status", "created_at"),
    )


# Register generalized provider-neutral models in SQLAlchemy class registry
import app.models.payment  # noqa: F401, E402
