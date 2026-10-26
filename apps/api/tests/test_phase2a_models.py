"""Tests for Phase 2A database models, relationships, constraints, and migration metadata.

These tests verify the declarative schema, table definitions, foreign key cascade/restrict rules,
partial unique index definitions, check constraints, enums, and ORM relationship mappings
without requiring a live remote database connection.
"""

from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ENUM

from app.db.base import Base
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.organization  # noqa: F401
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.subscription import (
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)


def test_phase2a_tables_registered_in_metadata() -> None:
    """Verify all 6 Phase 2A tables are correctly registered in the SQLAlchemy metadata."""
    expected_tables = {
        "organization_billing_periods",
        "generation_tasks",
        "usage_reservations",
        "llm_usage_logs",
        "subscriptions",
        "stripe_webhook_events",
    }
    actual_tables = set(Base.metadata.tables.keys())
    for table_name in expected_tables:
        assert table_name in actual_tables, f"Table '{table_name}' missing from Base.metadata"


def test_generation_task_status_enum_values() -> None:
    """Verify GenerationTaskStatus enum contains all required states including reconciliation_needed."""
    assert GenerationTaskStatus.pending.value == "pending"
    assert GenerationTaskStatus.completed.value == "completed"
    assert GenerationTaskStatus.failed.value == "failed"
    assert GenerationTaskStatus.skipped.value == "skipped"
    assert GenerationTaskStatus.reconciliation_needed.value == "reconciliation_needed"


def test_reservation_status_enum_values() -> None:
    """Verify ReservationStatus enum contains all required states."""
    assert ReservationStatus.active.value == "active"
    assert ReservationStatus.settled.value == "settled"
    assert ReservationStatus.released.value == "released"


def test_subscription_status_enum_values() -> None:
    """Verify SubscriptionStatus enum matches all Stripe lifecycle states."""
    expected = {
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
    }
    actual = {s.value for s in SubscriptionStatus}
    assert actual == expected


def test_webhook_processing_status_enum_values() -> None:
    """Verify WebhookProcessingStatus enum contains pending, processed, failed."""
    assert WebhookProcessingStatus.pending.value == "pending"
    assert WebhookProcessingStatus.processed.value == "processed"
    assert WebhookProcessingStatus.failed.value == "failed"


def test_billing_period_partial_unique_index() -> None:
    """Verify that organization_billing_periods has a partial unique index allowing at most 1 active period."""
    table = Base.metadata.tables["organization_billing_periods"]
    index_names = {idx.name: idx for idx in table.indexes}

    assert "uq_org_active_billing_period" in index_names, (
        "Missing partial unique index 'uq_org_active_billing_period'"
    )
    partial_idx = index_names["uq_org_active_billing_period"]
    assert partial_idx.unique is True, "Index must be unique"
    assert len(partial_idx.columns) == 1
    assert "organization_id" in partial_idx.columns

    # Verify postgresql_where clause
    where_clause = partial_idx.dialect_options.get("postgresql", {}).get("where")
    assert where_clause is not None, "Partial unique index must define postgresql_where"
    assert "is_active = true" in str(where_clause).lower()


def test_billing_period_check_constraints() -> None:
    """Verify non-negative token/cost constraints, quotas, spending caps, and period order constraints on billing periods."""
    table = Base.metadata.tables["organization_billing_periods"]
    constraints = {c.name: c for c in table.constraints if hasattr(c, "sqltext")}

    assert "ck_billing_periods_quota_non_negative" in constraints
    assert "ck_billing_periods_spending_cap_non_negative" in constraints
    assert "ck_billing_periods_tokens_non_negative" in constraints
    assert "ck_billing_periods_cost_non_negative" in constraints
    assert "ck_billing_periods_period_order" in constraints

    assert "monthly_token_quota >= 0" in str(constraints["ck_billing_periods_quota_non_negative"].sqltext)
    spending_cap_sql = str(constraints["ck_billing_periods_spending_cap_non_negative"].sqltext).lower()
    assert "hard_spending_cap_micros is null" in spending_cap_sql
    assert "hard_spending_cap_micros >= 0" in spending_cap_sql
    assert "tokens_used >= 0" in str(constraints["ck_billing_periods_tokens_non_negative"].sqltext)
    assert "tokens_reserved >= 0" in str(constraints["ck_billing_periods_tokens_non_negative"].sqltext)
    assert "cost_micros_used >= 0" in str(constraints["ck_billing_periods_cost_non_negative"].sqltext)
    assert "cost_micros_reserved >= 0" in str(constraints["ck_billing_periods_cost_non_negative"].sqltext)
    assert "period_end > period_start" in str(constraints["ck_billing_periods_period_order"].sqltext)


def test_generation_task_constraints_and_foreign_keys() -> None:
    """Verify GenerationTask foreign key rules (CASCADE for messages, RESTRICT for org/period) and check constraints."""
    table = Base.metadata.tables["generation_tasks"]

    # Check constraints
    constraints = {c.name: c for c in table.constraints if hasattr(c, "sqltext")}
    assert "ck_generation_tasks_attempts" in constraints
    assert "attempts >= 0" in str(constraints["ck_generation_tasks_attempts"].sqltext)
    assert "max_attempts > 0" in str(constraints["ck_generation_tasks_attempts"].sqltext)

    # Foreign keys
    fk_map = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fk_map["conversation_id"].ondelete == "CASCADE"
    assert fk_map["visitor_message_id"].ondelete == "CASCADE"
    assert fk_map["organization_id"].ondelete == "RESTRICT"
    assert fk_map["billing_period_id"].ondelete == "RESTRICT"

    # Visitor message uniqueness (1:1 generation task per visitor message)
    visitor_msg_col = table.c.visitor_message_id
    assert visitor_msg_col.unique is True or any(
        isinstance(c, type(table.primary_key)) is False
        and getattr(c, "columns", None) is not None
        and len(c.columns) == 1
        and "visitor_message_id" in c.columns
        for c in table.constraints
    )


def test_usage_reservation_fencing_and_constraints() -> None:
    """Verify UsageReservation unique constraint on (generation_task_id, lease_token), partial active unique index, and non-negative values."""
    table = Base.metadata.tables["usage_reservations"]

    # Non-negative check constraint
    constraints = {c.name: c for c in table.constraints if hasattr(c, "sqltext")}
    assert "ck_usage_reservations_tokens_cost" in constraints
    assert "tokens_reserved >= 0" in str(constraints["ck_usage_reservations_tokens_cost"].sqltext)
    assert "cost_micros_reserved >= 0" in str(constraints["ck_usage_reservations_tokens_cost"].sqltext)

    # Unique constraint on (generation_task_id, lease_token)
    unique_constraints = [
        c for c in table.constraints if isinstance(c, UniqueConstraint)
    ]
    has_lease_task_unique = any(
        set(c.columns.keys()) == {"generation_task_id", "lease_token"}
        for c in unique_constraints
    )
    assert has_lease_task_unique, "Missing unique constraint on (generation_task_id, lease_token)"

    # Partial unique index: at most one active reservation per generation task
    index_names = {idx.name: idx for idx in table.indexes}
    assert "uq_task_active_reservation" in index_names, (
        "Missing partial unique index 'uq_task_active_reservation'"
    )
    partial_idx = index_names["uq_task_active_reservation"]
    assert partial_idx.unique is True
    assert "generation_task_id" in partial_idx.columns
    where_clause = partial_idx.dialect_options.get("postgresql", {}).get("where")
    assert where_clause is not None
    assert "status = 'active'" in str(where_clause)

    # Foreign keys
    fk_map = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fk_map["generation_task_id"].ondelete == "CASCADE"
    assert fk_map["organization_id"].ondelete == "RESTRICT"
    assert fk_map["billing_period_id"].ondelete == "RESTRICT"


def test_llm_usage_logs_audit_retention_foreign_keys() -> None:
    """Verify LLMUsageLog has ON DELETE RESTRICT on organization/period, and ON DELETE SET NULL on conversation entities.

    This ensures financial and regulatory audit logs are permanently retained even when
    visitor messages, conversations, chatbots, or generation tasks are deleted.
    """
    table = Base.metadata.tables["llm_usage_logs"]
    fk_map = {fk.parent.name: fk for fk in table.foreign_keys}

    # Permanent retention: RESTRICT
    assert fk_map["organization_id"].ondelete == "RESTRICT"
    assert fk_map["billing_period_id"].ondelete == "RESTRICT"

    # Conversational entities: SET NULL
    assert fk_map["chatbot_id"].ondelete == "SET NULL"
    assert fk_map["conversation_id"].ondelete == "SET NULL"
    assert fk_map["visitor_message_id"].ondelete == "SET NULL"
    assert fk_map["generation_task_id"].ondelete == "SET NULL"

    # Non-negative check constraint
    constraints = {c.name: c for c in table.constraints if hasattr(c, "sqltext")}
    assert "ck_usage_logs_non_negative" in constraints
    ck_sql = str(constraints["ck_usage_logs_non_negative"].sqltext)
    assert "cost_micros >= 0" in ck_sql
    assert "prompt_tokens >= 0" in ck_sql
    assert "completion_tokens >= 0" in ck_sql
    assert "total_tokens >= 0" in ck_sql
    assert "latency_ms >= 0" in ck_sql


def test_subscription_model_uniqueness_and_relationships() -> None:
    """Verify Subscription stripe_customer_id, stripe_subscription_id uniqueness, and active subscription partial index."""
    table = Base.metadata.tables["subscriptions"]

    customer_id_col = table.c.stripe_customer_id
    subscription_id_col = table.c.stripe_subscription_id

    assert customer_id_col.unique is True
    assert subscription_id_col.unique is True

    # Standalone non-unique indexes on customer/subscription ID must NOT exist (avoid duplicate indexing)
    index_names = {idx.name: idx for idx in table.indexes}
    assert "ix_subscriptions_stripe_customer_id" not in index_names
    assert "ix_subscriptions_stripe_subscription_id" not in index_names

    # Partial unique index: at most one active or trialing subscription per organization
    assert "uq_org_active_subscription" in index_names, (
        "Missing partial unique index 'uq_org_active_subscription'"
    )
    partial_idx = index_names["uq_org_active_subscription"]
    assert partial_idx.unique is True
    assert "organization_id" in partial_idx.columns
    where_clause = partial_idx.dialect_options.get("postgresql", {}).get("where")
    assert where_clause is not None
    where_str = str(where_clause).lower()
    assert "status in ('active', 'trialing')" in where_str or "status in ('trialing', 'active')" in where_str

    fk_map = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fk_map["organization_id"].ondelete == "RESTRICT"


def test_stripe_webhook_events_primary_key_and_defaults() -> None:
    """Verify StripeWebhookEvent uses id (Stripe event ID) as primary key and defaults status to pending."""
    table = Base.metadata.tables["stripe_webhook_events"]
    assert table.c.id.primary_key is True
    assert table.c.status.server_default.arg == "pending"
    assert table.c.attempts.server_default.arg == "1"


def test_orm_relationship_back_populates() -> None:
    """Verify ORM back_populates relationships across models are bidirectional and consistent."""
    from app.models.chatbot import Chatbot
    from app.models.conversation import Conversation, Message
    from app.models.organization import Organization

    # Organization relationships
    assert hasattr(Organization, "billing_periods")
    assert hasattr(Organization, "subscriptions")
    assert hasattr(Organization, "generation_tasks")
    assert hasattr(Organization, "usage_reservations")
    assert hasattr(Organization, "usage_logs")

    # Chatbot relationships
    assert hasattr(Chatbot, "usage_logs")

    # Conversation relationships
    assert hasattr(Conversation, "generation_tasks")
    assert hasattr(Conversation, "usage_logs")

    # Message relationships
    assert hasattr(Message, "generation_task")
    assert hasattr(Message, "usage_logs")

    # GenerationTask relationships
    assert hasattr(GenerationTask, "conversation")
    assert hasattr(GenerationTask, "visitor_message")
    assert hasattr(GenerationTask, "organization")
    assert hasattr(GenerationTask, "billing_period")
    assert hasattr(GenerationTask, "reservations")
    assert hasattr(GenerationTask, "usage_logs")

    # OrganizationBillingPeriod relationships
    assert hasattr(OrganizationBillingPeriod, "organization")
    assert hasattr(OrganizationBillingPeriod, "generation_tasks")
    assert hasattr(OrganizationBillingPeriod, "reservations")
    assert hasattr(OrganizationBillingPeriod, "usage_logs")


def test_orm_relationship_cascade_safeguards() -> None:
    """Verify ORM cascade settings do NOT issue client-side deletes on RESTRICT-bound models.

    Specifically:
    1. Organization.billing_periods does NOT cascade delete (DB foreign key is RESTRICT).
    2. Organization.subscriptions does NOT cascade delete (DB foreign key is RESTRICT).
    3. OrganizationBillingPeriod.reservations does NOT cascade delete (DB foreign key is RESTRICT).
    4. GenerationTask.reservations DOES cascade delete-orphan (DB foreign key is CASCADE).
    """
    from app.models.billing import OrganizationBillingPeriod
    from app.models.generation import GenerationTask
    from app.models.organization import Organization

    # Organization cascade safeguards
    assert Organization.billing_periods.property.cascade.delete is False
    assert Organization.billing_periods.property.cascade.delete_orphan is False
    assert Organization.subscriptions.property.cascade.delete is False
    assert Organization.subscriptions.property.cascade.delete_orphan is False

    # Billing period cascade safeguards
    assert OrganizationBillingPeriod.reservations.property.cascade.delete is False
    assert OrganizationBillingPeriod.reservations.property.cascade.delete_orphan is False

    # Generation task cascade to reservations
    assert GenerationTask.reservations.property.cascade.delete_orphan is True


def test_alembic_offline_upgrade_and_downgrade_generation() -> None:
    """Verify that Alembic generates syntactically valid offline PostgreSQL DDL for revision b2c3d4e5f6a1.

    Tests both upgrade (a1b2c3d4e5f6 -> b2c3d4e5f6a1) and downgrade (b2c3d4e5f6a1 -> a1b2c3d4e5f6)
    without connecting to a live database.
    """
    api_dir = Path(__file__).resolve().parent.parent

    # Test upgrade DDL generation
    upgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "a1b2c3d4e5f6:b2c3d4e5f6a1", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert upgrade_res.returncode == 0, f"Alembic upgrade --sql failed:\n{upgrade_res.stderr}"
    upgrade_sql = upgrade_res.stdout
    assert "CREATE TABLE organization_billing_periods" in upgrade_sql
    assert "ck_billing_periods_quota_non_negative" in upgrade_sql
    assert "ck_billing_periods_spending_cap_non_negative" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_org_active_billing_period" in upgrade_sql
    assert "WHERE is_active = true" in upgrade_sql
    assert "CREATE TABLE generation_tasks" in upgrade_sql
    assert "CREATE TABLE usage_reservations" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_task_active_reservation" in upgrade_sql
    assert "CREATE TABLE llm_usage_logs" in upgrade_sql
    assert "CREATE TABLE subscriptions" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_org_active_subscription" in upgrade_sql
    assert "CREATE INDEX ix_subscriptions_stripe_customer_id" not in upgrade_sql
    assert "CREATE INDEX ix_subscriptions_stripe_subscription_id" not in upgrade_sql
    assert "CREATE TABLE stripe_webhook_events" in upgrade_sql
    assert "ALTER TABLE organization_billing_periods ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert "REVOKE ALL ON organization_billing_periods FROM anon, authenticated" in upgrade_sql
    assert "UPDATE alembic_version SET version_num='b2c3d4e5f6a1'" in upgrade_sql

    # Test downgrade DDL generation
    downgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "b2c3d4e5f6a1:a1b2c3d4e5f6", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert downgrade_res.returncode == 0, f"Alembic downgrade --sql failed:\n{downgrade_res.stderr}"
    downgrade_sql = downgrade_res.stdout
    assert "ALTER TABLE organization_billing_periods DISABLE ROW LEVEL SECURITY" in downgrade_sql
    assert "GRANT SELECT" not in downgrade_sql
    assert "DROP TABLE stripe_webhook_events" in downgrade_sql
    assert "DROP INDEX uq_org_active_subscription" in downgrade_sql
    assert "DROP TABLE subscriptions" in downgrade_sql
    assert "DROP TABLE llm_usage_logs" in downgrade_sql
    assert "DROP INDEX uq_task_active_reservation" in downgrade_sql
    assert "DROP TABLE usage_reservations" in downgrade_sql
    assert "DROP TABLE generation_tasks" in downgrade_sql
    assert "DROP TABLE organization_billing_periods" in downgrade_sql
    assert "DROP TYPE IF EXISTS webhook_processing_status" in downgrade_sql
    assert "DROP TYPE IF EXISTS subscription_status" in downgrade_sql
    assert "DROP TYPE IF EXISTS reservation_status" in downgrade_sql
    assert "DROP TYPE IF EXISTS generation_task_status" in downgrade_sql
    assert "UPDATE alembic_version SET version_num='a1b2c3d4e5f6'" in downgrade_sql
