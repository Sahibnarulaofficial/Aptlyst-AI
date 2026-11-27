"""add_billing_worker_heartbeats

Revision ID: a4b5c6d7e8f3
Revises: f3a4b5c6d7e2
Create Date: 2026-11-25 01:00:00.000000

Stage 2J.4 reversible schema migration adding durable worker heartbeat tracking:

1. billing_worker_heartbeats table:
   - Tracks worker_id, instance_token, worker_type, deployment_environment,
     environment, merchant_account_id, status, pid, hostname, started_at,
     last_heartbeat_at, last_processed_at, last_error_at, last_error_message,
     processed_count, error_count, created_at, updated_at.
2. Indexes:
   - ix_billing_worker_heartbeats_type_env_hb on (worker_type, environment, last_heartbeat_at)
   - ix_billing_worker_heartbeats_status_hb on (status, last_heartbeat_at)
3. RLS & Privileges:
   - Enables Row-Level Security and revokes untrusted direct role privileges (anon, authenticated).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f3"
down_revision: Union[str, None] = "f3a4b5c6d7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "billing_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("instance_token", sa.UUID(), nullable=False),
        sa.Column("worker_type", sa.String(length=64), nullable=False),
        sa.Column("deployment_environment", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=True),
        sa.Column("merchant_account_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="starting", nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("processed_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_billing_worker_heartbeats_type_env_hb",
        "billing_worker_heartbeats",
        ["worker_type", "environment", "last_heartbeat_at"],
        unique=False,
    )
    op.create_index(
        "ix_billing_worker_heartbeats_status_hb",
        "billing_worker_heartbeats",
        ["status", "last_heartbeat_at"],
        unique=False,
    )

    op.execute("ALTER TABLE billing_worker_heartbeats ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON billing_worker_heartbeats FROM anon, authenticated")


def downgrade() -> None:
    op.drop_index("ix_billing_worker_heartbeats_status_hb", table_name="billing_worker_heartbeats")
    op.drop_index("ix_billing_worker_heartbeats_type_env_hb", table_name="billing_worker_heartbeats")
    op.drop_table("billing_worker_heartbeats")
