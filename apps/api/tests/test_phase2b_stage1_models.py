"""Tests for Phase 2B Stage 1 durable generation attempts database model and migration metadata.

These tests verify declarative schema definitions, table definitions, foreign keys,
constraints, partial unique indexes, enums, ORM relationships, cascade safeguards,
and offline Alembic DDL generation without requiring a live remote database connection.
"""

from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import BigInteger, CheckConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
from app.models.generation import GenerationTask
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt


def test_generation_task_attempts_table_registered_in_metadata() -> None:
    """Verify generation_task_attempts is correctly registered in SQLAlchemy Base.metadata."""
    assert "generation_task_attempts" in Base.metadata.tables


def test_generation_attempt_status_enum_values() -> None:
    """Verify GenerationAttemptStatus enum contains all required states."""
    expected = {
        "claimed",
        "dispatched",
        "completed",
        "failed",
        "ambiguous",
        "stale_received",
    }
    actual = {s.value for s in GenerationAttemptStatus}
    assert actual == expected


def test_generation_task_attempts_columns_and_types() -> None:
    """Verify column definitions and nullability on generation_task_attempts."""
    table = Base.metadata.tables["generation_task_attempts"]
    col_names = {c.name for c in table.columns}
    expected_cols = {
        "id",
        "generation_task_id",
        "attempt_number",
        "lease_token",
        "status",
        "provider",
        "model",
        "dispatched_at",
        "completed_at",
        "provider_request_id",
        "prompt_tokens",
        "completion_tokens",
        "cost_micros",
        "latency_ms",
        "response_content",
        "error_type",
        "error_message",
        "created_at",
        "updated_at",
    }
    assert expected_cols.issubset(col_names)

    # Primary key
    assert table.c.id.primary_key is True
    assert isinstance(table.c.id.type, UUID)

    # Foreign key nullability: nullable=True to allow ON DELETE SET NULL audit retention
    assert table.c.generation_task_id.nullable is True

    # Required fields
    assert table.c.attempt_number.nullable is False
    assert table.c.lease_token.nullable is False
    assert table.c.status.nullable is False
    assert table.c.provider.nullable is False
    assert table.c.model.nullable is False
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False

    # Optional fields
    assert table.c.dispatched_at.nullable is True
    assert table.c.completed_at.nullable is True
    assert table.c.provider_request_id.nullable is True
    assert table.c.prompt_tokens.nullable is True
    assert table.c.completion_tokens.nullable is True
    assert table.c.cost_micros.nullable is True
    assert table.c.latency_ms.nullable is True
    assert table.c.response_content.nullable is True
    assert table.c.error_type.nullable is True
    assert table.c.error_message.nullable is True

    # Column types
    assert isinstance(table.c.cost_micros.type, BigInteger)
    assert isinstance(table.c.attempt_number.type, Integer)
    assert isinstance(table.c.prompt_tokens.type, Integer)
    assert isinstance(table.c.completion_tokens.type, Integer)
    assert isinstance(table.c.latency_ms.type, Integer)
    assert isinstance(table.c.provider.type, String)
    assert table.c.provider.type.length == 32
    assert isinstance(table.c.model.type, String)
    assert table.c.model.type.length == 64
    assert isinstance(table.c.provider_request_id.type, String)
    assert table.c.provider_request_id.type.length == 255
    assert isinstance(table.c.error_type.type, String)
    assert table.c.error_type.type.length == 64
    assert isinstance(table.c.response_content.type, Text)
    assert isinstance(table.c.error_message.type, Text)


def test_generation_task_attempts_foreign_key_and_retention_policy() -> None:
    """Verify foreign key from generation_task_attempts to generation_tasks uses ON DELETE SET NULL.

    Preserves provider execution and reconciliation evidence when conversational or task
    records are deleted, matching the retention policy of LLMUsageLog.
    """
    table = Base.metadata.tables["generation_task_attempts"]
    fk_list = list(table.foreign_keys)
    assert len(fk_list) == 1
    fk = fk_list[0]
    assert fk.column.table.name == "generation_tasks"
    assert fk.column.name == "id"
    assert fk.ondelete == "SET NULL"


def test_generation_task_attempts_partial_unique_indexes() -> None:
    """Verify partial unique indexes: (task_id, attempt_number) and (task_id, lease_token).

    Uniqueness applies strictly per attached task (WHERE generation_task_id IS NOT NULL),
    preventing duplicate attempts on active tasks while allowing preserved audit records
    whose parent task was deleted (generation_task_id IS NULL) to coexist without collision.
    """
    table = Base.metadata.tables["generation_task_attempts"]
    index_names = {idx.name: idx for idx in table.indexes}

    assert "uq_attempt_task_number" in index_names, "Missing partial unique index 'uq_attempt_task_number'"
    uq_num = index_names["uq_attempt_task_number"]
    assert uq_num.unique is True
    assert [c.name for c in uq_num.columns] == ["generation_task_id", "attempt_number"]
    where_num = uq_num.dialect_options.get("postgresql", {}).get("where")
    assert where_num is not None
    assert "generation_task_id is not null" in str(where_num).lower()

    assert "uq_attempt_task_lease" in index_names, "Missing partial unique index 'uq_attempt_task_lease'"
    uq_lease = index_names["uq_attempt_task_lease"]
    assert uq_lease.unique is True
    assert [c.name for c in uq_lease.columns] == ["generation_task_id", "lease_token"]
    where_lease = uq_lease.dialect_options.get("postgresql", {}).get("where")
    assert where_lease is not None
    assert "generation_task_id is not null" in str(where_lease).lower()


def test_generation_task_attempts_check_constraints() -> None:
    """Verify check constraints for non-negative tokens/costs/latency and positive attempt number."""
    table = Base.metadata.tables["generation_task_attempts"]
    ck_map = {c.name: c for c in table.constraints if isinstance(c, CheckConstraint)}

    assert "ck_attempt_tokens_cost" in ck_map
    sqltext = str(ck_map["ck_attempt_tokens_cost"].sqltext)
    assert "prompt_tokens >= 0" in sqltext
    assert "completion_tokens >= 0" in sqltext
    assert "cost_micros >= 0" in sqltext
    assert "latency_ms >= 0" in sqltext

    assert "ck_attempt_number_positive" in ck_map
    num_sql = str(ck_map["ck_attempt_number_positive"].sqltext)
    assert "attempt_number > 0" in num_sql


def test_generation_task_attempts_indexes() -> None:
    """Verify all expected indexes on generation_task_attempts and confirm no duplicate indexes exist."""
    table = Base.metadata.tables["generation_task_attempts"]
    idx_map = {idx.name: [col.name for col in idx.columns] for idx in table.indexes}

    # Foreign key index for efficient joins and lookups
    assert "ix_generation_task_attempts_generation_task_id" in idx_map
    assert idx_map["ix_generation_task_attempts_generation_task_id"] == ["generation_task_id"]

    # Composite status filter index
    assert "ix_generation_attempts_task_status" in idx_map
    assert idx_map["ix_generation_attempts_task_status"] == ["generation_task_id", "status"]

    # Provider request ID lookup index
    assert "ix_generation_attempts_provider_req" in idx_map
    assert idx_map["ix_generation_attempts_provider_req"] == ["provider_request_id"]

    # Partial unique indexes
    assert "uq_attempt_task_number" in idx_map
    assert "uq_attempt_task_lease" in idx_map

    # Ensure no redundant duplicate indexes on provider_request_id
    provider_req_indexes = [name for name, cols in idx_map.items() if cols == ["provider_request_id"]]
    assert len(provider_req_indexes) == 1, f"Expected exactly 1 index on provider_request_id, found: {provider_req_indexes}"


def test_generation_task_relationship_wiring_and_cascade_safeguards() -> None:
    """Verify ORM relationships between GenerationTask and GenerationTaskAttempt.

    Crucially verifies that ORM delete cascades are disabled so that deleting a GenerationTask
    does NOT delete its attempts, preserving audit records in accordance with the retention policy.
    """
    # Parent to child relationship
    assert hasattr(GenerationTask, "attempts_list")
    assert GenerationTask.attempts_list.property.back_populates == "generation_task"
    # Delete cascade must be False to prevent erasing audit records
    assert GenerationTask.attempts_list.property.cascade.delete is False
    assert GenerationTask.attempts_list.property.cascade.delete_orphan is False

    # Child to parent relationship
    assert hasattr(GenerationTaskAttempt, "generation_task")
    assert GenerationTaskAttempt.generation_task.property.back_populates == "attempts_list"
    assert GenerationTaskAttempt.generation_task.property.cascade.delete is False
    assert GenerationTaskAttempt.generation_task.property.cascade.delete_orphan is False


def test_alembic_heads_single_head() -> None:
    """Verify Alembic reports exactly one head: c3d4e5f6a1b2."""
    api_dir = Path(__file__).resolve().parent.parent

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Alembic heads failed:\n{res.stderr}"
    lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"Expected exactly one head, got: {lines}"
    assert "c3d4e5f6a1b2" in lines[0]
    assert "(head)" in lines[0]


def test_alembic_offline_upgrade_and_downgrade_generation() -> None:
    """Verify Alembic generates valid offline PostgreSQL DDL for revision c3d4e5f6a1b2."""
    api_dir = Path(__file__).resolve().parent.parent

    # Test upgrade DDL generation
    upgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "b2c3d4e5f6a1:c3d4e5f6a1b2", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert upgrade_res.returncode == 0, f"Alembic upgrade --sql failed:\n{upgrade_res.stderr}"
    upgrade_sql = upgrade_res.stdout
    assert "CREATE TYPE generation_attempt_status AS ENUM" in upgrade_sql
    assert "CREATE TABLE generation_task_attempts" in upgrade_sql
    assert "generation_task_id UUID," in upgrade_sql  # nullable column
    assert "ON DELETE SET NULL" in upgrade_sql  # foreign key retention policy
    assert "ck_attempt_tokens_cost" in upgrade_sql
    assert "ck_attempt_number_positive" in upgrade_sql
    assert "CREATE INDEX ix_generation_task_attempts_generation_task_id" in upgrade_sql
    assert "CREATE INDEX ix_generation_attempts_task_status" in upgrade_sql
    assert "CREATE INDEX ix_generation_attempts_provider_req" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_attempt_task_number" in upgrade_sql
    assert "WHERE generation_task_id IS NOT NULL" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_attempt_task_lease" in upgrade_sql
    assert "ALTER TABLE generation_task_attempts ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert "REVOKE ALL ON generation_task_attempts FROM anon, authenticated" in upgrade_sql
    assert "UPDATE alembic_version SET version_num='c3d4e5f6a1b2'" in upgrade_sql

    # Test downgrade DDL generation
    downgrade_res = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "c3d4e5f6a1b2:b2c3d4e5f6a1", "--sql"],
        cwd=str(api_dir),
        capture_output=True,
        text=True,
    )
    assert downgrade_res.returncode == 0, f"Alembic downgrade --sql failed:\n{downgrade_res.stderr}"
    downgrade_sql = downgrade_res.stdout
    assert "ALTER TABLE generation_task_attempts DISABLE ROW LEVEL SECURITY" in downgrade_sql
    assert "DROP INDEX uq_attempt_task_lease" in downgrade_sql
    assert "DROP INDEX uq_attempt_task_number" in downgrade_sql
    assert "DROP INDEX ix_generation_attempts_provider_req" in downgrade_sql
    assert "DROP INDEX ix_generation_attempts_task_status" in downgrade_sql
    assert "DROP INDEX ix_generation_task_attempts_generation_task_id" in downgrade_sql
    assert "DROP TABLE generation_task_attempts" in downgrade_sql
    assert "DROP TYPE IF EXISTS generation_attempt_status" in downgrade_sql
    assert "UPDATE alembic_version SET version_num='b2c3d4e5f6a1'" in downgrade_sql
