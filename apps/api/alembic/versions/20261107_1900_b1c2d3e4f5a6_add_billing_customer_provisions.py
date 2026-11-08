"""add_billing_customer_provisions

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f7
Create Date: 2026-11-07 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Billing Customer Provisions Table, Constraints & Indexes
    # -------------------------------------------------------------------------
    op.create_table(
        'billing_customer_provisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('merchant_account_id', sa.String(length=64), server_default='default', nullable=False),
        sa.Column('environment', sa.String(length=32), server_default='live', nullable=False),
        sa.Column('provider_customer_id', sa.String(length=255), nullable=True),
        sa.Column(
            'status',
            sa.Enum('pending', 'resolved', 'ambiguous', 'failed', name='customer_provision_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('claim_operation_id', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_code', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'organization_id',
            'provider',
            'merchant_account_id',
            'environment',
            name='uq_billing_customer_provisions_org_provider',
        ),
    )
    op.create_index('ix_billing_customer_provisions_organization_id', 'billing_customer_provisions', ['organization_id'])
    op.create_index('ix_billing_customer_provisions_status', 'billing_customer_provisions', ['status'])
    op.create_index('ix_billing_customer_provisions_claim', 'billing_customer_provisions', ['claim_operation_id'])
    op.create_index(
        'ix_billing_customer_provisions_lease',
        'billing_customer_provisions',
        ['status', 'lease_expires_at'],
    )
    op.create_index(
        'uq_billing_customer_provisions_provider_customer',
        'billing_customer_provisions',
        ['provider', 'merchant_account_id', 'environment', 'provider_customer_id'],
        unique=True,
        postgresql_where=sa.text('provider_customer_id IS NOT NULL'),
    )

    # -------------------------------------------------------------------------
    # 2. Enable Row-Level Security & Restrict Untrusted Client Access
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE billing_customer_provisions ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_customer_provisions FROM anon, authenticated")


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Concurrency-Safe Downgrade Guard with ACCESS EXCLUSIVE Table Lock
    # -------------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                cnt_provisions INTEGER;
            BEGIN
                LOCK TABLE billing_customer_provisions IN ACCESS EXCLUSIVE MODE;
                SELECT count(*) INTO cnt_provisions FROM billing_customer_provisions;
                IF cnt_provisions > 0 THEN
                    RAISE EXCEPTION 
                        'Alembic Downgrade Aborted: % customer provisioning records exist. Dropping billing_customer_provisions is strictly forbidden to preserve financial audit integrity.',
                        cnt_provisions;
                END IF;
            END $$;
            """
        )
    )

    # -------------------------------------------------------------------------
    # 2. Disable Row-Level Security
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE billing_customer_provisions DISABLE ROW LEVEL SECURITY")

    # -------------------------------------------------------------------------
    # 3. Drop Scoped Partial Index and Table
    # -------------------------------------------------------------------------
    op.drop_index(
        'uq_billing_customer_provisions_provider_customer',
        table_name='billing_customer_provisions',
        postgresql_where=sa.text('provider_customer_id IS NOT NULL'),
    )
    op.drop_table('billing_customer_provisions')

    # -------------------------------------------------------------------------
    # 4. Drop Enum Type
    # -------------------------------------------------------------------------
    op.execute("DROP TYPE IF EXISTS customer_provision_status")
