"""SQLAlchemy models for generalized provider-neutral billing, invoices, payments, and reconciliations.

Guarantees:
1. Multi-Provider Support: Scoped strictly by (provider, merchant_account_id, environment).
2. Payment-First Staging: billing_payments allows organization_id, subscription_id, and invoice_id
   to be NULL upon arrival, enabling out-of-order payment event staging.
3. Minor Currency Units: All financial amounts (gross, paid, due, refund) are stored as 64-bit integers
   in minor currency units (paise, cents) with non-negative check constraints.
4. Cryptographic Integrity: billing_webhook_events defaults signature_verified strictly to False;
   processing requires explicit verification.
5. Deterministic Reconciliation Case Identity: payment_reconciliations anchors on non-nullable
   case_key to prevent duplicate cases in PostgreSQL.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
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
from app.models.subscription import (
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    ReconciliationStatus,
    WebhookProcessingStatus,
)

if False:  # TYPE_CHECKING
    from app.models.billing import OrganizationBillingPeriod
    from app.models.organization import Organization
    from app.models.subscription import Subscription


class BillingWebhookEvent(Base):
    """Deduplication and audit record for provider-neutral webhook events.

    Guarantees:
    1. Signature Verification: signature_verified defaults strictly to False; events are processed
       only after verified cryptographic authenticity.
    2. Provider-Scoped Identity: UniqueConstraint on (provider, merchant_account_id, environment, provider_event_id)
       prevents cross-tenant or cross-environment event collisions.
    3. Ephemeral Security: Does not store raw signature headers as proof of authenticity.
    """

    __tablename__ = "billing_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    event_type: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    status: Mapped[WebhookProcessingStatus] = mapped_column(
        Enum(WebhookProcessingStatus, name="webhook_processing_status", create_type=False),
        nullable=False,
        default=WebhookProcessingStatus.pending,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    signature_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantine_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "merchant_account_id",
            "environment",
            "provider_event_id",
            name="uq_billing_webhook_provider_event",
        ),
        Index(
            "ix_billing_webhook_events_eligible",
            "status",
            "next_retry_at",
            "received_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
        Index("ix_billing_webhook_events_provider_type", "provider", "event_type"),
        Index(
            "ix_billing_webhook_events_claim",
            "provider",
            "status",
            "next_retry_at",
            "lease_expires_at",
            "received_at",
            postgresql_where=text("status IN ('pending', 'failed') AND signature_verified = true"),
        ),
    )


class BillingCheckoutAttempt(Base):
    """Durable tenant-scoped checkout session attempt for provider-neutral billing.

    Guarantees:
    1. Mutual Exclusion: At most one in-flight, pending, or open attempt per organization
       per provider/account/environment scope via partial unique index uq_org_scoped_active_billing_checkout_attempt.
    2. Deterministic Idempotency: UniqueConstraint on (organization_id, idempotency_key).
    3. Crash Safety: Leased creating state protects tenant reservation while external gateway
       call is in flight.
    """

    __tablename__ = "billing_checkout_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    provider_customer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_order_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    status: Mapped[CheckoutAttemptStatus] = mapped_column(
        Enum(CheckoutAttemptStatus, name="checkout_attempt_status", create_type=False),
        nullable=False,
        default=CheckoutAttemptStatus.creating,
        server_default="creating",
    )
    amount_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False
    )
    checkout_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    attempt_lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="billing_checkout_attempts"
    )
    payments: Mapped[list["BillingPayment"]] = relationship(
        back_populates="checkout_attempt",
        foreign_keys="[BillingPayment.checkout_attempt_id]",
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_billing_checkout_org_idempotency",
        ),
        UniqueConstraint(
            "id",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_subscription_id",
            "provider_payment_id",
            name="uq_bca_composite_identity",
        ),
        Index(
            "uq_org_scoped_active_billing_checkout_attempt",
            "organization_id",
            "provider",
            "merchant_account_id",
            "environment",
            unique=True,
            postgresql_where=text(
                "status IN ('creating', 'recovery_required', 'open', 'payment_pending')"
            ),
        ),
        Index("ix_billing_checkout_attempts_org_created", "organization_id", "created_at"),
        Index(
            "uq_checkout_attempts_provider_sub",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_subscription_id",
            unique=True,
            postgresql_where=text("provider_subscription_id IS NOT NULL"),
        ),
        Index(
            "uq_checkout_attempts_provider_payment",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
        Index(
            "ix_billing_checkout_attempts_provider_order",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_order_id",
        ),
        Index(
            "ix_billing_checkout_attempts_lease",
            "status",
            "attempt_lease_expires_at",
            postgresql_where=text("status = 'creating'"),
        ),
    )


class BillingInvoice(Base):
    """Authoritative ledger of provider-neutral invoices for renewal and financial settlement.

    Guarantees:
    1. Financial Invariants: gross_amount_minor_units >= 0, amount_paid_minor_units >= 0,
       amount_due_minor_units >= 0, and amount_paid_minor_units <= gross_amount_minor_units.
    2. Audit Conservation: Referenced organizations and subscriptions use ON DELETE RESTRICT.
    3. Multi-Event Convergence: Invoices serialize via advisory locking and are linked to
       settled payments during reconciliation.
    """

    __tablename__ = "billing_invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    provider_invoice_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_customer_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    billing_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False
    )
    gross_amount_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    amount_paid_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    amount_due_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_status: Mapped[InvoiceProcessingStatus] = mapped_column(
        Enum(InvoiceProcessingStatus, name="invoice_processing_status", create_type=False),
        nullable=False,
        default=InvoiceProcessingStatus.pending,
        server_default="pending",
    )
    reconciliation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unreconciled", server_default="unreconciled"
    )
    billing_period_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_billing_periods.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_webhook_events.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
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
        back_populates="billing_invoices"
    )
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="billing_invoices"
    )
    billing_period: Mapped["OrganizationBillingPeriod | None"] = relationship()
    raw_event: Mapped["BillingWebhookEvent | None"] = relationship()
    payments: Mapped[list["BillingPayment"]] = relationship(
        back_populates="invoice"
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "merchant_account_id",
            "environment",
            "provider_invoice_id",
            name="uq_billing_invoices_provider_invoice",
        ),
        CheckConstraint(
            "gross_amount_minor_units >= 0 AND amount_paid_minor_units >= 0 AND amount_due_minor_units >= 0",
            name="ck_billing_invoices_amounts_non_negative",
        ),
        CheckConstraint(
            "amount_paid_minor_units <= gross_amount_minor_units",
            name="ck_billing_invoices_paid_le_gross",
        ),
        CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end > period_start",
            name="ck_billing_invoices_period_order",
        ),
        Index("ix_billing_invoices_org_created", "organization_id", "created_at"),
        Index("ix_billing_invoices_provider_sub", "provider", "provider_subscription_id"),
        Index("ix_billing_invoices_status", "processing_status", "created_at"),
    )


class BillingPayment(Base):
    """Authoritative ledger of payment captures supporting payment-first staging.

    Guarantees:
    1. Payment-First Staging: organization_id, subscription_id, and invoice_id are NULLable,
       allowing an out-of-order payment.captured webhook to be staged safely before the invoice is created.
    2. Minor Unit Precision: amount_minor_units and amount_refunded_minor_units are positive/non-negative.
    3. Sweeper Crash Recovery: lease_token and lease_expires_at allow bounded batch claims via
       SELECT ... FOR UPDATE SKIP LOCKED without holding database connections during gateway network calls.
    """

    __tablename__ = "billing_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    provider_payment_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    provider_order_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_invoice_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_invoices.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    checkout_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_checkout_attempts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    amount_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refund_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    amount_refunded_minor_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reconciliation_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unreconciled", server_default="unreconciled"
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_webhook_events.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization | None"] = relationship(
        back_populates="billing_payments"
    )
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="billing_payments"
    )
    invoice: Mapped["BillingInvoice | None"] = relationship(
        back_populates="payments"
    )
    checkout_attempt: Mapped["BillingCheckoutAttempt | None"] = relationship(
        back_populates="payments",
        foreign_keys="[BillingPayment.checkout_attempt_id]",
    )
    raw_event: Mapped["BillingWebhookEvent | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "merchant_account_id",
            "environment",
            "provider_payment_id",
            name="uq_billing_payments_provider_payment",
        ),
        CheckConstraint(
            "amount_minor_units > 0",
            name="ck_billing_payments_amount_positive",
        ),
        CheckConstraint(
            "amount_refunded_minor_units >= 0 AND amount_refunded_minor_units <= amount_minor_units",
            name="ck_billing_payments_refund_non_negative",
        ),
        CheckConstraint(
            "(checkout_attempt_id IS NULL) OR ("
            "provider IS NOT NULL AND "
            "merchant_account_id IS NOT NULL AND "
            "environment IS NOT NULL AND "
            "provider_subscription_id IS NOT NULL AND "
            "provider_payment_id IS NOT NULL"
            ")",
            name="ck_billing_payments_checkout_attempt_complete",
        ),
        ForeignKeyConstraint(
            [
                "checkout_attempt_id",
                "provider",
                "merchant_account_id",
                "environment",
                "provider_subscription_id",
                "provider_payment_id",
            ],
            [
                "billing_checkout_attempts.id",
                "billing_checkout_attempts.provider",
                "billing_checkout_attempts.merchant_account_id",
                "billing_checkout_attempts.environment",
                "billing_checkout_attempts.provider_subscription_id",
                "billing_checkout_attempts.provider_payment_id",
            ],
            name="fk_billing_payments_checkout_attempt_composite",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_billing_payments_checkout_attempt",
            "checkout_attempt_id",
            unique=True,
            postgresql_where=text("checkout_attempt_id IS NOT NULL"),
        ),
        Index("ix_billing_payments_provider_invoice", "provider", "provider_invoice_id"),
        Index("ix_billing_payments_provider_sub", "provider", "provider_subscription_id"),
        Index("ix_billing_payments_provider_order", "provider", "provider_order_id"),
        Index(
            "ix_billing_payments_sweeper_claim",
            "reconciliation_state",
            "lease_expires_at",
            "attempt_count",
            postgresql_where=text("reconciliation_state IN ('unreconciled', 'reconciling')"),
        ),
    )


class PaymentReconciliation(Base):
    """Audit and administrative review record for provider-neutral payment discrepancies.

    Guarantees:
    1. Deterministic Case Identity: case_key is strictly NOT NULL (e.g. 'payment:pay_123:amount_mismatch'),
       guaranteeing unique constraint enforcement in PostgreSQL without NULL ambiguity.
    2. Idempotent Deduplication: Repeated webhook events for the same discrepancy update
       diagnostic_payload rather than inserting redundant review tickets.
    3. Unbound Payment Compatibility: organization_id is NULLable for cases where tenant identity
       could not be resolved.
    """

    __tablename__ = "payment_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    case_key: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_invoices.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_payments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_webhook_events.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        Enum(ReconciliationStatus, name="reconciliation_status", create_type=False),
        nullable=False,
        default=ReconciliationStatus.pending_review,
        server_default="pending_review",
    )
    diagnostic_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization | None"] = relationship(
        back_populates="payment_reconciliations"
    )
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="payment_reconciliations"
    )
    invoice: Mapped["BillingInvoice | None"] = relationship()
    payment: Mapped["BillingPayment | None"] = relationship()
    raw_event: Mapped["BillingWebhookEvent | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "merchant_account_id",
            "environment",
            "case_key",
            name="uq_payment_reconciliations_case_key",
        ),
        Index("ix_payment_reconciliations_org_status", "organization_id", "status"),
        Index("ix_payment_reconciliations_payment_id", "payment_id"),
        Index("ix_payment_reconciliations_invoice_id", "invoice_id"),
    )


class CustomerProvisionStatus(str, enum.Enum):
    """Lifecycle status of a multi-provider customer provisioning claim."""

    pending = "pending"
    resolved = "resolved"
    ambiguous = "ambiguous"
    failed = "failed"


class BillingCustomerProvision(Base):
    """Dedicated storage for multi-provider customer provisioning and concurrency claims.

    Guarantees:
    1. Independent Domain Lifecycle: Decoupled entirely from Subscription models.
       Customer provisioning never creates, mutates, or demotes a Subscription row.
    2. Tenant & Provider Scoping: Unique for (organization_id, provider, merchant_account_id, environment).
    3. Sentinel-Free Provider Identity: provider_customer_id stores ONLY real gateway IDs (e.g. cust_xxx),
       never internal concurrency markers (pending:, ambiguous:, claim:).
    4. Concurrency Lease Safety: Independent lease_expires_at timer not subject to entity updated_at triggers.
    5. Operation Ownership: claim_operation_id prevents stale workers from overwriting newer state transitions.
    """

    __tablename__ = "billing_customer_provisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    merchant_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default="default"
    )
    environment: Mapped[str] = mapped_column(
        String(32), nullable=False, default="live", server_default="live"
    )
    provider_customer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    status: Mapped[CustomerProvisionStatus] = mapped_column(
        Enum(
            CustomerProvisionStatus,
            name="customer_provision_status",
            create_type=False,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=CustomerProvisionStatus.pending,
        server_default="pending",
    )
    claim_operation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="billing_customer_provisions"
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "provider",
            "merchant_account_id",
            "environment",
            name="uq_billing_customer_provisions_org_provider",
        ),
        Index(
            "uq_billing_customer_provisions_provider_customer",
            "provider",
            "merchant_account_id",
            "environment",
            "provider_customer_id",
            unique=True,
            postgresql_where=text("provider_customer_id IS NOT NULL"),
        ),
        Index("ix_billing_customer_provisions_status", "status"),
        Index("ix_billing_customer_provisions_claim", "claim_operation_id"),
        Index(
            "ix_billing_customer_provisions_lease",
            "status",
            "lease_expires_at",
        ),
    )

