"""add_provider_neutral_billing_foundation

Revision ID: a1b2c3d4e5f7
Revises: f6a1b2c3d4e5
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = 'f6a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Add Provider-Neutral Columns to Subscriptions (Nullable initially)
    # -------------------------------------------------------------------------
    op.add_column('subscriptions', sa.Column('provider', sa.String(length=32), nullable=True))
    op.add_column('subscriptions', sa.Column('provider_customer_id', sa.String(length=255), nullable=True))
    op.add_column('subscriptions', sa.Column('provider_subscription_id', sa.String(length=255), nullable=True))
    op.add_column('subscriptions', sa.Column('provider_plan_id', sa.String(length=255), nullable=True))
    op.add_column('subscriptions', sa.Column('merchant_account_id', sa.String(length=64), nullable=True))
    op.add_column('subscriptions', sa.Column('environment', sa.String(length=32), nullable=True))
    op.add_column('subscriptions', sa.Column('last_webhook_event_at', sa.DateTime(timezone=True), nullable=True))

    # -------------------------------------------------------------------------
    # 2. Backfill Existing Stripe Subscriptions
    # -------------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET 
                provider = 'stripe',
                provider_customer_id = stripe_customer_id,
                provider_subscription_id = stripe_subscription_id,
                provider_plan_id = stripe_price_id,
                merchant_account_id = 'default',
                environment = 'legacy',
                last_webhook_event_at = last_stripe_event_created_at
            WHERE provider IS NULL;
            """
        )
    )

    # -------------------------------------------------------------------------
    # 3. Enforce NOT NULL on Provider-Neutral Identity (No permanent defaults)
    # -------------------------------------------------------------------------
    op.alter_column('subscriptions', 'provider', nullable=False)
    op.alter_column('subscriptions', 'provider_customer_id', nullable=False)
    op.alter_column('subscriptions', 'merchant_account_id', nullable=False)
    op.alter_column('subscriptions', 'environment', nullable=False)

    # -------------------------------------------------------------------------
    # 4. Safely Relax stripe_customer_id NOT NULL
    # -------------------------------------------------------------------------
    op.alter_column('subscriptions', 'stripe_customer_id', nullable=True)

    # -------------------------------------------------------------------------
    # 5. Scoped Unique Indexes on Subscriptions
    # -------------------------------------------------------------------------
    op.create_index(
        'uq_subscriptions_provider_customer',
        'subscriptions',
        ['provider', 'merchant_account_id', 'environment', 'provider_customer_id'],
        unique=True,
    )
    op.create_index(
        'uq_subscriptions_provider_subscription',
        'subscriptions',
        ['provider', 'merchant_account_id', 'environment', 'provider_subscription_id'],
        unique=True,
        postgresql_where=sa.text('provider_subscription_id IS NOT NULL'),
    )

    # -------------------------------------------------------------------------
    # 6. Billing Webhook Events Table & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_webhook_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), nullable=False),
        sa.Column('environment', sa.String(length=32), nullable=False),
        sa.Column('provider_event_id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=128), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'processed', 'failed', 'quarantined', name='webhook_processing_status', create_type=False),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('signature_verified', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('quarantined_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('quarantine_reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'provider', 'merchant_account_id', 'environment', 'provider_event_id',
            name='uq_billing_webhook_provider_event',
        ),
    )
    op.create_index('ix_billing_webhook_events_event_type', 'billing_webhook_events', ['event_type'])
    op.create_index(
        'ix_billing_webhook_events_eligible',
        'billing_webhook_events',
        ['status', 'next_retry_at', 'received_at'],
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.create_index(
        'ix_billing_webhook_events_provider_type',
        'billing_webhook_events',
        ['provider', 'event_type'],
    )

    # -------------------------------------------------------------------------
    # 7. Billing Checkout Attempts Table & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_checkout_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), nullable=False),
        sa.Column('environment', sa.String(length=32), nullable=False),
        sa.Column('provider_customer_id', sa.String(length=255), nullable=True),
        sa.Column('provider_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('provider_order_id', sa.String(length=255), nullable=True),
        sa.Column('provider_session_id', sa.String(length=255), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'creating', 'recovery_required', 'open', 'payment_pending', 'complete', 'expired', 'failed',
                name='checkout_attempt_status', create_type=False,
            ),
            server_default='creating',
            nullable=False,
        ),
        sa.Column('amount_minor_units', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('checkout_url', sa.Text(), nullable=True),
        sa.Column('request_payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('attempt_lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('session_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'idempotency_key', name='uq_billing_checkout_org_idempotency'),
    )
    op.create_index(
        'uq_org_active_billing_checkout_attempt',
        'billing_checkout_attempts',
        ['organization_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('creating', 'recovery_required', 'open', 'payment_pending')"),
    )
    op.create_index('ix_billing_checkout_attempts_organization_id', 'billing_checkout_attempts', ['organization_id'])
    op.create_index('ix_billing_checkout_attempts_org_created', 'billing_checkout_attempts', ['organization_id', 'created_at'])
    op.create_index(
        'ix_billing_checkout_attempts_provider_sub',
        'billing_checkout_attempts',
        ['provider', 'merchant_account_id', 'environment', 'provider_subscription_id'],
    )
    op.create_index(
        'ix_billing_checkout_attempts_provider_order',
        'billing_checkout_attempts',
        ['provider', 'merchant_account_id', 'environment', 'provider_order_id'],
    )
    op.create_index(
        'ix_billing_checkout_attempts_lease',
        'billing_checkout_attempts',
        ['status', 'attempt_lease_expires_at'],
        postgresql_where=sa.text("status = 'creating'"),
    )

    # -------------------------------------------------------------------------
    # 8. Billing Invoices Table, Constraints & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_invoices',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), nullable=False),
        sa.Column('environment', sa.String(length=32), nullable=False),
        sa.Column('provider_invoice_id', sa.String(length=255), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('subscription_id', sa.UUID(), nullable=True),
        sa.Column('provider_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('provider_customer_id', sa.String(length=255), nullable=False),
        sa.Column('billing_reason', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('gross_amount_minor_units', sa.BigInteger(), nullable=False),
        sa.Column('amount_paid_minor_units', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('amount_due_minor_units', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'processing_status',
            sa.Enum('pending', 'processed', 'ignored', 'failed', name='invoice_processing_status', create_type=False),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('reconciliation_status', sa.String(length=32), server_default='unreconciled', nullable=False),
        sa.Column('billing_period_id', sa.UUID(), nullable=True),
        sa.Column('raw_event_id', sa.UUID(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), server_default='1', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('provider_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            'gross_amount_minor_units >= 0 AND amount_paid_minor_units >= 0 AND amount_due_minor_units >= 0',
            name='ck_billing_invoices_amounts_non_negative',
        ),
        sa.CheckConstraint(
            'amount_paid_minor_units <= gross_amount_minor_units',
            name='ck_billing_invoices_paid_le_gross',
        ),
        sa.CheckConstraint(
            'period_end IS NULL OR period_start IS NULL OR period_end > period_start',
            name='ck_billing_invoices_period_order',
        ),
        sa.ForeignKeyConstraint(['billing_period_id'], ['organization_billing_periods.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['raw_event_id'], ['billing_webhook_events.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'provider', 'merchant_account_id', 'environment', 'provider_invoice_id',
            name='uq_billing_invoices_provider_invoice',
        ),
    )
    op.create_index('ix_billing_invoices_organization_id', 'billing_invoices', ['organization_id'])
    op.create_index('ix_billing_invoices_subscription_id', 'billing_invoices', ['subscription_id'])
    op.create_index('ix_billing_invoices_billing_period_id', 'billing_invoices', ['billing_period_id'])
    op.create_index('ix_billing_invoices_raw_event_id', 'billing_invoices', ['raw_event_id'])
    op.create_index('ix_billing_invoices_org_created', 'billing_invoices', ['organization_id', 'created_at'])
    op.create_index('ix_billing_invoices_provider_sub', 'billing_invoices', ['provider', 'provider_subscription_id'])
    op.create_index('ix_billing_invoices_status', 'billing_invoices', ['processing_status', 'created_at'])

    # -------------------------------------------------------------------------
    # 9. Billing Payments Table, Constraints & Indexes (Payment-First Staging)
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_payments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), nullable=False),
        sa.Column('environment', sa.String(length=32), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=255), nullable=False),
        sa.Column('provider_order_id', sa.String(length=255), nullable=True),
        sa.Column('provider_invoice_id', sa.String(length=255), nullable=True),
        sa.Column('provider_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('organization_id', sa.UUID(), nullable=True),
        sa.Column('subscription_id', sa.UUID(), nullable=True),
        sa.Column('invoice_id', sa.UUID(), nullable=True),
        sa.Column('amount_minor_units', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('refund_status', sa.String(length=32), nullable=True),
        sa.Column('amount_refunded_minor_units', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('reconciliation_state', sa.String(length=32), server_default='unreconciled', nullable=False),
        sa.Column('lease_token', sa.UUID(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('max_attempts', sa.Integer(), server_default='5', nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('raw_event_id', sa.UUID(), nullable=True),
        sa.Column('provider_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('amount_minor_units > 0', name='ck_billing_payments_amount_positive'),
        sa.CheckConstraint(
            'amount_refunded_minor_units >= 0 AND amount_refunded_minor_units <= amount_minor_units',
            name='ck_billing_payments_refund_non_negative',
        ),
        sa.ForeignKeyConstraint(['invoice_id'], ['billing_invoices.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['raw_event_id'], ['billing_webhook_events.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'provider', 'merchant_account_id', 'environment', 'provider_payment_id',
            name='uq_billing_payments_provider_payment',
        ),
    )
    op.create_index('ix_billing_payments_organization_id', 'billing_payments', ['organization_id'])
    op.create_index('ix_billing_payments_subscription_id', 'billing_payments', ['subscription_id'])
    op.create_index('ix_billing_payments_invoice_id', 'billing_payments', ['invoice_id'])
    op.create_index('ix_billing_payments_raw_event_id', 'billing_payments', ['raw_event_id'])
    op.create_index('ix_billing_payments_provider_invoice', 'billing_payments', ['provider', 'provider_invoice_id'])
    op.create_index('ix_billing_payments_provider_sub', 'billing_payments', ['provider', 'provider_subscription_id'])
    op.create_index('ix_billing_payments_provider_order', 'billing_payments', ['provider', 'provider_order_id'])
    op.create_index(
        'ix_billing_payments_sweeper_claim',
        'billing_payments',
        ['reconciliation_state', 'lease_expires_at', 'attempt_count'],
        postgresql_where=sa.text("reconciliation_state IN ('unreconciled', 'reconciling')"),
    )

    # -------------------------------------------------------------------------
    # 10. Payment Reconciliations Table, Constraints & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'payment_reconciliations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), nullable=False),
        sa.Column('environment', sa.String(length=32), nullable=False),
        sa.Column('case_key', sa.String(length=255), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=True),
        sa.Column('subscription_id', sa.UUID(), nullable=True),
        sa.Column('invoice_id', sa.UUID(), nullable=True),
        sa.Column('payment_id', sa.UUID(), nullable=True),
        sa.Column('raw_event_id', sa.UUID(), nullable=True),
        sa.Column('reason', sa.String(length=64), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending_review', 'resolved_refunded', 'resolved_ignored', name='reconciliation_status', create_type=False),
            server_default='pending_review',
            nullable=False,
        ),
        sa.Column('diagnostic_payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('resolved_by', sa.UUID(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['billing_invoices.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['payment_id'], ['billing_payments.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['raw_event_id'], ['billing_webhook_events.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'provider', 'merchant_account_id', 'environment', 'case_key',
            name='uq_payment_reconciliations_case_key',
        ),
    )
    op.create_index('ix_payment_reconciliations_organization_id', 'payment_reconciliations', ['organization_id'])
    op.create_index('ix_payment_reconciliations_subscription_id', 'payment_reconciliations', ['subscription_id'])
    op.create_index('ix_payment_reconciliations_invoice_id', 'payment_reconciliations', ['invoice_id'])
    op.create_index('ix_payment_reconciliations_payment_id', 'payment_reconciliations', ['payment_id'])
    op.create_index('ix_payment_reconciliations_raw_event_id', 'payment_reconciliations', ['raw_event_id'])
    op.create_index('ix_payment_reconciliations_org_status', 'payment_reconciliations', ['organization_id', 'status'])

    # -------------------------------------------------------------------------
    # 11. Enable Row-Level Security & Restrict Untrusted Client Access
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_webhook_events FROM anon, authenticated")

    op.execute("ALTER TABLE billing_checkout_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_checkout_attempts FROM anon, authenticated")

    op.execute("ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_invoices FROM anon, authenticated")

    op.execute("ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_payments FROM anon, authenticated")

    op.execute("ALTER TABLE payment_reconciliations ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON payment_reconciliations FROM anon, authenticated")


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Concurrency-Safe Downgrade Guard with ACCESS EXCLUSIVE Table Locks
    # -------------------------------------------------------------------------
    # Acquires table locks on all 6 tables to eliminate check-then-drop race conditions.
    # Refuses downgrade if ANY generalized records exist or non-Stripe records exist.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                cnt_webhooks INTEGER;
                cnt_checkouts INTEGER;
                cnt_invoices INTEGER;
                cnt_payments INTEGER;
                cnt_reconciliations INTEGER;
                cnt_non_stripe_subs INTEGER;
                total_records INTEGER;
            BEGIN
                -- Acquire exclusive table locks
                LOCK TABLE subscriptions IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_webhook_events IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_invoices IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE billing_payments IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE payment_reconciliations IN ACCESS EXCLUSIVE MODE;

                SELECT count(*) INTO cnt_webhooks FROM billing_webhook_events;
                SELECT count(*) INTO cnt_checkouts FROM billing_checkout_attempts;
                SELECT count(*) INTO cnt_invoices FROM billing_invoices;
                SELECT count(*) INTO cnt_payments FROM billing_payments;
                SELECT count(*) INTO cnt_reconciliations FROM payment_reconciliations;
                SELECT count(*) INTO cnt_non_stripe_subs 
                FROM subscriptions 
                WHERE stripe_customer_id IS NULL OR provider != 'stripe';

                total_records := cnt_webhooks + cnt_checkouts + cnt_invoices 
                                 + cnt_payments + cnt_reconciliations + cnt_non_stripe_subs;

                IF total_records > 0 THEN
                    RAISE EXCEPTION 
                        'Alembic Downgrade Aborted: % generalized billing records exist (webhooks: %, checkouts: %, invoices: %, payments: %, reconciliations: %, non-Stripe subs: %). Dropping generalized billing tables is strictly forbidden to preserve financial audit integrity.',
                        total_records, cnt_webhooks, cnt_checkouts, cnt_invoices, cnt_payments, cnt_reconciliations, cnt_non_stripe_subs;
                END IF;
            END $$;
            """
        )
    )

    # -------------------------------------------------------------------------
    # 2. Disable Row-Level Security
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE payment_reconciliations DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_payments DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_invoices DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_checkout_attempts DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_webhook_events DISABLE ROW LEVEL SECURITY")

    # -------------------------------------------------------------------------
    # 3. Drop Generalized Tables in Reverse Dependency Order
    # -------------------------------------------------------------------------
    op.drop_table('payment_reconciliations')
    op.drop_table('billing_payments')
    op.drop_table('billing_invoices')
    op.drop_table('billing_checkout_attempts')
    op.drop_table('billing_webhook_events')

    # -------------------------------------------------------------------------
    # 4. Drop Scoped Unique Indexes from Subscriptions
    # -------------------------------------------------------------------------
    op.drop_index(
        'uq_subscriptions_provider_subscription',
        table_name='subscriptions',
        postgresql_where=sa.text('provider_subscription_id IS NOT NULL'),
    )
    op.drop_index('uq_subscriptions_provider_customer', table_name='subscriptions')

    # -------------------------------------------------------------------------
    # 5. Restore stripe_customer_id NOT NULL Constraint
    # -------------------------------------------------------------------------
    op.alter_column('subscriptions', 'stripe_customer_id', nullable=False)

    # -------------------------------------------------------------------------
    # 6. Drop Provider-Neutral Columns from Subscriptions
    # -------------------------------------------------------------------------
    op.drop_column('subscriptions', 'last_webhook_event_at')
    op.drop_column('subscriptions', 'environment')
    op.drop_column('subscriptions', 'merchant_account_id')
    op.drop_column('subscriptions', 'provider_plan_id')
    op.drop_column('subscriptions', 'provider_subscription_id')
    op.drop_column('subscriptions', 'provider_customer_id')
    op.drop_column('subscriptions', 'provider')
