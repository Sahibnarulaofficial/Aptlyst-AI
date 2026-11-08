"""add_webhook_event_lease_ownership

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b7
Create Date: 2026-11-08 12:00:00.000000

Adds explicit database-backed lease ownership fields and partial index to billing_webhook_events:
- lease_token: nullable UUID
- lease_expires_at: nullable timezone-aware timestamp
- locked_by: nullable VARCHAR(64)
- ix_billing_webhook_events_claim: partial composite index for worker claiming

Guarantees:
1. Backward Compatibility: All lease columns are nullable. Existing webhook records are preserved.
2. Index Optimization: Creates partial index on pending/failed events to support high-throughput SKIP LOCKED claiming.
3. RLS Preservation: Ensures Row-Level Security remains active and anon/authenticated access remains revoked.
4. Safe Downgrade: Downgrade guard checks for unexpired active leases before dropping columns and indexes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c1d2e3f4a5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Add Lease Ownership Columns to billing_webhook_events
    # -------------------------------------------------------------------------
    op.add_column(
        'billing_webhook_events',
        sa.Column('lease_token', sa.UUID(), nullable=True),
    )
    op.add_column(
        'billing_webhook_events',
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'billing_webhook_events',
        sa.Column('locked_by', sa.String(length=64), nullable=True),
    )

    # -------------------------------------------------------------------------
    # 2. Add Partial Composite Index for Worker Claiming
    # -------------------------------------------------------------------------
    # Supports efficient evaluation of:
    # WHERE provider = :provider AND signature_verified = true
    #   AND status IN ('pending', 'failed')
    #   AND (next_retry_at IS NULL OR next_retry_at <= now())
    #   AND (lease_expires_at IS NULL OR lease_expires_at <= now())
    # ORDER BY received_at ASC
    op.create_index(
        'ix_billing_webhook_events_claim',
        'billing_webhook_events',
        ['provider', 'status', 'next_retry_at', 'lease_expires_at', 'received_at'],
        postgresql_where=sa.text("status IN ('pending', 'failed') AND signature_verified = true"),
    )

    # -------------------------------------------------------------------------
    # 3. Maintain RLS and Privileges
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_webhook_events FROM anon, authenticated")


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Downgrade Guard: Ensure no unexpired active leases exist
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            active_lease_check = sa.text("""
                SELECT count(*)
                FROM billing_webhook_events
                WHERE lease_token IS NOT NULL
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > now()
            """)
            active_count = conn.execute(active_lease_check).scalar()
            if active_count and active_count > 0:
                raise RuntimeError(
                    f"Alembic Downgrade Aborted: {active_count} active unexpired webhook lease(s) exist. "
                    "Wait for active worker leases to expire or complete before downgrading."
                )

    # -------------------------------------------------------------------------
    # 2. Drop Partial Claim Index
    # -------------------------------------------------------------------------
    op.drop_index(
        'ix_billing_webhook_events_claim',
        table_name='billing_webhook_events',
        postgresql_where=sa.text("status IN ('pending', 'failed') AND signature_verified = true"),
    )

    # -------------------------------------------------------------------------
    # 3. Drop Lease Ownership Columns
    # -------------------------------------------------------------------------
    op.drop_column('billing_webhook_events', 'locked_by')
    op.drop_column('billing_webhook_events', 'lease_expires_at')
    op.drop_column('billing_webhook_events', 'lease_token')
