"""Stage 2C Focused Tests: Checkout Recovery Schema, Env Exclusivity, Sweeper Index & Migration.

Covers:
1. BillingCheckoutAttempt recovery columns (ADR-006 Decision C):
   - next_recovery_retry_at (DateTime(timezone=True), nullable=True)
   - recovery_claim_expires_at (DateTime(timezone=True), nullable=True)
   - recovery_claim_id (UUID(as_uuid=True), nullable=True)
   - recovery_attempts (Integer, nullable=False, default=0, server_default='0')
   - Existing attempt_lease_expires_at untouched; existing records representable without env backfills.
2. Environment-scoped active checkout uniqueness (ADR-006 Decision A):
   - uq_org_env_active_billing_checkout_attempt on (organization_id, environment)
     WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending').
   - Old uq_org_scoped_active_billing_checkout_attempt removed from current model metadata.
   - Behavioral partial-unique-index verification (in-memory SQLite, not PG16 proof):
     same (org, env) conflicts across providers/merchants; different envs allowed; terminal states excluded.
   - Global uq_org_active_subscription and legacy stripe_checkout_attempts untouched.
   - webhook_errors classifies uq_org_env_active_billing_checkout_attempt as CONCURRENCY_RACE (Bucket 2).
3. Recovery and stale-creating indexes:
   - ix_billing_checkout_attempts_recovery_claim matches ADR-006 NULL-safe eligibility predicate.
   - Pre-existing ix_billing_checkout_attempts_lease covers stale creating attempts with zero duplicate index.
4. BillingPayment sweeper index alignment (BLK-04 / ADR-002):
   - ix_billing_payments_sweeper_claim predicate covers ('unreconciled', 'reconciling', 'pending_attempt_binding').
   - Existing lease_token, lease_expires_at, attempt_count, max_attempts fields untouched.
5. Reversible Alembic migration f3a4b5c6d7e2:
   - DAG metadata (revision='f3a4b5c6d7e2', down_revision='e2f3a4b5c6d1') and single linear head.
   - Offline PostgreSQL upgrade DDL, pre-validation gates, downgrade DDL, and full base-to-head chain DDL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import pytest
from sqlalchemy import DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID, dialect as pg_dialect
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex

from app.db.base import Base
import app.models.organization  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401
from app.models.payment import BillingCheckoutAttempt, BillingPayment
from app.models.subscription import CheckoutAttemptStatus
from app.services.billing.webhook_errors import (
    DbRetryDisposition,
    UniqueConstraintBucket,
    classify_pg_sqlstate,
    classify_unique_violation_constraint,
)


API_DIR = Path(__file__).resolve().parent.parent
MIGRATION_FILE = (
    API_DIR
    / "alembic"
    / "versions"
    / "20261117_2230_f3a4b5c6d7e2_add_checkout_recovery_and_env_exclusivity.py"
)


def _compile_pg_index_ddl(index: object) -> str:
    return str(CreateIndex(index).compile(dialect=pg_dialect()))  # type: ignore[arg-type]


# =============================================================================
# 1. Recovery Fields on BillingCheckoutAttempt
# =============================================================================


def test_s2c_recovery_columns_exist_with_expected_types_nullability_and_defaults() -> None:
    """All four ADR-006 recovery fields exist with exact SQLAlchemy types, timezone awareness, and defaults."""
    configure_mappers()
    table = Base.metadata.tables["billing_checkout_attempts"]

    # next_recovery_retry_at
    assert "next_recovery_retry_at" in table.c
    col_retry = table.c["next_recovery_retry_at"]
    assert isinstance(col_retry.type, DateTime)
    assert col_retry.type.timezone is True
    assert col_retry.nullable is True

    # recovery_claim_expires_at
    assert "recovery_claim_expires_at" in table.c
    col_claim_exp = table.c["recovery_claim_expires_at"]
    assert isinstance(col_claim_exp.type, DateTime)
    assert col_claim_exp.type.timezone is True
    assert col_claim_exp.nullable is True

    # recovery_claim_id
    assert "recovery_claim_id" in table.c
    col_claim_id = table.c["recovery_claim_id"]
    assert isinstance(col_claim_id.type, UUID)
    assert col_claim_id.type.as_uuid is True
    assert col_claim_id.nullable is True

    # recovery_attempts
    assert "recovery_attempts" in table.c
    col_attempts = table.c["recovery_attempts"]
    assert isinstance(col_attempts.type, Integer)
    assert col_attempts.nullable is False
    assert col_attempts.default is not None
    assert col_attempts.default.arg == 0
    assert col_attempts.server_default is not None
    assert str(col_attempts.server_default.arg) == "0"

    # Existing attempt_lease_expires_at preserved intact (not overloaded or renamed)
    assert "attempt_lease_expires_at" in table.c
    assert isinstance(table.c["attempt_lease_expires_at"].type, DateTime)
    assert table.c["attempt_lease_expires_at"].type.timezone is True
    assert table.c["attempt_lease_expires_at"].nullable is False


def test_s2c_existing_checkout_attempt_instantiates_without_speculative_backfills() -> None:
    """Existing records represent cleanly with NULL recovery scheduling/claim fields and 0 recovery_attempts."""
    now = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        idempotency_key="idemp_legacy_repr_001",
        status=CheckoutAttemptStatus.open,
        currency="INR",
        attempt_lease_expires_at=now + timedelta(seconds=60),
    )
    assert attempt.next_recovery_retry_at is None
    assert attempt.recovery_claim_expires_at is None
    assert attempt.recovery_claim_id is None
    assert attempt.environment == "test"

    # Explicitly setting recovery fields works with timezone-aware timestamps and UUIDs
    claim_uuid = uuid.uuid4()
    attempt.next_recovery_retry_at = now
    attempt.recovery_claim_expires_at = now + timedelta(seconds=60)
    attempt.recovery_claim_id = claim_uuid
    attempt.recovery_attempts = 2
    assert attempt.recovery_claim_id == claim_uuid
    assert attempt.recovery_attempts == 2


# =============================================================================
# 2. Environment-Scoped Active Checkout Uniqueness
# =============================================================================


def test_s2c_active_checkout_uniqueness_model_index_definition() -> None:
    """uq_org_env_active_billing_checkout_attempt replaces uq_org_scoped_active_billing_checkout_attempt."""
    table = Base.metadata.tables["billing_checkout_attempts"]
    indexes = {idx.name: idx for idx in table.indexes}

    assert "uq_org_scoped_active_billing_checkout_attempt" not in indexes
    assert "uq_org_env_active_billing_checkout_attempt" in indexes

    idx = indexes["uq_org_env_active_billing_checkout_attempt"]
    assert idx.unique is True
    assert [c.name for c in idx.columns] == ["organization_id", "environment"]

    ddl = _compile_pg_index_ddl(idx)
    assert (
        "CREATE UNIQUE INDEX uq_org_env_active_billing_checkout_attempt "
        "ON billing_checkout_attempts (organization_id, environment) "
        "WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending')"
    ) == ddl


def test_s2c_active_checkout_partial_unique_index_semantics_sqlite() -> None:
    """Verify partial unique index conflict/exclusion semantics in an in-memory SQLite table
    (unit-level index semantics check; live PostgreSQL 16 verification remains Stage 2E).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE billing_checkout_attempts (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            merchant_account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX uq_org_env_active_billing_checkout_attempt
        ON billing_checkout_attempts (organization_id, environment)
        WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending')
        """
    )

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())

    # 1. Insert active Razorpay attempt in live environment
    conn.execute(
        "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), org_a, "razorpay", "acc_rzp_1", "live", "open"),
    )

    # 2. Second active attempt for same (org_a, live) with DIFFERENT provider & merchant MUST CONFLICT
    for active_status in ("creating", "recovery_required", "open", "payment_pending"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), org_a, "stripe", "acc_other", "live", active_status),
            )

    # 3. Different environment ('test') for the same org_a is ALLOWED by this index
    conn.execute(
        "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), org_a, "razorpay", "acc_rzp_test", "test", "creating"),
    )

    # 4. Different organization ('org_b') in 'live' is ALLOWED
    conn.execute(
        "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), org_b, "razorpay", "acc_rzp_1", "live", "payment_pending"),
    )

    # 5. Terminal / historical attempts ('complete', 'expired', 'failed') in same (org_a, live) are EXCLUDED
    for terminal_status in ("complete", "expired", "failed"):
        conn.execute(
            "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), org_a, "razorpay", "acc_rzp_1", "live", terminal_status),
        )
        conn.execute(
            "INSERT INTO billing_checkout_attempts VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), org_a, "stripe", "default", "live", terminal_status),
        )

    conn.close()


def test_s2c_global_active_subscription_and_legacy_stripe_attempts_unchanged() -> None:
    """uq_org_active_subscription and stripe_checkout_attempts remain strictly untouched."""
    sub_table = Base.metadata.tables["subscriptions"]
    sub_indexes = {idx.name: idx for idx in sub_table.indexes}
    assert "uq_org_active_subscription" in sub_indexes
    sub_idx = sub_indexes["uq_org_active_subscription"]
    assert sub_idx.unique is True
    assert [c.name for c in sub_idx.columns] == ["organization_id"]
    assert (
        _compile_pg_index_ddl(sub_idx)
        == "CREATE UNIQUE INDEX uq_org_active_subscription ON subscriptions (organization_id) "
        "WHERE status IN ('active', 'trialing')"
    )

    stripe_att_table = Base.metadata.tables["stripe_checkout_attempts"]
    stripe_indexes = {idx.name: idx for idx in stripe_att_table.indexes}
    assert "uq_org_active_checkout_attempt" in stripe_indexes
    assert stripe_indexes["uq_org_active_checkout_attempt"].unique is True


def test_s2c_webhook_errors_classifies_new_env_active_attempt_constraint() -> None:
    """SQLSTATE 23505 on uq_org_env_active_billing_checkout_attempt maps to Bucket 2 CONCURRENCY_RACE."""
    name = "uq_org_env_active_billing_checkout_attempt"
    assert classify_unique_violation_constraint(name) == UniqueConstraintBucket.CONCURRENCY_RACE
    res = classify_pg_sqlstate("23505", constraint_name=name)
    assert res.disposition == DbRetryDisposition.RETRYABLE
    assert res.failure_category == "db_concurrent_unique_race"
    assert res.is_known is True


# =============================================================================
# 3. Recovery and Stale-Creating Indexes
# =============================================================================


def test_s2c_recovery_and_stale_creating_indexes_match_predicates_without_duplicates() -> None:
    """Verify recovery claim index matches ADR-006 and existing lease index covers stale creating with zero duplication."""
    table = Base.metadata.tables["billing_checkout_attempts"]
    indexes = {idx.name: idx for idx in table.indexes}

    # 1. Eligible recovery attempts partial index (ADR-006 Section 9 Phase 3)
    assert "ix_billing_checkout_attempts_recovery_claim" in indexes
    rec_idx = indexes["ix_billing_checkout_attempts_recovery_claim"]
    assert rec_idx.unique is False
    assert [c.name for c in rec_idx.columns] == [
        "status",
        "next_recovery_retry_at",
        "recovery_claim_expires_at",
    ]
    assert (
        _compile_pg_index_ddl(rec_idx)
        == "CREATE INDEX ix_billing_checkout_attempts_recovery_claim "
        "ON billing_checkout_attempts (status, next_recovery_retry_at, recovery_claim_expires_at) "
        "WHERE status = 'recovery_required' AND next_recovery_retry_at IS NOT NULL"
    )

    # 2. Stale creating attempts partial index (reuses existing ix_billing_checkout_attempts_lease)
    assert "ix_billing_checkout_attempts_lease" in indexes
    lease_idx = indexes["ix_billing_checkout_attempts_lease"]
    assert lease_idx.unique is False
    assert [c.name for c in lease_idx.columns] == ["status", "attempt_lease_expires_at"]
    assert (
        _compile_pg_index_ddl(lease_idx)
        == "CREATE INDEX ix_billing_checkout_attempts_lease "
        "ON billing_checkout_attempts (status, attempt_lease_expires_at) "
        "WHERE status = 'creating'"
    )

    # 3. Zero duplicate stale-creating indexes referencing attempt_lease_expires_at
    lease_covering_indexes = [
        idx.name
        for idx in table.indexes
        if "attempt_lease_expires_at" in [c.name for c in idx.columns]
    ]
    assert lease_covering_indexes == ["ix_billing_checkout_attempts_lease"]


# =============================================================================
# 4. Billing Payment Sweeper Index Alignment
# =============================================================================


def test_s2c_billing_payment_sweeper_index_aligned_and_lease_fields_preserved() -> None:
    """ix_billing_payments_sweeper_claim aligns with ADR-002 / BLK-04 states and preserves lease/retry fields."""
    table = Base.metadata.tables["billing_payments"]
    indexes = {idx.name: idx for idx in table.indexes}

    assert "ix_billing_payments_sweeper_claim" in indexes
    sw_idx = indexes["ix_billing_payments_sweeper_claim"]
    assert sw_idx.unique is False
    assert [c.name for c in sw_idx.columns] == [
        "reconciliation_state",
        "lease_expires_at",
        "attempt_count",
    ]
    assert (
        _compile_pg_index_ddl(sw_idx)
        == "CREATE INDEX ix_billing_payments_sweeper_claim "
        "ON billing_payments (reconciliation_state, lease_expires_at, attempt_count) "
        "WHERE reconciliation_state IN ('unreconciled', 'reconciling', 'pending_attempt_binding')"
    )

    # Existing sweeper lease and retry columns preserved unchanged
    assert table.c["reconciliation_state"].nullable is False
    assert str(table.c["reconciliation_state"].server_default.arg) == "unreconciled"
    assert isinstance(table.c["lease_token"].type, UUID)
    assert table.c["lease_token"].nullable is True
    assert isinstance(table.c["lease_expires_at"].type, DateTime)
    assert table.c["lease_expires_at"].type.timezone is True
    assert table.c["lease_expires_at"].nullable is True
    assert isinstance(table.c["attempt_count"].type, Integer)
    assert table.c["attempt_count"].nullable is False
    assert str(table.c["attempt_count"].server_default.arg) == "0"
    assert isinstance(table.c["max_attempts"].type, Integer)
    assert table.c["max_attempts"].nullable is False
    assert str(table.c["max_attempts"].server_default.arg) == "5"

    # Model instantiates cleanly with existing defaults
    pay = BillingPayment(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_test",
        environment="test",
        provider_payment_id="pay_test_s2c_01",
        amount_minor_units=290000,
        currency="INR",
        status="captured",
    )
    assert pay.provider_payment_id == "pay_test_s2c_01"


# =============================================================================
# 5. Alembic Migration DAG & Offline DDL Verification
# =============================================================================


def test_s2c_migration_metadata_and_single_linear_head() -> None:
    """Migration f3a4b5c6d7e2 points to e2f3a4b5c6d1 and feeds the sole linear Alembic head."""
    assert MIGRATION_FILE.exists(), f"Migration file missing: {MIGRATION_FILE}"

    spec = importlib.util.spec_from_file_location("migration_f3a4b5c6d7e2", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "f3a4b5c6d7e2"
    assert mod.down_revision == "e2f3a4b5c6d1"

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"alembic heads failed:\n{res.stderr}"
    heads = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    assert heads == ["a4b5c6d7e8f3 (head)"]


def test_s2c_offline_upgrade_ddl_generation() -> None:
    """Offline PostgreSQL upgrade DDL contains all Stage 2C changes, pre-validation gates, and zero unrelated mutations."""
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "e2f3a4b5c6d1:f3a4b5c6d7e2", "--sql"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"alembic upgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Four recovery columns
    assert "ALTER TABLE billing_checkout_attempts ADD COLUMN next_recovery_retry_at TIMESTAMP WITH TIME ZONE;" in sql
    assert "ALTER TABLE billing_checkout_attempts ADD COLUMN recovery_claim_expires_at TIMESTAMP WITH TIME ZONE;" in sql
    assert "ALTER TABLE billing_checkout_attempts ADD COLUMN recovery_claim_id UUID;" in sql
    assert "ALTER TABLE billing_checkout_attempts ADD COLUMN recovery_attempts INTEGER DEFAULT '0' NOT NULL;" in sql

    # 2. Fail-closed pre-validation gates (environment provenance + duplicate (org, env) check)
    assert "LOCK TABLE billing_checkout_attempts IN SHARE ROW EXCLUSIVE MODE;" in sql
    assert "lower(btrim(environment)) NOT IN ('test', 'live')" in sql
    assert "GROUP BY organization_id, environment" in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "CRITICAL MIGRATION ABORT:" in sql

    # 3. Active checkout unique index swap
    assert "DROP INDEX uq_org_scoped_active_billing_checkout_attempt;" in sql
    assert (
        "CREATE UNIQUE INDEX uq_org_env_active_billing_checkout_attempt "
        "ON billing_checkout_attempts (organization_id, environment) "
        "WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending');"
    ) in sql

    # 4. Recovery claim index
    assert (
        "CREATE INDEX ix_billing_checkout_attempts_recovery_claim "
        "ON billing_checkout_attempts (status, next_recovery_retry_at, recovery_claim_expires_at) "
        "WHERE status = 'recovery_required' AND next_recovery_retry_at IS NOT NULL;"
    ) in sql

    # 5. Billing payments sweeper index alignment
    assert "DROP INDEX ix_billing_payments_sweeper_claim;" in sql
    assert (
        "CREATE INDEX ix_billing_payments_sweeper_claim "
        "ON billing_payments (reconciliation_state, lease_expires_at, attempt_count) "
        "WHERE reconciliation_state IN ('unreconciled', 'reconciling', 'pending_attempt_binding');"
    ) in sql

    # 6. RLS & untrusted role revocations preserved
    assert "ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY;" in sql
    assert "REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated;" in sql
    assert "ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY;" in sql
    assert "REVOKE ALL ON billing_payments FROM anon, authenticated;" in sql

    # 7. Unrelated objects NOT touched (no subscriptions / stripe_checkout_attempts / DELETE / UPDATE mutations)
    assert "uq_org_active_subscription" not in sql
    assert "stripe_checkout_attempts" not in sql
    assert "DELETE FROM" not in sql
    assert "UPDATE billing_checkout_attempts" not in sql
    assert "UPDATE alembic_version SET version_num='f3a4b5c6d7e2'" in sql


def test_s2c_offline_downgrade_ddl_generation() -> None:
    """Offline PostgreSQL downgrade DDL safely reverses only Stage 2C objects."""
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "f3a4b5c6d7e2:e2f3a4b5c6d1", "--sql"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"alembic downgrade --sql failed:\n{res.stderr}"
    sql = res.stdout

    # 1. Downgrade guard on unexpired recovery claims
    assert "LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE;" in sql
    assert "Alembic Downgrade Aborted:" in sql
    assert "recovery_claim_id IS NOT NULL" in sql

    # 2. Restore prior billing_payments sweeper index
    assert "DROP INDEX ix_billing_payments_sweeper_claim;" in sql
    assert (
        "CREATE INDEX ix_billing_payments_sweeper_claim "
        "ON billing_payments (reconciliation_state, lease_expires_at, attempt_count) "
        "WHERE reconciliation_state IN ('unreconciled', 'reconciling');"
    ) in sql

    # 3. Drop recovery claim index (without dropping pre-existing ix_billing_checkout_attempts_lease!)
    assert "DROP INDEX ix_billing_checkout_attempts_recovery_claim;" in sql
    assert "ix_billing_checkout_attempts_lease" not in sql

    # 4. Restore prior scoped active checkout unique index
    assert "DROP INDEX uq_org_env_active_billing_checkout_attempt;" in sql
    assert (
        "CREATE UNIQUE INDEX uq_org_scoped_active_billing_checkout_attempt "
        "ON billing_checkout_attempts (organization_id, provider, merchant_account_id, environment) "
        "WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending');"
    ) in sql

    # 5. Drop the 4 recovery columns in reverse order
    assert "ALTER TABLE billing_checkout_attempts DROP COLUMN recovery_attempts;" in sql
    assert "ALTER TABLE billing_checkout_attempts DROP COLUMN recovery_claim_id;" in sql
    assert "ALTER TABLE billing_checkout_attempts DROP COLUMN recovery_claim_expires_at;" in sql
    assert "ALTER TABLE billing_checkout_attempts DROP COLUMN next_recovery_retry_at;" in sql
    assert "UPDATE alembic_version SET version_num='e2f3a4b5c6d1'" in sql


def test_s2c_full_chain_offline_upgrade_base_to_head() -> None:
    """Entire Alembic revision graph from base to head compiles cleanly offline."""
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"alembic upgrade head --sql failed:\n{res.stderr}"
    assert "CREATE UNIQUE INDEX uq_org_env_active_billing_checkout_attempt" in res.stdout
    assert "CREATE INDEX ix_billing_checkout_attempts_recovery_claim" in res.stdout
    assert (
        "UPDATE alembic_version SET version_num='f3a4b5c6d7e2' "
        "WHERE alembic_version.version_num = 'e2f3a4b5c6d1';"
    ) in res.stdout
    assert (
        "UPDATE alembic_version SET version_num='a4b5c6d7e8f3' "
        "WHERE alembic_version.version_num = 'f3a4b5c6d7e2';"
    ) in res.stdout
