"""Offline Unit Tests for Hardened Staging Migration Preflight Gate.

VERIFICATION OF PREFLIGHT SAFETY WITHOUT CONNECTING TO ANY REMOTE DATABASE.
Zero network calls. Zero mutations.
"""

from pathlib import Path
import sys
import pytest

@pytest.fixture
def windows_safe_proactor_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrowly scoped fixture to avoid loopback socket hangs on Windows in sandboxed test environments.

    Safely restores original BaseProactorEventLoop methods after each test completes.
    """
    if sys.platform == "win32":
        import socket
        from asyncio.proactor_events import BaseProactorEventLoop

        def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
            self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._internal_fds += 1

        monkeypatch.setattr(BaseProactorEventLoop, "_make_self_pipe", _safe_make_self_pipe)
        monkeypatch.setattr(BaseProactorEventLoop, "_loop_self_reading", lambda self, f=None: None)

# Ensure repo root and apps/api are on sys.path
repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "apps" / "api"))

from scripts.check_staging_migration_gate import (
    ALLOWLISTED_HOST_DIRECT,
    ALLOWLISTED_HOST_POOLER,
    EXPECTED_HEAD_REV,
    EXPECTED_REF,
    EXPECTED_ROLE,
    EXPECTED_START_REV,
    REQUIRED_START_TABLES,
    PreflightGateError,
    validate_alembic_graph,
    validate_alembic_revision_rows,
    validate_database_user_and_role,
    validate_environment,
    validate_required_tables_present,
    validate_staging_database_url,
)


# =============================================================================
# 1. Database URL Structural Parsing & Allowlisting (Zero Credential Leaks)
# =============================================================================


def test_validate_staging_database_url_valid_pooler() -> None:
    """Verifies valid Supabase pooler URL passes and credentials are sanitized in output."""
    raw = f"postgresql+asyncpg://postgres.{EXPECTED_REF}:secret_pw_123@{ALLOWLISTED_HOST_POOLER}:5432/postgres"
    result = validate_staging_database_url(raw)

    assert result["hostname"] == ALLOWLISTED_HOST_POOLER
    assert result["port"] == 5432
    assert result["database"] == "postgres"
    assert result["role"] == EXPECTED_ROLE
    assert result["project_ref"] == EXPECTED_REF
    assert "secret_pw_123" not in result["sanitized_url"]
    assert "***" in result["sanitized_url"]


def test_validate_staging_database_url_valid_direct() -> None:
    """Verifies valid direct Supabase connection URL passes."""
    raw = f"postgresql://postgres:secret_pw_456@{ALLOWLISTED_HOST_DIRECT}:5432/postgres"
    result = validate_staging_database_url(raw)

    assert result["hostname"] == ALLOWLISTED_HOST_DIRECT
    assert result["port"] == 5432
    assert result["database"] == "postgres"
    assert result["role"] == EXPECTED_ROLE
    assert result["project_ref"] == EXPECTED_REF
    assert "secret_pw_456" not in result["sanitized_url"]


@pytest.mark.parametrize(
    "invalid_url, expected_error_fragment",
    [
        (None, "missing or empty"),
        ("", "missing or empty"),
        ("mysql://postgres:pass@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres", "Invalid scheme"),
        ("http://localhost:5432/postgres", "Invalid scheme"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:invalid_port/postgres", "not a valid integer"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5433/postgres", "Port '5433' is not allowlisted"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/other_db", "Database path '/other_db' is not allowlisted"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres/extra", "Database path '/postgres/extra' is not allowlisted"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/", "Database path '/' is not allowlisted"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres?sslmode=require", "URL query parameters are not permitted"),
        (f"postgresql://postgres.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres#frag", "URL fragments are not permitted"),
        (f"postgresql://postgres.{EXPECTED_REF}@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "Database password is missing"),
        (f"postgresql://:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "username is missing"),
        (f"postgresql://postgres%2E{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "Percent-encoded characters"),
        (f"postgresql://postgres:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "must strictly follow '<role>.<project_ref>'"),
        (f"postgresql://postgres.{EXPECTED_REF}.extra:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "must strictly follow '<role>.<project_ref>'"),
        (f"postgresql://wrong_role.{EXPECTED_REF}:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "Database role mismatch"),
        (f"postgresql://postgres.wrong_ref_12345:pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres", "Project reference mismatch"),
        (f"postgresql://postgres:pass@evil-host.com:5432/postgres", "not in the staging allowlist"),
        (f"postgresql://postgres:pass@db.wrong_ref.supabase.co:5432/postgres", "not in the staging allowlist"),
    ],
)
def test_validate_staging_database_url_invalid(invalid_url: str | None, expected_error_fragment: str) -> None:
    """Verifies all structural deviations fail closed and never leak credentials."""
    secret_marker = "sensitive_password_test"
    test_url = invalid_url.replace("pass", secret_marker) if invalid_url and "pass" in invalid_url else invalid_url

    with pytest.raises(PreflightGateError) as exc_info:
        validate_staging_database_url(test_url)

    err_msg = str(exc_info.value)
    assert expected_error_fragment in err_msg
    assert secret_marker not in err_msg, "Security violation: secret password leaked in exception message!"


# =============================================================================
# 2. Environment Precedence & Exact Staging Assertion
# =============================================================================


def test_validate_environment_success() -> None:
    """Verifies that explicit staging environment in both ambient and settings passes."""
    validate_environment("staging", "staging")
    validate_environment("  STAGING  ", "staging")


@pytest.mark.parametrize(
    "ambient, settings_env, expected_msg",
    [
        (None, "staging", "Shell environment variable 'ENVIRONMENT' is not set"),
        ("", "staging", "Shell environment variable 'ENVIRONMENT' is not set"),
        ("development", "staging", "is 'development'; must be explicitly 'staging'"),
        ("production", "staging", "is 'production'; must be explicitly 'staging'"),
        ("test", "staging", "is 'test'; must be explicitly 'staging'"),
        ("staging", None, "Application settings.environment is empty"),
        ("staging", "", "Application settings.environment is empty"),
        ("staging", "development", "resolved to 'development'; must be explicitly 'staging'"),
        ("staging", "production", "resolved to 'production'; must be explicitly 'staging'"),
    ],
)
def test_validate_environment_failures(ambient: str | None, settings_env: str | None, expected_msg: str) -> None:
    """Verifies fail-closed behavior on non-staging environments."""
    with pytest.raises(PreflightGateError) as exc_info:
        validate_environment(ambient, settings_env)
    assert expected_msg in str(exc_info.value)


# =============================================================================
# 3. Alembic Revision Rows Validation
# =============================================================================


def test_validate_alembic_revision_rows_success() -> None:
    """Verifies exactly one row with starting revision passes."""
    rows = [(EXPECTED_START_REV,)]
    assert validate_alembic_revision_rows(rows) == EXPECTED_START_REV

    # Also verify list of list fixture
    assert validate_alembic_revision_rows([[EXPECTED_START_REV]]) == EXPECTED_START_REV


def test_validate_alembic_revision_rows_sqlalchemy_row_success() -> None:
    """Verifies that a real SQLAlchemy Row object is parsed correctly without remote DB."""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT '{EXPECTED_START_REV}' AS version_num")).one()
        assert validate_alembic_revision_rows([row]) == EXPECTED_START_REV


def test_validate_alembic_revision_rows_sqlalchemy_row_mismatch() -> None:
    """Verifies that a real SQLAlchemy Row with mismatched revision fails closed."""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT 'wrong_rev_c3d4' AS version_num")).one()
        with pytest.raises(PreflightGateError) as exc_info:
            validate_alembic_revision_rows([row])
        assert "Database revision mismatch: expected 'a1b2c3d4e5f6', found 'wrong_rev_c3d4'" in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_rows, expected_msg",
    [
        ([], "Table 'alembic_version' is empty"),
        ([("a1b2c3d4e5f6",), ("b2c3d4e5f6a1",)], "invalid row count: expected 1 row, found 2"),
        ([("c3d4e5f6a1b2",)], "Database revision mismatch: expected 'a1b2c3d4e5f6', found 'c3d4e5f6a1b2'"),
        ([("unknown_rev",)], "Database revision mismatch: expected 'a1b2c3d4e5f6', found 'unknown_rev'"),
    ],
)
def test_validate_alembic_revision_rows_failures(invalid_rows: list, expected_msg: str) -> None:
    """Verifies row count mismatches and revision divergences fail closed."""
    with pytest.raises(PreflightGateError) as exc_info:
        validate_alembic_revision_rows(invalid_rows)
    assert expected_msg in str(exc_info.value)


# =============================================================================
# 4. Database User and Role Privileges
# =============================================================================


def test_validate_database_user_and_role_success() -> None:
    """Verifies postgres user with rolbypassrls=True passes."""
    validate_database_user_and_role("postgres", True)


@pytest.mark.parametrize(
    "user, bypass, expected_msg",
    [
        (None, True, "Unable to determine current_user"),
        ("", True, "Unable to determine current_user"),
        ("anon", True, "Authenticated database user is 'anon'; expected 'postgres'"),
        ("authenticated", True, "Authenticated database user is 'authenticated'; expected 'postgres'"),
        ("postgres", False, "Database user 'postgres' has rolbypassrls=False"),
        ("postgres", None, "Database user 'postgres' has rolbypassrls=False"),
    ],
)
def test_validate_database_user_and_role_failures(user: str | None, bypass: bool | None, expected_msg: str) -> None:
    """Verifies untrusted roles or missing RLS bypass privileges fail closed."""
    with pytest.raises(PreflightGateError) as exc_info:
        validate_database_user_and_role(user, bypass)
    assert expected_msg in str(exc_info.value)


# =============================================================================
# 5. Baseline Required Tables
# =============================================================================


def test_validate_required_tables_present_success() -> None:
    """Verifies that all 6 required Phase 1 tables pass validation."""
    existing = REQUIRED_START_TABLES | {"extra_cache_table"}
    validate_required_tables_present(existing)


def test_validate_required_tables_present_missing() -> None:
    """Verifies that any missing baseline table fails closed."""
    existing = {"alembic_version", "organizations", "chatbots"}  # missing members, conversations, messages
    with pytest.raises(PreflightGateError) as exc_info:
        validate_required_tables_present(existing)
    err = str(exc_info.value)
    assert "Baseline tables missing" in err
    assert "conversations" in err
    assert "messages" in err
    assert "organization_members" in err


# =============================================================================
# 6. Offline Alembic Migration Graph Traversal
# =============================================================================


def test_validate_alembic_graph_offline_success() -> None:
    """Verifies real repository migration graph has exactly one head and two pending revisions."""
    api_dir = repo_root / "apps" / "api"
    ini_path = api_dir / "alembic.ini"
    script_loc = api_dir / "alembic"

    pending = validate_alembic_graph(ini_path, script_loc)
    assert pending == ["b2c3d4e5f6a1", "c3d4e5f6a1b2"]


def test_validate_alembic_graph_offline_missing_file() -> None:
    """Verifies non-existent ini path fails closed."""
    with pytest.raises(PreflightGateError) as exc_info:
        validate_alembic_graph("nonexistent.ini", "nonexistent")
    assert "does not exist" in str(exc_info.value)


def test_validate_alembic_graph_offline_head_mismatch() -> None:
    """Verifies assertion fails if expected head does not match repository head."""
    api_dir = repo_root / "apps" / "api"
    ini_path = api_dir / "alembic.ini"
    script_loc = api_dir / "alembic"

    with pytest.raises(PreflightGateError) as exc_info:
        validate_alembic_graph(ini_path, script_loc, expected_head="b2c3d4e5f6a1")
    assert "Alembic head revision mismatch" in str(exc_info.value)


def test_validate_alembic_graph_offline_pending_mismatch() -> None:
    """Verifies assertion fails if pending sequence differs."""
    api_dir = repo_root / "apps" / "api"
    ini_path = api_dir / "alembic.ini"
    script_loc = api_dir / "alembic"

    with pytest.raises(PreflightGateError) as exc_info:
        validate_alembic_graph(ini_path, script_loc, expected_pending=["wrong_rev"])
    assert "Pending revision sequence mismatch" in str(exc_info.value)


# =============================================================================
# 7. Alembic Version Table Row-Level Security (RLS) & Privilege Isolation
# =============================================================================


def test_offline_sql_generation_secures_alembic_version() -> None:
    """Verifies that Alembic offline SQL generation includes RLS and privilege revocation."""
    import io
    from contextlib import redirect_stdout
    from alembic.config import Config
    from alembic import command

    api_dir = repo_root / "apps" / "api"
    ini_path = api_dir / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))

    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(cfg, f"{EXPECTED_START_REV}:{EXPECTED_HEAD_REV}", sql=True)
    generated_sql = buf.getvalue()

    assert "ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY;" in generated_sql
    assert "REVOKE ALL ON alembic_version FROM anon, authenticated;" in generated_sql
    assert "GRANT ALL ON alembic_version" not in generated_sql


def test_env_py_contains_alembic_version_hardening() -> None:
    """Verifies that alembic/env.py hardens alembic_version in both offline and online modes."""
    env_py_path = repo_root / "apps" / "api" / "alembic" / "env.py"
    content = env_py_path.read_text(encoding="utf-8")

    rls_stmt = 'context.execute("ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY")'
    revoke_stmt = 'context.execute("REVOKE ALL ON alembic_version FROM anon, authenticated")'

    assert content.count(rls_stmt) >= 2, "RLS statement must be in both run_migrations_offline and do_run_migrations"
    assert content.count(revoke_stmt) >= 2, "Revoke statement must be in both run_migrations_offline and do_run_migrations"


def test_migration_c3d4e5f6a1b2_contains_alembic_version_hardening() -> None:
    """Verifies migration c3d4e5f6a1b2 explicitly hardens alembic_version during upgrade."""
    migration_path = (
        repo_root
        / "apps"
        / "api"
        / "alembic"
        / "versions"
        / "20261027_0000_c3d4e5f6a1b2_add_generation_task_attempts.py"
    )
    content = migration_path.read_text(encoding="utf-8")

    assert 'op.execute("ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY")' in content
    assert 'op.execute("REVOKE ALL ON alembic_version FROM anon, authenticated")' in content
    # Ensure downgrade does not accidentally disable RLS on the alembic_version table
    assert "DISABLE ROW LEVEL SECURITY" not in content.split("def downgrade()")[1] or "alembic_version" not in content.split("def downgrade()")[1]


# =============================================================================
# 8. Timeout Bounding, Error Sanitization & Immediate Stream Flushing
# =============================================================================


def test_check_remote_database_timeout(
    monkeypatch: pytest.MonkeyPatch,
    windows_safe_proactor_loop: None,
) -> None:
    """Verifies that database operations exceeding operation_timeout fail fast and cleanly."""
    import asyncio
    from scripts.check_staging_migration_gate import check_remote_database

    class HangingConnection:
        async def __aenter__(self):
            await asyncio.sleep(5.0)
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class HangingEngine:
        def connect(self):
            return HangingConnection()

        async def dispose(self):
            pass

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine",
        lambda *args, **kwargs: HangingEngine(),
    )

    valid_url = f"postgresql+asyncpg://postgres.{EXPECTED_REF}:dummy_pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres"

    with pytest.raises(PreflightGateError) as exc_info:
        asyncio.run(check_remote_database(valid_url, connect_timeout=0.05, operation_timeout=0.05))

    err = str(exc_info.value)
    assert "Database preflight timed out after" in err
    assert "dummy_pass" not in err


def test_check_remote_database_driver_error_sanitization(
    monkeypatch: pytest.MonkeyPatch,
    windows_safe_proactor_loop: None,
) -> None:
    """Verifies that driver exceptions do not leak connection strings or passwords."""
    import asyncio
    from scripts.check_staging_migration_gate import check_remote_database

    secret_in_error = "super_secret_raw_token_xyz"

    class FailingEngine:
        def connect(self):
            raise RuntimeError(f"FATAL: authentication failed password={secret_in_error}")

        async def dispose(self):
            pass

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine",
        lambda *args, **kwargs: FailingEngine(),
    )

    valid_url = f"postgresql+asyncpg://postgres.{EXPECTED_REF}:dummy_pass@{ALLOWLISTED_HOST_POOLER}:5432/postgres"

    with pytest.raises(PreflightGateError) as exc_info:
        asyncio.run(check_remote_database(valid_url))

    err = str(exc_info.value)
    assert "Database connection or query failed with RuntimeError" in err
    assert secret_in_error not in err
    assert "dummy_pass" not in err


def test_immediate_stream_flushing_and_logging(capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies that log() outputs to stdout and flush executes without error."""
    from scripts.check_staging_migration_gate import configure_immediate_flushing, log

    configure_immediate_flushing()
    log("Preflight step test message")
    captured = capsys.readouterr()
    assert "Preflight step test message\n" == captured.out
