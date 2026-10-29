"""add_generation_task_attempts

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a1b2'
down_revision: Union[str, None] = 'b2c3d4e5f6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Generation Task Attempts
    # -------------------------------------------------------------------------
    op.create_table(
        'generation_task_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('generation_task_id', sa.UUID(), nullable=True),
        sa.Column('attempt_number', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.UUID(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'claimed',
                'dispatched',
                'completed',
                'failed',
                'ambiguous',
                'stale_received',
                name='generation_attempt_status',
            ),
            server_default='claimed',
            nullable=False,
        ),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider_request_id', sa.String(length=255), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_micros', sa.BigInteger(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('response_content', sa.Text(), nullable=True),
        sa.Column('error_type', sa.String(length=64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            '(prompt_tokens IS NULL OR prompt_tokens >= 0) AND '
            '(completion_tokens IS NULL OR completion_tokens >= 0) AND '
            '(cost_micros IS NULL OR cost_micros >= 0) AND '
            '(latency_ms IS NULL OR latency_ms >= 0)',
            name='ck_attempt_tokens_cost',
        ),
        sa.CheckConstraint('attempt_number > 0', name='ck_attempt_number_positive'),
        sa.ForeignKeyConstraint(['generation_task_id'], ['generation_tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_generation_task_attempts_generation_task_id',
        'generation_task_attempts',
        ['generation_task_id'],
    )
    op.create_index(
        'ix_generation_attempts_task_status',
        'generation_task_attempts',
        ['generation_task_id', 'status'],
    )
    op.create_index(
        'ix_generation_attempts_provider_req',
        'generation_task_attempts',
        ['provider_request_id'],
    )
    # Partial unique indexes: enforce uniqueness per active generation task
    op.create_index(
        'uq_attempt_task_number',
        'generation_task_attempts',
        ['generation_task_id', 'attempt_number'],
        unique=True,
        postgresql_where=sa.text('generation_task_id IS NOT NULL'),
    )
    op.create_index(
        'uq_attempt_task_lease',
        'generation_task_attempts',
        ['generation_task_id', 'lease_token'],
        unique=True,
        postgresql_where=sa.text('generation_task_id IS NOT NULL'),
    )

    # -------------------------------------------------------------------------
    # 2. Enable Row-Level Security
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE generation_task_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY")

    # -------------------------------------------------------------------------
    # 3. Revoke direct table privileges from untrusted PostgREST client roles
    # -------------------------------------------------------------------------
    op.execute("REVOKE ALL ON generation_task_attempts FROM anon, authenticated")
    op.execute("REVOKE ALL ON alembic_version FROM anon, authenticated")


def downgrade() -> None:
    # 1. Disable Row-Level Security
    op.execute("ALTER TABLE generation_task_attempts DISABLE ROW LEVEL SECURITY")

    # 2. Drop indexes in reverse dependency order
    op.drop_index(
        'uq_attempt_task_lease',
        table_name='generation_task_attempts',
        postgresql_where=sa.text('generation_task_id IS NOT NULL'),
    )
    op.drop_index(
        'uq_attempt_task_number',
        table_name='generation_task_attempts',
        postgresql_where=sa.text('generation_task_id IS NOT NULL'),
    )
    op.drop_index('ix_generation_attempts_provider_req', table_name='generation_task_attempts')
    op.drop_index('ix_generation_attempts_task_status', table_name='generation_task_attempts')
    op.drop_index('ix_generation_task_attempts_generation_task_id', table_name='generation_task_attempts')

    # 3. Drop table
    op.drop_table('generation_task_attempts')

    # 4. Drop enum type
    op.execute("DROP TYPE IF EXISTS generation_attempt_status")
