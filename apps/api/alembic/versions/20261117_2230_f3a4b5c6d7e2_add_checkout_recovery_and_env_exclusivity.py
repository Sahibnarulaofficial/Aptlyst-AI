"""add_checkout_recovery_and_env_exclusivity

Revision ID: f3a4b5c6d7e2
Revises: e2f3a4b5c6d1
Create Date: 2026-11-17 22:30:00.000000

Stage 2C (BLK-04 / BLK-05) reversible schema migration implementing ADR-006 Phase 3
and BLK-04 payment sweeper index alignment:

1. billing_checkout_attempts — ADR-006 Decision C recovery scheduling & fencing columns:
   - next_recovery_retry_at: DateTime(timezone=True), nullable=True
   - recovery_claim_expires_at: DateTime(timezone=True), nullable=True
   - recovery_claim_id: UUID, nullable=True
   - recovery_attempts: Integer, nullable=False, server_default='0'
2. billing_checkout_attempts — ADR-006 Decision A active checkout uniqueness:
   - Fail-closed pre-validation gates (under SHARE ROW EXCLUSIVE lock) verifying:
     (a) zero active attempts have NULL or unsupported environment provenance (must be 'test' or 'live'),
     (b) zero (organization_id, environment) groups contain duplicate active attempts.
     Does NOT guess environment provenance or mutate/delete historical records.
   - Replaces uq_org_scoped_active_billing_checkout_attempt (organization_id, provider,
     merchant_account_id, environment) with uq_org_env_active_billing_checkout_attempt
     (organization_id, environment) WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending').
   - Preserves global uq_org_active_subscription and historical stripe_checkout_attempts untouched.
3. billing_checkout_attempts — Recovery & stale-creating index coverage:
   - Adds partial index ix_billing_checkout_attempts_recovery_claim on
     (status, next_recovery_retry_at, recovery_claim_expires_at)
     WHERE status = 'recovery_required' AND next_recovery_retry_at IS NOT NULL.
   - Preserves existing partial index ix_billing_checkout_attempts_lease on
     (status, attempt_lease_expires_at) WHERE status = 'creating' without creating
     a redundant duplicate stale-creating index.
4. billing_payments — BLK-04 sweeper claim index alignment:
   - Aligns ix_billing_payments_sweeper_claim partial predicate to
     reconciliation_state IN ('unreconciled', 'reconciling', 'pending_attempt_binding')
     per ADR-002 / BLK-04 while preserving all existing lease/retry columns and constraints.
5. RLS & Privileges:
   - Maintains Row-Level Security and revokes untrusted direct role privileges (anon, authenticated).
6. Safe Downgrade:
   - Reverses only Stage 2C objects with an ACCESS EXCLUSIVE guard aborting if active unexpired
     recovery worker claims exist.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e2"
down_revision: Union[str, None] = "e2f3a4b5c6d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_ATTEMPT_PREDICATE = (
    "status IN ('creating', 'recovery_required', 'open', 'payment_pending')"
)
RECOVERY_CLAIM_PREDICATE = (
    "status = 'recovery_required' AND next_recovery_retry_at IS NOT NULL"
)
OLD_PAYMENT_SWEEPER_PREDICATE = (
    "reconciliation_state IN ('unreconciled', 'reconciling')"
)
NEW_PAYMENT_SWEEPER_PREDICATE = (
    "reconciliation_state IN ('unreconciled', 'reconciling', 'pending_attempt_binding')"
)


def upgrade() -> None:
    # =========================================================================
    # 1. billing_checkout_attempts: Add ADR-006 Recovery Scheduling & Fencing Columns
    # =========================================================================
    op.add_column(
        "billing_checkout_attempts",
        sa.Column("next_recovery_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "billing_checkout_attempts",
        sa.Column("recovery_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "billing_checkout_attempts",
        sa.Column("recovery_claim_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "billing_checkout_attempts",
        sa.Column("recovery_attempts", sa.Integer(), server_default="0", nullable=False),
    )

    # =========================================================================
    # 2. Pre-Validation Gates Before Creating uq_org_env_active_billing_checkout_attempt
    # =========================================================================
    # Fail closed without deleting, expiring, merging, or guessing environment
    # provenance for any existing checkout attempt.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                invalid_env_count INTEGER;
                invalid_env_details TEXT;
                dup_group_count INTEGER;
                dup_details TEXT;
            BEGIN
                LOCK TABLE billing_checkout_attempts IN SHARE ROW EXCLUSIVE MODE;

                -- Gate A: Verify zero active attempts have NULL or unsupported environment provenance
                SELECT
                    COUNT(*),
                    string_agg(
                        'attempt_id=' || id::text
                        || ' org_id=' || organization_id::text
                        || ' env=' || COALESCE(quote_literal(environment), 'NULL'),
                        ', '
                    )
                INTO invalid_env_count, invalid_env_details
                FROM billing_checkout_attempts
                WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending')
                  AND (
                      environment IS NULL
                      OR lower(btrim(environment)) NOT IN ('test', 'live')
                  );

                IF invalid_env_count > 0 THEN
                    RAISE EXCEPTION
                        'CRITICAL MIGRATION ABORT: % active billing_checkout_attempts row(s) have missing or unsupported environment provenance (must be test or live): %. Manual remediation required; migration will not infer environment values.',
                        invalid_env_count, invalid_env_details;
                END IF;

                -- Gate B: Verify zero duplicate active attempts exist per (organization_id, environment)
                SELECT
                    COUNT(*),
                    string_agg(
                        '(org=' || organization_id::text
                        || ', env=' || environment
                        || ', count=' || grp_count::text || ')',
                        ', '
                    )
                INTO dup_group_count, dup_details
                FROM (
                    SELECT organization_id, environment, COUNT(*) AS grp_count
                    FROM billing_checkout_attempts
                    WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending')
                    GROUP BY organization_id, environment
                    HAVING COUNT(*) > 1
                ) dup_groups;

                IF dup_group_count > 0 THEN
                    RAISE EXCEPTION
                        'CRITICAL MIGRATION ABORT: Cannot create uq_org_env_active_billing_checkout_attempt. % (organization_id, environment) group(s) contain duplicate active checkout attempts: %. Resolve conflicting active attempts manually before applying this migration.',
                        dup_group_count, dup_details;
                END IF;
            END $$
            """
        )
    )

    # =========================================================================
    # 3. Replace Provider-Scoped Index with (organization_id, environment) Unique Index
    # =========================================================================
    op.drop_index(
        "uq_org_scoped_active_billing_checkout_attempt",
        table_name="billing_checkout_attempts",
        postgresql_where=sa.text(ACTIVE_ATTEMPT_PREDICATE),
    )
    op.create_index(
        "uq_org_env_active_billing_checkout_attempt",
        "billing_checkout_attempts",
        ["organization_id", "environment"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_ATTEMPT_PREDICATE),
    )

    # =========================================================================
    # 4. Add Recovery Claim Partial Index (Existing ix_billing_checkout_attempts_lease Preserved)
    # =========================================================================
    op.create_index(
        "ix_billing_checkout_attempts_recovery_claim",
        "billing_checkout_attempts",
        ["status", "next_recovery_retry_at", "recovery_claim_expires_at"],
        unique=False,
        postgresql_where=sa.text(RECOVERY_CLAIM_PREDICATE),
    )

    # =========================================================================
    # 5. Align billing_payments Sweeper Partial Index Predicate (BLK-04 / ADR-002)
    # =========================================================================
    op.drop_index(
        "ix_billing_payments_sweeper_claim",
        table_name="billing_payments",
        postgresql_where=sa.text(OLD_PAYMENT_SWEEPER_PREDICATE),
    )
    op.create_index(
        "ix_billing_payments_sweeper_claim",
        "billing_payments",
        ["reconciliation_state", "lease_expires_at", "attempt_count"],
        unique=False,
        postgresql_where=sa.text(NEW_PAYMENT_SWEEPER_PREDICATE),
    )

    # =========================================================================
    # 6. Maintain Row-Level Security & Untrusted Role Revocations
    # =========================================================================
    op.execute("ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated")
    op.execute("ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_payments FROM anon, authenticated")


def downgrade() -> None:
    # =========================================================================
    # 1. Concurrency-Safe Downgrade Guard (Abort If Active Unexpired Recovery Claims Exist)
    # =========================================================================
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                active_claim_count INTEGER;
            BEGIN
                LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE;

                SELECT COUNT(*)
                INTO active_claim_count
                FROM billing_checkout_attempts
                WHERE recovery_claim_id IS NOT NULL
                  AND recovery_claim_expires_at IS NOT NULL
                  AND recovery_claim_expires_at > now();

                IF active_claim_count > 0 THEN
                    RAISE EXCEPTION
                        'Alembic Downgrade Aborted: % active unexpired checkout recovery claim(s) exist. Wait for active recovery worker claims to expire or complete before downgrading.',
                        active_claim_count;
                END IF;
            END $$
            """
        )
    )

    # =========================================================================
    # 2. Restore Prior billing_payments Sweeper Index Predicate
    # =========================================================================
    op.drop_index(
        "ix_billing_payments_sweeper_claim",
        table_name="billing_payments",
        postgresql_where=sa.text(NEW_PAYMENT_SWEEPER_PREDICATE),
    )
    op.create_index(
        "ix_billing_payments_sweeper_claim",
        "billing_payments",
        ["reconciliation_state", "lease_expires_at", "attempt_count"],
        unique=False,
        postgresql_where=sa.text(OLD_PAYMENT_SWEEPER_PREDICATE),
    )

    # =========================================================================
    # 3. Drop Recovery Claim Index (Preserving Pre-Existing ix_billing_checkout_attempts_lease)
    # =========================================================================
    op.drop_index(
        "ix_billing_checkout_attempts_recovery_claim",
        table_name="billing_checkout_attempts",
        postgresql_where=sa.text(RECOVERY_CLAIM_PREDICATE),
    )

    # =========================================================================
    # 4. Restore Provider-Scoped Active Checkout Unique Index
    # =========================================================================
    op.drop_index(
        "uq_org_env_active_billing_checkout_attempt",
        table_name="billing_checkout_attempts",
        postgresql_where=sa.text(ACTIVE_ATTEMPT_PREDICATE),
    )
    op.create_index(
        "uq_org_scoped_active_billing_checkout_attempt",
        "billing_checkout_attempts",
        ["organization_id", "provider", "merchant_account_id", "environment"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_ATTEMPT_PREDICATE),
    )

    # =========================================================================
    # 5. Drop Stage 2C Recovery Columns in Reverse Order
    # =========================================================================
    op.drop_column("billing_checkout_attempts", "recovery_attempts")
    op.drop_column("billing_checkout_attempts", "recovery_claim_id")
    op.drop_column("billing_checkout_attempts", "recovery_claim_expires_at")
    op.drop_column("billing_checkout_attempts", "next_recovery_retry_at")
