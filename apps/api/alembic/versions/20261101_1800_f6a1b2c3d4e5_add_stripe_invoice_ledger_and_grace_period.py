"""add_stripe_invoice_ledger_and_grace_period

Revision ID: f6a1b2c3d4e5
Revises: e5f6a1b2c3d4
Create Date: 2026-11-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f6a1b2c3d4e5'
down_revision: Union[str, None] = 'e5f6a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Expand WebhookProcessingStatus Enum in PostgreSQL
    # -------------------------------------------------------------------------
    # In PostgreSQL 12+, ALTER TYPE ... ADD VALUE is supported.
    # autocommit_block guarantees execution outside a restricted transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE webhook_processing_status ADD VALUE IF NOT EXISTS 'quarantined'")

    # -------------------------------------------------------------------------
    # 2. Add past_due_since to Subscriptions Table
    # -------------------------------------------------------------------------
    op.add_column(
        'subscriptions',
        sa.Column('past_due_since', sa.DateTime(timezone=True), nullable=True),
    )

    # -------------------------------------------------------------------------
    # 3. Create Stripe Invoices Table & Explicit Constraints
    # -------------------------------------------------------------------------
    op.create_table(
        'stripe_invoices',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('stripe_invoice_id', sa.String(length=255), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=255), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('billing_reason', sa.String(length=64), nullable=True),
        sa.Column('invoice_status', sa.String(length=32), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('amount_paid_minor_units', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'processing_status',
            sa.Enum('pending', 'processed', 'ignored', 'failed', name='invoice_processing_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('billing_period_id', sa.UUID(), nullable=True),
        sa.Column('raw_event_id', sa.String(length=255), nullable=True),
        sa.Column('attempt_count', sa.Integer(), server_default='1', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('diagnostic_payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['billing_period_id'], ['organization_billing_periods.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['raw_event_id'], ['stripe_webhook_events.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_invoice_id', name='uq_stripe_invoices_invoice_id'),
    )
    # Single explicit indexes (no redundant duplicate on stripe_invoice_id)
    op.create_index('ix_stripe_invoices_organization_id', 'stripe_invoices', ['organization_id'])
    op.create_index('ix_stripe_invoices_customer_id', 'stripe_invoices', ['stripe_customer_id'])
    op.create_index('ix_stripe_invoices_subscription_id', 'stripe_invoices', ['stripe_subscription_id'])
    op.create_index('ix_stripe_invoices_billing_period_id', 'stripe_invoices', ['billing_period_id'])
    op.create_index('ix_stripe_invoices_raw_event_id', 'stripe_invoices', ['raw_event_id'])
    op.create_index('ix_stripe_invoices_org_created', 'stripe_invoices', ['organization_id', 'created_at'])
    op.create_index('ix_stripe_invoices_status', 'stripe_invoices', ['processing_status', 'created_at'])

    # -------------------------------------------------------------------------
    # 4. Enable Row-Level Security & Restrict Privileges
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE stripe_invoices ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON stripe_invoices FROM anon, authenticated")


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Audit Barrier: Abort if Quarantined Records Exist
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            res = conn.execute(
                sa.text("SELECT count(*) FROM stripe_webhook_events WHERE status = 'quarantined'")
            )
            count = res.scalar() if res is not None else 0
            if count and count > 0:
                raise RuntimeError(
                    f"Cannot downgrade migration: {count} quarantined audit records exist in stripe_webhook_events. "
                    "Downgrade aborted to preserve audit integrity."
                )

    # -------------------------------------------------------------------------
    # 2. Drop Invoices Table, Constraints, and Type
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE stripe_invoices DISABLE ROW LEVEL SECURITY")
    op.drop_table('stripe_invoices')
    op.execute("DROP TYPE IF EXISTS invoice_processing_status")

    # -------------------------------------------------------------------------
    # 3. Drop past_due_since Column from Subscriptions
    # -------------------------------------------------------------------------
    op.drop_column('subscriptions', 'past_due_since')

    # -------------------------------------------------------------------------
    # 4. Safely Revert WebhookProcessingStatus Enum
    # -------------------------------------------------------------------------
    op.execute("ALTER TYPE webhook_processing_status RENAME TO webhook_processing_status_old")
    op.execute("CREATE TYPE webhook_processing_status AS ENUM ('pending', 'processed', 'failed')")
    op.execute(
        "ALTER TABLE stripe_webhook_events ALTER COLUMN status DROP DEFAULT, "
        "ALTER COLUMN status TYPE webhook_processing_status USING status::text::webhook_processing_status, "
        "ALTER COLUMN status SET DEFAULT 'pending'"
    )
    op.execute("DROP TYPE webhook_processing_status_old")
