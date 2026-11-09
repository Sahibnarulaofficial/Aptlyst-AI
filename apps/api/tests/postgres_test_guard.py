"""Fail-closed PostgreSQL integration target & version guard for CI and test suites.

Enforces explicit isolation and server version guarantees before any test database
connection is opened:
- Never falls back to application `.env` or `DATABASE_URL`.
- Never prints passwords, userinfo, or raw connection strings.
- Rejects non-local hosts (including shared Supabase poolers) using exact host,
  port, database-name, query-parameter, and PostgreSQL 16 version verification.
- Preserves optional local developer skip behavior when `REQUIRE_POSTGRES_TESTS`
  is not enabled and `POSTGRES_TEST_URL` is unset.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit
import xml.etree.ElementTree as ET

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

REQUIRE_POSTGRES_ENV_VAR = "REQUIRE_POSTGRES_TESTS"
POSTGRES_TEST_URL_ENV_VAR = "POSTGRES_TEST_URL"
DISALLOWED_ENVIRONMENTS = frozenset({"production", "prod"})

PERMITTED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PERMITTED_CI_PORT = 5432
PERMITTED_CI_DATABASES = frozenset({"aptlyst_ci_test"})
DISALLOWED_DB_NAMES = frozenset({"", "postgres", "template0", "template1", "aptlyst"})
ALLOWED_URL_SCHEMES = frozenset(
    {
        "postgresql",
        "postgres",
        "postgresql+psycopg2",
        "postgresql+psycopg",
        "postgresql+asyncpg",
    }
)
DISALLOWED_QUERY_OVERRIDE_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "service",
        "passfile",
        "pgservicefile",
        "target_session_attrs",
    }
)
AMBIENT_PG_OVERRIDE_ENV_VARS = frozenset(
    {
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGPASSFILE",
        "PGTARGETSESSIONATTRS",
    }
)

_MIN_RAW_SECRET_REDACT_LEN = 8
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-zA-Z0-9+.-]+://)([^/@\s]+)@")
_HOSTADDR_QUERY_RE = re.compile(r"(?i)([?&](?:host|hostaddr|password|passfile)=)([^&\s]+)")
_DSN_SECRET_RE = re.compile(r"(?i)\b(password|passfile)\s*=\s*('[^']*'|\"[^\"]*\"|[^\s'\"&]+)")
_KWARG_SECRET_RE = re.compile(
    r"(?i)(['\"]?(?:password|passfile)['\"]?\s*[:=]\s*)('[^']*'|\"[^\"]*\")"
)


class PostgresTestConfigError(RuntimeError):
    """Raised when PostgreSQL test configuration fails closed-by-default safety validation."""


class PostgresTestConnectionError(PostgresTestConfigError):
    """Raised when required PostgreSQL connection/handshake fails cleanly before verification."""


@dataclass(frozen=True)
class ValidatedPostgresTestTarget:
    """Sanitized, validated PostgreSQL test connection target (never stores raw secrets verbatim in repr)."""

    scheme: str
    host: str
    port: int
    database: str
    sync_url: str
    async_url: str
    redacted_target: str

    def __repr__(self) -> str:
        return (
            f"ValidatedPostgresTestTarget(scheme={self.scheme!r}, host={self.host!r}, "
            f"port={self.port!r}, database={self.database!r}, target={self.redacted_target!r})"
        )


def detect_ambient_pg_overrides(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return sorted names of non-empty ambient PostgreSQL/libpq override variables."""
    env = os.environ if environ is None else environ
    return sorted(
        var_name
        for var_name in AMBIENT_PG_OVERRIDE_ENV_VARS
        if var_name in env and str(env.get(var_name, "")) != ""
    )


def is_postgres_tests_required(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when REQUIRE_POSTGRES_TESTS explicitly enforces fail-closed execution."""
    env = os.environ if environ is None else environ
    raw = str(env.get(REQUIRE_POSTGRES_ENV_VAR, "") or "").strip().lower()
    return raw in ("1", "true", "yes")


def redact_postgres_secrets(text: str, raw_url: str | None = None) -> str:
    """Scrub URL userinfo/passwords and libpq credential parameters without mangling unrelated text."""
    sanitized = str(text or "")
    sanitized = _CREDENTIALS_IN_URL_RE.sub(r"\1<redacted>@", sanitized)
    sanitized = _HOSTADDR_QUERY_RE.sub(r"\1<redacted>", sanitized)
    sanitized = _KWARG_SECRET_RE.sub(r"\1'<redacted>'", sanitized)
    sanitized = _DSN_SECRET_RE.sub(r"\1=<redacted>", sanitized)
    if raw_url:
        try:
            parsed = urlsplit(raw_url.strip())
            candidates: list[str] = []
            if parsed.password:
                candidates.append(parsed.password)
                unquoted_password = unquote(parsed.password)
                if unquoted_password and unquoted_password != parsed.password:
                    candidates.append(unquoted_password)
            for candidate in sorted(set(candidates), key=len, reverse=True):
                if len(candidate) >= _MIN_RAW_SECRET_REDACT_LEN:
                    sanitized = sanitized.replace(candidate, "<redacted>")
            if parsed.username and len(parsed.username) >= _MIN_RAW_SECRET_REDACT_LEN:
                sanitized = sanitized.replace(f"{parsed.username}:", "<redacted>:")
        except Exception:
            pass
    return sanitized


def normalize_to_sync_postgres_url(raw_url: str) -> str:
    """Normalize an already-validated PostgreSQL URL to a synchronous psycopg2 dialect URL."""
    trimmed = raw_url.strip()
    if "://" not in trimmed:
        return trimmed
    scheme, rest = trimmed.split("://", 1)
    normalized_scheme = scheme.lower()
    if normalized_scheme in ("postgresql+asyncpg", "postgresql+psycopg"):
        return f"postgresql+psycopg2://{rest}"
    if normalized_scheme == "postgres":
        return f"postgresql://{rest}"
    return f"{normalized_scheme}://{rest}"


def normalize_to_async_postgres_url(raw_url: str) -> str:
    """Normalize an already-validated PostgreSQL URL to an asyncpg dialect URL."""
    trimmed = raw_url.strip()
    if "://" not in trimmed:
        return trimmed
    scheme, rest = trimmed.split("://", 1)
    normalized_scheme = scheme.lower()
    if normalized_scheme in ("postgresql", "postgres", "postgresql+psycopg2", "postgresql+psycopg", "postgresql+asyncpg"):
        return f"postgresql+asyncpg://{rest}"
    return f"{normalized_scheme}://{rest}"


def validate_postgres_test_url(
    raw_url: str | None,
    *,
    require_postgres: bool,
) -> ValidatedPostgresTestTarget | None:
    """Validate POSTGRES_TEST_URL before connecting, failing closed on unsafe or invalid targets.

    Returns:
        ValidatedPostgresTestTarget when URL is valid and safe to connect.
        None ONLY when raw_url is empty/unset AND require_postgres is False.

    Raises:
        PostgresTestConfigError: When require_postgres is True and URL is missing, or whenever
            the supplied URL targets a disallowed scheme, host, port, query override, or database.
    """
    trimmed = (raw_url or "").strip()
    if not trimmed:
        if require_postgres:
            raise PostgresTestConfigError(
                "REQUIRE_POSTGRES_TESTS=1 is enabled, but POSTGRES_TEST_URL is unset or empty. "
                "Refusing to skip required PostgreSQL integration tests or fall back to .env DATABASE_URL."
            )
        return None

    if any(ch.isspace() for ch in trimmed) or "\\" in trimmed:
        raise PostgresTestConfigError("POSTGRES_TEST_URL contains unexpected whitespace or backslash characters.")

    if "://" not in trimmed:
        raise PostgresTestConfigError("POSTGRES_TEST_URL must be a complete connection URI with scheme.")

    scheme = trimmed.split("://", 1)[0].strip().lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise PostgresTestConfigError(
            f"Unsupported POSTGRES_TEST_URL scheme '{scheme}'. Expected an explicit PostgreSQL scheme."
        )

    try:
        parsed = urlsplit(trimmed)
        parsed_port = parsed.port
    except ValueError as exc:
        raise PostgresTestConfigError(
            f"Malformed POSTGRES_TEST_URL URI or port: {redact_postgres_secrets(str(exc), trimmed)}"
        ) from None

    if parsed.netloc.count("@") > 1:
        raise PostgresTestConfigError(
            "POSTGRES_TEST_URL authority contains ambiguous multiple '@' separators; "
            "percent-encode literal '@' characters inside credentials."
        )

    host = (parsed.hostname or "").strip().lower()
    if not host or "%" in host or host not in PERMITTED_LOCAL_HOSTS:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL host '{host or '<missing>'}' is outside the permitted local allowlist "
            f"({sorted(PERMITTED_LOCAL_HOSTS)}). Shared Supabase or remote hosts are strictly prohibited."
        )

    if parsed_port is None:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL must specify explicit port {PERMITTED_CI_PORT} (omitted port is prohibited "
            "to prevent implicit PGPORT overrides)."
        )
    if parsed_port != PERMITTED_CI_PORT:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL port {parsed_port} does not match permitted CI PostgreSQL port {PERMITTED_CI_PORT}."
        )

    if parsed.fragment:
        raise PostgresTestConfigError("POSTGRES_TEST_URL must not contain a URI fragment.")

    raw_path = (parsed.path or "").strip()
    if not raw_path.startswith("/") or len(raw_path) <= 1 or "/" in raw_path[1:] or "%" in raw_path:
        raise PostgresTestConfigError(
            "POSTGRES_TEST_URL must target an explicit single dedicated test database name."
        )
    db_name = raw_path[1:]
    if ";" in db_name:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL database '{db_name}' contains prohibited ';' delimiter."
        )

    normalized_db = db_name.lower()
    if normalized_db in DISALLOWED_DB_NAMES:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL database '{db_name}' is disallowed (never target default 'postgres', "
            "'aptlyst', or system template databases)."
        )

    if require_postgres:
        if normalized_db not in PERMITTED_CI_DATABASES:
            raise PostgresTestConfigError(
                f"POSTGRES_TEST_URL database '{db_name}' is not in the explicit CI test database allowlist "
                f"({sorted(PERMITTED_CI_DATABASES)})."
            )
    elif not normalized_db.endswith("_test"):
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL database '{db_name}' must end with '_test' to confirm isolated test-only intent."
        )

    if parsed.query:
        if ";" in parsed.query:
            raise PostgresTestConfigError(
                "POSTGRES_TEST_URL query contains prohibited ';' parameter delimiter."
            )
        query_keys = {k.strip().lower() for k in parse_qs(parsed.query, keep_blank_values=True)}
        for segment in parsed.query.split("&"):
            raw_key = unquote(segment.split("=", 1)[0]).split("=", 1)[0].strip().lower()
            if raw_key:
                query_keys.add(raw_key)
        forbidden_keys = sorted(query_keys & DISALLOWED_QUERY_OVERRIDE_KEYS)
        if forbidden_keys:
            raise PostgresTestConfigError(
                f"POSTGRES_TEST_URL query overrides {forbidden_keys} are prohibited for host/port/database isolation."
            )

    sync_url = normalize_to_sync_postgres_url(trimmed)
    async_url = normalize_to_async_postgres_url(trimmed)

    from sqlalchemy.dialects.postgresql import asyncpg as sa_asyncpg_dialect
    from sqlalchemy.dialects.postgresql import psycopg2 as sa_psycopg2_dialect
    from sqlalchemy.engine import make_url

    try:
        sa_sync = make_url(sync_url)
        sa_async = make_url(async_url)
        sync_cargs = sa_psycopg2_dialect.dialect().create_connect_args(sa_sync)[1]
        async_cargs = sa_asyncpg_dialect.dialect().create_connect_args(sa_async)[1]
    except Exception as exc:
        raise PostgresTestConfigError(
            f"Malformed SQLAlchemy PostgreSQL URL: {redact_postgres_secrets(str(exc), trimmed)}"
        ) from None

    if (
        (sa_sync.host or "").strip().lower() != host
        or sa_sync.port != parsed_port
        or (sa_sync.database or "") != db_name
        or (sa_async.host or "").strip().lower() != host
        or sa_async.port != parsed_port
        or (sa_async.database or "") != db_name
        or str(sync_cargs.get("host") or "").strip().lower() != host
        or sync_cargs.get("port") != parsed_port
        or str(sync_cargs.get("dbname") or "") != db_name
        or str(async_cargs.get("host") or "").strip().lower() != host
        or async_cargs.get("port") != parsed_port
        or str(async_cargs.get("database") or "") != db_name
    ):
        raise PostgresTestConfigError(
            "POSTGRES_TEST_URL effective target differs across URI, psycopg2, and asyncpg dialects."
        )

    sa_query_keys = {k.strip().lower() for k in sa_sync.query}
    sa_forbidden = sorted(sa_query_keys & DISALLOWED_QUERY_OVERRIDE_KEYS)
    if sa_forbidden:
        raise PostgresTestConfigError(
            f"POSTGRES_TEST_URL query overrides {sa_forbidden} are prohibited for host/port/database isolation."
        )

    if require_postgres and not (sa_sync.username or "").strip():
        raise PostgresTestConfigError(
            "POSTGRES_TEST_URL must include an explicit username when REQUIRE_POSTGRES_TESTS=1."
        )

    display_host = f"[{host}]" if ":" in host else host
    redacted_target = f"{scheme}://{display_host}:{parsed_port}/{db_name}"
    return ValidatedPostgresTestTarget(
        scheme=scheme,
        host=host,
        port=parsed_port,
        database=db_name,
        sync_url=sync_url,
        async_url=async_url,
        redacted_target=redacted_target,
    )


def verify_live_postgres_16_target(engine: Engine, expected_database: str) -> str:
    """Verify connected server is PostgreSQL 16 and matches the expected isolated test database."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database(), "
                "current_setting('server_version_num')::integer, "
                "version()"
            )
        ).one()

    actual_db = str(row[0] or "")
    version_num = int(row[1])
    full_version = str(row[2] or "")

    if actual_db != expected_database:
        raise PostgresTestConfigError(
            f"Connected database '{actual_db}' does not match expected dedicated test database "
            f"'{expected_database}'."
        )

    if not (160000 <= version_num < 170000) or not full_version.startswith("PostgreSQL 16"):
        major_detected = version_num // 10000
        raise PostgresTestConfigError(
            f"PostgreSQL 16 is required, but connected server reported major version {major_detected} "
            f"(server_version_num={version_num})."
        )

    return f"PostgreSQL {version_num // 10000} ({version_num}) on database '{actual_db}'"


def resolve_verified_pg_engine(
    environ: Mapping[str, str] | None = None,
    *,
    engine_factory=None,
) -> tuple[Engine | None, str | None, bool]:
    """Resolve and verify a live PostgreSQL 16 engine or return skip state when optional.

    Returns:
        (engine, version_summary, required)
        - When required is False and POSTGRES_TEST_URL is unset -> (None, None, False).
        - Raises PostgresTestConfigError on unsafe configuration, unreachable required DB,
          wrong DB name, or non-PG16 version.
    """
    from sqlalchemy import create_engine

    env = os.environ if environ is None else environ
    required = is_postgres_tests_required(env)
    raw_url = env.get(POSTGRES_TEST_URL_ENV_VAR)
    env_name = str(env.get("ENVIRONMENT", "") or "").strip().lower()

    if not required and not (raw_url or "").strip():
        return None, None, False

    if env_name in DISALLOWED_ENVIRONMENTS:
        raise PostgresTestConfigError(
            f"Refusing to run PostgreSQL integration tests with ENVIRONMENT='{env_name}'."
        )

    ambient_overrides = detect_ambient_pg_overrides(env)
    if engine_factory is None and environ is not None:
        ambient_overrides = sorted(set(ambient_overrides) | set(detect_ambient_pg_overrides(os.environ)))
    if ambient_overrides:
        raise PostgresTestConfigError(
            f"Disallowed ambient PostgreSQL/libpq environment override(s) detected: {ambient_overrides}. "
            "Unset PG* override variables so validated connection targets cannot be redirected."
        )

    target = validate_postgres_test_url(raw_url, require_postgres=required)
    assert target is not None

    if required and (env.get("DATABASE_URL") or "").strip():
        db_target = validate_postgres_test_url(env.get("DATABASE_URL"), require_postgres=True)
        assert db_target is not None
        if (db_target.host, db_target.port, db_target.database) != (
            target.host,
            target.port,
            target.database,
        ):
            raise PostgresTestConfigError(
                "DATABASE_URL target does not match POSTGRES_TEST_URL target while REQUIRE_POSTGRES_TESTS=1."
            )

    make_engine = engine_factory or (lambda url: create_engine(url, echo=False))
    engine = None
    try:
        engine = make_engine(target.sync_url)
        summary = verify_live_postgres_16_target(engine, target.database)
        return engine, summary, required
    except PostgresTestConfigError:
        if engine is not None:
            engine.dispose()
        raise
    except Exception as exc:
        if engine is not None:
            engine.dispose()
        safe_err = redact_postgres_secrets(str(exc), raw_url)
        if required:
            raise PostgresTestConnectionError(
                f"Required PostgreSQL 16 connection/verification failed for {target.redacted_target}: "
                f"{type(exc).__name__}: {safe_err}"
            ) from None
        raise _OptionalPostgresUnreachableError(
            f"PostgreSQL connection to {target.redacted_target} failed: {type(exc).__name__}: {safe_err}"
        ) from None


class _OptionalPostgresUnreachableError(RuntimeError):
    """Internal signal when optional local POSTGRES_TEST_URL is unreachable and REQUIRE_POSTGRES_TESTS is unset."""


def _parse_junit_timestamp_age_seconds(raw_timestamp: str) -> float:
    cleaned = raw_timestamp.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed_dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise PostgresTestConfigError(f"Malformed JUnit testsuite timestamp '{raw_timestamp}': {exc}") from None
    if parsed_dt.tzinfo is None:
        return (datetime.now() - parsed_dt).total_seconds()
    return (datetime.now(timezone.utc) - parsed_dt.astimezone(timezone.utc)).total_seconds()


def assert_junit_zero_skips(
    xml_path: str | Path,
    *,
    expected_min_tests: int = 1,
    expected_exact_tests: int | None = None,
    min_mtime: float | None = None,
    max_age_seconds: float | None = None,
    require_testcase_elements: bool = False,
) -> tuple[int, int, int, int]:
    """Parse JUnit XML test report and fail closed on stale, skipped, failed, errored, or incomplete runs."""
    path = Path(xml_path)
    if not path.is_file():
        raise PostgresTestConfigError(f"Expected JUnit XML file does not exist: {path}")

    stat_info = path.stat()
    if stat_info.st_size == 0:
        raise PostgresTestConfigError(f"Expected JUnit XML file is empty: {path}")

    now_epoch = time.time()
    if min_mtime is not None and stat_info.st_mtime < min_mtime:
        raise PostgresTestConfigError(
            f"Stale JUnit XML file {path}: mtime {stat_info.st_mtime:.3f} predates expected minimum {min_mtime:.3f}."
        )
    if max_age_seconds is not None:
        if max_age_seconds < 0:
            raise PostgresTestConfigError("max_age_seconds must be non-negative.")
        file_age = now_epoch - stat_info.st_mtime
        if file_age > max_age_seconds:
            raise PostgresTestConfigError(
                f"Stale JUnit XML file {path}: file age {file_age:.1f}s exceeds maximum allowed {max_age_seconds:.1f}s."
            )

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise PostgresTestConfigError(f"Malformed JUnit XML file {path}: {exc}") from None

    root = tree.getroot()
    if root.tag == "testsuites":
        unexpected_root_children = [child.tag for child in root if child.tag != "testsuite"]
        if unexpected_root_children:
            raise PostgresTestConfigError(
                f"Unexpected child elements under <testsuites> in {path}: {unexpected_root_children}"
            )
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise PostgresTestConfigError(f"Unexpected JUnit XML root element '{root.tag}' in {path}")

    if not suites:
        raise PostgresTestConfigError(f"No <testsuite> elements found in JUnit XML {path}")

    total_tests = 0
    total_skipped = 0
    total_failures = 0
    total_errors = 0
    missing_testcase_suites: list[str] = []
    mismatched_testcase_messages: list[str] = []
    for suite in suites:
        if suite.findall("testsuite"):
            raise PostgresTestConfigError(f"Unsupported nested <testsuite> structure in {path}.")
        if "tests" not in suite.attrib:
            raise PostgresTestConfigError(f"Missing required 'tests' attribute on <testsuite> in {path}.")

        try:
            tests_count = int(suite.attrib["tests"])
            attr_skipped = int(suite.attrib.get("skipped", 0))
            attr_failures = int(suite.attrib.get("failures", 0))
            attr_errors = int(suite.attrib.get("errors", 0))
        except ValueError as exc:
            raise PostgresTestConfigError(f"Non-integer JUnit counter in {path}: {exc}") from None

        if min(tests_count, attr_skipped, attr_failures, attr_errors) < 0:
            raise PostgresTestConfigError(f"Negative JUnit counter detected in {path}.")

        if (max_age_seconds is not None or min_mtime is not None) and "timestamp" in suite.attrib:
            ts_age = _parse_junit_timestamp_age_seconds(suite.attrib["timestamp"])
            if max_age_seconds is not None and ts_age > max_age_seconds:
                raise PostgresTestConfigError(
                    f"Stale JUnit testsuite timestamp in {path}: report age {ts_age:.1f}s exceeds {max_age_seconds:.1f}s."
                )
            if min_mtime is not None:
                ts_epoch = now_epoch - ts_age
                if ts_epoch < (min_mtime - 2.0):
                    raise PostgresTestConfigError(
                        f"Stale JUnit testsuite timestamp in {path}: predates minimum run timestamp."
                    )

        testcases = suite.findall("testcase")
        if require_testcase_elements and tests_count > 0 and not testcases:
            missing_testcase_suites.append(
                f"JUnit report {path} claims tests={tests_count} without any <testcase> elements."
            )
        elif testcases and len(testcases) != tests_count:
            mismatched_testcase_messages.append(
                f"JUnit report {path} <testcase> element count ({len(testcases)}) does not match tests={tests_count}."
            )

        child_skipped = sum(1 for tc in testcases if tc.find("skipped") is not None)
        child_failures = sum(1 for tc in testcases if tc.find("failure") is not None)
        child_errors = sum(1 for tc in testcases if tc.find("error") is not None)

        total_tests += tests_count
        total_skipped += max(attr_skipped, child_skipped)
        total_failures += max(attr_failures, child_failures)
        total_errors += max(attr_errors, child_errors)

    if total_skipped > 0:
        raise PostgresTestConfigError(
            f"JUnit report {path} recorded {total_skipped} skipped test(s); zero skips are permitted."
        )
    if total_failures > 0 or total_errors > 0:
        raise PostgresTestConfigError(
            f"JUnit report {path} recorded failures={total_failures}, errors={total_errors}."
        )
    if missing_testcase_suites:
        raise PostgresTestConfigError(missing_testcase_suites[0])
    if mismatched_testcase_messages:
        raise PostgresTestConfigError(mismatched_testcase_messages[0])
    if total_tests < expected_min_tests:
        raise PostgresTestConfigError(
            f"JUnit report {path} executed {total_tests} test(s), expected at least {expected_min_tests}."
        )
    if expected_exact_tests is not None and total_tests != expected_exact_tests:
        raise PostgresTestConfigError(
            f"JUnit report {path} executed {total_tests} test(s), expected exactly {expected_exact_tests}."
        )

    return total_tests, total_skipped, total_failures, total_errors
