"""add_stripe_checkout_and_reconciliation

Revision ID: d4e5f6a1b2c3
Revises: c3d4e5f6a1b2
Create Date: 2026-10-30 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a1b2c3'
down_revision: Union[str, None] = 'c3d4e5f6a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Stripe Checkout Attempts Table & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'stripe_checkout_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=255), nullable=False),
        sa.Column('stripe_session_id', sa.String(length=255), nullable=True),
        sa.Column('checkout_url', sa.Text(), nullable=True),
        sa.Column('request_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'creating',
                'recovery_required',
                'open',
                'payment_pending',
                'complete',
                'expired',
                'failed',
                name='checkout_attempt_status',
            ),
            server_default='creating',
            nullable=False,
        ),
        sa.Column('attempt_lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('session_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_session_id', name='uq_stripe_checkout_attempts_session_id'),
    )
    op.create_index(
        'ix_stripe_checkout_attempts_organization_id',
        'stripe_checkout_attempts',
        ['organization_id'],
    )
    op.create_index(
        'ix_stripe_checkout_attempts_org_created',
        'stripe_checkout_attempts',
        ['organization_id', 'created_at'],
    )
    # Partial unique index: at most ONE active/pending/in-flight attempt per organization
    op.create_index(
        'uq_org_active_checkout_attempt',
        'stripe_checkout_attempts',
        ['organization_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('creating', 'recovery_required', 'open', 'payment_pending')"),
    )
    op.create_index(
        'ix_stripe_checkout_attempts_lease',
        'stripe_checkout_attempts',
        ['status', 'attempt_lease_expires_at'],
        postgresql_where=sa.text("status = 'creating'"),
    )

    # -------------------------------------------------------------------------
    # 2. Billing Reconciliations Table & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_reconciliations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.String(length=255), nullable=False),
        sa.Column('existing_subscription_id', sa.String(length=255), nullable=False),
        sa.Column('incoming_subscription_id', sa.String(length=255), nullable=False),
        sa.Column('reason', sa.String(length=64), server_default='duplicate_active_subscription', nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'pending_review',
                'resolved_refunded',
                'resolved_ignored',
                name='reconciliation_status',
            ),
            server_default='pending_review',
            nullable=False,
        ),
        sa.Column('diagnostic_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('resolved_by', sa.UUID(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['stripe_webhook_events.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'incoming_subscription_id', name='uq_reconciliation_event_sub'),
    )
    op.create_index(
        'ix_billing_reconciliations_organization_id',
        'billing_reconciliations',
        ['organization_id'],
    )
    op.create_index(
        'ix_billing_reconciliations_event_id',
        'billing_reconciliations',
        ['event_id'],
    )
    op.create_index(
        'ix_billing_reconciliations_org_status',
        'billing_reconciliations',
        ['organization_id', 'status'],
    )

    # -------------------------------------------------------------------------
    # 3. Enable Row-Level Security
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE stripe_checkout_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_reconciliations ENABLE ROW LEVEL SECURITY")

    # -------------------------------------------------------------------------
    # 4. Revoke direct table privileges from untrusted PostgREST client roles
    # -------------------------------------------------------------------------
    op.execute("REVOKE ALL ON stripe_checkout_attempts FROM anon, authenticated")
    op.execute("REVOKE ALL ON billing_reconciliations FROM anon, authenticated")


def downgrade() -> None:
    # 1. Disable Row-Level Security
    op.execute("ALTER TABLE billing_reconciliations DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stripe_checkout_attempts DISABLE ROW LEVEL SECURITY")

    # 2. Drop billing_reconciliations indexes, constraints and table
    op.drop_index('ix_billing_reconciliations_org_status', table_name='billing_reconciliations')
    op.drop_index('ix_billing_reconciliations_event_id', table_name='billing_reconciliations')
    op.drop_index('ix_billing_reconciliations_organization_id', table_name='billing_reconciliations')
    op.drop_constraint('uq_reconciliation_event_sub', 'billing_reconciliations', type_='unique')
    op.drop_table('billing_reconciliations')
    op.execute("DROP TYPE IF EXISTS reconciliation_status")

    # 3. Drop stripe_checkout_attempts indexes, constraints and table
    op.drop_index(
        'ix_stripe_checkout_attempts_lease',
        table_name='stripe_checkout_attempts',
        postgresql_where=sa.text("status = 'creating'"),
    )
    op.drop_index(
        'uq_org_active_checkout_attempt',
        table_name='stripe_checkout_attempts',
        postgresql_where=sa.text("status IN ('creating', 'recovery_required', 'open', 'payment_pending')"),
    )
    op.drop_index('ix_stripe_checkout_attempts_org_created', table_name='stripe_checkout_attempts')
    op.drop_index('ix_stripe_checkout_attempts_organization_id', table_name='stripe_checkout_attempts')
    op.drop_constraint('uq_stripe_checkout_attempts_session_id', 'stripe_checkout_attempts', type_='unique')
    op.drop_table('stripe_checkout_attempts')
    op.execute("DROP TYPE IF EXISTS checkout_attempt_status")
