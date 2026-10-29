"""Comprehensive Integration Test Suite for Supabase-Hosted PostgreSQL Architecture.

NON-NEGOTIABLE SAFETY CONSTRAINTS:
1. Fail-Closed Opt-In:
   - This suite NEVER executes during standard local testing or CI unit test runs.
   - Requires explicit opt-in: RUN_SUPABASE_INTEGRATION_TESTS=1.
   - If unset or 0, all tests in this file are skipped immediately during collection.
2. Preflight Target Verification:
   - Requires EXPECTED_SUPABASE_HOST or EXPECTED_SUPABASE_PROJECT_REF matching DATABASE_URL hostname.
   - Requires EXPECTED_ENVIRONMENT matching settings.environment (must be 'development', 'staging', or 'test').
   - Rejects missing configuration, target mismatches, or production environments fail-closed.
   - Verifies target database current_user ('postgres'), alembic_version ('c3d4e5f6a1b2'),
     and table schemas before any test writes occur.
   - NEVER prints connection credentials, JWT secrets, service-role keys, or database passwords.
3. Isolated Multi-Tenant Partitioning:
   - Generates a unique test organization (UUID) and child records for every test run.
   - Never modifies or reads non-test records.
4. Deterministic Cleanup:
   - Cleans up all test-created rows in strict reverse foreign-key order respecting ON DELETE RESTRICT.
   - Cleanup failures are logged and raised explicitly without suppression.
5. Zero Live AI Calls:
   - Inference uses MockLLMProvider only; never contacts external AI endpoints.

PostgreSQL Engine Behaviors Verified by this Suite:
1. Row Locks (SELECT ... FOR UPDATE): Verifies blocking and non-conflicting serial acquisition.
2. Quota Concurrency: Verifies atomic quota exhaustion under simultaneous reservation requests.
3. Partial Unique Indexes: Verifies engine enforcement of uq_attempt_task_number,
   uq_attempt_task_lease, and uq_one_active_reservation_per_task.
4. Transaction Rollback & InFailedSqlTransaction: Verifies clean recovery after aborted operations.
5. Settlement & Reply Persistence Atomicity: Verifies multi-table commit in Transaction 2.
6. Ambiguity Quarantine: Verifies committed dispatched attempts with expired leases are quarantined
   to reconciliation_needed rather than redispatched.
7. Foreign-Key Retention Policy: Verifies ON DELETE SET NULL on attempts and ON DELETE RESTRICT on billing.
8. Row-Level Security (RLS) & Grants: Verifies rowsecurity=True and zero client table privileges.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any
from urllib.parse import urlparse
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSenderType
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.conversation import GenerationStatus
from app.services.billing.accounting import (
    AccountingErrorCode,
    reserve_usage,
    settle_reservation,
)
from app.services.billing.pricing import MOCK_GPT_4O_MINI_PRICING
from app.services.llm.base import BaseLLMProvider, LLMResponse, LLMUsage
from app.services.llm.mock import MockLLMProvider
from app.services.llm.orchestrator import (
    OrchestratorErrorCode,
    orchestrate_assistant_reply,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 1. Fail-Closed Opt-In & Module Skip Guard
# ============================================================================

RUN_INTEGRATION_TESTS = os.getenv("RUN_SUPABASE_INTEGRATION_TESTS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

pytestmark = [
    pytest.mark.skipif(
        not RUN_INTEGRATION_TESTS,
        reason=(
            "Supabase PostgreSQL integration tests are disabled. "
            "Set RUN_SUPABASE_INTEGRATION_TESTS=1 and configure test environment variables to enable."
        ),
    ),
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ============================================================================
# 2. Preflight Target Database & Revision Verification
# ============================================================================


async def run_fail_closed_preflight_checks() -> None:
    """Validate database host, environment, credentials, and schema before any test writes.

    Fails immediately if target is not allowlisted, if target is production, if Alembic
    revision is not at head, or if required tables and constraints are absent.
    NEVER logs or displays connection strings, passwords, or secret tokens.
    """
    # 1. Expected Allowlisted Project Host / Reference Verification
    expected_host = os.getenv("EXPECTED_SUPABASE_HOST", "").strip().lower()
    expected_ref = os.getenv("EXPECTED_SUPABASE_PROJECT_REF", "").strip().lower()
    if not expected_host and not expected_ref:
        raise pytest.UsageError(
            "Preflight failed: Either EXPECTED_SUPABASE_HOST or EXPECTED_SUPABASE_PROJECT_REF "
            "must be set to allowlist the integration test database target."
        )

    db_url_str = str(settings.database_url)
    try:
        parsed_url = urlparse(db_url_str)
        actual_host = (parsed_url.hostname or "").strip().lower()
        actual_user = (parsed_url.username or "").strip().lower()
    except Exception as exc:
        raise pytest.UsageError(f"Preflight failed: Could not parse database URL: {exc}") from None

    if expected_host and actual_host != expected_host:
        raise pytest.UsageError(
            f"Preflight failed: DATABASE_URL host '{actual_host}' does not exactly match "
            f"allowlisted EXPECTED_SUPABASE_HOST '{expected_host}'."
        )

    if expected_ref:
        # Determine project identity unambiguously from connection parameters.
        # Do not use weak substring checks or infer identity from shared pooler hostnames.
        actual_project_ref: str | None = None

        if "pooler.supabase.com" in actual_host:
            # Format B: Supabase Pooled connection (e.g. *.pooler.supabase.com)
            # Username must strictly be '<role>.<project_ref>' (e.g. 'postgres.abcdefghijklmno')
            if "." in actual_user:
                _, user_project_ref = actual_user.split(".", 1)
                actual_project_ref = user_project_ref.strip()
            else:
                raise pytest.UsageError(
                    f"Preflight failed: Target host '{actual_host}' is a shared Supabase pooler, "
                    f"but username '{actual_user}' does not follow the required '<role>.<project_ref>' format."
                )
        elif actual_host.endswith(".supabase.co"):
            # Format A: Direct Supabase connection (e.g. 'db.<project_ref>.supabase.co')
            subdomain = actual_host[:-len(".supabase.co")]
            if subdomain.startswith("db."):
                actual_project_ref = subdomain[3:].strip()
            else:
                actual_project_ref = subdomain.strip()
        elif "." in actual_user and actual_user.split(".", 1)[1].strip() == expected_ref:
            # Custom proxy host where username specifies the project ref
            actual_project_ref = actual_user.split(".", 1)[1].strip()

        if not actual_project_ref:
            raise pytest.UsageError(
                f"Preflight failed: Could not unambiguously establish Supabase project reference from "
                f"host '{actual_host}' or username '{actual_user}'. Failing closed."
            )

        if actual_project_ref != expected_ref:
            raise pytest.UsageError(
                f"Preflight failed: Discovered project reference '{actual_project_ref}' does not match "
                f"allowlisted EXPECTED_SUPABASE_PROJECT_REF '{expected_ref}'."
            )

    # 2. Environment Mode Verification (Strict Non-Production Assertion)
    expected_env = os.getenv("EXPECTED_ENVIRONMENT", "").strip().lower()
    current_env = settings.environment.strip().lower()
    if not expected_env or expected_env in ("production", "prod"):
        raise pytest.UsageError(
            "Preflight failed: EXPECTED_ENVIRONMENT must be set to a non-production environment "
            "(e.g., 'staging', 'development', or 'test')."
        )
    if current_env != expected_env or current_env in ("production", "prod"):
        raise pytest.UsageError(
            f"Preflight failed: Application environment '{current_env}' is invalid or does not "
            f"match expected test environment '{expected_env}'."
        )

    # 3. Target Database Connection & Schema Preflight
    async with AsyncSessionLocal() as session:
        # Check current user and role attributes
        res_user = await session.execute(
            text("SELECT current_user, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user);")
        )
        row_user = res_user.one_or_none()
        if not row_user:
            raise RuntimeError("Preflight failed: Unable to query current_user from PostgreSQL.")
        current_user, bypasses_rls = row_user[0], row_user[1]
        if current_user != "postgres" or not bypasses_rls:
            raise RuntimeError(
                f"Preflight failed: Backend user must be 'postgres' with rolbypassrls=True, "
                f"got user='{current_user}', rolbypassrls={bypasses_rls}."
            )

        # Check Alembic Migration Head
        res_alembic = await session.execute(text("SELECT version_num FROM alembic_version;"))
        current_rev = res_alembic.scalar_one_or_none()
        expected_head = "c3d4e5f6a1b2"
        if current_rev != expected_head:
            raise RuntimeError(
                f"Preflight failed: Database migration mismatch. Current revision is '{current_rev}', "
                f"expected head revision is '{expected_head}'. Run 'alembic upgrade head' first."
            )

        # Check required schema tables
        res_tables = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename IN ("
                "'organizations', 'organization_members', 'chatbots', 'conversations', 'messages', "
                "'organization_billing_periods', 'generation_tasks', 'generation_task_attempts', "
                "'usage_reservations', 'llm_usage_logs', 'subscriptions', 'stripe_webhook_events'"
                ");"
            )
        )
        found_tables = {r[0] for r in res_tables.all()}
        required_tables = {
            "organizations",
            "organization_members",
            "chatbots",
            "conversations",
            "messages",
            "organization_billing_periods",
            "generation_tasks",
            "generation_task_attempts",
            "usage_reservations",
            "llm_usage_logs",
            "subscriptions",
            "stripe_webhook_events",
        }
        missing_tables = required_tables - found_tables
        if missing_tables:
            raise RuntimeError(
                f"Preflight failed: Database is missing required tables: {sorted(missing_tables)}"
            )

        # Check required partial unique indexes
        res_indexes = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname IN ("
                "'uq_attempt_task_number', 'uq_attempt_task_lease', 'uq_org_active_billing_period', "
                "'uq_task_active_reservation', 'uq_messages_conversation_client_id'"
                ");"
            )
        )
        found_indexes = {r[0] for r in res_indexes.all()}
        required_indexes = {
            "uq_attempt_task_number",
            "uq_attempt_task_lease",
            "uq_org_active_billing_period",
            "uq_task_active_reservation",
            "uq_messages_conversation_client_id",
        }
        missing_indexes = required_indexes - found_indexes
        if missing_indexes:
            raise RuntimeError(
                f"Preflight failed: Database is missing required unique indexes: {sorted(missing_indexes)}"
            )


@pytest.fixture(autouse=True)
async def preflight_safety_guard() -> None:
    """Execute fail-closed preflight safety guard before running any test in this session."""
    if RUN_INTEGRATION_TESTS:
        await run_fail_closed_preflight_checks()


# ============================================================================
# 3. Test Tenant Fixture & Strict Foreign-Key Ordered Cleanup
# ============================================================================


@pytest.fixture
async def test_tenant() -> AsyncGenerator[dict[str, Any], None]:
    """Provide an isolated, unique test tenant and perform deterministic reverse-FK cleanup.

    Cleanup Order Respecting ON DELETE RESTRICT:
    1. llm_usage_logs (restricted by billing_period_id & organization_id)
    2. usage_reservations (restricted by billing_period_id, organization_id, generation_task_id)
    3. generation_task_attempts (references generation_tasks)
    4. generation_tasks (restricted by billing_period_id & organization_id)
    5. messages (cascades from conversations)
    6. conversations (cascades from chatbots & organizations)
    7. chatbots (cascades from organizations)
    8. subscriptions (restricted by organization_id)
    9. organization_billing_periods (restricted by organization_id)
    10. organization_members (cascades from organizations)
    11. organizations (root)
    """
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    msg_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        # Create Root Organization
        org = Organization(id=org_id, name=f"Test-Tenant-{org_id.hex[:8]}")
        session.add(org)

        # Create Member (simulated user_id; does not require Supabase Auth user creation)
        member = OrganizationMember(
            organization_id=org_id,
            user_id=user_id,
            role=MemberRole.owner,
        )
        session.add(member)

        # Create Active Billing Period
        bp = OrganizationBillingPeriod(
            id=bp_id,
            organization_id=org_id,
            period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=29),
            monthly_token_quota=100_000,
            tokens_used=0,
            tokens_reserved=0,
            hard_spending_cap_micros=1_000_000,  # $1.00 USD
            cost_micros_used=0,
            cost_micros_reserved=0,
            is_active=True,
            is_blocked=False,
        )
        session.add(bp)

        # Create Chatbot
        bot = Chatbot(
            id=bot_id,
            organization_id=org_id,
            name="Supabase-TestBot",
            instructions="You are a helpful customer assistant.",
            status=ChatbotStatus.active,
        )
        session.add(bot)

        # Create Conversation
        conv = Conversation(
            id=conv_id,
            organization_id=org_id,
            chatbot_id=bot_id,
            visitor_id=str(uuid.uuid4()),
            status=ConversationStatus.active,
        )
        session.add(conv)

        # Create Visitor Message
        msg = Message(
            id=msg_id,
            conversation_id=conv_id,
            sender_type=MessageSenderType.visitor,
            content="Hello Supabase Integration!",
            client_message_id=f"vis_{uuid.uuid4().hex[:10]}",
        )
        session.add(msg)

        await session.commit()

    tenant_data: dict[str, Any] = {
        "org_id": org_id,
        "user_id": user_id,
        "bot_id": bot_id,
        "conv_id": conv_id,
        "bp_id": bp_id,
        "msg_id": msg_id,
        "now": now,
        "attempt_ids": set(),
    }

    try:
        yield tenant_data
    finally:
        # Deterministic reverse-FK cleanup sequence
        async with AsyncSessionLocal() as cleanup_session:
            try:
                # 1. llm_usage_logs
                await cleanup_session.execute(
                    delete(LLMUsageLog).where(LLMUsageLog.organization_id == org_id)
                )
                # 2. usage_reservations
                await cleanup_session.execute(
                    delete(UsageReservation).where(UsageReservation.organization_id == org_id)
                )
                # 3. generation_task_attempts
                # Delete attempts attached to the org's generation tasks
                await cleanup_session.execute(
                    delete(GenerationTaskAttempt).where(
                        GenerationTaskAttempt.generation_task_id.in_(
                            select(GenerationTask.id).where(GenerationTask.organization_id == org_id)
                        )
                    )
                )
                # Also delete any attempts explicitly registered by ID (e.g. if task was deleted and FK became NULL)
                if tenant_data.get("attempt_ids"):
                    await cleanup_session.execute(
                        delete(GenerationTaskAttempt).where(
                            GenerationTaskAttempt.id.in_(tenant_data["attempt_ids"])
                        )
                    )
                # 4. generation_tasks
                await cleanup_session.execute(
                    delete(GenerationTask).where(GenerationTask.organization_id == org_id)
                )
                # 5. messages
                await cleanup_session.execute(
                    delete(Message).where(
                        Message.conversation_id.in_(
                            select(Conversation.id).where(Conversation.organization_id == org_id)
                        )
                    )
                )
                # 6. conversations
                await cleanup_session.execute(
                    delete(Conversation).where(Conversation.organization_id == org_id)
                )
                # 7. chatbots
                await cleanup_session.execute(
                    delete(Chatbot).where(Chatbot.organization_id == org_id)
                )
                # 8. subscriptions
                await cleanup_session.execute(
                    delete(Subscription).where(Subscription.organization_id == org_id)
                )
                # 9. organization_billing_periods
                await cleanup_session.execute(
                    delete(OrganizationBillingPeriod).where(
                        OrganizationBillingPeriod.organization_id == org_id
                    )
                )
                # 10. organization_members
                await cleanup_session.execute(
                    delete(OrganizationMember).where(OrganizationMember.organization_id == org_id)
                )
                # 11. organizations
                await cleanup_session.execute(
                    delete(Organization).where(Organization.id == org_id)
                )
                await cleanup_session.commit()
            except Exception as cleanup_err:
                await cleanup_session.rollback()
                logger.critical(
                    "CRITICAL: Failed to clean up test tenant %s from database: %s",
                    org_id,
                    cleanup_err,
                )
                raise RuntimeError(
                    f"Test tenant cleanup failed for org {org_id}: {cleanup_err}"
                ) from cleanup_err


# ============================================================================
# 4. Integration Test Cases
# ============================================================================


async def test_supabase_concurrent_task_acquisition_row_locking(test_tenant: dict[str, Any]) -> None:
    """Verifies that two concurrent worker sessions serialize on task acquisition via row lock and unique index.

    Guarantees:
    1. Independent Sessions: Worker A and Worker B operate in separate AsyncSession instances.
    2. Zero Deadlock on Runner: Neither worker holds an uncommitted lock while waiting on the other sequentially.
    3. Conflict Handling: Catches both IntegrityError (unique violation) and DBAPIError (e.g. DeadlockDetected
       under high concurrency), safely rolling back the failed transaction block before reloading.
    4. Invariant: Exactly one active task is created in the database, with no duplicate or unsafe redispatch.
    """
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    org_id = test_tenant["org_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    worker_a_token = uuid.uuid4()
    worker_b_token = uuid.uuid4()

    start_gate = asyncio.Event()

    # Coroutine simulating concurrent worker task acquisition logic
    async def acquire_or_create(worker_token: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, datetime, bool]:
        await start_gate.wait()
        async with AsyncSessionLocal() as session:
            stmt = (
                select(GenerationTask)
                .where(GenerationTask.visitor_message_id == msg_id)
                .with_for_update()
            )
            task = (await session.execute(stmt)).scalar_one_or_none()
            created = False
            if task is None:
                task = GenerationTask(
                    id=uuid.uuid4(),
                    conversation_id=conv_id,
                    visitor_message_id=msg_id,
                    organization_id=org_id,
                    billing_period_id=bp_id,
                    status=GenerationTaskStatus.pending,
                    lease_token=worker_token,
                    lease_version=1,
                    lease_expires_at=now + timedelta(seconds=60),
                    attempts=0,
                    max_attempts=3,
                )
                session.add(task)
                try:
                    await session.commit()
                    created = True
                except (IntegrityError, DBAPIError):
                    # Concurrently created by racing worker or deadlock detected;
                    # safely clear aborted transaction block and reload winner's task under FOR UPDATE
                    await session.rollback()
                    res_winner = await session.execute(stmt)
                    task = res_winner.scalar_one_or_none()
                    assert task is not None
            # Extract attributes before session closes
            assert task is not None
            task_id = task.id
            lease_token = task.lease_token
            lease_expires_at = task.lease_expires_at
            return (task_id, lease_token, lease_expires_at, created)

    # Launch both workers simultaneously via start_gate and asyncio.gather
    worker_task_a = asyncio.create_task(acquire_or_create(worker_a_token))
    worker_task_b = asyncio.create_task(acquire_or_create(worker_b_token))
    start_gate.set()

    (task_id_a, lease_a, exp_a, created_a), (task_id_b, lease_b, exp_b, created_b) = await asyncio.gather(
        worker_task_a,
        worker_task_b,
    )

    # Invariant: Exactly one worker created the task; both resolve to the identical task ID
    assert (created_a and not created_b) or (created_b and not created_a)
    assert task_id_a == task_id_b
    winner_token = worker_a_token if created_a else worker_b_token
    assert lease_a == winner_token
    assert exp_a > now

    # Verify physical database state in a fresh session
    async with AsyncSessionLocal() as session:
        persisted_tasks = (
            await session.scalars(
                select(GenerationTask).where(GenerationTask.visitor_message_id == msg_id)
            )
        ).all()
        assert len(persisted_tasks) == 1, "Must never create duplicate generation tasks for a single visitor message"
        persisted_task = persisted_tasks[0]
        assert persisted_task.id == task_id_a
        assert persisted_task.lease_token == winner_token
        assert persisted_task.status == GenerationTaskStatus.pending
        assert persisted_task.attempts == 0


async def test_supabase_concurrent_quota_reservation_atomicity(test_tenant: dict[str, Any]) -> None:
    """Verifies that simultaneous reservations against a tight one-turn quota serialize atomically.

    Guarantees:
    1. Both workers invoke the actual accounting service (reserve_usage) with independent sessions
       against the same billing period.
    2. Exact One-Turn Capacity: monthly_token_quota is configured to the exact maximum token demand
       of a single turn under MOCK_GPT_4O_MINI_PRICING (max_input + max_output = 5,000 tokens).
    3. Explicit Coordination: An asyncio.Event start gate ensures simultaneous worker release and overlapping execution.
    4. Quota Invariant: Exactly one reservation succeeds and commits, while the other is rejected with QUOTA_EXHAUSTED.
    """
    org_id = test_tenant["org_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    one_turn_tokens = (
        MOCK_GPT_4O_MINI_PRICING.max_input_tokens
        + MOCK_GPT_4O_MINI_PRICING.max_output_tokens
    )

    # Configure billing period with quota sufficient for EXACTLY ONE turn (5,000 tokens)
    async with AsyncSessionLocal() as session:
        bp = await session.get(OrganizationBillingPeriod, bp_id)
        assert bp is not None
        bp.monthly_token_quota = one_turn_tokens
        bp.tokens_used = 0
        bp.tokens_reserved = 0
        await session.commit()

    # Pre-create two distinct tasks
    task1_id = uuid.uuid4()
    task2_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        t1 = GenerationTask(
            id=task1_id,
            conversation_id=test_tenant["conv_id"],
            visitor_message_id=test_tenant["msg_id"],
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=0,
            max_attempts=3,
        )
        session.add(t1)
        await session.commit()

    # Create a distinct visitor message and task 2
    msg2_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        m2 = Message(
            id=msg2_id,
            conversation_id=test_tenant["conv_id"],
            sender_type=MessageSenderType.visitor,
            content="Task 2 message",
            client_message_id=f"vis_{uuid.uuid4().hex[:10]}",
        )
        session.add(m2)
        t2 = GenerationTask(
            id=task2_id,
            conversation_id=test_tenant["conv_id"],
            visitor_message_id=msg2_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=0,
            max_attempts=3,
        )
        session.add(t2)
        await session.commit()

    start_gate = asyncio.Event()

    # Run reservations concurrently in separate sessions via asyncio.gather and start_gate
    async def do_reserve(task_id: uuid.UUID) -> Any:
        await start_gate.wait()
        async with AsyncSessionLocal() as session:
            result = await reserve_usage(
                session,
                organization_id=org_id,
                task_id=task_id,
                billing_period_id=bp_id,
                pricing=MOCK_GPT_4O_MINI_PRICING,
                now=now,
            )
            if result.success:
                await session.commit()
            else:
                await session.rollback()
            return result

    worker_a = asyncio.create_task(do_reserve(task1_id))
    worker_b = asyncio.create_task(do_reserve(task2_id))
    start_gate.set()

    res_a, res_b = await asyncio.gather(worker_a, worker_b)

    # Exactly one reservation succeeded under row lock serialization, and the second was rejected
    results = [res_a, res_b]
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    assert len(successes) == 1, "Exactly one reservation must succeed under tight quota capacity"
    assert len(failures) == 1, "The competing concurrent reservation must fail"
    assert failures[0].error_code == AccountingErrorCode.QUOTA_EXHAUSTED

    # Verify billing period balances and reservation records in fresh session
    async with AsyncSessionLocal() as session:
        bp_final = await session.get(OrganizationBillingPeriod, bp_id)
        assert bp_final is not None
        assert bp_final.tokens_reserved == one_turn_tokens
        assert bp_final.tokens_used == 0

        res_rows = (
            await session.scalars(
                select(UsageReservation).where(UsageReservation.organization_id == org_id)
            )
        ).all()
        assert len(res_rows) == 1
        assert res_rows[0].status == ReservationStatus.active
        assert res_rows[0].tokens_reserved == one_turn_tokens


async def test_supabase_partial_unique_indexes_enforcement(test_tenant: dict[str, Any]) -> None:
    """Verifies that PostgreSQL physically enforces partial unique indexes."""
    org_id = test_tenant["org_id"]
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    task_id = uuid.uuid4()
    worker_token = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        task = GenerationTask(
            id=task_id,
            conversation_id=conv_id,
            visitor_message_id=msg_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=worker_token,
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=1,
            max_attempts=3,
        )
        session.add(task)

        # 1. Create Attempt 1
        att1_id = uuid.uuid4()
        test_tenant["attempt_ids"].add(att1_id)
        att1 = GenerationTaskAttempt(
            id=att1_id,
            generation_task_id=task_id,
            attempt_number=1,
            lease_token=worker_token,
            status=GenerationAttemptStatus.claimed,
            provider="mock",
            model="mock-gpt-4o-mini",
        )
        session.add(att1)
        await session.commit()

    # Part A: Duplicate attempt_number must violate uq_attempt_task_number
    async with AsyncSessionLocal() as session:
        duplicate_num_att = GenerationTaskAttempt(
            id=uuid.uuid4(),
            generation_task_id=task_id,
            attempt_number=1,  # Duplicate!
            lease_token=uuid.uuid4(),
            status=GenerationAttemptStatus.claimed,
            provider="mock",
            model="mock-gpt-4o-mini",
        )
        session.add(duplicate_num_att)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "uq_attempt_task_number" in str(exc_info.value).lower()

    # Part B: Duplicate lease_token must violate uq_attempt_task_lease
    async with AsyncSessionLocal() as session:
        duplicate_lease_att = GenerationTaskAttempt(
            id=uuid.uuid4(),
            generation_task_id=task_id,
            attempt_number=2,
            lease_token=worker_token,  # Duplicate lease token for this task!
            status=GenerationAttemptStatus.claimed,
            provider="mock",
            model="mock-gpt-4o-mini",
        )
        session.add(duplicate_lease_att)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "uq_attempt_task_lease" in str(exc_info.value).lower()

    # Part C: Single active reservation index (uq_one_active_reservation_per_task)
    async with AsyncSessionLocal() as session:
        res1 = UsageReservation(
            id=uuid.uuid4(),
            organization_id=org_id,
            billing_period_id=bp_id,
            generation_task_id=task_id,
            lease_token=uuid.uuid4(),
            tokens_reserved=1000,
            cost_micros_reserved=150,
            status=ReservationStatus.active,
        )
        session.add(res1)
        await session.commit()

    async with AsyncSessionLocal() as session:
        res2 = UsageReservation(
            id=uuid.uuid4(),
            organization_id=org_id,
            billing_period_id=bp_id,
            generation_task_id=task_id,
            lease_token=uuid.uuid4(),
            tokens_reserved=1000,
            cost_micros_reserved=150,
            status=ReservationStatus.active,  # Duplicate active reservation!
        )
        session.add(res2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "uq_task_active_reservation" in str(exc_info.value).lower()


async def test_supabase_aborted_transaction_rollback_and_reconciliation_recovery(
    test_tenant: dict[str, Any],
) -> None:
    """Verifies that an aborted transaction block is cleared by rollback, permitting recovery commit."""
    org_id = test_tenant["org_id"]
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    task_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        task = GenerationTask(
            id=task_id,
            conversation_id=conv_id,
            visitor_message_id=msg_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=1,
            max_attempts=3,
        )
        session.add(task)
        await session.commit()

    async with AsyncSessionLocal() as session:
        # Trigger an intentional unique violation
        t_dup = GenerationTask(
            id=uuid.uuid4(),
            conversation_id=conv_id,
            visitor_message_id=msg_id,  # Collides on visitor_message_id
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=0,
            max_attempts=3,
        )
        session.add(t_dup)
        with pytest.raises(IntegrityError):
            await session.flush()

        # At this point, PostgreSQL transaction block is in aborted state
        # Executing rollback clears the aborted transaction
        await session.rollback()

        # In the same session, re-acquire task and commit reconciliation_needed quarantine
        t_clean = await session.get(GenerationTask, task_id)
        assert t_clean is not None
        t_clean.status = GenerationTaskStatus.reconciliation_needed
        t_clean.last_error = "Quarantined following aborted transaction recovery"
        await session.commit()

    # Verify committed state in fresh session
    async with AsyncSessionLocal() as session:
        t_verified = await session.get(GenerationTask, task_id)
        assert t_verified is not None
        assert t_verified.status == GenerationTaskStatus.reconciliation_needed


async def test_supabase_settlement_and_reply_persistence_atomicity(
    test_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that settlement, usage logging, reply persistence, and completion commit atomically."""
    monkeypatch.setattr(settings, "llm_provider", "mock")
    org_id = test_tenant["org_id"]
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    now = test_tenant["now"]

    provider = MockLLMProvider(canned_response="Real Supabase PostgreSQL Reply")

    async with AsyncSessionLocal() as session:
        bot = await session.get(Chatbot, test_tenant["bot_id"])
        msg = await session.get(Message, msg_id)
        assert bot is not None and msg is not None

        result = await orchestrate_assistant_reply(
            db=session,
            chatbot=bot,
            conversation_id=conv_id,
            visitor_message=msg,
            provider=provider,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )

    assert result.status == GenerationStatus.completed
    assert result.reply_message is not None
    assert result.reply_message.content == "Real Supabase PostgreSQL Reply"
    assert result.reply_message.client_message_id == f"reply:{msg_id}"
    assert result.tokens_used > 0
    assert result.cost_micros > 0

    # Verify physical database records across all 5 tables
    async with AsyncSessionLocal() as session:
        # 1. Message persisted
        persisted_reply = await session.scalar(
            select(Message).where(Message.client_message_id == f"reply:{msg_id}")
        )
        assert persisted_reply is not None
        assert persisted_reply.content == "Real Supabase PostgreSQL Reply"

        # 2. GenerationTask completed
        task = await session.scalar(
            select(GenerationTask).where(GenerationTask.visitor_message_id == msg_id)
        )
        assert task is not None
        assert task.status == GenerationTaskStatus.completed

        # 3. GenerationTaskAttempt completed
        attempt = await session.scalar(
            select(GenerationTaskAttempt).where(
                GenerationTaskAttempt.generation_task_id == task.id
            )
        )
        assert attempt is not None
        assert attempt.status == GenerationAttemptStatus.completed
        assert attempt.response_content == "Real Supabase PostgreSQL Reply"

        # 4. UsageReservation settled
        res = await session.scalar(
            select(UsageReservation).where(UsageReservation.generation_task_id == task.id)
        )
        assert res is not None
        assert res.status == ReservationStatus.settled

        # 5. LLMUsageLog created
        log = await session.scalar(
            select(LLMUsageLog).where(LLMUsageLog.generation_task_id == task.id)
        )
        assert log is not None
        assert log.total_tokens == result.tokens_used
        assert log.cost_micros == result.cost_micros


class ExceedingUsageMockProvider(BaseLLMProvider):
    """Mock provider simulating an LLM completion whose token usage exceeds the reserved capacity."""

    def __init__(self, model_name: str = "mock-gpt-4o-mini"):
        self.model_name = model_name
        self.call_count = 0

    async def generate_reply(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            content="Valid response but usage exceeded reservation limits",
            model=self.model_name,
            usage=LLMUsage(
                prompt_tokens=4000,
                completion_tokens=2000,  # 4000 + 2000 = 6000 > 5000 max_tokens_reserved
                total_tokens=6000,
            ),
            finish_reason="stop",
        )


async def test_supabase_settlement_failure_atomicity_and_quarantine(
    test_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that when post-inference settlement fails, all Transaction 2 mutations abort atomically.

    Guarantees:
    1. Reply Message is NOT persisted (never exposes unbilled or uncommitted replies).
    2. LLMUsageLog is NOT persisted (financial usage logs remain clean).
    3. UsageReservation liability is PRESERVED as active (never silently released as free).
    4. GenerationTask is quarantined to reconciliation_needed.
    5. GenerationTaskAttempt preserves provider output and token breakdown for auditing.
    """
    monkeypatch.setattr(settings, "llm_provider", "mock")
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    now = test_tenant["now"]

    provider = ExceedingUsageMockProvider()

    async with AsyncSessionLocal() as session:
        bot = await session.get(Chatbot, test_tenant["bot_id"])
        msg = await session.get(Message, msg_id)
        assert bot is not None and msg is not None

        result = await orchestrate_assistant_reply(
            db=session,
            chatbot=bot,
            conversation_id=conv_id,
            visitor_message=msg,
            provider=provider,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )

    # Invariant: Orchestrator reports failed status with SETTLEMENT_FAILED
    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SETTLEMENT_FAILED
    assert provider.call_count == 1

    # Verify physical database state in a fresh session
    async with AsyncSessionLocal() as session:
        # 1. Reply message was NOT persisted
        persisted_reply = await session.scalar(
            select(Message).where(Message.client_message_id == f"reply:{msg_id}")
        )
        assert persisted_reply is None, "Failed settlement must never commit the assistant reply message"

        # 2. GenerationTask quarantined to reconciliation_needed
        task = await session.scalar(
            select(GenerationTask).where(GenerationTask.visitor_message_id == msg_id)
        )
        assert task is not None
        assert task.status == GenerationTaskStatus.reconciliation_needed
        assert "exceeded reserved limits" in (task.last_error or "")

        # 3. GenerationTaskAttempt records provider output for auditing
        attempt = await session.scalar(
            select(GenerationTaskAttempt).where(
                GenerationTaskAttempt.generation_task_id == task.id
            )
        )
        assert attempt is not None
        assert attempt.status == GenerationAttemptStatus.completed
        assert attempt.response_content == "Valid response but usage exceeded reservation limits"
        assert attempt.prompt_tokens == 4000
        assert attempt.completion_tokens == 2000

        # 4. UsageReservation liability remains ACTIVE
        res = await session.scalar(
            select(UsageReservation).where(UsageReservation.generation_task_id == task.id)
        )
        assert res is not None
        assert res.status == ReservationStatus.active, "Failed settlement must preserve reservation liability"

        # 5. LLMUsageLog was NOT persisted
        log = await session.scalar(
            select(LLMUsageLog).where(LLMUsageLog.generation_task_id == task.id)
        )
        assert log is None, "Failed settlement must never create a usage log"


async def test_supabase_expired_lease_after_dispatched_attempt_never_redispatches(
    test_tenant: dict[str, Any],
) -> None:
    """Verifies that an expired lease after dispatch commitment quarantines rather than redispatches."""
    org_id = test_tenant["org_id"]
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    dead_worker_token = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        task = GenerationTask(
            id=task_id,
            conversation_id=conv_id,
            visitor_message_id=msg_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.pending,
            lease_token=dead_worker_token,
            lease_version=1,
            lease_expires_at=now - timedelta(seconds=10),  # Expired lease
            attempts=1,
            max_attempts=3,
        )
        session.add(task)

        # Worker 1 committed attempt 1 as 'dispatched' before crashing
        att_id = uuid.uuid4()
        test_tenant["attempt_ids"].add(att_id)
        att = GenerationTaskAttempt(
            id=att_id,
            generation_task_id=task_id,
            attempt_number=1,
            lease_token=dead_worker_token,
            status=GenerationAttemptStatus.dispatched,
            provider="mock",
            model="mock-gpt-4o-mini",
            dispatched_at=now - timedelta(seconds=20),
        )
        session.add(att)

        # Active reservation
        r = UsageReservation(
            id=uuid.uuid4(),
            organization_id=org_id,
            billing_period_id=bp_id,
            generation_task_id=task_id,
            tokens_reserved=1000,
            cost_micros_reserved=150,
            status=ReservationStatus.active,
        )
        session.add(r)
        await session.commit()

    provider = MockLLMProvider()

    # Worker 2 attempts to process the task
    async with AsyncSessionLocal() as session:
        bot = await session.get(Chatbot, test_tenant["bot_id"])
        msg = await session.get(Message, msg_id)
        assert bot is not None and msg is not None

        result = await orchestrate_assistant_reply(
            db=session,
            chatbot=bot,
            conversation_id=conv_id,
            visitor_message=msg,
            provider=provider,
            now=now,
        )

    # Invariant: Zero calls to provider, task quarantined, attempt marked ambiguous
    assert provider.call_count == 0
    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.TASK_QUARANTINED

    async with AsyncSessionLocal() as session:
        task_db = await session.get(GenerationTask, task_id)
        assert task_db is not None
        assert task_db.status == GenerationTaskStatus.reconciliation_needed

        att_db = await session.scalar(
            select(GenerationTaskAttempt).where(
                GenerationTaskAttempt.generation_task_id == task_id
            )
        )
        assert att_db is not None
        assert att_db.status == GenerationAttemptStatus.ambiguous

        # Reservation liability remains active
        r_db = await session.scalar(
            select(UsageReservation).where(UsageReservation.generation_task_id == task_id)
        )
        assert r_db is not None
        assert r_db.status == ReservationStatus.active


async def test_supabase_foreign_key_deletion_and_audit_retention(test_tenant: dict[str, Any]) -> None:
    """Verifies that deleting a Conversation cascades to Task and Reservation, but retains Attempts and Usage Logs (ON DELETE SET NULL)."""
    conv_id = test_tenant["conv_id"]
    msg_id = test_tenant["msg_id"]
    org_id = test_tenant["org_id"]
    bp_id = test_tenant["bp_id"]
    now = test_tenant["now"]

    task_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    log_id = uuid.uuid4()

    # Track attempt_id in test_tenant so fixture cleans it up deterministically if task FK becomes NULL
    test_tenant["attempt_ids"].add(attempt_id)

    async with AsyncSessionLocal() as session:
        task = GenerationTask(
            id=task_id,
            conversation_id=conv_id,
            visitor_message_id=msg_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            status=GenerationTaskStatus.completed,
            lease_token=uuid.uuid4(),
            lease_version=1,
            lease_expires_at=now + timedelta(seconds=60),
            attempts=1,
            max_attempts=3,
        )
        session.add(task)

        attempt = GenerationTaskAttempt(
            id=attempt_id,
            generation_task_id=task_id,
            attempt_number=1,
            lease_token=task.lease_token,
            status=GenerationAttemptStatus.completed,
            provider="mock",
            model="mock-gpt-4o-mini",
            prompt_tokens=25,
            completion_tokens=50,
            cost_micros=15,
            response_content="Audit-preserved response",
        )
        session.add(attempt)

        reservation = UsageReservation(
            id=reservation_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            generation_task_id=task_id,
            lease_token=task.lease_token,
            tokens_reserved=1000,
            cost_micros_reserved=150,
            status=ReservationStatus.settled,
        )
        session.add(reservation)

        log = LLMUsageLog(
            id=log_id,
            organization_id=org_id,
            billing_period_id=bp_id,
            generation_task_id=task_id,
            provider="mock",
            model="mock-gpt-4o-mini",
            pricing_version="v1",
            prompt_tokens=25,
            completion_tokens=50,
            total_tokens=75,
            cost_micros=15,
            latency_ms=120,
        )
        session.add(log)
        await session.commit()

    # Delete Conversation record (simulating customer conversation purge)
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Conversation).where(Conversation.id == conv_id))
        await session.commit()

    # Verification:
    async with AsyncSessionLocal() as session:
        # 1. GenerationTask was CASCADE deleted with Conversation
        t_check = await session.get(GenerationTask, task_id)
        assert t_check is None

        # 2. UsageReservation was CASCADE deleted with GenerationTask (ondelete='CASCADE')
        res_check = await session.get(UsageReservation, reservation_id)
        assert res_check is None

        # 3. GenerationTaskAttempt SURVIVES with generation_task_id set to NULL (ondelete='SET NULL')
        att_check = await session.get(GenerationTaskAttempt, attempt_id)
        assert att_check is not None
        assert att_check.generation_task_id is None
        assert att_check.prompt_tokens == 25
        assert att_check.cost_micros == 15
        assert att_check.response_content == "Audit-preserved response"

        # 4. LLMUsageLog SURVIVES with generation_task_id set to NULL (ondelete='SET NULL')
        log_check = await session.get(LLMUsageLog, log_id)
        assert log_check is not None
        assert log_check.generation_task_id is None
        assert log_check.total_tokens == 75
        assert log_check.cost_micros == 15

        # 5. Attempting to delete OrganizationBillingPeriod violates ON DELETE RESTRICT from LLMUsageLog
        with pytest.raises(IntegrityError):
            await session.execute(
                delete(OrganizationBillingPeriod).where(OrganizationBillingPeriod.id == bp_id)
            )
            await session.commit()


async def test_supabase_rls_and_privilege_boundaries(test_tenant: dict[str, Any]) -> None:
    """Verifies table RLS activation and privilege isolation.

    Architecture Note:
    1. Table Privileges vs RLS Policies:
       In Aptlyst AI, direct PostgREST client roles ('anon', 'authenticated') have ALL table privileges
       revoked across all Aptlyst application tables in migrations d0e25ba60860, 5c91f0a21d8b,
       a1b2c3d4e5f6, b2c3d4e5f6a1, and c3d4e5f6a1b2.
       Untrusted clients are blocked at the PostgreSQL privilege layer ('permission denied for table')
       before row-level security policy evaluation occurs.
    2. Backend Execution Context:
       The FastAPI backend runs as user 'postgres' (which has rolbypassrls=True in Supabase).
       Tenant boundary enforcement is applied in the application queries via explicit organization_id scoping.
    3. Defense-in-Depth:
       ROW LEVEL SECURITY is enabled on all 12 tables (rowsecurity=True in pg_tables) to prevent any
       accidental privilege grant from exposing tenant records without explicit policy creation.
    """
    async with AsyncSessionLocal() as session:
        # Part A: Verify RLS is physically enabled on all 12 application tables
        res_rls = await session.execute(
            text(
                "SELECT tablename, rowsecurity FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename IN ("
                "'organizations', 'organization_members', 'chatbots', 'conversations', 'messages', "
                "'organization_billing_periods', 'generation_tasks', 'generation_task_attempts', "
                "'usage_reservations', 'llm_usage_logs', 'subscriptions', 'stripe_webhook_events'"
                ");"
            )
        )
        rls_map = dict(res_rls.all())
        assert len(rls_map) == 12
        for table_name, is_rls_enabled in rls_map.items():
            assert is_rls_enabled is True, f"Table '{table_name}' must have rowsecurity=True"

        # Part B: Verify untrusted PostgREST client roles have ZERO direct table privileges
        res_privs = await session.execute(
            text(
                "SELECT grantee, table_name, privilege_type "
                "FROM information_schema.table_privileges "
                "WHERE table_schema = 'public' "
                "AND table_name IN ("
                "'organizations', 'chatbots', 'conversations', 'messages', "
                "'generation_tasks', 'generation_task_attempts', 'usage_reservations'"
                ") AND grantee IN ('anon', 'authenticated');"
            )
        )
        privileges = res_privs.all()
        assert len(privileges) == 0, (
            f"Expected 0 privileges for anon/authenticated, found: {privileges}"
        )

        # Part C: Verify untrusted client role query is denied at the privilege layer
        try:
            await session.execute(text("SET LOCAL ROLE anon;"))
            with pytest.raises(ProgrammingError) as exc_info:
                await session.execute(text("SELECT * FROM generation_tasks LIMIT 1;"))
            assert "permission denied" in str(exc_info.value).lower()
        finally:
            await session.rollback()
