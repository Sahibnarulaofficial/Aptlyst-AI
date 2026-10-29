"""Verification tests for database table exposure, RLS, and permissions.

NOTE: All tests in this file connect directly to the real PostgreSQL database configured
in `DATABASE_URL` (the designated development/test Supabase database). They verify
actual database-level constraints, role attributes, Row Level Security (RLS) state,
and PostgreSQL privilege catalogs (`pg_roles`, `pg_tables`, and `information_schema.table_privileges`).
"""

import os
import pytest
from sqlalchemy import text

from app.db.session import AsyncSessionLocal


RUN_INTEGRATION_TESTS = os.getenv("RUN_SUPABASE_INTEGRATION_TESTS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

pytestmark = [
    pytest.mark.skipif(
        not RUN_INTEGRATION_TESTS,
        reason=(
            "PostgreSQL security catalog tests are disabled. "
            "Set RUN_SUPABASE_INTEGRATION_TESTS=1 and configure test environment variables to enable."
        ),
    ),
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_database_connection_role_bypasses_rls() -> None:
    """REAL DATABASE TEST:
    Verifies that the FastAPI backend connects as a role with rolbypassrls=True
    so backend queries succeed without interruption when RLS is active.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT 
                    current_user,
                    (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) as bypasses_rls
                """
            )
        )
        row = result.one()
        current_user, bypasses_rls = row[0], row[1]
        assert current_user == "postgres", f"Expected backend user 'postgres', got '{current_user}'"
        assert bypasses_rls is True, "Backend role must have rolbypassrls=True to safely manage RLS-enabled tables"


@pytest.mark.anyio
async def test_tables_have_rls_enabled() -> None:
    """REAL DATABASE TEST:
    Verifies that 'organizations', 'organization_members', and 'chatbots' have rowsecurity=True in pg_tables.
    Validates that direct PostgREST client queries are subject to RLS policy enforcement.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT tablename, rowsecurity
                FROM pg_tables
                WHERE schemaname = 'public' AND tablename IN ('organizations', 'organization_members', 'chatbots')
                ORDER BY tablename;
                """
            )
        )
        rows = dict(result.all())
        assert "organizations" in rows, "Table 'organizations' was not found in schema 'public'"
        assert "organization_members" in rows, "Table 'organization_members' was not found in schema 'public'"
        assert "chatbots" in rows, "Table 'chatbots' was not found in schema 'public'"
        assert rows["organizations"] is True, "Table 'organizations' must have RLS enabled (rowsecurity=True)"
        assert rows["organization_members"] is True, "Table 'organization_members' must have RLS enabled (rowsecurity=True)"
        assert rows["chatbots"] is True, "Table 'chatbots' must have RLS enabled (rowsecurity=True)"


@pytest.mark.anyio
async def test_untrusted_roles_have_no_direct_table_privileges() -> None:
    """REAL DATABASE TEST:
    Verifies that untrusted client roles ('anon', 'authenticated') have zero direct table privileges
    (SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER) on Aptlyst tables.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.table_privileges
                WHERE table_schema = 'public' 
                  AND table_name IN ('organizations', 'organization_members', 'chatbots')
                  AND grantee IN ('anon', 'authenticated')
                ORDER BY table_name, grantee, privilege_type;
                """
            )
        )
        privileges = result.all()
        assert len(privileges) == 0, (
            f"Expected 0 privileges for untrusted roles (anon, authenticated) on Aptlyst tables, "
            f"but found {len(privileges)}: {privileges}"
        )
