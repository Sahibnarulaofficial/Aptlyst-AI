"""Tests for Step 5.1B Alembic migration a1b2c3d4e5f7 (Provider-Neutral Billing Foundation).

Covers:
1. Migration file identity and DAG integrity:
   - Revision ID: a1b2c3d4e5f7
   - Down revision: f6a1b2c3d4e5
2. Offline Alembic Upgrade DDL generation:
   - Nullable column addition on subscriptions.
   - Explicit backfill to Stripe legacy records.
   - Enforcing NOT NULL on provider-neutral columns without permanent defaults.
   - Relaxing stripe_customer_id NOT NULL constraint.
   - Scoped unique indexes on subscriptions.
   - Creation of all five generalized tables.
   - Row-Level Security enable statements.
   - Privilege revoke statements from anon and authenticated.
3. Offline Alembic Downgrade DDL generation:
   - Concurrency-safe downgrade guard with ACCESS EXCLUSIVE table locks.
   - Pre-drop data loss exception barrier.
   - Dropping generalized tables in reverse dependency order.
   - Restoring stripe_customer_id NOT NULL.
   - Dropping provider-neutral columns from subscriptions.
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

import importlib.util
from pathlib import Path
import subprocess
import pytest


def test_migration_metadata() -> None:
    """Verify revision and down_revision match expected migration chain."""
    api_dir = Path(__file__).resolve().parent.parent
    migration_file = api_dir / "alembic" / "versions" / "20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py"
    assert migration_file.exists(), f"Migration file not found at {migration_file}"

    spec = importlib.util.spec_from_file_location("migration_a1b2c3d4e5f7", migration_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "a1b2c3d4e5f7"
    assert migration.down_revision == "f6a1b2c3d4e5"


def test_offline_upgrade_ddl_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL upgrade DDL for revision a1b2c3d4e5f7."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "f6a1b2c3d4e5:a1b2c3d4e5f7", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic upgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Column additions on subscriptions
    assert "ALTER TABLE subscriptions ADD COLUMN provider VARCHAR(32)" in sql
    assert "ALTER TABLE subscriptions ADD COLUMN provider_customer_id VARCHAR(255)" in sql
    assert "ALTER TABLE subscriptions ADD COLUMN merchant_account_id VARCHAR(64)" in sql
    assert "ALTER TABLE subscriptions ADD COLUMN environment VARCHAR(32)" in sql

    # 2. Backfill execution
    assert "UPDATE subscriptions" in sql
    assert "provider = 'stripe'" in sql
    assert "environment = 'legacy'" in sql

    # 3. NOT NULL enforcement
    assert "ALTER TABLE subscriptions ALTER COLUMN provider SET NOT NULL" in sql
    assert "ALTER TABLE subscriptions ALTER COLUMN provider_customer_id SET NOT NULL" in sql

    # 4. Relax stripe_customer_id
    assert "ALTER TABLE subscriptions ALTER COLUMN stripe_customer_id DROP NOT NULL" in sql

    # 5. Scoped unique indexes
    assert "CREATE UNIQUE INDEX uq_subscriptions_provider_customer ON subscriptions" in sql
    assert "CREATE UNIQUE INDEX uq_subscriptions_provider_subscription ON subscriptions" in sql

    # 6. Generalized tables
    assert "CREATE TABLE billing_webhook_events" in sql
    assert "signature_verified BOOLEAN DEFAULT 'false' NOT NULL" in sql

    assert "CREATE TABLE billing_checkout_attempts" in sql
    assert "CREATE TABLE billing_invoices" in sql
    assert "CREATE TABLE billing_payments" in sql
    assert "CREATE TABLE payment_reconciliations" in sql

    # 7. Check constraints
    assert "ck_billing_invoices_amounts_non_negative" in sql
    assert "ck_billing_invoices_paid_le_gross" in sql
    assert "ck_billing_payments_amount_positive" in sql
    assert "ck_billing_payments_refund_non_negative" in sql

    # 8. RLS and privilege revokes
    assert "ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_webhook_events FROM anon, authenticated" in sql
    assert "ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated" in sql
    assert "ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_invoices FROM anon, authenticated" in sql
    assert "ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_payments FROM anon, authenticated" in sql
    assert "ALTER TABLE payment_reconciliations ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON payment_reconciliations FROM anon, authenticated" in sql

    assert "UPDATE alembic_version SET version_num='a1b2c3d4e5f7'" in sql


def test_offline_downgrade_ddl_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL downgrade DDL with table locks and data guard."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "a1b2c3d4e5f7:f6a1b2c3d4e5", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic downgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Exclusive table locks to prevent check-then-drop race conditions
    assert "LOCK TABLE subscriptions IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE billing_webhook_events IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE billing_invoices IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE billing_payments IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE payment_reconciliations IN ACCESS EXCLUSIVE MODE" in sql

    # 2. Concurrency-safe data loss guard
    assert "Alembic Downgrade Aborted:" in sql
    assert "Dropping generalized billing tables is strictly forbidden" in sql

    # 3. Table drop order (reverse dependency)
    assert "DROP TABLE payment_reconciliations" in sql
    assert "DROP TABLE billing_payments" in sql
    assert "DROP TABLE billing_invoices" in sql
    assert "DROP TABLE billing_checkout_attempts" in sql
    assert "DROP TABLE billing_webhook_events" in sql

    # 4. Scoped indexes drop
    assert "DROP INDEX uq_subscriptions_provider_subscription" in sql
    assert "DROP INDEX uq_subscriptions_provider_customer" in sql

    # 5. Restore stripe_customer_id NOT NULL
    assert "ALTER TABLE subscriptions ALTER COLUMN stripe_customer_id SET NOT NULL" in sql

    # 6. Drop provider-neutral columns
    assert "ALTER TABLE subscriptions DROP COLUMN provider" in sql
    assert "ALTER TABLE subscriptions DROP COLUMN provider_customer_id" in sql
    assert "UPDATE alembic_version SET version_num='f6a1b2c3d4e5'" in sql


def test_billing_customer_provisions_migration_metadata() -> None:
    """Verify revision and down_revision for b1c2d3e4f5a6 (Billing Customer Provisions)."""
    api_dir = Path(__file__).resolve().parent.parent
    migration_file = api_dir / "alembic" / "versions" / "20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py"
    assert migration_file.exists(), f"Migration file not found at {migration_file}"

    spec = importlib.util.spec_from_file_location("migration_b1c2d3e4f5a6", migration_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "b1c2d3e4f5a6"
    assert migration.down_revision == "a1b2c3d4e5f7"


def test_offline_upgrade_ddl_customer_provisions() -> None:
    """Verify Alembic generates valid offline PostgreSQL upgrade DDL for revision b1c2d3e4f5a6."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "a1b2c3d4e5f7:b1c2d3e4f5a6", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic upgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Table creation
    assert "CREATE TABLE billing_customer_provisions" in sql
    assert "provider VARCHAR(32) NOT NULL" in sql
    assert "provider_customer_id VARCHAR(255)" in sql
    assert "uq_billing_customer_provisions_org_provider" in sql

    # 2. Indexes
    assert "ix_billing_customer_provisions_organization_id" in sql
    assert "ix_billing_customer_provisions_status" in sql
    assert "ix_billing_customer_provisions_claim" in sql
    assert "ix_billing_customer_provisions_lease" in sql
    assert "uq_billing_customer_provisions_provider_customer" in sql

    # 3. RLS and privilege revoke
    assert "ALTER TABLE billing_customer_provisions ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON billing_customer_provisions FROM anon, authenticated" in sql


def test_offline_downgrade_ddl_customer_provisions() -> None:
    """Verify Alembic generates valid offline PostgreSQL downgrade DDL for revision b1c2d3e4f5a6."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "b1c2d3e4f5a6:a1b2c3d4e5f7", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic downgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Exclusive table lock and guard
    assert "LOCK TABLE billing_customer_provisions IN ACCESS EXCLUSIVE MODE" in sql
    assert "Alembic Downgrade Aborted:" in sql
    assert "customer provisioning records exist" in sql

    # 2. Disable RLS and drop table
    assert "ALTER TABLE billing_customer_provisions DISABLE ROW LEVEL SECURITY" in sql
    assert "DROP TABLE billing_customer_provisions" in sql
    assert "DROP TYPE IF EXISTS customer_provision_status" in sql


def test_migration_metadata_scoped_checkout_uniqueness() -> None:
    """Verify revision c1d2e3f4a5b7 matches DAG chain."""
    api_dir = Path(__file__).resolve().parent.parent
    migration_file = api_dir / "alembic" / "versions" / "20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py"
    assert migration_file.exists(), f"Migration file not found at {migration_file}"

    spec = importlib.util.spec_from_file_location("migration_c1d2e3f4a5b7", migration_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "c1d2e3f4a5b7"
    assert migration.down_revision == "b1c2d3e4f5a6"


def test_offline_upgrade_ddl_scoped_checkout_uniqueness() -> None:
    """Verify Alembic generates valid offline PostgreSQL upgrade DDL for revision c1d2e3f4a5b7."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "b1c2d3e4f5a6:c1d2e3f4a5b7", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic upgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    assert "DROP INDEX uq_org_active_billing_checkout_attempt;" in sql
    assert "CREATE UNIQUE INDEX uq_org_scoped_active_billing_checkout_attempt ON billing_checkout_attempts (organization_id, provider, merchant_account_id, environment) WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending');" in sql


def test_offline_downgrade_ddl_scoped_checkout_uniqueness() -> None:
    """Verify Alembic generates valid offline PostgreSQL downgrade DDL for revision c1d2e3f4a5b7."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "c1d2e3f4a5b7:b1c2d3e4f5a6", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic downgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    assert "DROP INDEX uq_org_scoped_active_billing_checkout_attempt;" in sql
    assert "CREATE UNIQUE INDEX uq_org_active_billing_checkout_attempt ON billing_checkout_attempts (organization_id) WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending');" in sql

