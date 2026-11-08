"""Tests for Step 5.1B provider-neutral billing database models and constraints.

Covers:
1. Registration of all six tables in SQLAlchemy Base.metadata:
   - subscriptions
   - billing_webhook_events
   - billing_checkout_attempts
   - billing_invoices
   - billing_payments
   - payment_reconciliations
2. Column data types, nullability, and defaults:
   - subscriptions: provider, provider_customer_id, merchant_account_id, environment are NOT NULL.
   - subscriptions: stripe_customer_id is nullable.
   - billing_webhook_events: signature_verified defaults to False.
   - billing_payments: organization_id, subscription_id, invoice_id are nullable (payment-first staging).
   - payment_reconciliations: case_key is strictly NOT NULL, organization_id is nullable.
3. Check constraints:
   - billing_invoices: amounts non-negative, paid <= gross, period order.
   - billing_payments: amount positive, refund non-negative and <= amount.
4. Unique constraints and scoped partial indexes:
   - uq_subscriptions_provider_customer
   - uq_subscriptions_provider_subscription
   - uq_org_active_subscription (preserved)
   - uq_billing_webhook_provider_event
   - uq_org_scoped_active_billing_checkout_attempt
   - uq_billing_invoices_provider_invoice
   - uq_billing_payments_provider_payment
   - uq_payment_reconciliations_case_key
5. Foreign key relationships and ondelete actions:
   - ON DELETE RESTRICT on financial audit references.
   - ON DELETE SET NULL on billing_period_id.
6. Subscription model instantiation & backward compatibility:
   - Passing stripe_customer_id automatically populates provider='stripe' and provider_customer_id.
   - Passing explicit provider retains custom provider values.
"""

import sys
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import uuid
import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
import app.models.organization  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingInvoice,
    BillingPayment,
    BillingWebhookEvent,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    ReconciliationStatus,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)


# =============================================================================
# 1. Table Registration & Column Specifications
# =============================================================================


def test_all_provider_neutral_tables_registered_in_metadata() -> None:
    """Verify that all five new generalized tables and subscriptions exist in Base.metadata."""
    tables = Base.metadata.tables
    expected_tables = [
        "subscriptions",
        "billing_webhook_events",
        "billing_checkout_attempts",
        "billing_invoices",
        "billing_payments",
        "payment_reconciliations",
        "billing_customer_provisions",
    ]
    for table_name in expected_tables:
        assert table_name in tables, f"Expected table '{table_name}' in Base.metadata"


def test_subscription_provider_neutral_columns() -> None:
    """Verify subscription model contains provider-neutral fields and relaxed stripe_customer_id."""
    table = Base.metadata.tables["subscriptions"]

    # Relaxed stripe_customer_id
    assert "stripe_customer_id" in table.c
    assert table.c["stripe_customer_id"].nullable is True

    # Provider-neutral mandatory columns (no permanent defaults)
    assert "provider" in table.c
    assert table.c["provider"].nullable is False
    assert isinstance(table.c["provider"].type, String)
    assert table.c["provider"].type.length == 32

    assert "provider_customer_id" in table.c
    assert table.c["provider_customer_id"].nullable is False
    assert isinstance(table.c["provider_customer_id"].type, String)
    assert table.c["provider_customer_id"].type.length == 255

    assert "merchant_account_id" in table.c
    assert table.c["merchant_account_id"].nullable is False
    assert isinstance(table.c["merchant_account_id"].type, String)
    assert table.c["merchant_account_id"].type.length == 64

    assert "environment" in table.c
    assert table.c["environment"].nullable is False
    assert isinstance(table.c["environment"].type, String)
    assert table.c["environment"].type.length == 32

    # Optional provider-neutral columns
    assert "provider_subscription_id" in table.c
    assert table.c["provider_subscription_id"].nullable is True

    assert "provider_plan_id" in table.c
    assert table.c["provider_plan_id"].nullable is True

    assert "last_webhook_event_at" in table.c
    assert table.c["last_webhook_event_at"].nullable is True
    assert isinstance(table.c["last_webhook_event_at"].type, DateTime)


def test_billing_webhook_events_columns_and_defaults() -> None:
    """Verify billing_webhook_events schema, especially signature_verified default False."""
    table = Base.metadata.tables["billing_webhook_events"]

    assert isinstance(table.c["id"].type, UUID)
    assert table.c["id"].primary_key is True

    assert table.c["provider"].nullable is False
    assert table.c["merchant_account_id"].nullable is False
    assert table.c["environment"].nullable is False
    assert table.c["provider_event_id"].nullable is False
    assert table.c["event_type"].nullable is False

    # Signature verification strictly default False
    assert table.c["signature_verified"].nullable is False
    assert isinstance(table.c["signature_verified"].type, Boolean)
    assert table.c["signature_verified"].server_default.arg == "false"

    assert isinstance(table.c["payload"].type, JSONB)
    assert table.c["payload"].nullable is False

    # Lease ownership columns (nullable)
    assert "lease_token" in table.c
    assert isinstance(table.c["lease_token"].type, UUID)
    assert table.c["lease_token"].nullable is True

    assert "lease_expires_at" in table.c
    assert isinstance(table.c["lease_expires_at"].type, DateTime)
    assert table.c["lease_expires_at"].nullable is True

    assert "locked_by" in table.c
    assert isinstance(table.c["locked_by"].type, String)
    assert table.c["locked_by"].type.length == 64
    assert table.c["locked_by"].nullable is True

    # Claim index present
    index_names = {idx.name for idx in table.indexes}
    assert "ix_billing_webhook_events_claim" in index_names


def test_billing_checkout_attempts_columns() -> None:
    """Verify billing_checkout_attempts columns and properties."""
    table = Base.metadata.tables["billing_checkout_attempts"]

    assert isinstance(table.c["id"].type, UUID)
    assert table.c["organization_id"].nullable is False
    assert table.c["provider"].nullable is False
    assert table.c["idempotency_key"].nullable is False
    assert table.c["amount_minor_units"].nullable is False
    assert isinstance(table.c["amount_minor_units"].type, BigInteger)
    assert table.c["currency"].nullable is False
    assert table.c["attempt_lease_expires_at"].nullable is False


def test_billing_invoices_columns_and_minor_units() -> None:
    """Verify billing_invoices financial columns are BigInteger minor units."""
    table = Base.metadata.tables["billing_invoices"]

    assert table.c["provider"].nullable is False
    assert table.c["provider_invoice_id"].nullable is False
    assert table.c["organization_id"].nullable is False
    assert table.c["subscription_id"].nullable is True
    assert table.c["currency"].nullable is False
    assert table.c["currency"].type.length == 3

    # Financial minor units
    assert isinstance(table.c["gross_amount_minor_units"].type, BigInteger)
    assert table.c["gross_amount_minor_units"].nullable is False

    assert isinstance(table.c["amount_paid_minor_units"].type, BigInteger)
    assert table.c["amount_paid_minor_units"].nullable is False

    assert isinstance(table.c["amount_due_minor_units"].type, BigInteger)
    assert table.c["amount_due_minor_units"].nullable is False


def test_billing_payments_payment_first_staging_nullable_fields() -> None:
    """Verify billing_payments supports payment-first staging with nullable foreign keys."""
    table = Base.metadata.tables["billing_payments"]

    assert table.c["provider"].nullable is False
    assert table.c["provider_payment_id"].nullable is False

    # Crucial payment-first staging: nullable before invoice/org binding
    assert table.c["organization_id"].nullable is True
    assert table.c["subscription_id"].nullable is True
    assert table.c["invoice_id"].nullable is True

    # Financial amounts
    assert isinstance(table.c["amount_minor_units"].type, BigInteger)
    assert table.c["amount_minor_units"].nullable is False

    assert isinstance(table.c["amount_refunded_minor_units"].type, BigInteger)
    assert table.c["amount_refunded_minor_units"].nullable is False

    # Sweeper leasing columns
    assert table.c["reconciliation_state"].nullable is False
    assert isinstance(table.c["lease_token"].type, UUID)
    assert table.c["lease_token"].nullable is True
    assert table.c["lease_expires_at"].nullable is True


def test_payment_reconciliations_deterministic_case_key() -> None:
    """Verify payment_reconciliations anchors on non-nullable case_key."""
    table = Base.metadata.tables["payment_reconciliations"]

    # Non-nullable deterministic case key
    assert table.c["case_key"].nullable is False
    assert isinstance(table.c["case_key"].type, String)
    assert table.c["case_key"].type.length == 255

    # Organization is nullable for unbound payment discrepancy cases
    assert table.c["organization_id"].nullable is True
    assert table.c["reason"].nullable is False
    assert isinstance(table.c["diagnostic_payload"].type, JSONB)


# =============================================================================
# 2. Constraints, Checks & Indexes
# =============================================================================


def test_billing_invoices_check_constraints() -> None:
    """Verify billing_invoices financial sanity check constraints."""
    table = Base.metadata.tables["billing_invoices"]
    check_names = {ck.name for ck in table.constraints if ck.__class__.__name__ == "CheckConstraint"}

    assert "ck_billing_invoices_amounts_non_negative" in check_names
    assert "ck_billing_invoices_paid_le_gross" in check_names
    assert "ck_billing_invoices_period_order" in check_names


def test_billing_payments_check_constraints() -> None:
    """Verify billing_payments check constraints: amount > 0, refund non-negative and <= amount."""
    table = Base.metadata.tables["billing_payments"]
    check_names = {ck.name for ck in table.constraints if ck.__class__.__name__ == "CheckConstraint"}

    assert "ck_billing_payments_amount_positive" in check_names
    assert "ck_billing_payments_refund_non_negative" in check_names


def test_scoped_unique_constraints_and_indexes() -> None:
    """Verify scoped unique constraints across all provider-neutral tables."""
    sub_table = Base.metadata.tables["subscriptions"]
    sub_indexes = {idx.name: idx for idx in sub_table.indexes}

    # Preserved active subscription partial uniqueness
    assert "uq_org_active_subscription" in sub_indexes
    assert sub_indexes["uq_org_active_subscription"].unique is True

    # Scoped provider uniqueness
    assert "uq_subscriptions_provider_customer" in sub_indexes
    assert sub_indexes["uq_subscriptions_provider_customer"].unique is True

    assert "uq_subscriptions_provider_subscription" in sub_indexes
    assert sub_indexes["uq_subscriptions_provider_subscription"].unique is True

    # Webhook provider event uniqueness
    webhook_table = Base.metadata.tables["billing_webhook_events"]
    webhook_uq = {c.name for c in webhook_table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_billing_webhook_provider_event" in webhook_uq

    # Checkout attempt uniqueness
    checkout_table = Base.metadata.tables["billing_checkout_attempts"]
    checkout_uq = {c.name for c in checkout_table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_billing_checkout_org_idempotency" in checkout_uq
    checkout_indexes = {idx.name: idx for idx in checkout_table.indexes}
    assert "uq_org_scoped_active_billing_checkout_attempt" in checkout_indexes
    scoped_attempt_idx = checkout_indexes["uq_org_scoped_active_billing_checkout_attempt"]
    assert scoped_attempt_idx.unique is True
    assert [c.name for c in scoped_attempt_idx.columns] == [
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
    ]

    # Invoice provider invoice uniqueness
    invoice_table = Base.metadata.tables["billing_invoices"]
    invoice_uq = {c.name for c in invoice_table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_billing_invoices_provider_invoice" in invoice_uq

    # Payment provider payment uniqueness
    payment_table = Base.metadata.tables["billing_payments"]
    payment_uq = {c.name for c in payment_table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_billing_payments_provider_payment" in payment_uq

    # Payment reconciliation case_key uniqueness
    recon_table = Base.metadata.tables["payment_reconciliations"]
    recon_uq = {c.name for c in recon_table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_payment_reconciliations_case_key" in recon_uq


# =============================================================================
# 3. Foreign Key Deletion Semantics
# =============================================================================


def test_foreign_key_deletion_rules() -> None:
    """Verify financial audit trails use ON DELETE RESTRICT and period detachment uses SET NULL."""
    inv_table = Base.metadata.tables["billing_invoices"]
    for fk in inv_table.foreign_keys:
        if fk.column.table.name == "organizations":
            assert fk.ondelete == "RESTRICT"
        elif fk.column.table.name == "subscriptions":
            assert fk.ondelete == "RESTRICT"
        elif fk.column.table.name == "billing_webhook_events":
            assert fk.ondelete == "RESTRICT"
        elif fk.column.table.name == "organization_billing_periods":
            assert fk.ondelete == "SET NULL"

    pay_table = Base.metadata.tables["billing_payments"]
    for fk in pay_table.foreign_keys:
        if fk.column.table.name in ("organizations", "subscriptions", "billing_invoices", "billing_webhook_events"):
            assert fk.ondelete == "RESTRICT"


# =============================================================================
# 4. Subscription Backward-Compatibility Instantiation
# =============================================================================


def test_subscription_backward_compatibility_stripe_instantiation() -> None:
    """Verify passing stripe_customer_id auto-populates provider='stripe' and provider_customer_id."""
    org_id = uuid.uuid4()
    sub = Subscription(
        organization_id=org_id,
        stripe_customer_id="cus_test_12345",
        stripe_subscription_id="sub_test_67890",
        stripe_price_id="price_pro_abc",
    )
    assert sub.organization_id == org_id
    assert sub.stripe_customer_id == "cus_test_12345"
    assert sub.provider == "stripe"
    assert sub.provider_customer_id == "cus_test_12345"
    assert sub.provider_subscription_id == "sub_test_67890"
    assert sub.provider_plan_id == "price_pro_abc"
    assert sub.merchant_account_id == "default"
    assert sub.environment == "legacy"


def test_subscription_explicit_provider_instantiation() -> None:
    """Verify passing explicit provider does not get overwritten by Stripe defaults."""
    org_id = uuid.uuid4()
    sub = Subscription(
        organization_id=org_id,
        provider="razorpay",
        provider_customer_id="cust_rzp_999",
        provider_subscription_id="sub_rzp_888",
        merchant_account_id="acc_live_123",
        environment="live",
    )
    assert sub.organization_id == org_id
    assert sub.stripe_customer_id is None
    assert sub.provider == "razorpay"
    assert sub.provider_customer_id == "cust_rzp_999"
    assert sub.provider_subscription_id == "sub_rzp_888"
    assert sub.merchant_account_id == "acc_live_123"
    assert sub.environment == "live"


def test_billing_customer_provisions_columns_and_constraints() -> None:
    """Verify billing_customer_provisions model, column specs, and constraints."""
    table = Base.metadata.tables["billing_customer_provisions"]

    # Primary Key
    assert "id" in table.c
    assert table.c["id"].primary_key is True

    # Foreign Key to organizations
    assert "organization_id" in table.c
    assert table.c["organization_id"].nullable is False
    fk = list(table.c["organization_id"].foreign_keys)[0]
    assert fk.target_fullname == "organizations.id"
    assert fk.ondelete == "RESTRICT"

    # Provider & scoping columns
    assert "provider" in table.c
    assert table.c["provider"].nullable is False
    assert "merchant_account_id" in table.c
    assert table.c["merchant_account_id"].nullable is False
    assert "environment" in table.c
    assert table.c["environment"].nullable is False

    # Provider customer ID (nullable, actual ID only)
    assert "provider_customer_id" in table.c
    assert table.c["provider_customer_id"].nullable is True

    # Status, claim, lease, error columns
    assert "status" in table.c
    assert table.c["status"].nullable is False
    assert "claim_operation_id" in table.c
    assert table.c["claim_operation_id"].nullable is True
    assert "lease_expires_at" in table.c
    assert table.c["lease_expires_at"].nullable is True
    assert "last_error_code" in table.c
    assert table.c["last_error_code"].nullable is True

    # Timestamps
    assert "created_at" in table.c
    assert "updated_at" in table.c

    # Unique constraint on (organization_id, provider, merchant_account_id, environment)
    uq_names = {uq.name for uq in table.constraints if hasattr(uq, "name") and uq.name}
    assert "uq_billing_customer_provisions_org_provider" in uq_names

    # Partial unique index on (provider, merchant_account_id, environment, provider_customer_id)
    index_names = {idx.name for idx in table.indexes}
    assert "uq_billing_customer_provisions_provider_customer" in index_names
    assert "ix_billing_customer_provisions_status" in index_names
    assert "ix_billing_customer_provisions_claim" in index_names
    assert "ix_billing_customer_provisions_lease" in index_names


def test_billing_customer_provision_instantiation() -> None:
    """Verify instantiating BillingCustomerProvision model defaults and values."""
    org_id = uuid.uuid4()
    prov = BillingCustomerProvision(
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_456",
        environment="test",
        provider_customer_id="cust_rzp_model_test",
        status=CustomerProvisionStatus.resolved,
    )
    assert prov.organization_id == org_id
    assert prov.provider == "razorpay"
    assert prov.merchant_account_id == "acc_test_456"
    assert prov.environment == "test"
    assert prov.provider_customer_id == "cust_rzp_model_test"
    assert prov.status == CustomerProvisionStatus.resolved
    assert prov.claim_operation_id is None
    assert prov.lease_expires_at is None

