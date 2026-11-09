"""Offline unit and subprocess guard tests for CI PostgreSQL 16 enforcement.

IMPORTANT HONESTY BOUNDARY:
- All tests in this module run OFFLINE using pure config checks, in-memory Engine stubs,
  and isolated `pytest` subprocesses without connecting to any live PostgreSQL 16 server.
- They verify fail-closed safety guards, anti-Supabase isolation, secret redaction,
  PG 16 version/database verification logic, and JUnit zero-skip enforcement.
- Genuine live PostgreSQL 16 execution against `aptlyst_ci_test` is performed in CI.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import pytest

from tests.postgres_test_guard import (
    AMBIENT_PG_OVERRIDE_ENV_VARS,
    PostgresTestConfigError,
    PostgresTestConnectionError,
    _OptionalPostgresUnreachableError,
    assert_junit_zero_skips,
    detect_ambient_pg_overrides,
    is_postgres_tests_required,
    normalize_to_async_postgres_url,
    normalize_to_sync_postgres_url,
    redact_postgres_secrets,
    resolve_verified_pg_engine,
    validate_postgres_test_url,
    verify_live_postgres_16_target,
)


_API_ROOT = Path(__file__).resolve().parents[1]
_DUMMY_SECRET = "DummyGuardSecret987!"
_PROVENANCE_SUBPROCESS_TIMEOUT_SECONDS = 30.0
_SUBPROCESS_TERMINATION_WAIT_SECONDS = 5.0


class _FakeResultRow:
    def __init__(self, row: tuple[str, int, str]) -> None:
        self._row = row

    def one(self) -> tuple[str, int, str]:
        return self._row


class _FakeConnection:
    def __init__(self, row: tuple[str, int, str] | None = None, exc: Exception | None = None) -> None:
        self._row = row
        self._exc = exc

    def __enter__(self) -> _FakeConnection:
        if self._exc is not None:
            raise self._exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, statement) -> _FakeResultRow:
        if self._exc is not None:
            raise self._exc
        assert self._row is not None
        return _FakeResultRow(self._row)


class _FakeEngine:
    def __init__(self, row: tuple[str, int, str] | None = None, exc: Exception | None = None) -> None:
        self._row = row
        self._exc = exc
        self.disposed = False

    def connect(self) -> _FakeConnection:
        return _FakeConnection(row=self._row, exc=self._exc)

    def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
    ],
)
def test_is_postgres_tests_required_flag_parsing(raw_value: str | None, expected: bool) -> None:
    env = {} if raw_value is None else {"REQUIRE_POSTGRES_TESTS": raw_value}
    assert is_postgres_tests_required(env) is expected


def test_optional_mode_returns_none_when_url_and_flag_unset() -> None:
    calls: list[str] = []
    engine, summary, required = resolve_verified_pg_engine(
        {},
        engine_factory=lambda url: calls.append(url) or _FakeEngine(),
    )
    assert (engine, summary, required) == (None, None, False)
    assert calls == []


def test_required_mode_missing_url_fails_closed_without_dotenv_fallback() -> None:
    calls: list[str] = []
    env = {
        "REQUIRE_POSTGRES_TESTS": "1",
        "DATABASE_URL": f"postgresql+asyncpg://postgres:{_DUMMY_SECRET}@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres",
    }
    with pytest.raises(PostgresTestConfigError) as exc_info:
        resolve_verified_pg_engine(env, engine_factory=lambda url: calls.append(url) or _FakeEngine())

    assert "REQUIRE_POSTGRES_TESTS=1" in str(exc_info.value)
    assert ".env DATABASE_URL" in str(exc_info.value)
    assert _DUMMY_SECRET not in str(exc_info.value)
    assert calls == []


@pytest.mark.parametrize(
    "unsafe_url",
    [
        f"postgresql+psycopg2://postgres:{_DUMMY_SECRET}@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres",
        f"postgresql://postgres:{_DUMMY_SECRET}@db.exampleproj.supabase.co:5432/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@localhost.evil.com:5432/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@10.0.0.5:5432/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:6543/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5433/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/postgres",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/template1",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test/extra",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test#frag",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?host=pooler.supabase.com",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?hostaddr=1.2.3.4",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?port=6543",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?dbname=postgres",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?service=prod",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?passfile=/tmp/pgpass",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?pgservicefile=/tmp/svc",
        "sqlite:///aptlyst_ci_test.db",
        f"mysql://user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
        f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:99999/aptlyst_ci_test",
    ],
)
@pytest.mark.parametrize("require_flag", ["0", "1"])
def test_unsafe_postgres_test_urls_rejected_before_connect(unsafe_url: str, require_flag: str) -> None:
    calls: list[str] = []
    env = {
        "REQUIRE_POSTGRES_TESTS": require_flag,
        "POSTGRES_TEST_URL": unsafe_url,
    }
    with pytest.raises(PostgresTestConfigError) as exc_info:
        resolve_verified_pg_engine(env, engine_factory=lambda url: calls.append(url) or _FakeEngine())

    assert calls == [], "Engine/connection must never be attempted for unsafe POSTGRES_TEST_URL"
    assert _DUMMY_SECRET not in str(exc_info.value)


def test_required_mode_rejects_non_ci_test_database_name() -> None:
    url = f"postgresql://user:{_DUMMY_SECRET}@127.0.0.1:5432/local_dev_test"
    with pytest.raises(PostgresTestConfigError):
        validate_postgres_test_url(url, require_postgres=True)

    target = validate_postgres_test_url(url, require_postgres=False)
    assert target is not None
    assert target.database == "local_dev_test"
    assert _DUMMY_SECRET not in repr(target)


def test_valid_local_targets_normalize_and_redact_cleanly() -> None:
    async_url = f"POSTGRESQL+ASYNCPG://ci_user:{_DUMMY_SECRET}@localhost:5432/aptlyst_ci_test?sslmode=disable"
    target = validate_postgres_test_url(async_url, require_postgres=True)
    assert target is not None
    assert target.host == "localhost"
    assert target.port == 5432
    assert target.database == "aptlyst_ci_test"
    assert target.sync_url.startswith("postgresql+psycopg2://")
    assert target.redacted_target == "postgresql+asyncpg://localhost:5432/aptlyst_ci_test"
    assert _DUMMY_SECRET not in repr(target)

    ipv6_target = validate_postgres_test_url(
        f"postgres://ci_user:{_DUMMY_SECRET}@[::1]:5432/aptlyst_ci_test",
        require_postgres=True,
    )
    assert ipv6_target is not None
    assert ipv6_target.host == "::1"
    assert ipv6_target.redacted_target == "postgres://[::1]:5432/aptlyst_ci_test"
    assert normalize_to_sync_postgres_url(ipv6_target.sync_url).startswith("postgresql://")


def test_production_environment_rejected_fail_closed() -> None:
    env = {
        "ENVIRONMENT": "production",
        "REQUIRE_POSTGRES_TESTS": "1",
        "POSTGRES_TEST_URL": f"postgresql+psycopg2://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
    }
    calls: list[str] = []
    with pytest.raises(PostgresTestConfigError, match="ENVIRONMENT='production'"):
        resolve_verified_pg_engine(env, engine_factory=lambda url: calls.append(url) or _FakeEngine())
    assert calls == []


def test_secret_redaction_covers_url_unquoted_and_libpq_dsn() -> None:
    encoded_secret = "Pass%21987%40Sec"
    decoded_secret = "Pass!987@Sec"
    raw_url = f"postgresql+psycopg2://ci_user:{encoded_secret}@127.0.0.1:5432/aptlyst_ci_test"
    noisy_error = (
        f"connect failed for postgresql+psycopg2://ci_user:{encoded_secret}@127.0.0.1:5432/aptlyst_ci_test "
        f"with password={decoded_secret} &passfile=/etc/pgpass"
    )
    cleaned = redact_postgres_secrets(noisy_error, raw_url)
    assert encoded_secret not in cleaned
    assert decoded_secret not in cleaned
    assert "ci_user:" not in cleaned
    assert "/etc/pgpass" not in cleaned


def test_verify_live_postgres_16_target_accepts_only_pg16_and_expected_db() -> None:
    pg16_engine = _FakeEngine(
        row=("aptlyst_ci_test", 160004, "PostgreSQL 16.4 on x86_64-pc-linux-musl")
    )
    summary = verify_live_postgres_16_target(pg16_engine, "aptlyst_ci_test")
    assert "PostgreSQL 16 (160004)" in summary
    assert "aptlyst_ci_test" in summary

    wrong_db_engine = _FakeEngine(row=("postgres", 160004, "PostgreSQL 16.4 on x86_64"))
    with pytest.raises(PostgresTestConfigError, match="does not match expected dedicated test database"):
        verify_live_postgres_16_target(wrong_db_engine, "aptlyst_ci_test")

    for bad_version in (
        ("aptlyst_ci_test", 150008, "PostgreSQL 15.8 on x86_64"),
        ("aptlyst_ci_test", 170001, "PostgreSQL 17.1 on x86_64"),
        ("aptlyst_ci_test", 160004, "UnexpectedEngine 16.4"),
    ):
        with pytest.raises(PostgresTestConfigError, match="PostgreSQL 16 is required"):
            verify_live_postgres_16_target(_FakeEngine(row=bad_version), "aptlyst_ci_test")


def test_connection_failure_disposes_engine_and_distinguishes_required_vs_optional() -> None:
    created: list[_FakeEngine] = []

    def _factory(_url: str) -> _FakeEngine:
        eng = _FakeEngine(
            exc=RuntimeError(f"could not connect using postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test")
        )
        created.append(eng)
        return eng

    required_env = {
        "REQUIRE_POSTGRES_TESTS": "1",
        "POSTGRES_TEST_URL": f"postgresql+psycopg2://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
    }
    with pytest.raises(PostgresTestConnectionError) as req_exc:
        resolve_verified_pg_engine(required_env, engine_factory=_factory)
    assert created[-1].disposed is True
    assert _DUMMY_SECRET not in str(req_exc.value)

    wrong_ver_engine = _FakeEngine(row=("aptlyst_ci_test", 150008, "PostgreSQL 15.8"))
    with pytest.raises(PostgresTestConfigError) as ver_exc:
        resolve_verified_pg_engine(required_env, engine_factory=lambda _url: wrong_ver_engine)
    assert not isinstance(ver_exc.value, PostgresTestConnectionError)
    assert wrong_ver_engine.disposed is True

    optional_env = {
        "POSTGRES_TEST_URL": f"postgresql+psycopg2://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
    }
    with pytest.raises(_OptionalPostgresUnreachableError) as opt_exc:
        resolve_verified_pg_engine(optional_env, engine_factory=_factory)
    assert created[-1].disposed is True
    assert _DUMMY_SECRET not in str(opt_exc.value)


def test_assert_junit_zero_skips_enforces_counts_and_zero_skips(tmp_path: Path) -> None:
    valid_xml = tmp_path / "valid.xml"
    valid_xml.write_text(
        '<testsuites><testsuite name="pytest" tests="14" skipped="0" failures="0" errors="0" /></testsuites>',
        encoding="utf-8",
    )
    assert assert_junit_zero_skips(valid_xml, expected_exact_tests=14) == (14, 0, 0, 0)

    skipped_attr_xml = tmp_path / "skipped_attr.xml"
    skipped_attr_xml.write_text(
        '<testsuites><testsuite name="pytest" tests="14" skipped="1" failures="0" errors="0" /></testsuites>',
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="zero skips are permitted"):
        assert_junit_zero_skips(skipped_attr_xml, expected_exact_tests=14)

    skipped_child_xml = tmp_path / "skipped_child.xml"
    skipped_child_xml.write_text(
        '<testsuite name="pytest" tests="14" failures="0" errors="0">'
        '<testcase name="t1"><skipped message="skipped" /></testcase></testsuite>',
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="zero skips are permitted"):
        assert_junit_zero_skips(skipped_child_xml, expected_exact_tests=14)

    wrong_count_xml = tmp_path / "wrong_count.xml"
    wrong_count_xml.write_text(
        '<testsuite name="pytest" tests="13" skipped="0" failures="0" errors="0" />',
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="expected exactly 14"):
        assert_junit_zero_skips(wrong_count_xml, expected_exact_tests=14)

    with pytest.raises(PostgresTestConfigError, match="does not exist"):
        assert_junit_zero_skips(tmp_path / "missing.xml", expected_exact_tests=14)


def _build_provenance_pytest_command(
    junit_path: Path,
    api_root: Path | None = None,
) -> tuple[list[str], Path]:
    resolved_api_root = (_API_ROOT if api_root is None else api_root).resolve()
    if not resolved_api_root.is_dir():
        raise AssertionError(f"Expected isolated API directory does not exist: {resolved_api_root}")
    pytest_ini = resolved_api_root / "pytest.ini"
    provenance_test = resolved_api_root / "tests" / "test_postgres_provenance_integration.py"
    if not pytest_ini.is_file():
        raise AssertionError(f"Expected pytest.ini in isolated API directory: {pytest_ini}")
    if not provenance_test.is_file():
        raise AssertionError(f"Expected provenance test module in isolated API directory: {provenance_test}")

    cmd = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-c",
        str(pytest_ini),
        "--rootdir",
        str(resolved_api_root),
        "tests/test_postgres_provenance_integration.py",
        f"--junitxml={junit_path}",
    ]
    return cmd, resolved_api_root


def _get_win32_kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, wintypes


def _try_create_windows_job() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        kernel32, _ = _get_win32_kernel32()
        job_handle = kernel32.CreateJobObjectW(None, None)
        return int(job_handle) if job_handle else None
    except Exception:
        return None


def _try_assign_process_to_windows_job(
    job_handle: int | None,
    proc: subprocess.Popen[bytes],
) -> bool:
    if sys.platform != "win32" or not job_handle:
        return False
    try:
        kernel32, wintypes = _get_win32_kernel32()
        return bool(
            kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job_handle),
                wintypes.HANDLE(int(proc._handle)),
            )
        )
    except Exception:
        return False


def _close_windows_job(job_handle: int | None) -> None:
    if sys.platform != "win32" or not job_handle:
        return
    try:
        kernel32, wintypes = _get_win32_kernel32()
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except Exception:
        pass


def _terminate_subprocess_tree(
    proc: subprocess.Popen[bytes],
    *,
    job_handle: int | None = None,
    job_assigned: bool = False,
) -> None:
    if sys.platform == "win32":
        if job_assigned and job_handle:
            try:
                kernel32, wintypes = _get_win32_kernel32()
                kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass


def _execute_subprocess_with_timeout(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    secret_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")

    popen_kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    job_handle = _try_create_windows_job()
    job_assigned = False
    timed_out = False
    returncode: int | None = None
    stdout_text = ""
    stderr_text = ""
    pid: int | None = None

    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **popen_kwargs,
            )
            pid = proc.pid
            if job_handle is not None and proc.poll() is None:
                job_assigned = _try_assign_process_to_windows_job(job_handle, proc)

            try:
                returncode = proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_subprocess_tree(
                    proc,
                    job_handle=job_handle,
                    job_assigned=job_assigned,
                )
                try:
                    returncode = proc.wait(timeout=_SUBPROCESS_TERMINATION_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    returncode = proc.poll()
        except BaseException:
            if proc is not None and proc.poll() is None:
                _terminate_subprocess_tree(
                    proc,
                    job_handle=job_handle,
                    job_assigned=job_assigned,
                )
                try:
                    proc.wait(timeout=_SUBPROCESS_TERMINATION_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            _close_windows_job(job_handle)
            stdout_file.flush()
            stderr_file.flush()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_text = stdout_file.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
            stderr_text = stderr_file.read().decode("utf-8", errors="replace").replace("\r\n", "\n")

    if timed_out:
        safe_cmd = redact_postgres_secrets(shlex.join(cmd), secret_url)
        safe_stdout = redact_postgres_secrets(stdout_text, secret_url) or "<empty>"
        safe_stderr = redact_postgres_secrets(stderr_text, secret_url) or "<empty>"
        pytest.fail(
            f"Provenance subprocess timed out after {timeout_seconds:.2f}s "
            f"(pid={pid}, post_termination_returncode={returncode!r}).\n"
            f"cwd: {cwd}\n"
            f"command: {safe_cmd}\n"
            f"captured stdout:\n{safe_stdout}\n"
            f"captured stderr:\n{safe_stderr}"
        )

    assert returncode is not None
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
    )


def _run_provenance_subprocess(
    env_overrides: dict[str, str | None],
    junit_path: Path,
    *,
    timeout_seconds: float = _PROVENANCE_SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "POSTGRES_TEST_URL",
        "REQUIRE_POSTGRES_TESTS",
        "DATABASE_URL",
        "ENVIRONMENT",
        *AMBIENT_PG_OVERRIDE_ENV_VARS,
    ):
        env.pop(key, None)
    for key, value in env_overrides.items():
        if value is not None:
            env[key] = value

    cmd, api_root = _build_provenance_pytest_command(junit_path)
    return _execute_subprocess_with_timeout(
        cmd,
        cwd=api_root,
        env=env,
        timeout_seconds=timeout_seconds,
        secret_url=env.get("POSTGRES_TEST_URL") or env.get("DATABASE_URL"),
    )


def test_provenance_subprocess_optional_unset_skips_all_14_cleanly(tmp_path: Path) -> None:
    junit_xml = tmp_path / "optional.xml"
    proc = _run_provenance_subprocess({}, junit_xml)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    suite = ET.parse(junit_xml).getroot().find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "14"
    assert suite.attrib["skipped"] == "14"
    assert suite.attrib["failures"] == "0"
    assert suite.attrib["errors"] == "0"


def test_provenance_subprocess_required_without_url_fails_closed_zero_skips(tmp_path: Path) -> None:
    junit_xml = tmp_path / "required_missing_url.xml"
    proc = _run_provenance_subprocess(
        {
            "REQUIRE_POSTGRES_TESTS": "1",
            "DATABASE_URL": f"postgresql+asyncpg://postgres:{_DUMMY_SECRET}@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres",
        },
        junit_xml,
    )
    assert proc.returncode != 0
    combined_output = proc.stdout + proc.stderr
    assert "REQUIRE_POSTGRES_TESTS=1 is enabled" in combined_output
    assert _DUMMY_SECRET not in combined_output
    assert _DUMMY_SECRET not in junit_xml.read_text(encoding="utf-8")

    suite = ET.parse(junit_xml).getroot().find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "14"
    assert suite.attrib["skipped"] == "0"
    with pytest.raises(PostgresTestConfigError):
        assert_junit_zero_skips(junit_xml, expected_exact_tests=14)


def test_provenance_subprocess_supabase_url_rejected_zero_skips_no_secret_leak(tmp_path: Path) -> None:
    junit_xml = tmp_path / "supabase_url.xml"
    proc = _run_provenance_subprocess(
        {
            "REQUIRE_POSTGRES_TESTS": "1",
            "POSTGRES_TEST_URL": f"postgresql+psycopg2://postgres:{_DUMMY_SECRET}@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres",
        },
        junit_xml,
    )
    assert proc.returncode != 0
    combined_output = proc.stdout + proc.stderr
    assert "permitted local allowlist" in combined_output
    assert _DUMMY_SECRET not in combined_output
    assert _DUMMY_SECRET not in junit_xml.read_text(encoding="utf-8")

    suite = ET.parse(junit_xml).getroot().find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "14"
    assert suite.attrib["skipped"] == "0"


def test_provenance_subprocess_ambient_pghostaddr_rejected_zero_skips(tmp_path: Path) -> None:
    junit_xml = tmp_path / "ambient_pghostaddr.xml"
    proc = _run_provenance_subprocess(
        {
            "REQUIRE_POSTGRES_TESTS": "1",
            "POSTGRES_TEST_URL": f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
            "PGHOSTADDR": "127.0.0.2",
        },
        junit_xml,
    )
    assert proc.returncode != 0
    combined_output = proc.stdout + proc.stderr
    assert "PGHOSTADDR" in combined_output
    assert _DUMMY_SECRET not in combined_output
    assert _DUMMY_SECRET not in junit_xml.read_text(encoding="utf-8")

    suite = ET.parse(junit_xml).getroot().find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "14"
    assert suite.attrib["skipped"] == "0"
    with pytest.raises(PostgresTestConfigError):
        assert_junit_zero_skips(junit_xml, expected_exact_tests=14)


def test_provenance_subprocess_unconnectable_local_pg_required_vs_optional(tmp_path: Path) -> None:
    bad_cred_url = f"postgresql+psycopg2://nonexistent_ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test"

    required_xml = tmp_path / "unreachable_required.xml"
    req_proc = _run_provenance_subprocess(
        {
            "REQUIRE_POSTGRES_TESTS": "1",
            "POSTGRES_TEST_URL": bad_cred_url,
        },
        required_xml,
    )
    assert req_proc.returncode != 0
    req_output = req_proc.stdout + req_proc.stderr
    assert _DUMMY_SECRET not in req_output
    assert _DUMMY_SECRET not in required_xml.read_text(encoding="utf-8")
    req_suite = ET.parse(required_xml).getroot().find("testsuite")
    assert req_suite is not None
    assert req_suite.attrib["tests"] == "14"
    assert req_suite.attrib["skipped"] == "0"
    with pytest.raises(PostgresTestConfigError):
        assert_junit_zero_skips(required_xml, expected_exact_tests=14)

    optional_xml = tmp_path / "unreachable_optional.xml"
    opt_proc = _run_provenance_subprocess(
        {
            "POSTGRES_TEST_URL": bad_cred_url,
        },
        optional_xml,
    )
    assert opt_proc.returncode == 0, opt_proc.stdout + opt_proc.stderr
    opt_output = opt_proc.stdout + opt_proc.stderr
    assert _DUMMY_SECRET not in opt_output
    assert _DUMMY_SECRET not in optional_xml.read_text(encoding="utf-8")
    opt_suite = ET.parse(optional_xml).getroot().find("testsuite")
    assert opt_suite is not None
    assert opt_suite.attrib["tests"] == "14"
    assert opt_suite.attrib["skipped"] == "14"


@pytest.mark.parametrize(
    "ambient_var",
    sorted(
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
    ),
)
@pytest.mark.parametrize("require_flag", ["0", "1"])
def test_ambient_libpq_overrides_rejected_before_connect(ambient_var: str, require_flag: str) -> None:
    assert ambient_var in AMBIENT_PG_OVERRIDE_ENV_VARS
    calls: list[str] = []
    env = {
        "REQUIRE_POSTGRES_TESTS": require_flag,
        "POSTGRES_TEST_URL": f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
        ambient_var: "127.0.0.2" if ambient_var in ("PGHOST", "PGHOSTADDR") else "override_val",
    }
    assert detect_ambient_pg_overrides(env) == [ambient_var]

    with pytest.raises(PostgresTestConfigError) as exc_info:
        resolve_verified_pg_engine(env, engine_factory=lambda url: calls.append(url) or _FakeEngine())

    assert ambient_var in str(exc_info.value)
    assert _DUMMY_SECRET not in str(exc_info.value)
    assert calls == [], "Engine/connection must never be attempted when ambient PG* overrides are set"


def test_ambient_libpq_vars_do_not_block_optional_skip_when_url_unset() -> None:
    calls: list[str] = []
    engine, summary, required = resolve_verified_pg_engine(
        {"PGHOST": "127.0.0.2", "PGUSER": "postgres"},
        engine_factory=lambda url: calls.append(url) or _FakeEngine(),
    )
    assert (engine, summary, required) == (None, None, False)
    assert calls == []


def test_ambient_pghostaddr_in_os_environ_rejected_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var_name in (
        "POSTGRES_TEST_URL",
        "REQUIRE_POSTGRES_TESTS",
        "DATABASE_URL",
        "ENVIRONMENT",
        *AMBIENT_PG_OVERRIDE_ENV_VARS,
    ):
        monkeypatch.delenv(var_name, raising=False)

    monkeypatch.setenv("REQUIRE_POSTGRES_TESTS", "1")
    monkeypatch.setenv(
        "POSTGRES_TEST_URL",
        f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
    )
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.2")

    with pytest.raises(PostgresTestConfigError) as exc_info:
        resolve_verified_pg_engine()

    assert not isinstance(exc_info.value, PostgresTestConnectionError)
    assert "PGHOSTADDR" in str(exc_info.value)
    assert _DUMMY_SECRET not in str(exc_info.value)
    assert os.environ.get("PGHOSTADDR") == "127.0.0.2"

    # Also enforce when caller supplies an explicit environ mapping without custom engine_factory.
    explicit_env = {
        "REQUIRE_POSTGRES_TESTS": "1",
        "POSTGRES_TEST_URL": f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
    }
    with pytest.raises(PostgresTestConfigError, match="PGHOSTADDR"):
        resolve_verified_pg_engine(explicit_env)
    assert os.environ.get("PGHOSTADDR") == "127.0.0.2"


def test_required_mode_validates_database_url_consistency_and_explicit_username() -> None:
    calls: list[str] = []
    valid_pg_url = f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test"
    valid_async_db_url = f"postgresql+asyncpg://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test"

    with pytest.raises(PostgresTestConfigError) as supa_exc:
        resolve_verified_pg_engine(
            {
                "REQUIRE_POSTGRES_TESTS": "1",
                "POSTGRES_TEST_URL": valid_pg_url,
                "DATABASE_URL": f"postgresql+asyncpg://postgres:{_DUMMY_SECRET}@db.exampleproj.supabase.co:5432/postgres",
            },
            engine_factory=lambda url: calls.append(url) or _FakeEngine(),
        )
    assert _DUMMY_SECRET not in str(supa_exc.value)
    assert calls == []

    with pytest.raises(PostgresTestConfigError, match="does not match POSTGRES_TEST_URL"):
        resolve_verified_pg_engine(
            {
                "REQUIRE_POSTGRES_TESTS": "1",
                "POSTGRES_TEST_URL": valid_pg_url,
                "DATABASE_URL": f"postgresql+asyncpg://ci_user:{_DUMMY_SECRET}@localhost:5432/aptlyst_ci_test",
            },
            engine_factory=lambda url: calls.append(url) or _FakeEngine(),
        )
    assert calls == []

    with pytest.raises(PostgresTestConfigError, match="explicit username"):
        validate_postgres_test_url(
            f"postgresql://:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test",
            require_postgres=True,
        )

    fake = _FakeEngine(row=("aptlyst_ci_test", 160004, "PostgreSQL 16.4"))
    eng, summary, req = resolve_verified_pg_engine(
        {
            "REQUIRE_POSTGRES_TESTS": "1",
            "POSTGRES_TEST_URL": valid_pg_url,
            "DATABASE_URL": valid_async_db_url,
        },
        engine_factory=lambda url: calls.append(url) or fake,
    )
    assert eng is fake
    assert summary is not None and "PostgreSQL 16" in summary
    assert req is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "smuggled_url",
    [
        f"postgresql://u:{_DUMMY_SECRET}@evil.com@127.0.0.1:5432/aptlyst_ci_test",
        f"postgresql://u@127.0.0.1:{_DUMMY_SECRET}@evil.com:5432/aptlyst_ci_test",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1%2eevil.com:5432/aptlyst_ci_test",
        f"postgresql://u:{_DUMMY_SECRET}@localhost.:5432/aptlyst_ci_test",
        f"postgresql://u:{_DUMMY_SECRET}@[::2]:5432/aptlyst_ci_test",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test%3fhost%3devil",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test;host=evil.com",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?%68ost=evil.com",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?host%3devil.com",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?sslmode=disable;host=evil.com",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?target_session_attrs=read-write",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432\\@evil.com/aptlyst_ci_test",
        f"postgresql://u:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test?connect_timeout=5#frag",
    ],
)
def test_parser_differential_and_smuggled_urls_rejected_before_connect(smuggled_url: str) -> None:
    calls: list[str] = []
    with pytest.raises(PostgresTestConfigError) as exc_info:
        resolve_verified_pg_engine(
            {"REQUIRE_POSTGRES_TESTS": "1", "POSTGRES_TEST_URL": smuggled_url},
            engine_factory=lambda url: calls.append(url) or _FakeEngine(),
        )
    assert calls == []
    assert _DUMMY_SECRET not in str(exc_info.value)


def test_valid_percent_encoded_at_in_password_and_async_sync_equivalence() -> None:
    raw = "postgresql+psycopg2://ci_user:Pass%40987%21@127.0.0.1:5432/aptlyst_ci_test?sslmode=disable"
    target = validate_postgres_test_url(raw, require_postgres=True)
    assert target is not None
    assert target.host == "127.0.0.1"
    assert target.port == 5432
    assert target.database == "aptlyst_ci_test"
    assert target.sync_url.startswith("postgresql+psycopg2://")
    assert target.async_url.startswith("postgresql+asyncpg://")
    assert normalize_to_async_postgres_url(raw) == target.async_url
    assert normalize_to_sync_postgres_url(target.async_url) == target.sync_url
    assert "Pass%40987%21" not in repr(target)

    ipv6 = validate_postgres_test_url(
        f"postgresql+asyncpg://ci_user:{_DUMMY_SECRET}@[::1]:5432/aptlyst_ci_test",
        require_postgres=True,
    )
    assert ipv6 is not None
    assert ipv6.host == "::1"
    assert ipv6.async_url.startswith("postgresql+asyncpg://")
    assert ipv6.sync_url.startswith("postgresql+psycopg2://")


def test_short_password_redaction_preserves_diagnostics_and_scrubs_secrets() -> None:
    for short_pw in ("p", "db", "!"):
        raw_url = f"postgresql+psycopg2://u:{short_pw}@127.0.0.1:5432/aptlyst_ci_test"
        err_text = (
            f"psycopg2.OperationalError connection on port 5432 failed for "
            f"postgresql+psycopg2://u:{short_pw}@127.0.0.1:5432/aptlyst_ci_test "
            f"with dsn='password={short_pw}' and kwargs={{'password': '{short_pw}', 'passfile': '/tmp/pgpass'}}"
        )
        cleaned = redact_postgres_secrets(err_text, raw_url)
        assert "psycopg2.OperationalError" in cleaned
        assert "connection on port 5432 failed" in cleaned
        assert f":{short_pw}@" not in cleaned
        assert f"password={short_pw}" not in cleaned
        assert f"'{short_pw}'" not in cleaned
        assert "/tmp/pgpass" not in cleaned
        assert "password=<redacted>" in cleaned


def test_assert_junit_zero_skips_rejects_malformed_failures_errors_and_stale_reports(tmp_path: Path) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    passing_cases = "".join(f'<testcase classname="t" name="test_{i}" time="0.01" />' for i in range(14))
    full_valid = tmp_path / "full_valid.xml"
    full_valid.write_text(
        f'<testsuites><testsuite name="pytest" tests="14" skipped="0" failures="0" errors="0" timestamp="{now_iso}">'
        f"{passing_cases}</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert assert_junit_zero_skips(
        full_valid,
        expected_exact_tests=14,
        max_age_seconds=1800,
        require_testcase_elements=True,
    ) == (14, 0, 0, 0)

    # Empty, malformed, unexpected root, unexpected child, nested suite, missing attrs, bad counters
    empty_xml = tmp_path / "empty.xml"
    empty_xml.write_text("", encoding="utf-8")
    with pytest.raises(PostgresTestConfigError, match="is empty"):
        assert_junit_zero_skips(empty_xml, expected_exact_tests=14)

    malformed_xml = tmp_path / "malformed.xml"
    malformed_xml.write_text("<testsuites><testsuite", encoding="utf-8")
    with pytest.raises(PostgresTestConfigError, match="Malformed JUnit XML"):
        assert_junit_zero_skips(malformed_xml, expected_exact_tests=14)

    for bad_name, bad_body in [
        ("wrong_root.xml", "<notjunit />"),
        ("empty_suites.xml", "<testsuites />"),
        ("bad_child.xml", "<testsuites><failure /></testsuites>"),
        ("nested_suite.xml", '<testsuite tests="14"><testsuite tests="0" /></testsuite>'),
        ("missing_tests.xml", '<testsuite name="pytest" skipped="0" />'),
        ("non_int.xml", '<testsuite tests="abc" skipped="0" />'),
        ("negative.xml", '<testsuite tests="14" skipped="-1" />'),
        ("overcount.xml", '<testsuite tests="15" skipped="0" failures="0" errors="0" />'),
        ("attr_failure.xml", '<testsuite tests="14" skipped="0" failures="1" errors="0" />'),
        ("attr_error.xml", '<testsuite tests="14" skipped="0" failures="0" errors="1" />'),
        (
            "child_failure_forged_zero.xml",
            '<testsuite tests="14" skipped="0" failures="0" errors="0">'
            '<testcase name="t1"><failure message="boom" /></testcase></testsuite>',
        ),
        (
            "child_error_forged_zero.xml",
            '<testsuite tests="14" skipped="0" failures="0" errors="0">'
            '<testcase name="t1"><error message="err" /></testcase></testsuite>',
        ),
    ]:
        bad_file = tmp_path / bad_name
        bad_file.write_text(bad_body, encoding="utf-8")
        with pytest.raises(PostgresTestConfigError):
            assert_junit_zero_skips(bad_file, expected_exact_tests=14)

    # Forged summary claiming 14 passed without <testcase> nodes or with mismatched <testcase> count
    no_cases = tmp_path / "no_cases.xml"
    no_cases.write_text(
        '<testsuite name="pytest" tests="14" skipped="0" failures="0" errors="0" />',
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="without any <testcase> elements"):
        assert_junit_zero_skips(no_cases, expected_exact_tests=14, require_testcase_elements=True)

    fewer_cases_body = "".join(f'<testcase name="t_{i}" />' for i in range(13))
    fewer_cases = tmp_path / "fewer_cases.xml"
    fewer_cases.write_text(
        f'<testsuite name="pytest" tests="14" skipped="0" failures="0" errors="0">'
        f"{fewer_cases_body}</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="does not match tests=14"):
        assert_junit_zero_skips(fewer_cases, expected_exact_tests=14, require_testcase_elements=True)

    # Stale mtime on disk fails max_age_seconds and min_mtime
    stale_mtime_xml = tmp_path / "stale_mtime.xml"
    stale_mtime_xml.write_text(full_valid.read_text(encoding="utf-8"), encoding="utf-8")
    old_epoch = time.time() - 3600
    os.utime(stale_mtime_xml, (old_epoch, old_epoch))
    with pytest.raises(PostgresTestConfigError, match="Stale JUnit XML file"):
        assert_junit_zero_skips(stale_mtime_xml, expected_exact_tests=14, max_age_seconds=60)
    with pytest.raises(PostgresTestConfigError, match="predates expected minimum"):
        assert_junit_zero_skips(stale_mtime_xml, expected_exact_tests=14, min_mtime=time.time() - 10)

    # Fresh file mtime carrying stale internal testsuite timestamp still fails closed
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale_ts_xml = tmp_path / "stale_ts.xml"
    stale_ts_xml.write_text(
        f'<testsuites><testsuite name="pytest" tests="14" skipped="0" failures="0" errors="0" timestamp="{old_iso}">'
        f"{passing_cases}</testsuite></testsuites>",
        encoding="utf-8",
    )
    with pytest.raises(PostgresTestConfigError, match="Stale JUnit testsuite timestamp"):
        assert_junit_zero_skips(stale_ts_xml, expected_exact_tests=14, max_age_seconds=60)
    with pytest.raises(PostgresTestConfigError, match="Stale JUnit testsuite timestamp"):
        assert_junit_zero_skips(stale_ts_xml, expected_exact_tests=14, min_mtime=time.time() - 10)


def test_provenance_subprocess_command_pins_isolated_api_cwd_and_no_cache_flags(tmp_path: Path) -> None:
    junit_xml = tmp_path / "cmd_probe.xml"
    cmd, api_root = _build_provenance_pytest_command(junit_xml)

    assert api_root == _API_ROOT.resolve()
    assert api_root.is_dir()
    assert (api_root / "tests" / "test_postgres_provenance_integration.py").is_file()
    assert cmd[0] == sys.executable
    assert "-B" in cmd[: cmd.index("-m")]
    p_index = cmd.index("-p")
    assert cmd[p_index + 1] == "no:cacheprovider"
    assert str(api_root / "pytest.ini") in cmd
    assert str(api_root) in cmd
    assert "tests/test_postgres_provenance_integration.py" in cmd
    assert f"--junitxml={junit_xml}" in cmd


def test_provenance_subprocess_timeout_terminates_child_and_reports_diagnostics(tmp_path: Path) -> None:
    leaked_secret_url = f"postgresql+psycopg2://ci_user:{_DUMMY_SECRET}@127.0.0.1:5432/aptlyst_ci_test"
    child_script = (
        "import sys, time; "
        "sys.stdout.write('partial_stdout_marker\\n'); "
        "sys.stdout.flush(); "
        f"sys.stderr.write('partial_stderr_marker {leaked_secret_url}\\n'); "
        "sys.stderr.flush(); "
        "time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception) as exc_info:
        _execute_subprocess_with_timeout(
            [sys.executable, "-B", "-c", child_script],
            cwd=_API_ROOT.resolve(),
            env=os.environ.copy(),
            timeout_seconds=0.5,
            secret_url=leaked_secret_url,
        )
    elapsed = time.monotonic() - started

    failure_msg = str(exc_info.value)
    assert elapsed < 10.0, f"Child process termination took unexpectedly long: {elapsed:.2f}s"
    assert "timed out after 0.50s" in failure_msg
    assert str(_API_ROOT.resolve()) in failure_msg
    assert "command:" in failure_msg
    assert "partial_stdout_marker" in failure_msg
    assert "partial_stderr_marker" in failure_msg
    assert _DUMMY_SECRET not in failure_msg
    assert "<redacted>" in failure_msg
