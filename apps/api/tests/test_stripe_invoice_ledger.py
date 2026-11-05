"""Tests for Stage 4.3B Step 3B StripeInvoice model, migration f6a1b2c3d4e5, and onboarding.

Covers:
1. StripeInvoice model registration, attributes, and constraints:
   - Single explicit unique constraint uq_stripe_invoices_invoice_id.
   - Foreign keys: organization_id (RESTRICT), raw_event_id (RESTRICT), billing_period_id (SET NULL).
   - Non-unique billing_period_id index.
   - Minor units currency column amount_paid_minor_units.
   - Absence of hosted_invoice_url and invoice_pdf fields.
2. WebhookProcessingStatus enum:
   - Contains 'quarantined' state.
3. Subscription model:
   - Contains nullable past_due_since timestamp.
4. Offline Alembic migration f6a1b2c3d4e5 DDL generation:
   - Upgrade DDL: autocommit enum expansion, past_due_since column, stripe_invoices table, RLS, revokes.
   - Downgrade DDL: quarantine audit guard, table drop, column drop, enum rollback.
5. Organization Onboarding:
   - setup_initial_organization creates Organization, OrganizationMember, and active Free OrganizationBillingPeriod.
   - Zero fake subscriptions.
   - Initial period anchored to 30 days.
   - Configured default_free_monthly_token_quota.
6. Startup Warning:
   - Emits warning when default_free_monthly_token_quota <= 0.
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

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.subscription import (
    InvoiceProcessingStatus,
    StripeInvoice,
    StripeWebhookEvent,
    Subscription,
    WebhookProcessingStatus,
)
from app.schemas.organization import OrganizationCreate


# =============================================================================
# 1. Model Registration & Column Specifications
# =============================================================================


def test_stripe_invoice_model_registered_in_metadata() -> None:
    """Verify stripe_invoices table is registered in SQLAlchemy Base.metadata."""
    assert "stripe_invoices" in Base.metadata.tables
    table = Base.metadata.tables["stripe_invoices"]

    # Primary key
    assert table.c.id.primary_key is True

    # Columns
    assert isinstance(table.c.stripe_invoice_id.type, String)
    assert table.c.stripe_invoice_id.nullable is False

    assert isinstance(table.c.amount_paid_minor_units.type, BigInteger)
    assert table.c.amount_paid_minor_units.nullable is False

    assert isinstance(table.c.organization_id.type, UUID)
    assert table.c.organization_id.nullable is False

    assert isinstance(table.c.billing_period_id.type, UUID)
    assert table.c.billing_period_id.nullable is True

    assert isinstance(table.c.raw_event_id.type, String)
    assert table.c.raw_event_id.nullable is True

    assert isinstance(table.c.diagnostic_payload.type, JSONB)

    # Verify ephemeral URL columns were NOT added
    assert "hosted_invoice_url" not in table.c
    assert "invoice_pdf" not in table.c


def test_stripe_invoice_single_explicit_unique_constraint() -> None:
    """Verify stripe_invoice_id has exactly one unique constraint and no redundant duplicate."""
    table = Base.metadata.tables["stripe_invoices"]
    unique_constraints = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    invoice_uniques = [
        c for c in unique_constraints if any(col.name == "stripe_invoice_id" for col in c.columns)
    ]
    assert len(invoice_uniques) == 1
    assert invoice_uniques[0].name == "uq_stripe_invoices_invoice_id"

    # Verify column itself does not define a separate duplicate unique index
    col_indexes = [
        idx for idx in table.indexes if any(col.name == "stripe_invoice_id" for col in idx.columns)
    ]
    # No redundant index on stripe_invoice_id
    assert len(col_indexes) == 0


def test_stripe_invoice_foreign_keys_and_ondelete() -> None:
    """Verify exact foreign key targets and deletion safeguards."""
    table = Base.metadata.tables["stripe_invoices"]
    fk_map = {fk.parent.name: fk for fk in table.foreign_keys}

    # organization_id -> organizations.id ON DELETE RESTRICT
    assert "organization_id" in fk_map
    assert fk_map["organization_id"].target_fullname == "organizations.id"
    assert fk_map["organization_id"].ondelete == "RESTRICT"

    # raw_event_id -> stripe_webhook_events.id ON DELETE RESTRICT
    assert "raw_event_id" in fk_map
    assert fk_map["raw_event_id"].target_fullname == "stripe_webhook_events.id"
    assert fk_map["raw_event_id"].ondelete == "RESTRICT"

    # billing_period_id -> organization_billing_periods.id ON DELETE SET NULL
    assert "billing_period_id" in fk_map
    assert fk_map["billing_period_id"].target_fullname == "organization_billing_periods.id"
    assert fk_map["billing_period_id"].ondelete == "SET NULL"


def test_stripe_invoice_indexes() -> None:
    """Verify non-unique index on billing_period_id and required lookup indexes."""
    table = Base.metadata.tables["stripe_invoices"]
    index_names = {idx.name for idx in table.indexes}

    assert "ix_stripe_invoices_organization_id" in index_names
    assert "ix_stripe_invoices_customer_id" in index_names
    assert "ix_stripe_invoices_subscription_id" in index_names
    assert "ix_stripe_invoices_billing_period_id" in index_names
    assert "ix_stripe_invoices_raw_event_id" in index_names
    assert "ix_stripe_invoices_org_created" in index_names
    assert "ix_stripe_invoices_status" in index_names

    # Verify billing_period_id index is NOT unique (allows multiple invoices per period)
    bp_idx = next(idx for idx in table.indexes if idx.name == "ix_stripe_invoices_billing_period_id")
    assert bp_idx.unique is False


def test_invoice_processing_status_enum() -> None:
    """Verify InvoiceProcessingStatus enum values."""
    expected = {"pending", "processed", "ignored", "failed"}
    actual = {s.value for s in InvoiceProcessingStatus}
    assert actual == expected


def test_webhook_processing_status_quarantined_state() -> None:
    """Verify WebhookProcessingStatus contains quarantined state."""
    assert "quarantined" in {s.value for s in WebhookProcessingStatus}
    assert WebhookProcessingStatus.quarantined.value == "quarantined"


def test_subscription_past_due_since_column() -> None:
    """Verify Subscription model contains nullable past_due_since column."""
    table = Base.metadata.tables["subscriptions"]
    assert "past_due_since" in table.c
    col = table.c.past_due_since
    assert col.nullable is True
    assert isinstance(col.type, DateTime)


def test_organization_stripe_invoices_relationship() -> None:
    """Verify Organization model defines stripe_invoices back_populates relationship."""
    assert hasattr(Organization, "stripe_invoices")


# =============================================================================
# 2. Offline Alembic Migration DDL Verification
# =============================================================================


def test_alembic_offline_f6a1b2c3d4e5_upgrade_and_downgrade_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL DDL for revision f6a1b2c3d4e5."""
    api_dir = Path(__file__).resolve().parent.parent

    # Test upgrade DDL generation
    upgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "e5f6a1b2c3d4:f6a1b2c3d4e5", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert upgrade_res.returncode == 0, f"Alembic upgrade --sql failed:\n{upgrade_res.stderr}"
    upgrade_sql = upgrade_res.stdout

    # 1. Autocommit block for enum expansion
    assert "ALTER TYPE webhook_processing_status ADD VALUE IF NOT EXISTS 'quarantined'" in upgrade_sql
    # 2. Subscriptions past_due_since column
    assert "ALTER TABLE subscriptions ADD COLUMN past_due_since TIMESTAMP WITH TIME ZONE" in upgrade_sql
    # 3. Invoices table & columns
    assert "CREATE TABLE stripe_invoices" in upgrade_sql
    assert "amount_paid_minor_units BIGINT DEFAULT '0' NOT NULL" in upgrade_sql
    # 4. Foreign key constraints
    assert "FOREIGN KEY(billing_period_id) REFERENCES organization_billing_periods (id) ON DELETE SET NULL" in upgrade_sql
    assert "FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT" in upgrade_sql
    assert "FOREIGN KEY(raw_event_id) REFERENCES stripe_webhook_events (id) ON DELETE RESTRICT" in upgrade_sql
    # 5. Single unique constraint
    assert "CONSTRAINT uq_stripe_invoices_invoice_id UNIQUE (stripe_invoice_id)" in upgrade_sql
    # 6. Indexes
    assert "CREATE INDEX ix_stripe_invoices_billing_period_id ON stripe_invoices (billing_period_id)" in upgrade_sql
    assert "CREATE INDEX ix_stripe_invoices_raw_event_id ON stripe_invoices (raw_event_id)" in upgrade_sql
    # 7. RLS and privilege revokes
    assert "ALTER TABLE stripe_invoices ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert "REVOKE ALL ON stripe_invoices FROM anon, authenticated" in upgrade_sql
    assert "UPDATE alembic_version SET version_num='f6a1b2c3d4e5'" in upgrade_sql

    # Test downgrade DDL generation
    downgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "f6a1b2c3d4e5:e5f6a1b2c3d4", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert downgrade_res.returncode == 0, f"Alembic downgrade --sql failed:\n{downgrade_res.stderr}"
    downgrade_sql = downgrade_res.stdout

    assert "ALTER TABLE stripe_invoices DISABLE ROW LEVEL SECURITY" in downgrade_sql
    assert "DROP TABLE stripe_invoices" in downgrade_sql
    assert "DROP TYPE IF EXISTS invoice_processing_status" in downgrade_sql
    assert "ALTER TABLE subscriptions DROP COLUMN past_due_since" in downgrade_sql
    assert "CREATE TYPE webhook_processing_status AS ENUM ('pending', 'processed', 'failed')" in downgrade_sql
    assert "UPDATE alembic_version SET version_num='e5f6a1b2c3d4'" in downgrade_sql


# =============================================================================
# 3. Atomic Organization Onboarding & Startup Warning
# =============================================================================


@pytest.mark.anyio
async def test_atomic_onboarding_provisions_billing_period() -> None:
    """Verify setup_initial_organization provisions an active Free billing period in the same transaction."""
    from app.api.organizations import setup_initial_organization
    from app.auth.dependencies import AuthenticatedUser

    user_id = uuid.uuid4()
    user = AuthenticatedUser(id=user_id, email="owner@example.com", is_paid=False)
    body = OrganizationCreate(name="Acme Free Org")

    mock_db = AsyncMock()
    # No existing owned organizations
    mock_db.scalar.return_value = 0
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_db.get_bind = MagicMock(return_value=mock_bind)

    added_objects = []

    def fake_add(obj):
        added_objects.append(obj)

    async def fake_flush():
        for o in added_objects:
            if hasattr(o, "id") and o.id is None:
                o.id = uuid.uuid4()
            if hasattr(o, "created_at") and getattr(o, "created_at", None) is None:
                o.created_at = datetime.now(timezone.utc)
            if hasattr(o, "updated_at") and getattr(o, "updated_at", None) is None:
                o.updated_at = datetime.now(timezone.utc)

    mock_db.add = MagicMock(side_effect=fake_add)
    mock_db.flush = AsyncMock(side_effect=fake_flush)

    # Call onboarding handler
    with patch("app.api.organizations.settings") as mock_settings:
        mock_settings.default_free_monthly_token_quota = 50000

        res = await setup_initial_organization(body=body, current_user=user, db=mock_db)

    # Verify commit was executed once (atomic transaction)
    mock_db.commit.assert_called_once()

    # Verify added objects: Organization, OrganizationMember, OrganizationBillingPeriod
    assert len(added_objects) == 3
    org = next(o for o in added_objects if isinstance(o, Organization))
    member = next(o for o in added_objects if isinstance(o, OrganizationMember))
    period = next(o for o in added_objects if isinstance(o, OrganizationBillingPeriod))

    assert org.name == "Acme Free Org"
    assert member.user_id == user_id
    assert member.role == MemberRole.owner

    # Verify billing period properties
    assert period.plan_tier == "free"
    assert period.is_active is True
    assert period.is_blocked is False
    assert period.tokens_used == 0
    assert period.tokens_reserved == 0
    assert period.cost_micros_used == 0
    assert period.monthly_token_quota == 50000
    assert (period.period_end - period.period_start) == timedelta(days=30)

    # Verify NO fake Subscription was created
    assert not any(isinstance(o, Subscription) for o in added_objects)


def test_free_quota_startup_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Verify startup warning is emitted when default_free_monthly_token_quota <= 0."""
    from app.config import Settings
    from app.main import lifespan
    from fastapi import FastAPI

    test_app = FastAPI()
    with patch("app.main.settings", Settings(default_free_monthly_token_quota=0)):
        with caplog.at_level(logging.WARNING):
            # Enter lifespan context
            import asyncio
            async def run():
                async with lifespan(test_app):
                    pass
            asyncio.run(run())

    assert any(
        "DEFAULT_FREE_MONTHLY_TOKEN_QUOTA is configured to 0" in record.message
        for record in caplog.records
    )


@pytest.mark.anyio
async def test_atomic_onboarding_rollback_on_billing_period_failure() -> None:
    """Verify that if initial billing period provisioning fails, the transaction rolls back cleanly.

    Simulates an IntegrityError during atomic onboarding (e.g., billing period constraint violation).
    Confirms:
    1. db.rollback() is invoked.
    2. HTTPException with status 409 Conflict is raised.
    3. The transaction is not committed.
    """
    from app.api.organizations import setup_initial_organization
    from app.auth.dependencies import AuthenticatedUser
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    user_id = uuid.uuid4()
    user = AuthenticatedUser(id=user_id, email="owner@example.com", is_paid=False)
    body = OrganizationCreate(name="Acme Failing Org")

    mock_db = AsyncMock()
    mock_db.scalar.return_value = 0
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_db.get_bind = MagicMock(return_value=mock_bind)

    async def fake_flush():
        pass

    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock(side_effect=fake_flush)
    mock_db.commit.side_effect = IntegrityError(
        "INSERT INTO organization_billing_periods", {}, Exception("billing period constraint violation")
    )

    with patch("app.api.organizations.settings") as mock_settings:
        mock_settings.default_free_monthly_token_quota = 50000

        with pytest.raises(HTTPException) as exc_info:
            await setup_initial_organization(body=body, current_user=user, db=mock_db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Conflict creating organization"
    mock_db.rollback.assert_called_once()


def test_stripe_invoice_table_metadata_parity() -> None:
    """Verify complete column and constraint parity of stripe_invoices table in SQLAlchemy metadata."""
    table = Base.metadata.tables["stripe_invoices"]

    expected_columns = {
        "id",
        "stripe_invoice_id",
        "organization_id",
        "stripe_customer_id",
        "stripe_subscription_id",
        "billing_reason",
        "invoice_status",
        "currency",
        "amount_paid_minor_units",
        "period_start",
        "period_end",
        "processing_status",
        "billing_period_id",
        "raw_event_id",
        "attempt_count",
        "error_message",
        "diagnostic_payload",
        "created_at",
        "updated_at",
    }
    assert set(table.c.keys()) == expected_columns

    # Verify column nullability invariants
    assert table.c.id.nullable is False
    assert table.c.stripe_invoice_id.nullable is False
    assert table.c.organization_id.nullable is False
    assert table.c.stripe_customer_id.nullable is False
    assert table.c.stripe_subscription_id.nullable is True
    assert table.c.billing_reason.nullable is True
    assert table.c.invoice_status.nullable is False
    assert table.c.currency.nullable is True
    assert table.c.amount_paid_minor_units.nullable is False
    assert table.c.period_start.nullable is True
    assert table.c.period_end.nullable is True
    assert table.c.processing_status.nullable is False
    assert table.c.billing_period_id.nullable is True
    assert table.c.raw_event_id.nullable is True
    assert table.c.attempt_count.nullable is False
    assert table.c.error_message.nullable is True
    assert table.c.diagnostic_payload.nullable is False
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False

    # Verify explicit unique constraint name
    unique_names = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_stripe_invoices_invoice_id" in unique_names

    # Verify exact set of 7 indexes
    expected_indexes = {
        "ix_stripe_invoices_organization_id",
        "ix_stripe_invoices_customer_id",
        "ix_stripe_invoices_subscription_id",
        "ix_stripe_invoices_billing_period_id",
        "ix_stripe_invoices_raw_event_id",
        "ix_stripe_invoices_org_created",
        "ix_stripe_invoices_status",
    }
    actual_indexes = {idx.name for idx in table.indexes}
    assert actual_indexes == expected_indexes

