"""add_phase2a_billing_and_generation

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-10-26 19:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a1'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Organization Billing Periods
    # -------------------------------------------------------------------------
    op.create_table(
        'organization_billing_periods',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('plan_tier', sa.String(length=32), server_default='free', nullable=False),
        sa.Column('monthly_token_quota', sa.BigInteger(), nullable=False),
        sa.Column('tokens_used', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('tokens_reserved', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('hard_spending_cap_micros', sa.BigInteger(), nullable=True),
        sa.Column('cost_micros_used', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('cost_micros_reserved', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('is_blocked', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('blocked_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('monthly_token_quota >= 0', name='ck_billing_periods_quota_non_negative'),
        sa.CheckConstraint('hard_spending_cap_micros IS NULL OR hard_spending_cap_micros >= 0', name='ck_billing_periods_spending_cap_non_negative'),
        sa.CheckConstraint('tokens_used >= 0 AND tokens_reserved >= 0', name='ck_billing_periods_tokens_non_negative'),
        sa.CheckConstraint('cost_micros_used >= 0 AND cost_micros_reserved >= 0', name='ck_billing_periods_cost_non_negative'),
        sa.CheckConstraint('period_end > period_start', name='ck_billing_periods_period_order'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_billing_periods_organization_id', 'organization_billing_periods', ['organization_id'])
    op.create_index(
        'ix_billing_periods_org_dates',
        'organization_billing_periods',
        ['organization_id', 'period_start', 'period_end'],
    )
    # Partial unique index: at most ONE active period per organization
    op.create_index(
        'uq_org_active_billing_period',
        'organization_billing_periods',
        ['organization_id'],
        unique=True,
        postgresql_where=sa.text('is_active = true'),
    )

    # -------------------------------------------------------------------------
    # 2. Generation Tasks
    # -------------------------------------------------------------------------
    op.create_table(
        'generation_tasks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('visitor_message_id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('billing_period_id', sa.UUID(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'completed', 'failed', 'skipped', 'reconciliation_needed', name='generation_task_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('lease_token', sa.UUID(), nullable=False),
        sa.Column('lease_version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
        sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('attempts >= 0 AND max_attempts > 0', name='ck_generation_tasks_attempts'),
        sa.ForeignKeyConstraint(['billing_period_id'], ['organization_billing_periods.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['visitor_message_id'], ['messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('visitor_message_id', name='uq_generation_tasks_visitor_message_id'),
    )
    op.create_index('ix_generation_tasks_conversation_id', 'generation_tasks', ['conversation_id'])
    op.create_index('ix_generation_tasks_organization_id', 'generation_tasks', ['organization_id'])
    op.create_index('ix_generation_tasks_billing_period_id', 'generation_tasks', ['billing_period_id'])
    op.create_index('ix_generation_tasks_conv_status', 'generation_tasks', ['conversation_id', 'status'])
    op.create_index('ix_generation_tasks_lease_recovery', 'generation_tasks', ['status', 'lease_expires_at'])
    op.create_index('ix_generation_tasks_org_period', 'generation_tasks', ['organization_id', 'billing_period_id'])

    # -------------------------------------------------------------------------
    # 3. Usage Reservations
    # -------------------------------------------------------------------------
    op.create_table(
        'usage_reservations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('generation_task_id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('billing_period_id', sa.UUID(), nullable=False),
        sa.Column('lease_token', sa.UUID(), nullable=False),
        sa.Column('tokens_reserved', sa.Integer(), nullable=False),
        sa.Column('cost_micros_reserved', sa.BigInteger(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('active', 'settled', 'released', name='reservation_status'),
            server_default='active',
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('tokens_reserved >= 0 AND cost_micros_reserved >= 0', name='ck_usage_reservations_tokens_cost'),
        sa.ForeignKeyConstraint(['billing_period_id'], ['organization_billing_periods.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['generation_task_id'], ['generation_tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('generation_task_id', 'lease_token', name='uq_reservation_task_lease'),
    )
    op.create_index('ix_usage_reservations_generation_task_id', 'usage_reservations', ['generation_task_id'])
    op.create_index('ix_usage_reservations_organization_id', 'usage_reservations', ['organization_id'])
    op.create_index('ix_usage_reservations_billing_period_id', 'usage_reservations', ['billing_period_id'])
    op.create_index('ix_usage_reservations_org_status', 'usage_reservations', ['organization_id', 'status'])
    op.create_index('ix_usage_reservations_period_status', 'usage_reservations', ['billing_period_id', 'status'])
    op.create_index(
        'uq_task_active_reservation',
        'usage_reservations',
        ['generation_task_id'],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # -------------------------------------------------------------------------
    # 4. LLM Usage Logs
    # -------------------------------------------------------------------------
    op.create_table(
        'llm_usage_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('billing_period_id', sa.UUID(), nullable=False),
        sa.Column('chatbot_id', sa.UUID(), nullable=True),
        sa.Column('conversation_id', sa.UUID(), nullable=True),
        sa.Column('visitor_message_id', sa.UUID(), nullable=True),
        sa.Column('generation_task_id', sa.UUID(), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('pricing_version', sa.String(length=32), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False),
        sa.Column('total_tokens', sa.Integer(), nullable=False),
        sa.Column('cost_micros', sa.BigInteger(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            'prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0 AND cost_micros >= 0 AND latency_ms >= 0',
            name='ck_usage_logs_non_negative',
        ),
        sa.ForeignKeyConstraint(['billing_period_id'], ['organization_billing_periods.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['generation_task_id'], ['generation_tasks.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['visitor_message_id'], ['messages.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_llm_usage_logs_organization_id', 'llm_usage_logs', ['organization_id'])
    op.create_index('ix_llm_usage_logs_billing_period_id', 'llm_usage_logs', ['billing_period_id'])
    op.create_index('ix_llm_usage_logs_chatbot_id', 'llm_usage_logs', ['chatbot_id'])
    op.create_index('ix_llm_usage_logs_conversation_id', 'llm_usage_logs', ['conversation_id'])
    op.create_index('ix_llm_usage_logs_visitor_message_id', 'llm_usage_logs', ['visitor_message_id'])
    op.create_index('ix_llm_usage_logs_generation_task_id', 'llm_usage_logs', ['generation_task_id'])
    op.create_index('ix_llm_usage_logs_org_created', 'llm_usage_logs', ['organization_id', 'created_at'])
    op.create_index('ix_llm_usage_logs_period_created', 'llm_usage_logs', ['billing_period_id', 'created_at'])

    # -------------------------------------------------------------------------
    # 5. Subscriptions
    # -------------------------------------------------------------------------
    op.create_table(
        'subscriptions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=255), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('stripe_price_id', sa.String(length=255), nullable=True),
        sa.Column(
            'status',
            sa.Enum(
                'incomplete',
                'incomplete_expired',
                'trialing',
                'active',
                'past_due',
                'canceled',
                'unpaid',
                'paused',
                name='subscription_status',
            ),
            server_default='incomplete',
            nullable=False,
        ),
        sa.Column('plan_tier', sa.String(length=32), server_default='free', nullable=False),
        sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('canceled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_stripe_event_created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_customer_id', name='uq_subscriptions_stripe_customer_id'),
        sa.UniqueConstraint('stripe_subscription_id', name='uq_subscriptions_stripe_subscription_id'),
    )
    op.create_index('ix_subscriptions_organization_id', 'subscriptions', ['organization_id'])
    op.create_index('ix_subscriptions_org_status', 'subscriptions', ['organization_id', 'status'])
    op.create_index(
        'uq_org_active_subscription',
        'subscriptions',
        ['organization_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'trialing')"),
    )

    # -------------------------------------------------------------------------
    # 6. Stripe Webhook Events
    # -------------------------------------------------------------------------
    op.create_table(
        'stripe_webhook_events',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=128), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'processed', 'failed', name='webhook_processing_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stripe_webhook_events_event_type', 'stripe_webhook_events', ['event_type'])
    op.create_index('ix_stripe_webhook_events_status', 'stripe_webhook_events', ['status', 'created_at'])

    # -------------------------------------------------------------------------
    # 7. Enable Row-Level Security
    # -------------------------------------------------------------------------
    op.execute("ALTER TABLE organization_billing_periods ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE generation_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_usage_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stripe_webhook_events ENABLE ROW LEVEL SECURITY")

    # -------------------------------------------------------------------------
    # 8. Revoke direct table privileges from untrusted PostgREST client roles
    # -------------------------------------------------------------------------
    op.execute("REVOKE ALL ON organization_billing_periods FROM anon, authenticated")
    op.execute("REVOKE ALL ON generation_tasks FROM anon, authenticated")
    op.execute("REVOKE ALL ON usage_reservations FROM anon, authenticated")
    op.execute("REVOKE ALL ON llm_usage_logs FROM anon, authenticated")
    op.execute("REVOKE ALL ON subscriptions FROM anon, authenticated")
    op.execute("REVOKE ALL ON stripe_webhook_events FROM anon, authenticated")


def downgrade() -> None:
    # 1. Disable Row-Level Security
    op.execute("ALTER TABLE stripe_webhook_events DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE subscriptions DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_usage_logs DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_reservations DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE generation_tasks DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_billing_periods DISABLE ROW LEVEL SECURITY")

    # 2. Drop tables and indexes in reverse dependency order
    op.drop_index('ix_stripe_webhook_events_status', table_name='stripe_webhook_events')
    op.drop_index('ix_stripe_webhook_events_event_type', table_name='stripe_webhook_events')
    op.drop_table('stripe_webhook_events')

    op.drop_index('uq_org_active_subscription', table_name='subscriptions', postgresql_where=sa.text("status IN ('active', 'trialing')"))
    op.drop_index('ix_subscriptions_org_status', table_name='subscriptions')
    op.drop_index('ix_subscriptions_organization_id', table_name='subscriptions')
    op.drop_table('subscriptions')

    op.drop_index('ix_llm_usage_logs_period_created', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_org_created', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_generation_task_id', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_visitor_message_id', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_conversation_id', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_chatbot_id', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_billing_period_id', table_name='llm_usage_logs')
    op.drop_index('ix_llm_usage_logs_organization_id', table_name='llm_usage_logs')
    op.drop_table('llm_usage_logs')

    op.drop_index('uq_task_active_reservation', table_name='usage_reservations', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('ix_usage_reservations_period_status', table_name='usage_reservations')
    op.drop_index('ix_usage_reservations_org_status', table_name='usage_reservations')
    op.drop_index('ix_usage_reservations_billing_period_id', table_name='usage_reservations')
    op.drop_index('ix_usage_reservations_organization_id', table_name='usage_reservations')
    op.drop_index('ix_usage_reservations_generation_task_id', table_name='usage_reservations')
    op.drop_table('usage_reservations')

    op.drop_index('ix_generation_tasks_org_period', table_name='generation_tasks')
    op.drop_index('ix_generation_tasks_lease_recovery', table_name='generation_tasks')
    op.drop_index('ix_generation_tasks_conv_status', table_name='generation_tasks')
    op.drop_index('ix_generation_tasks_billing_period_id', table_name='generation_tasks')
    op.drop_index('ix_generation_tasks_organization_id', table_name='generation_tasks')
    op.drop_index('ix_generation_tasks_conversation_id', table_name='generation_tasks')
    op.drop_table('generation_tasks')

    op.drop_index('uq_org_active_billing_period', table_name='organization_billing_periods', postgresql_where=sa.text('is_active = true'))
    op.drop_index('ix_billing_periods_org_dates', table_name='organization_billing_periods')
    op.drop_index('ix_billing_periods_organization_id', table_name='organization_billing_periods')
    op.drop_table('organization_billing_periods')

    # 3. Drop enums
    op.execute("DROP TYPE IF EXISTS webhook_processing_status")
    op.execute("DROP TYPE IF EXISTS subscription_status")
    op.execute("DROP TYPE IF EXISTS reservation_status")
    op.execute("DROP TYPE IF EXISTS generation_task_status")
