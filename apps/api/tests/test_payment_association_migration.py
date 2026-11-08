"""Tests for Step 5.2D.3.3A Alembic migration e2f3a4b5c6d1 (Payment Association & Entitlement Provenance).

Covers:
1. Migration file identity and DAG integrity:
   - Revision ID: e2f3a4b5c6d1
   - Down revision: d1e2f3a4b5c6
2. Offline Alembic Upgrade DDL generation:
   - Column additions: subscription_id, provider_payment_id, checkout_attempt_id.
   - Partial unique indexes: uq_checkout_attempts_provider_sub, uq_checkout_attempts_provider_payment, uq_billing_payments_checkout_attempt.
   - Composite unique constraint: uq_bca_composite_identity.
   - Foreign keys: fk_billing_periods_subscription_id, fk_billing_payments_checkout_attempt, fk_billing_payments_checkout_attempt_composite.
   - Check constraint: ck_billing_payments_checkout_attempt_complete.
   - Deterministic backfill queries for Stripe and multi-provider billing periods.
   - Transactional abort gate PL/pgSQL block for unassociated active Pro periods.
   - Immutability triggers on billing_payments, billing_checkout_attempts, and organization_billing_periods.
   - Row-Level Security enable statements and revoke statements from anon and authenticated.
3. Offline Alembic Downgrade DDL generation:
   - Concurrency-safe downgrade guard with ACCESS EXCLUSIVE table locks.
   - Pre-drop data loss exception barrier.
   - Dropping triggers and functions.
   - Dropping constraints, indexes, and columns in reverse dependency order.
4. Model-level instantiation and attribute presence:
   - OrganizationBillingPeriod.subscription_id and relationship.
   - BillingCheckoutAttempt.provider_payment_id and relationships.
   - BillingPayment.checkout_attempt_id and relationships.
   - Subscription.billing_periods relationship.
5. Invariant enforcement in model __table_args__:
   - Constraint and index name verification.
   - Relationship mapper ambiguity check (foreign_keys disambiguation).
6. Scope containment:
   - Absence of payment settlement handlers or sweeper background tasks in this migration phase.
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

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
import uuid
import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from app.db.base import Base
import app.models.organization  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import Organization
from app.models.payment import BillingCheckoutAttempt, BillingPayment
from app.models.subscription import CheckoutAttemptStatus, Subscription, SubscriptionStatus


def test_migration_metadata() -> None:
    """Verify revision and down_revision match expected linear DAG."""
    api_dir = Path(__file__).resolve().parent.parent
    migration_file = (
        api_dir
        / "alembic"
        / "versions"
        / "20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py"
    )
    assert migration_file.exists(), f"Migration file not found at {migration_file}"

    spec = importlib.util.spec_from_file_location("migration_e2f3a4b5c6d1", migration_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "e2f3a4b5c6d1"
    assert migration.down_revision == "d1e2f3a4b5c6"


def test_offline_upgrade_ddl_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL upgrade DDL for revision e2f3a4b5c6d1."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "d1e2f3a4b5c6:e2f3a4b5c6d1", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic upgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Column additions
    assert "ALTER TABLE organization_billing_periods ADD COLUMN subscription_id UUID" in sql
    assert "ALTER TABLE billing_checkout_attempts ADD COLUMN provider_payment_id VARCHAR(255)" in sql
    assert "ALTER TABLE billing_payments ADD COLUMN checkout_attempt_id UUID" in sql

    # 2. Foreign keys and indexes
    assert "fk_billing_periods_subscription_id FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE RESTRICT" in sql
    assert "CREATE INDEX ix_billing_periods_subscription_id ON organization_billing_periods (subscription_id)" in sql

    assert "CREATE UNIQUE INDEX uq_checkout_attempts_provider_sub ON billing_checkout_attempts" in sql
    assert "WHERE provider_subscription_id IS NOT NULL" in sql
    assert "CREATE UNIQUE INDEX uq_checkout_attempts_provider_payment ON billing_checkout_attempts" in sql
    assert "WHERE provider_payment_id IS NOT NULL" in sql
    assert "ADD CONSTRAINT uq_bca_composite_identity UNIQUE" in sql

    assert "fk_billing_payments_checkout_attempt FOREIGN KEY(checkout_attempt_id) REFERENCES billing_checkout_attempts (id) ON DELETE RESTRICT" in sql
    assert "CREATE INDEX ix_billing_payments_checkout_attempt ON billing_payments (checkout_attempt_id)" in sql
    assert "CREATE UNIQUE INDEX uq_billing_payments_checkout_attempt ON billing_payments (checkout_attempt_id)" in sql

    # 3. Check and Composite Foreign Key on billing_payments
    assert "ck_billing_payments_checkout_attempt_complete CHECK" in sql
    assert "fk_billing_payments_checkout_attempt_composite FOREIGN KEY" in sql

    # 4. Pre-backfill Conflict Detection & Unified Backfill SQL
    assert "conflicting subscription evidence across invoices" in sql
    assert "existing provenance disagreeing with invoice evidence" in sql
    assert "WITH unified_invoice_candidates AS (" in sql
    assert "candidate_counts AS (" in sql
    assert "unambiguous_candidates AS (" in sql
    assert "HAVING COUNT(DISTINCT subscription_id) > 1" in sql
    assert "WHERE cc.sub_count = 1" in sql

    # 5. Transactional Abort Gate
    assert "CRITICAL MIGRATION ABORT" in sql
    assert "lack proven subscription provenance" in sql

    # 6. Immutability triggers
    assert "prevent_billing_payment_checkout_attempt_mutation" in sql
    assert "prevent_checkout_attempt_payment_mutation" in sql
    assert "prevent_billing_period_subscription_mutation" in sql

    # 7. Row Level Security and Revocations
    assert "ALTER TABLE organization_billing_periods ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON organization_billing_periods FROM anon, authenticated" in sql
    assert "ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated" in sql
    assert "ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_payments FROM anon, authenticated" in sql


def test_offline_downgrade_ddl_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL downgrade DDL with safety guards."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "e2f3a4b5c6d1:d1e2f3a4b5c6", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic downgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Downgrade guard with ACCESS EXCLUSIVE locks
    assert "LOCK TABLE billing_payments IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE organization_billing_periods IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE" in sql
    assert "Alembic Downgrade Aborted" in sql

    # 2. Trigger and function drops
    assert "DROP TRIGGER IF EXISTS trg_prevent_billing_period_subscription_mutation" in sql
    assert "DROP FUNCTION IF EXISTS prevent_billing_period_subscription_mutation()" in sql
    assert "DROP TRIGGER IF EXISTS trg_prevent_checkout_attempt_payment_mutation" in sql
    assert "DROP FUNCTION IF EXISTS prevent_checkout_attempt_payment_mutation()" in sql
    assert "DROP TRIGGER IF EXISTS trg_prevent_billing_payment_checkout_attempt_mutation" in sql
    assert "DROP FUNCTION IF EXISTS prevent_billing_payment_checkout_attempt_mutation()" in sql

    # 3. Drops in reverse dependency order
    assert "ALTER TABLE billing_payments DROP CONSTRAINT fk_billing_payments_checkout_attempt_composite" in sql
    assert "ALTER TABLE billing_payments DROP CONSTRAINT ck_billing_payments_checkout_attempt_complete" in sql
    assert "DROP INDEX uq_billing_payments_checkout_attempt" in sql
    assert "ALTER TABLE billing_payments DROP COLUMN checkout_attempt_id" in sql

    assert "ALTER TABLE billing_checkout_attempts DROP CONSTRAINT uq_bca_composite_identity" in sql
    assert "DROP INDEX uq_checkout_attempts_provider_payment" in sql
    assert "DROP INDEX uq_checkout_attempts_provider_sub" in sql
    assert "ALTER TABLE billing_checkout_attempts DROP COLUMN provider_payment_id" in sql

    assert "DROP INDEX ix_billing_periods_subscription_id" in sql
    assert "ALTER TABLE organization_billing_periods DROP CONSTRAINT fk_billing_periods_subscription_id" in sql
    assert "ALTER TABLE organization_billing_periods DROP COLUMN subscription_id" in sql


def test_organization_billing_period_model_provenance_instantiation() -> None:
    """Verify OrganizationBillingPeriod model instantiates with subscription_id and default None."""
    org_id = uuid.uuid4()
    sub_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # 1. Nullable subscription_id default
    period_free = OrganizationBillingPeriod(
        organization_id=org_id,
        period_start=now,
        period_end=now,
        plan_tier="free",
        monthly_token_quota=100000,
    )
    assert period_free.subscription_id is None

    # 2. Explicit subscription_id for Pro
    period_pro = OrganizationBillingPeriod(
        organization_id=org_id,
        subscription_id=sub_id,
        period_start=now,
        period_end=now,
        plan_tier="pro",
        monthly_token_quota=2000000,
    )
    assert period_pro.subscription_id == sub_id


def test_organization_billing_period_table_args() -> None:
    """Verify index on subscription_id is present in OrganizationBillingPeriod.__table_args__."""
    table_args = OrganizationBillingPeriod.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "ix_billing_periods_subscription_id" in index_names


def test_billing_checkout_attempt_payment_id_instantiation() -> None:
    """Verify BillingCheckoutAttempt model supports provider_payment_id attribute."""
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    attempt = BillingCheckoutAttempt(
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_live_123",
        environment="live",
        provider_subscription_id="sub_rzp_123",
        provider_payment_id="pay_rzp_456",
        idempotency_key=str(uuid.uuid4()),
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=4900,
        currency="USD",
        attempt_lease_expires_at=now,
    )
    assert attempt.provider_payment_id == "pay_rzp_456"
    assert attempt.provider_subscription_id == "sub_rzp_123"


def test_billing_checkout_attempt_table_args_constraints() -> None:
    """Verify unique constraints and partial indexes in BillingCheckoutAttempt.__table_args__."""
    table_args = BillingCheckoutAttempt.__table_args__

    unique_constraints = [arg.name for arg in table_args if isinstance(arg, UniqueConstraint)]
    assert "uq_bca_composite_identity" in unique_constraints

    indexes = {arg.name: arg for arg in table_args if isinstance(arg, Index)}
    assert "uq_checkout_attempts_provider_sub" in indexes
    assert indexes["uq_checkout_attempts_provider_sub"].unique is True

    assert "uq_checkout_attempts_provider_payment" in indexes
    assert indexes["uq_checkout_attempts_provider_payment"].unique is True


def test_billing_payment_checkout_attempt_instantiation() -> None:
    """Verify BillingPayment model supports checkout_attempt_id attribute."""
    attempt_id = uuid.uuid4()
    payment = BillingPayment(
        provider="razorpay",
        merchant_account_id="acc_live_123",
        environment="live",
        provider_payment_id="pay_rzp_789",
        checkout_attempt_id=attempt_id,
        amount_minor_units=4900,
        currency="USD",
        status="captured",
    )
    assert payment.checkout_attempt_id == attempt_id


def test_billing_payment_table_args_constraints() -> None:
    """Verify constraints and indexes in BillingPayment.__table_args__."""
    table_args = BillingPayment.__table_args__

    # Check constraint
    check_constraints = [arg.name for arg in table_args if isinstance(arg, CheckConstraint)]
    assert "ck_billing_payments_checkout_attempt_complete" in check_constraints

    # Composite foreign key constraint
    fk_constraints = [arg.name for arg in table_args if isinstance(arg, ForeignKeyConstraint)]
    assert "fk_billing_payments_checkout_attempt_composite" in fk_constraints

    # Partial unique index
    indexes = {arg.name: arg for arg in table_args if isinstance(arg, Index)}
    assert "uq_billing_payments_checkout_attempt" in indexes
    assert indexes["uq_billing_payments_checkout_attempt"].unique is True


def test_sqlalchemy_mapper_relationships_configuration() -> None:
    """Verify configure_mappers succeeds without ambiguity between BCA and BillingPayment."""
    # This specifically tests that foreign_keys disambiguation prevents InvalidRequestError
    configure_mappers()

    # Inspect relationships on mapper
    bca_mapper = BillingCheckoutAttempt.__mapper__
    assert "payments" in bca_mapper.relationships
    bp_mapper = BillingPayment.__mapper__
    assert "checkout_attempt" in bp_mapper.relationships

    sub_mapper = Subscription.__mapper__
    assert "billing_periods" in sub_mapper.relationships
    obp_mapper = OrganizationBillingPeriod.__mapper__
    assert "subscription" in obp_mapper.relationships


def test_subscription_and_billing_period_relationship_bidirectional() -> None:
    """Verify bidirectional relationship mapping between Subscription and OrganizationBillingPeriod."""
    sub = Subscription(
        organization_id=uuid.uuid4(),
        status=SubscriptionStatus.active,
        plan_tier="pro",
        provider="razorpay",
        merchant_account_id="acc_live_123",
        environment="live",
        provider_customer_id="cust_test_123",
    )
    now = datetime.now(timezone.utc)
    period = OrganizationBillingPeriod(
        organization_id=sub.organization_id,
        period_start=now,
        period_end=now,
        plan_tier="pro",
        monthly_token_quota=2000000,
        subscription=sub,
    )
    assert period.subscription == sub
    assert period in sub.billing_periods


def test_checkout_attempt_and_payment_relationship_bidirectional() -> None:
    """Verify bidirectional relationship mapping between BillingCheckoutAttempt and BillingPayment."""
    now = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        organization_id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_live_123",
        environment="live",
        provider_subscription_id="sub_test_123",
        provider_payment_id="pay_test_123",
        idempotency_key=str(uuid.uuid4()),
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=4900,
        currency="USD",
        attempt_lease_expires_at=now,
    )
    payment = BillingPayment(
        provider="razorpay",
        merchant_account_id="acc_live_123",
        environment="live",
        provider_payment_id="pay_test_123",
        amount_minor_units=4900,
        currency="USD",
        status="captured",
        checkout_attempt=attempt,
    )
    assert payment.checkout_attempt == attempt
    assert payment in attempt.payments


def test_check_constraint_sql_text_structure() -> None:
    """Verify the exact text of ck_billing_payments_checkout_attempt_complete."""
    table_args = BillingPayment.__table_args__
    check_constraint = next(
        arg for arg in table_args if isinstance(arg, CheckConstraint) and arg.name == "ck_billing_payments_checkout_attempt_complete"
    )
    sql_text = str(check_constraint.sqltext)
    assert "checkout_attempt_id IS NULL" in sql_text
    assert "provider IS NOT NULL" in sql_text
    assert "merchant_account_id IS NOT NULL" in sql_text
    assert "environment IS NOT NULL" in sql_text
    assert "provider_subscription_id IS NOT NULL" in sql_text
    assert "provider_payment_id IS NOT NULL" in sql_text


def test_composite_foreign_key_column_alignment() -> None:
    """Verify column alignment in fk_billing_payments_checkout_attempt_composite."""
    table_args = BillingPayment.__table_args__
    fk = next(
        arg for arg in table_args if isinstance(arg, ForeignKeyConstraint) and arg.name == "fk_billing_payments_checkout_attempt_composite"
    )
    source_cols = [col if isinstance(col, str) else col.name for col in fk.columns]
    target_cols = [elem.target_fullname for elem in fk.elements]

    assert source_cols == [
        "checkout_attempt_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_subscription_id",
        "provider_payment_id",
    ]
    assert target_cols == [
        "billing_checkout_attempts.id",
        "billing_checkout_attempts.provider",
        "billing_checkout_attempts.merchant_account_id",
        "billing_checkout_attempts.environment",
        "billing_checkout_attempts.provider_subscription_id",
        "billing_checkout_attempts.provider_payment_id",
    ]


def test_composite_unique_identity_column_alignment() -> None:
    """Verify column alignment in uq_bca_composite_identity."""
    table_args = BillingCheckoutAttempt.__table_args__
    uq = next(
        arg for arg in table_args if isinstance(arg, UniqueConstraint) and arg.name == "uq_bca_composite_identity"
    )
    cols = [col if isinstance(col, str) else col.name for col in uq.columns]
    assert cols == [
        "id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_subscription_id",
        "provider_payment_id",
    ]


def test_metadata_table_columns_and_nullability() -> None:
    """Verify column definitions, types, and nullability in Base.metadata.tables."""
    # 1. organization_billing_periods.subscription_id
    obp_table = Base.metadata.tables["organization_billing_periods"]
    assert "subscription_id" in obp_table.c
    sub_col = obp_table.c["subscription_id"]
    assert sub_col.nullable is True

    # 2. billing_checkout_attempts.provider_payment_id
    bca_table = Base.metadata.tables["billing_checkout_attempts"]
    assert "provider_payment_id" in bca_table.c
    bca_pay_col = bca_table.c["provider_payment_id"]
    assert bca_pay_col.nullable is True

    # 3. billing_payments.checkout_attempt_id
    bp_table = Base.metadata.tables["billing_payments"]
    assert "checkout_attempt_id" in bp_table.c
    bp_attempt_col = bp_table.c["checkout_attempt_id"]
    assert bp_attempt_col.nullable is True


def test_absence_of_settlement_handlers_or_sweeper_in_step_3_3a() -> None:
    """Verify Step 5.2D.3.3A boundaries: settlement handlers and sweeper are not yet implemented."""
    # Ensure payment binding sweeper worker module does NOT yet exist
    sweeper_worker = Path(__file__).resolve().parent.parent / "app" / "workers" / "payment_binding_sweeper.py"
    assert not sweeper_worker.exists(), "PaymentBindingSweeper must not be created in Step 5.2D.3.3A"

    # Ensure payment settlement handlers module does NOT yet exist
    settlement_handlers = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "services"
        / "billing"
        / "razorpay"
        / "settlement_handlers.py"
    )
    assert not settlement_handlers.exists(), "Razorpay settlement_handlers must not be created in Step 5.2D.3.3A"
