"""Hardened Staging Migration Preflight Gate.

Strictly non-mutating and read-only.
Validates the target environment, exact database connection parameters,
Alembic migration graph, and remote database schema baseline before allowing
a staging migration to proceed.

Zero DDL/DML mutations. Zero credential leaks.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Any
import urllib.parse

from alembic.config import Config
from alembic.script import ScriptDirectory

try:
    from sqlalchemy.engine.row import Row as SqlAlchemyRow
except ImportError:
    SqlAlchemyRow = None  # type: ignore[assignment, misc]

# -----------------------------------------------------------------------------
# Allowlisted Staging Constants
# -----------------------------------------------------------------------------
EXPECTED_REF = "ewrpmunvqdtkucyojohe"
EXPECTED_ROLE = "postgres"
EXPECTED_START_REV = "a1b2c3d4e5f6"
EXPECTED_HEAD_REV = "c3d4e5f6a1b2"
EXPECTED_PENDING_CHAIN = ["b2c3d4e5f6a1", "c3d4e5f6a1b2"]

ALLOWLISTED_HOST_POOLER = "aws-0-ap-southeast-2.pooler.supabase.com"
ALLOWLISTED_HOST_DIRECT = f"db.{EXPECTED_REF}.supabase.co"
ALLOWLISTED_PORT = 5432
ALLOWLISTED_DATABASE = "postgres"
ALLOWLISTED_SCHEMES = ("postgresql", "postgresql+asyncpg", "postgres")

REQUIRED_START_TABLES = {
    "alembic_version",
    "organizations",
    "organization_members",
    "chatbots",
    "conversations",
    "messages",
}

CONNECT_TIMEOUT_SECONDS: float = 10.0
OPERATION_TIMEOUT_SECONDS: float = 20.0


def configure_immediate_flushing() -> None:
    """Ensure standard output and error streams flush immediately."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass


def log(msg: str = "") -> None:
    """Print message to stdout with immediate flushing."""
    print(msg, flush=True)


class PreflightGateError(Exception):
    """Raised when any preflight safety assertion fails."""


def fail(msg: str) -> None:
    """Print sanitized failure message and exit with code 1."""
    sys.stderr.write(f"\n[PREFLIGHT GATE ERROR] {msg}\n\n")
    sys.stderr.flush()
    sys.exit(1)


def validate_environment(ambient_env: str | None, settings_env: str | None) -> None:
    """Verify that both ambient ENVIRONMENT and settings.environment are explicitly 'staging'.

    Fails closed if either is missing, unset, or set to 'development', 'production', or 'test'.
    """
    clean_ambient = (ambient_env or "").strip().lower()
    clean_settings = (settings_env or "").strip().lower()

    if not clean_ambient:
        raise PreflightGateError(
            "Shell environment variable 'ENVIRONMENT' is not set; must be explicitly 'staging'."
        )
    if clean_ambient != "staging":
        raise PreflightGateError(
            f"Shell environment variable 'ENVIRONMENT' is '{clean_ambient}'; must be explicitly 'staging'."
        )

    if not clean_settings:
        raise PreflightGateError(
            "Application settings.environment is empty; must be explicitly 'staging'."
        )
    if clean_settings != "staging":
        raise PreflightGateError(
            f"Application settings.environment resolved to '{clean_settings}'; must be explicitly 'staging'."
        )


def validate_staging_database_url(
    url_str: str | None,
    expected_ref: str = EXPECTED_REF,
    expected_role: str = EXPECTED_ROLE,
) -> dict[str, Any]:
    """Structurally parse and validate database URL against exact staging allowlist.

    Guarantees zero credential leakage in error messages.
    """
    if not url_str or not isinstance(url_str, str):
        raise PreflightGateError("Database URL is missing or empty.")

    try:
        u = urllib.parse.urlparse(url_str)
    except Exception:
        raise PreflightGateError("Database URL is structurally malformed.") from None

    # 1. Scheme Check
    if u.scheme not in ALLOWLISTED_SCHEMES:
        raise PreflightGateError(
            f"Invalid scheme '{u.scheme}'; must be one of {ALLOWLISTED_SCHEMES}."
        )

    # 2. Query and Fragment Rejection
    if u.query:
        raise PreflightGateError(
            "URL query parameters are not permitted; connection options must come from application settings."
        )
    if u.fragment:
        raise PreflightGateError("URL fragments are not permitted.")

    # 3. Port Check
    try:
        port = u.port
    except ValueError:
        raise PreflightGateError("Database port is not a valid integer.") from None

    if port != ALLOWLISTED_PORT:
        raise PreflightGateError(
            f"Port '{port}' is not allowlisted; staging database must connect on port {ALLOWLISTED_PORT}."
        )

    # 4. Database Name & Path Check
    raw_path = u.path or ""
    path_segments = [seg for seg in raw_path.strip("/").split("/") if seg]
    if path_segments != [ALLOWLISTED_DATABASE]:
        raise PreflightGateError(
            f"Database path '{raw_path}' is not allowlisted; staging database must be '/{ALLOWLISTED_DATABASE}'."
        )
    db_name = ALLOWLISTED_DATABASE

    # 5. Hostname Check
    hostname = (u.hostname or "").strip().lower()
    if not hostname:
        raise PreflightGateError("Database hostname is missing.")

    # 6. Credentials (Username and Password) Check
    raw_user = u.username
    if not raw_user:
        raise PreflightGateError("Database connection username is missing.")

    if not u.password:
        raise PreflightGateError("Database password is missing from connection URL.")

    if "%" in raw_user:
        raise PreflightGateError("Percent-encoded characters in username are not permitted.")

    # 6. Target Project Reference & Role Allowlisting
    discovered_ref: str | None = None
    discovered_role: str | None = None

    if hostname == ALLOWLISTED_HOST_POOLER:
        parts = raw_user.split(".")
        if len(parts) != 2:
            raise PreflightGateError(
                "Pooler username must strictly follow '<role>.<project_ref>' format."
            )
        discovered_role, discovered_ref = parts[0], parts[1]
        if discovered_role != expected_role:
            raise PreflightGateError(
                f"Database role mismatch: got '{discovered_role}', expected '{expected_role}'."
            )
        if discovered_ref != expected_ref:
            raise PreflightGateError(
                f"Project reference mismatch: got '{discovered_ref}', expected '{expected_ref}'."
            )
    elif hostname == ALLOWLISTED_HOST_DIRECT:
        discovered_role = raw_user
        discovered_ref = expected_ref
        if discovered_role != expected_role:
            raise PreflightGateError(
                f"Database role mismatch: got '{discovered_role}', expected '{expected_role}'."
            )
    else:
        raise PreflightGateError(
            f"Database host '{hostname}' is not in the staging allowlist "
            f"('{ALLOWLISTED_HOST_POOLER}', '{ALLOWLISTED_HOST_DIRECT}')."
        )

    return {
        "hostname": hostname,
        "port": port,
        "database": db_name,
        "role": discovered_role,
        "project_ref": discovered_ref,
        "sanitized_url": f"{u.scheme}://{raw_user}:***@{hostname}:{port}/{db_name}",
    }


def validate_alembic_graph(
    alembic_ini_path: str | Path,
    script_location: str | Path,
    expected_start: str = EXPECTED_START_REV,
    expected_head: str = EXPECTED_HEAD_REV,
    expected_pending: list[str] = EXPECTED_PENDING_CHAIN,
) -> list[str]:
    """Verify offline that the migration graph has exactly one head and the expected pending chain."""
    ini_path = Path(alembic_ini_path)
    if not ini_path.exists():
        raise PreflightGateError(f"Alembic configuration file '{ini_path}' does not exist.")

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(script_location))
    script = ScriptDirectory.from_config(cfg)

    heads = script.get_heads()
    if len(heads) != 1:
        raise PreflightGateError(
            f"Alembic graph must have exactly 1 head revision, found {len(heads)}: {heads}"
        )
    if heads[0] != expected_head:
        raise PreflightGateError(
            f"Alembic head revision mismatch: got '{heads[0]}', expected '{expected_head}'."
        )

    try:
        revisions_down = list(script.iterate_revisions(expected_head, expected_start))
    except Exception as e:
        raise PreflightGateError(
            f"Failed to traverse migration graph from '{expected_head}' down to '{expected_start}': {e}"
        ) from None

    pending = [r.revision for r in reversed(revisions_down)]
    if pending != expected_pending:
        raise PreflightGateError(
            f"Pending revision sequence mismatch: got {pending}, expected {expected_pending}."
        )

    return pending


def validate_alembic_revision_rows(
    rows: list[Any],
    expected_rev: str = EXPECTED_START_REV,
) -> str:
    """Verify that alembic_version contains exactly one row matching the expected starting revision."""
    if not rows:
        raise PreflightGateError("Table 'alembic_version' is empty (0 rows found).")
    if len(rows) != 1:
        raise PreflightGateError(
            f"Table 'alembic_version' has invalid row count: expected 1 row, found {len(rows)}."
        )

    row = rows[0]
    # Extract revision from SQLAlchemy Row, tuple, list, or sequence
    if SqlAlchemyRow is not None and isinstance(row, SqlAlchemyRow):
        if len(row) == 0:
            raise PreflightGateError("Alembic revision row is empty (0 columns).")
        actual_rev = str(row[0]).strip()
    elif isinstance(row, (list, tuple)):
        if len(row) == 0:
            raise PreflightGateError("Alembic revision row is empty (0 columns).")
        actual_rev = str(row[0]).strip()
    elif hasattr(row, "__getitem__") and not isinstance(row, (str, bytes, bytearray)):
        try:
            actual_rev = str(row[0]).strip()
        except (IndexError, KeyError, TypeError):
            actual_rev = str(row).strip()
    else:
        actual_rev = str(row).strip()

    if actual_rev != expected_rev:
        raise PreflightGateError(
            f"Database revision mismatch: expected '{expected_rev}', found '{actual_rev}'."
        )

    return actual_rev


def validate_database_user_and_role(
    current_user: str | None,
    rolbypassrls: bool | None,
    expected_role: str = EXPECTED_ROLE,
) -> None:
    """Verify authenticated user is 'postgres' and possesses rolbypassrls privilege."""
    if not current_user:
        raise PreflightGateError("Unable to determine current_user from PostgreSQL.")
    if current_user != expected_role:
        raise PreflightGateError(
            f"Authenticated database user is '{current_user}'; expected '{expected_role}'."
        )
    if not rolbypassrls:
        raise PreflightGateError(
            f"Database user '{current_user}' has rolbypassrls=False; backend migration execution requires True."
        )


def validate_required_tables_present(
    existing_tables: set[str],
    required_tables: set[str] = REQUIRED_START_TABLES,
) -> None:
    """Verify that all baseline Phase 1 tables exist on the target database."""
    missing = required_tables - existing_tables
    if missing:
        raise PreflightGateError(
            f"Baseline tables missing from public schema: {sorted(missing)}."
        )


async def check_remote_database(
    database_url: str,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    operation_timeout: float = OPERATION_TIMEOUT_SECONDS,
) -> None:
    """Execute strictly read-only preflight queries against the target PostgreSQL database.

    Enforces finite connection and operation timeouts to fail fast and prevent hanging.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        database_url,
        connect_args={
            "statement_cache_size": 0,
            "timeout": connect_timeout,
            "command_timeout": connect_timeout,
        },
        pool_pre_ping=True,
    )

    async def _execute_queries() -> None:
        async with engine.connect() as conn:
            # 1. User & Role Check
            res_user = await conn.execute(
                text("SELECT current_user, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user);")
            )
            row_user = res_user.one_or_none()
            if not row_user:
                raise PreflightGateError("Could not retrieve current_user from pg_roles.")
            validate_database_user_and_role(row_user[0], bool(row_user[1]))

            # 2. Alembic Revision Check
            res_ver = await conn.execute(text("SELECT version_num FROM alembic_version;"))
            rows = res_ver.fetchall()
            validate_alembic_revision_rows(rows, expected_rev=EXPECTED_START_REV)

            # 3. Baseline Tables Check
            res_tables = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
            )
            found_tables = {r[0] for r in res_tables.fetchall()}
            validate_required_tables_present(found_tables, required_tables=REQUIRED_START_TABLES)

    try:
        await asyncio.wait_for(_execute_queries(), timeout=operation_timeout)
    except asyncio.TimeoutError:
        raise PreflightGateError(
            f"Database preflight timed out after {operation_timeout:.1f}s. "
            "Verify database network reachability, sandbox isolation, and connection pooler health."
        ) from None
    except PreflightGateError:
        raise
    except Exception as e:
        # Sanitize exception message to eliminate any risk of credential exposure
        err_type = type(e).__name__
        raise PreflightGateError(
            f"Database connection or query failed with {err_type}. "
            "Verify database network reachability and connection pooler health."
        ) from None
    finally:
        await engine.dispose()


def main() -> int:
    """Entry point for CLI execution."""
    configure_immediate_flushing()

    log("=" * 70)
    log(" Aptlyst AI — Hardened Staging Migration Preflight Gate")
    log(" Mode: 100% Read-Only Safety Verification")
    log("=" * 70)

    try:
        # Determine repository paths
        script_file = Path(__file__).resolve()
        repo_root = script_file.parent.parent
        api_dir = repo_root / "apps" / "api"
        if not api_dir.exists():
            api_dir = Path.cwd() / "apps" / "api"
        if not api_dir.exists():
            fail("Could not locate 'apps/api' directory.")

        sys.path.insert(0, str(api_dir))
        from app.config import settings

        # Gate 1: Environment check
        log("[1/4] Verifying environment mode...")
        validate_environment(os.environ.get("ENVIRONMENT"), settings.environment)
        log("      PASS: ENVIRONMENT=staging verified in shell and app settings.")

        # Gate 2: Database URL allowlist check
        log("[2/4] Validating database URL against staging allowlist...")
        parsed_target = validate_staging_database_url(str(settings.database_url))
        log(f"      PASS: Target host allowlisted: {parsed_target['hostname']}")
        log(f"      PASS: Target project ref verified: {parsed_target['project_ref']}")
        log(f"      PASS: Sanitized connection: {parsed_target['sanitized_url']}")

        # Gate 3: Offline Alembic graph check
        log("[3/4] Verifying offline migration graph...")
        ini_path = api_dir / "alembic.ini"
        alembic_loc = api_dir / "alembic"
        pending = validate_alembic_graph(ini_path, alembic_loc)
        log(f"      PASS: Single head verified: {EXPECTED_HEAD_REV}")
        log(f"      PASS: Pending revision chain verified: {pending}")

        # Gate 4: Read-Only Remote DB check
        log("[4/4] Performing read-only remote database preflight...")
        asyncio.run(check_remote_database(settings.database_url))
        log(f"      PASS: Current revision is verified at '{EXPECTED_START_REV}'.")
        log("      PASS: Role 'postgres' with rolbypassrls=True verified.")
        log("      PASS: Required baseline tables present.")

        log("\n" + "=" * 70)
        log(" ALL PREFLIGHT GATES PASSED (100% READ-ONLY).")
        log(" Target is verified at revision a1b2c3d4e5f6.")
        log(" Ready for human review and explicit migration authorization.")
        log("=" * 70 + "\n")
        return 0

    except PreflightGateError as pge:
        fail(str(pge))
        return 1
    except Exception as e:
        fail(f"Unexpected preflight failure: {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
