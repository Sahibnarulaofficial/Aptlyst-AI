"""add_payment_association_and_entitlement_provenance

Revision ID: e2f3a4b5c6d1
Revises: d1e2f3a4b5c6
Create Date: 2026-11-08 16:00:00.000000

Adds database-level payment-to-checkout association, entitlement provenance,
composite relational guarantees, and immutability triggers for Razorpay billing:
1. organization_billing_periods:
   - Add subscription_id (UUID, ForeignKey to subscriptions.id, ON DELETE RESTRICT)
   - Index ix_billing_periods_subscription_id
   - Pre-backfill unified candidate conflict detection and validation gate
   - Unified deterministic backfill across Stripe and provider-neutral invoices
   - Transactional abort gate verifying zero active Pro periods lack provenance
   - Immutability trigger preventing mutation of subscription_id once set
2. billing_checkout_attempts:
   - Add provider_payment_id (VARCHAR(255), nullable)
   - Partial unique index uq_checkout_attempts_provider_sub
   - Partial unique index uq_checkout_attempts_provider_payment
   - Composite unique constraint uq_bca_composite_identity
   - Immutability trigger preventing mutation of provider_payment_id once set
3. billing_payments:
   - Add checkout_attempt_id (UUID, ForeignKey to billing_checkout_attempts.id, ON DELETE RESTRICT)
   - Partial unique index uq_billing_payments_checkout_attempt
   - Index ix_billing_payments_checkout_attempt
   - Check constraint ck_billing_payments_checkout_attempt_complete
   - Composite foreign key fk_billing_payments_checkout_attempt_composite
   - Immutability trigger preventing mutation of checkout_attempt_id once set
4. RLS & Privileges:
   - Enforce Row-Level Security on all modified tables
   - Revoke privileges from untrusted roles (anon, authenticated)
5. Concurrency-Safe Downgrade:
   - ACCESS EXCLUSIVE lock downgrade guard aborting if linked records exist
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d1'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # 1. organization_billing_periods: Column, Index & FK
    # =========================================================================
    op.add_column(
        'organization_billing_periods',
        sa.Column('subscription_id', sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        'fk_billing_periods_subscription_id',
        'organization_billing_periods',
        'subscriptions',
        ['subscription_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_index(
        'ix_billing_periods_subscription_id',
        'organization_billing_periods',
        ['subscription_id'],
    )

    # =========================================================================
    # 2. Pre-Backfill Provenance Conflict Detection & Validation Gate
    # =========================================================================
    # Detects:
    # A. Conflicting candidate subscriptions across stripe_invoices and billing_invoices.
    # B. Disagreements between existing non-null subscription_id and invoice evidence.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                conflict_count INTEGER;
                conflict_details TEXT;
                mismatch_count INTEGER;
                mismatch_details TEXT;
            BEGIN
                -- 1. Check for cross-invoice candidate conflicts (more than one distinct subscription candidate)
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT
                        si.billing_period_id,
                        s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s 
                      ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id IS NOT NULL
                      AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT
                        bi.billing_period_id,
                        bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id IS NOT NULL
                      AND bi.subscription_id IS NOT NULL
                )
                SELECT 
                    COUNT(DISTINCT billing_period_id),
                    string_agg('period_id=' || billing_period_id::text || ' (sub_count=' || sub_count::text || ')', ', ')
                INTO conflict_count, conflict_details
                FROM (
                    SELECT billing_period_id, COUNT(DISTINCT subscription_id) AS sub_count
                    FROM unified_invoice_candidates
                    GROUP BY billing_period_id
                    HAVING COUNT(DISTINCT subscription_id) > 1
                ) conflicting_periods;

                IF conflict_count > 0 THEN
                    RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: % billing period(s) have conflicting subscription evidence across invoices: %. Aborting transaction to prevent ambiguous provenance.',
                        conflict_count, conflict_details;
                END IF;

                -- 2. Check for conflicts between existing non-null provenance and invoice evidence
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT
                        si.billing_period_id,
                        s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s 
                      ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id IS NOT NULL
                      AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT
                        bi.billing_period_id,
                        bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id IS NOT NULL
                      AND bi.subscription_id IS NOT NULL
                )
                SELECT 
                    COUNT(*),
                    string_agg('period_id=' || obp.id::text || ' existing_sub=' || obp.subscription_id::text || ' invoice_sub=' || uic.subscription_id::text, ', ')
                INTO mismatch_count, mismatch_details
                FROM organization_billing_periods obp
                JOIN unified_invoice_candidates uic ON uic.billing_period_id = obp.id
                WHERE obp.subscription_id IS NOT NULL
                  AND obp.subscription_id IS DISTINCT FROM uic.subscription_id;

                IF mismatch_count > 0 THEN
                    RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: % billing period(s) have existing provenance disagreeing with invoice evidence: %. Aborting transaction to prevent corrupting established provenance.',
                        mismatch_count, mismatch_details;
                END IF;
            END $$;
            """
        )
    )

    # =========================================================================
    # 3. Unified Deterministic Backfill
    # =========================================================================
    # Updates only periods where subscription_id is NULL and exactly one
    # authoritative subscription candidate exists across both invoice sources.
    op.execute(
        sa.text(
            """
            WITH unified_invoice_candidates AS (
                SELECT DISTINCT
                    si.billing_period_id,
                    s.id AS subscription_id
                FROM stripe_invoices si
                JOIN subscriptions s 
                  ON s.stripe_subscription_id = si.stripe_subscription_id
                WHERE si.billing_period_id IS NOT NULL
                  AND si.stripe_subscription_id IS NOT NULL
                UNION
                SELECT DISTINCT
                    bi.billing_period_id,
                    bi.subscription_id
                FROM billing_invoices bi
                WHERE bi.billing_period_id IS NOT NULL
                  AND bi.subscription_id IS NOT NULL
            ),
            candidate_counts AS (
                SELECT 
                    billing_period_id,
                    COUNT(DISTINCT subscription_id) AS sub_count
                FROM unified_invoice_candidates
                GROUP BY billing_period_id
            ),
            unambiguous_candidates AS (
                SELECT 
                    uic.billing_period_id,
                    uic.subscription_id
                FROM unified_invoice_candidates uic
                JOIN candidate_counts cc ON cc.billing_period_id = uic.billing_period_id
                WHERE cc.sub_count = 1
            )
            UPDATE organization_billing_periods obp
            SET subscription_id = uc.subscription_id
            FROM unambiguous_candidates uc
            WHERE obp.id = uc.billing_period_id
              AND obp.subscription_id IS NULL;
            """
        )
    )

    # =========================================================================
    # 4. Transactional Abort Gate for Unresolved Active Pro Billing Periods
    # =========================================================================
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                unresolved_count INTEGER;
                unresolved_details TEXT;
            BEGIN
                SELECT COUNT(*), string_agg('period_id=' || id::text || ' org_id=' || organization_id::text, ', ')
                INTO unresolved_count, unresolved_details
                FROM organization_billing_periods
                WHERE plan_tier != 'free'
                  AND is_active = true
                  AND subscription_id IS NULL;

                IF unresolved_count > 0 THEN
                    RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: % active Pro billing period(s) lack proven subscription provenance: %. Aborting transaction to prevent breaking live LLM token reservation.', 
                        unresolved_count, unresolved_details;
                END IF;
            END $$;
            """
        )
    )

    # =========================================================================
    # 5. billing_checkout_attempts: Columns, Scoped Unique Indexes & Identity
    # =========================================================================
    op.add_column(
        'billing_checkout_attempts',
        sa.Column('provider_payment_id', sa.String(length=255), nullable=True),
    )

    # Scoped partial unique index on provider_subscription_id
    op.create_index(
        'uq_checkout_attempts_provider_sub',
        'billing_checkout_attempts',
        ['provider', 'merchant_account_id', 'environment', 'provider_subscription_id'],
        unique=True,
        postgresql_where=sa.text('provider_subscription_id IS NOT NULL'),
    )

    # Scoped partial unique index on provider_payment_id
    op.create_index(
        'uq_checkout_attempts_provider_payment',
        'billing_checkout_attempts',
        ['provider', 'merchant_account_id', 'environment', 'provider_payment_id'],
        unique=True,
        postgresql_where=sa.text('provider_payment_id IS NOT NULL'),
    )

    # Composite unique constraint for composite foreign key targeting
    op.create_unique_constraint(
        'uq_bca_composite_identity',
        'billing_checkout_attempts',
        [
            'id',
            'provider',
            'merchant_account_id',
            'environment',
            'provider_subscription_id',
            'provider_payment_id',
        ],
    )

    # =========================================================================
    # 6. billing_payments: Columns, Unique, Check & Composite Foreign Key
    # =========================================================================
    op.add_column(
        'billing_payments',
        sa.Column('checkout_attempt_id', sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        'fk_billing_payments_checkout_attempt',
        'billing_payments',
        'billing_checkout_attempts',
        ['checkout_attempt_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_index(
        'ix_billing_payments_checkout_attempt',
        'billing_payments',
        ['checkout_attempt_id'],
    )
    op.create_index(
        'uq_billing_payments_checkout_attempt',
        'billing_payments',
        ['checkout_attempt_id'],
        unique=True,
        postgresql_where=sa.text('checkout_attempt_id IS NOT NULL'),
    )

    # Check constraint: when checkout_attempt_id IS NOT NULL, all composite attributes must be non-null
    op.create_check_constraint(
        'ck_billing_payments_checkout_attempt_complete',
        'billing_payments',
        sa.text(
            "(checkout_attempt_id IS NULL) OR ("
            "provider IS NOT NULL AND "
            "merchant_account_id IS NOT NULL AND "
            "environment IS NOT NULL AND "
            "provider_subscription_id IS NOT NULL AND "
            "provider_payment_id IS NOT NULL"
            ")"
        ),
    )

    # Composite foreign key constraint: binds attempt_id + 5 provider attributes
    op.create_foreign_key(
        'fk_billing_payments_checkout_attempt_composite',
        'billing_payments',
        'billing_checkout_attempts',
        [
            'checkout_attempt_id',
            'provider',
            'merchant_account_id',
            'environment',
            'provider_subscription_id',
            'provider_payment_id',
        ],
        [
            'id',
            'provider',
            'merchant_account_id',
            'environment',
            'provider_subscription_id',
            'provider_payment_id',
        ],
        ondelete='RESTRICT',
    )

    # =========================================================================
    # 7. Immutability Triggers (Prevent Mutation Once Populated)
    # =========================================================================
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION prevent_billing_payment_checkout_attempt_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.checkout_attempt_id IS NOT NULL AND NEW.checkout_attempt_id IS DISTINCT FROM OLD.checkout_attempt_id THEN
                    RAISE EXCEPTION 'Immutability violation: billing_payments.checkout_attempt_id cannot be reassigned once established (attempted change from % to %)',
                        OLD.checkout_attempt_id, NEW.checkout_attempt_id;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_prevent_billing_payment_checkout_attempt_mutation ON billing_payments;
            CREATE TRIGGER trg_prevent_billing_payment_checkout_attempt_mutation
            BEFORE UPDATE ON billing_payments
            FOR EACH ROW EXECUTE FUNCTION prevent_billing_payment_checkout_attempt_mutation();
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION prevent_checkout_attempt_payment_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.provider_payment_id IS NOT NULL AND NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id THEN
                    RAISE EXCEPTION 'Immutability violation: billing_checkout_attempts.provider_payment_id cannot be reassigned once established (attempted change from % to %)',
                        OLD.provider_payment_id, NEW.provider_payment_id;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_prevent_checkout_attempt_payment_mutation ON billing_checkout_attempts;
            CREATE TRIGGER trg_prevent_checkout_attempt_payment_mutation
            BEFORE UPDATE ON billing_checkout_attempts
            FOR EACH ROW EXECUTE FUNCTION prevent_checkout_attempt_payment_mutation();
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION prevent_billing_period_subscription_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.subscription_id IS NOT NULL AND NEW.subscription_id IS DISTINCT FROM OLD.subscription_id THEN
                    RAISE EXCEPTION 'Immutability violation: organization_billing_periods.subscription_id cannot be reassigned once established (attempted change from % to %)',
                        OLD.subscription_id, NEW.subscription_id;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_prevent_billing_period_subscription_mutation ON organization_billing_periods;
            CREATE TRIGGER trg_prevent_billing_period_subscription_mutation
            BEFORE UPDATE ON organization_billing_periods
            FOR EACH ROW EXECUTE FUNCTION prevent_billing_period_subscription_mutation();
            """
        )
    )

    # =========================================================================
    # 8. Maintain Row-Level Security & Untrusted Client Revocations
    # =========================================================================
    op.execute("ALTER TABLE organization_billing_periods ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON organization_billing_periods FROM anon, authenticated")
    op.execute("ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated")
    op.execute("ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_payments FROM anon, authenticated")


def downgrade() -> None:
    # =========================================================================
    # 1. Concurrency-Safe Downgrade Guard with ACCESS EXCLUSIVE Table Locks
    # =========================================================================
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                cnt_linked_periods INTEGER;
                cnt_linked_attempts INTEGER;
                cnt_linked_payments INTEGER;
                total_records INTEGER;
            BEGIN
                LOCK TABLE organization_billing_periods IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_payments IN ACCESS EXCLUSIVE MODE;

                SELECT count(*) INTO cnt_linked_periods FROM organization_billing_periods WHERE subscription_id IS NOT NULL;
                SELECT count(*) INTO cnt_linked_attempts FROM billing_checkout_attempts WHERE provider_payment_id IS NOT NULL;
                SELECT count(*) INTO cnt_linked_payments FROM billing_payments WHERE checkout_attempt_id IS NOT NULL;

                total_records := cnt_linked_periods + cnt_linked_attempts + cnt_linked_payments;
                IF total_records > 0 THEN
                    RAISE EXCEPTION 
                        'Alembic Downgrade Aborted: % associated records exist (linked periods: %, linked attempts: %, linked payments: %). Downgrading payment association schema is strictly forbidden to preserve financial provenance.',
                        total_records, cnt_linked_periods, cnt_linked_attempts, cnt_linked_payments;
                END IF;
            END $$;
            """
        )
    )

    # =========================================================================
    # 2. Drop Immutability Triggers and Functions
    # =========================================================================
    op.execute("DROP TRIGGER IF EXISTS trg_prevent_billing_period_subscription_mutation ON organization_billing_periods")
    op.execute("DROP FUNCTION IF EXISTS prevent_billing_period_subscription_mutation()")

    op.execute("DROP TRIGGER IF EXISTS trg_prevent_checkout_attempt_payment_mutation ON billing_checkout_attempts")
    op.execute("DROP FUNCTION IF EXISTS prevent_checkout_attempt_payment_mutation()")

    op.execute("DROP TRIGGER IF EXISTS trg_prevent_billing_payment_checkout_attempt_mutation ON billing_payments")
    op.execute("DROP FUNCTION IF EXISTS prevent_billing_payment_checkout_attempt_mutation()")

    # =========================================================================
    # 3. Drop billing_payments Constraints, Indexes & Column
    # =========================================================================
    op.drop_constraint('fk_billing_payments_checkout_attempt_composite', 'billing_payments', type_='foreignkey')
    op.drop_constraint('ck_billing_payments_checkout_attempt_complete', 'billing_payments', type_='check')
    op.drop_index(
        'uq_billing_payments_checkout_attempt',
        table_name='billing_payments',
        postgresql_where=sa.text('checkout_attempt_id IS NOT NULL'),
    )
    op.drop_index('ix_billing_payments_checkout_attempt', table_name='billing_payments')
    op.drop_constraint('fk_billing_payments_checkout_attempt', 'billing_payments', type_='foreignkey')
    op.drop_column('billing_payments', 'checkout_attempt_id')

    # =========================================================================
    # 4. Drop billing_checkout_attempts Constraints, Indexes & Column
    # =========================================================================
    op.drop_constraint('uq_bca_composite_identity', 'billing_checkout_attempts', type_='unique')
    op.drop_index(
        'uq_checkout_attempts_provider_payment',
        table_name='billing_checkout_attempts',
        postgresql_where=sa.text('provider_payment_id IS NOT NULL'),
    )
    op.drop_index(
        'uq_checkout_attempts_provider_sub',
        table_name='billing_checkout_attempts',
        postgresql_where=sa.text('provider_subscription_id IS NOT NULL'),
    )
    op.drop_column('billing_checkout_attempts', 'provider_payment_id')

    # =========================================================================
    # 5. Drop organization_billing_periods FK, Index & Column
    # =========================================================================
    op.drop_index('ix_billing_periods_subscription_id', table_name='organization_billing_periods')
    op.drop_constraint('fk_billing_periods_subscription_id', 'organization_billing_periods', type_='foreignkey')
    op.drop_column('organization_billing_periods', 'subscription_id')
