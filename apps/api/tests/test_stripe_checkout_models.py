"""Tests for Stage 4.3B Stripe Checkout Attempts and Billing Reconciliations models and migration metadata.

These tests verify declarative schema definitions, table definitions, foreign keys,
constraints, partial unique indexes, enums, ORM relationships, cascade safeguards,
and offline Alembic DDL generation without connecting to any remote database.
"""

from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
    ReconciliationStatus,
    StripeCheckoutAttempt,
    StripeWebhookEvent,
    Subscription,
)


# =============================================================================
# 1. Table Registration & Metadata
# =============================================================================


def test_stage4_3b_tables_registered_in_metadata() -> None:
    """Verify both tables are correctly registered in SQLAlchemy Base.metadata."""
    assert "stripe_checkout_attempts" in Base.metadata.tables
    assert "billing_reconciliations" in Base.metadata.tables


# =============================================================================
# 2. Enum Definitions
# =============================================================================


def test_checkout_attempt_status_seven_states() -> None:
    """Verify CheckoutAttemptStatus enum contains all seven required states."""
    expected = {
        "creating",
        "recovery_required",
        "open",
        "payment_pending",
        "complete",
        "expired",
        "failed",
    }
    actual = {s.value for s in CheckoutAttemptStatus}
    assert actual == expected


def test_reconciliation_status_three_states() -> None:
    """Verify ReconciliationStatus enum contains all three required states."""
    expected = {
        "pending_review",
        "resolved_refunded",
        "resolved_ignored",
    }
    actual = {s.value for s in ReconciliationStatus}
    assert actual == expected


# =============================================================================
# 3. Stripe Checkout Attempts Schema & Constraints
# =============================================================================


def test_stripe_checkout_attempts_columns_and_types() -> None:
    """Verify column definitions, types, and nullability on stripe_checkout_attempts."""
    table = Base.metadata.tables["stripe_checkout_attempts"]
    col_names = {c.name for c in table.columns}
    expected_cols = {
        "id",
        "organization_id",
        "stripe_customer_id",
        "stripe_session_id",
        "stripe_subscription_id",
        "checkout_url",
        "request_payload",
        "status",
        "attempt_lease_expires_at",
        "session_expires_at",
        "error_message",
        "created_at",
        "updated_at",
    }
    assert col_names == expected_cols

    # Primary key
    assert table.c.id.primary_key is True
    assert isinstance(table.c.id.type, UUID)

    # Required fields
    assert table.c.organization_id.nullable is False
    assert table.c.stripe_customer_id.nullable is False
    assert table.c.request_payload.nullable is False
    assert isinstance(table.c.request_payload.type, JSONB)
    assert table.c.status.nullable is False
    assert table.c.attempt_lease_expires_at.nullable is False
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False

    # Optional fields
    assert table.c.stripe_session_id.nullable is True
    assert table.c.stripe_subscription_id.nullable is True
    assert table.c.checkout_url.nullable is True
    assert table.c.session_expires_at.nullable is True
    assert table.c.error_message.nullable is True


def test_stripe_checkout_attempts_foreign_keys_and_indexes() -> None:
    """Verify foreign keys, restrict behavior, and partial unique indexes on stripe_checkout_attempts."""
    table = Base.metadata.tables["stripe_checkout_attempts"]

    # Foreign key to organizations.id with ON DELETE RESTRICT
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "organizations"
    assert fk.column.name == "id"
    assert fk.ondelete == "RESTRICT"

    # Unique constraint on stripe_session_id
    uq_session = [
        c for c in table.constraints
        if getattr(c, "columns", None) is not None and "stripe_session_id" in [col.name for col in c.columns]
    ]
    assert len(uq_session) >= 1

    # Indexes
    index_names = {idx.name for idx in table.indexes}
    assert "ix_stripe_checkout_attempts_org_created" in index_names
    assert "ix_stripe_checkout_attempts_subscription_id" in index_names
    assert "uq_org_active_checkout_attempt" in index_names
    assert "ix_stripe_checkout_attempts_lease" in index_names

    # Partial unique index for active attempts
    active_idx = [idx for idx in table.indexes if idx.name == "uq_org_active_checkout_attempt"][0]
    assert active_idx.unique is True
    where_sql = str(active_idx.dialect_options["postgresql"]["where"])
    assert "status IN ('creating', 'recovery_required', 'open', 'payment_pending')" in where_sql


# =============================================================================
# 4. Billing Reconciliations Schema & Constraints
# =============================================================================


def test_billing_reconciliations_columns_and_types() -> None:
    """Verify column definitions, types, and nullability on billing_reconciliations."""
    table = Base.metadata.tables["billing_reconciliations"]
    col_names = {c.name for c in table.columns}
    expected_cols = {
        "id",
        "organization_id",
        "event_id",
        "existing_subscription_id",
        "incoming_subscription_id",
        "reason",
        "status",
        "diagnostic_payload",
        "resolved_by",
        "resolved_at",
        "resolution_notes",
        "created_at",
    }
    assert col_names == expected_cols

    # Primary key
    assert table.c.id.primary_key is True
    assert isinstance(table.c.id.type, UUID)

    # Required fields
    assert table.c.organization_id.nullable is False
    assert table.c.event_id.nullable is False
    assert table.c.existing_subscription_id.nullable is False
    assert table.c.incoming_subscription_id.nullable is False
    assert table.c.reason.nullable is False
    assert table.c.status.nullable is False
    assert table.c.diagnostic_payload.nullable is False
    assert isinstance(table.c.diagnostic_payload.type, JSONB)
    assert table.c.created_at.nullable is False

    # Resolution fields nullable until resolved
    assert table.c.resolved_by.nullable is True
    assert table.c.resolved_at.nullable is True
    assert table.c.resolution_notes.nullable is True


def test_billing_reconciliations_foreign_keys_and_uniqueness() -> None:
    """Verify non-cascading RESTRICT rules and duplicate-prevention uniqueness constraint."""
    table = Base.metadata.tables["billing_reconciliations"]

    # Foreign keys
    fk_targets = {(fk.column.table.name, fk.ondelete) for fk in table.foreign_keys}
    assert ("organizations", "RESTRICT") in fk_targets
    assert ("stripe_webhook_events", "RESTRICT") in fk_targets

    # Unique constraint on (event_id, incoming_subscription_id)
    uqs = [
        c for c in table.constraints
        if getattr(c, "columns", None) is not None and len(c.columns) == 2
        and {"event_id", "incoming_subscription_id"} == {col.name for col in c.columns}
    ]
    assert len(uqs) == 1
    assert uqs[0].name == "uq_reconciliation_event_sub"

    # Index on (organization_id, status)
    index_names = {idx.name for idx in table.indexes}
    assert "ix_billing_reconciliations_org_status" in index_names


# =============================================================================
# 5. ORM Relationships
# =============================================================================


def test_organization_orm_relationships() -> None:
    """Verify Organization model relationships for checkout attempts and reconciliations."""
    org_rels = Organization.__mapper__.relationships
    assert "checkout_attempts" in org_rels
    assert "billing_reconciliations" in org_rels

    # Reverse relationships
    attempt_rels = StripeCheckoutAttempt.__mapper__.relationships
    assert "organization" in attempt_rels

    reconcile_rels = BillingReconciliation.__mapper__.relationships
    assert "organization" in reconcile_rels
    assert "webhook_event" in reconcile_rels


# =============================================================================
# 6. Offline Alembic Migration DDL Generation
# =============================================================================


def test_alembic_offline_upgrade_and_downgrade_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL DDL for revision d4e5f6a1b2c3."""
    api_dir = Path(__file__).resolve().parent.parent

    # Test upgrade DDL generation
    upgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "c3d4e5f6a1b2:d4e5f6a1b2c3", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert upgrade_res.returncode == 0, f"Alembic upgrade --sql failed:\n{upgrade_res.stderr}"
    upgrade_sql = upgrade_res.stdout
    assert "CREATE TYPE checkout_attempt_status AS ENUM" in upgrade_sql
    assert "'payment_pending'" in upgrade_sql
    assert "'recovery_required'" in upgrade_sql
    assert "CREATE TABLE stripe_checkout_attempts" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_org_active_checkout_attempt" in upgrade_sql
    assert "status IN ('creating', 'recovery_required', 'open', 'payment_pending')" in upgrade_sql
    assert "CREATE TYPE reconciliation_status AS ENUM" in upgrade_sql
    assert "CREATE TABLE billing_reconciliations" in upgrade_sql
    assert "CONSTRAINT uq_reconciliation_event_sub UNIQUE (event_id, incoming_subscription_id)" in upgrade_sql
    assert "ALTER TABLE stripe_checkout_attempts ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert "ALTER TABLE billing_reconciliations ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert "REVOKE ALL ON stripe_checkout_attempts FROM anon, authenticated" in upgrade_sql
    assert "REVOKE ALL ON billing_reconciliations FROM anon, authenticated" in upgrade_sql
    assert "UPDATE alembic_version SET version_num='d4e5f6a1b2c3'" in upgrade_sql

    # Test downgrade DDL generation
    downgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "d4e5f6a1b2c3:c3d4e5f6a1b2", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert downgrade_res.returncode == 0, f"Alembic downgrade --sql failed:\n{downgrade_res.stderr}"
    downgrade_sql = downgrade_res.stdout
    assert "ALTER TABLE billing_reconciliations DISABLE ROW LEVEL SECURITY" in downgrade_sql
    assert "ALTER TABLE stripe_checkout_attempts DISABLE ROW LEVEL SECURITY" in downgrade_sql
    assert "DROP TABLE billing_reconciliations" in downgrade_sql
    assert "DROP TYPE IF EXISTS reconciliation_status" in downgrade_sql
    assert "DROP INDEX uq_org_active_checkout_attempt" in downgrade_sql
    assert "DROP TABLE stripe_checkout_attempts" in downgrade_sql
    assert "DROP TYPE IF EXISTS checkout_attempt_status" in downgrade_sql
    assert "UPDATE alembic_version SET version_num='c3d4e5f6a1b2'" in downgrade_sql


def test_alembic_offline_e5f6a1b2c3d4_upgrade_and_downgrade_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL DDL for revision e5f6a1b2c3d4."""
    api_dir = Path(__file__).resolve().parent.parent

    # Test upgrade DDL generation
    upgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "d4e5f6a1b2c3:e5f6a1b2c3d4", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert upgrade_res.returncode == 0, f"Alembic upgrade --sql failed:\n{upgrade_res.stderr}"
    upgrade_sql = upgrade_res.stdout
    assert "ALTER TABLE stripe_checkout_attempts ADD COLUMN stripe_subscription_id VARCHAR(255)" in upgrade_sql
    assert "CREATE INDEX ix_stripe_checkout_attempts_subscription_id ON stripe_checkout_attempts (stripe_subscription_id)" in upgrade_sql
    assert "UPDATE alembic_version SET version_num='e5f6a1b2c3d4'" in upgrade_sql

    # Test downgrade DDL generation
    downgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "e5f6a1b2c3d4:d4e5f6a1b2c3", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert downgrade_res.returncode == 0, f"Alembic downgrade --sql failed:\n{downgrade_res.stderr}"
    downgrade_sql = downgrade_res.stdout
    assert "DROP INDEX ix_stripe_checkout_attempts_subscription_id" in downgrade_sql
    assert "ALTER TABLE stripe_checkout_attempts DROP COLUMN stripe_subscription_id" in downgrade_sql
    assert "UPDATE alembic_version SET version_num='d4e5f6a1b2c3'" in downgrade_sql
