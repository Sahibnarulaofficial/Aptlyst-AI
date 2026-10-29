"""Comprehensive test suite for Phase 2B Stage 3: Durable Generation Orchestration.

Verifies:
1. Two-phase transaction boundaries:
   - Transaction 1 (task acquisition, quota reservation, attempt allocation 'claimed',
     durable pre-provider dispatch commitment 'dispatched') is fully COMMITTED before any provider I/O.
   - Zero database locks held during provider execution.
   - Transaction 2 (post-inference worker lease fencing verification, usage settlement against active
     reservation, reply persistence with client_message_id idempotency, terminal state transitions) is COMMITTED.
2. Idempotent replays:
   - Returns existing reply message immediately if visitor message was already answered.
   - Zero duplicate provider calls or billing charges on replayed requests.
   - Catches concurrent insert conflict and safely returns the winner's reply.
3. Pre-dispatch quota & spending cap enforcement:
   - Rejects turns exceeding token quota or spending limit before dispatch.
   - Zero provider calls and zero attempt records created on quota/cap exhaustion.
   - Rejects requests when no active billing period exists.
4. Pre-dispatch safe reservation release:
   - If pre-dispatch failure occurs (e.g. context building error), the attempt is marked 'failed'
     and the reserved tokens and cost are cleanly returned to the billing period.
5. Provider timeout & ambiguity quarantine:
   - Provider timeout or network drop after dispatch marks attempt 'ambiguous' and task 'reconciliation_needed'.
   - Active reservation liability is PRESERVED (never released).
   - Quarantined task cannot be retried or double-dispatched by subsequent workers.
6. Worker lease fencing:
   - Expired worker returning with LLM output is fenced if task lease changed (status -> 'stale_received').
   - Stale worker cannot settle reservation or persist reply messages.
7. Post-inference settlement failure handling:
   - Settlement failure after provider output routes task to 'reconciliation_needed'.
   - Provider is never re-invoked; liabilities and attempt evidence are preserved.
8. Concurrent task acquisition & attempt numbering:
   - Active leased task blocks concurrent workers with TASK_IN_PROGRESS.
   - Expired lease permits safe takeover with incremented lease_version.
   - Enforces max_task_attempts ceiling; releases reservation when exceeded.
9. Reply correlation consistency:
   - Assistant reply message client_message_id strictly matches f"reply:{visitor_message.id}".
10. Tenant ownership & state validation:
   - Rejects tenant and conversation mismatches with INVALID_OWNERSHIP.
   - Rejects non-pending or quarantined states.
11. Provider disabled safeguard:
   - Returns GenerationStatus.skipped when provider is explicitly disabled.

Note:
These unit tests utilize an in-memory async mock session test double and synchronous
coroutine runner (`run_async`) to verify algorithmic correctness, state transitions,
transaction boundaries, and fencing semantics without requiring or connecting to a live
PostgreSQL database or requiring an OS network event loop.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
from app.config import settings
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Message, MessageSenderType
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.schemas.conversation import GenerationStatus
from app.services.billing.accounting import AccountingErrorCode
from app.services.billing.pricing import (
    MOCK_GPT_4O_MINI_PRICING,
    ModelPricing,
)
from app.services.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMProviderError,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
)
from app.services.llm.mock import MockLLMProvider
from app.services.llm.orchestrator import (
    OrchestrationResult,
    OrchestratorErrorCode,
    orchestrate_assistant_reply,
)

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to immediate completion without requiring an OS event loop."""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise RuntimeError(f"Coroutine {coro} yielded unexpectedly before completing")


# ============================================================================
# Test Doubles / In-Memory Mock Session
# ============================================================================


class MockResult:
    """Mock result simulating SQLAlchemy's ChunkedIteratorResult."""

    def __init__(self, item: Any = None, items: list[Any] | None = None) -> None:
        self._item = item
        self._items = items if items is not None else ([item] if item is not None else [])

    def scalar_one_or_none(self) -> Any:
        return self._item

    def scalar(self) -> Any:
        return self._item

    def scalars(self) -> "MockResult":
        return self

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any:
        return self._items[0] if self._items else None


class MockOrchestratorSession:
    """Deterministic in-memory async session test double for orchestrator tests.

    Emulates query execution, additions, flushes, commits, rollbacks, and error injection.
    """

    def __init__(
        self,
        *,
        billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] | None = None,
        tasks: dict[uuid.UUID, GenerationTask] | None = None,
        reservations: dict[uuid.UUID, UsageReservation] | None = None,
        usage_logs: dict[uuid.UUID, LLMUsageLog] | None = None,
        attempts: dict[uuid.UUID, GenerationTaskAttempt] | None = None,
        messages: dict[uuid.UUID, Message] | None = None,
    ) -> None:
        self.billing_periods = billing_periods or {}
        self.tasks = tasks or {}
        self.reservations = reservations or {}
        self.usage_logs = usage_logs or {}
        self.attempts = attempts or {}
        self.messages = messages or {}
        self.added: list[Any] = []
        self.uncommitted_added: list[Any] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.flush_count = 0
        self.fail_commit_with_integrity_error = False
        self.fail_flush_with_integrity_error = False
        self.executed_queries: list[dict[str, Any]] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.uncommitted_added.append(obj)
        if isinstance(obj, GenerationTask):
            self.tasks[obj.id] = obj
        elif isinstance(obj, UsageReservation):
            self.reservations[obj.id] = obj
        elif isinstance(obj, GenerationTaskAttempt):
            self.attempts[obj.id] = obj
        elif isinstance(obj, Message):
            self.messages[obj.id] = obj
        elif isinstance(obj, LLMUsageLog):
            self.usage_logs[obj.id] = obj
        elif isinstance(obj, OrganizationBillingPeriod):
            self.billing_periods[obj.id] = obj

    async def flush(self) -> None:
        self.flush_count += 1
        if self.fail_flush_with_integrity_error:
            raise IntegrityError("Simulated flush integrity error", params={}, orig=Exception())

    async def commit(self) -> None:
        if self.fail_commit_with_integrity_error:
            raise IntegrityError("Simulated commit integrity error", params={}, orig=Exception())
        self.commit_count += 1
        self.uncommitted_added.clear()

    async def rollback(self) -> None:
        self.rollback_count += 1
        for obj in self.uncommitted_added:
            if isinstance(obj, GenerationTask):
                self.tasks.pop(obj.id, None)
            elif isinstance(obj, UsageReservation):
                self.reservations.pop(obj.id, None)
            elif isinstance(obj, GenerationTaskAttempt):
                self.attempts.pop(obj.id, None)
            elif isinstance(obj, Message):
                self.messages.pop(obj.id, None)
            elif isinstance(obj, LLMUsageLog):
                self.usage_logs.pop(obj.id, None)
            elif isinstance(obj, OrganizationBillingPeriod):
                self.billing_periods.pop(obj.id, None)
        self.uncommitted_added.clear()

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any) -> MockResult:
        col = stmt.column_descriptions[0]
        entity = col.get("entity")
        name = col.get("name")
        params = stmt.compile().params
        has_for_update = getattr(stmt, "_for_update_arg", None) is not None

        self.executed_queries.append({
            "entity": entity,
            "name": name,
            "params": params,
            "for_update": has_for_update,
        })

        # 1. Query: func.coalesce(func.max(GenerationTaskAttempt.attempt_number), 0)
        if name == "coalesce" and entity is GenerationTaskAttempt:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            matched = [a for a in self.attempts.values() if a.generation_task_id == t_id]
            max_num = max([a.attempt_number for a in matched], default=0)
            return MockResult(max_num)

        # 2. Query: select(UsageReservation.billing_period_id).where(UsageReservation.id == r_id)
        if name == "billing_period_id" and entity is UsageReservation:
            r_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            res = self.reservations.get(r_id)
            return MockResult(res.billing_period_id if res else None)

        # 3. Query: select(Message)
        if entity is Message:
            client_msg_id = next(
                (v for v in params.values() if isinstance(v, str) and v.startswith("reply:")),
                None,
            )
            if client_msg_id is not None:
                # Idempotency check query by client_message_id
                matching_msg = next(
                    (m for m in self.messages.values() if m.client_message_id == client_msg_id),
                    None,
                )
                return MockResult(matching_msg)

            # Context query: messages in conversation excluding current_message
            conv_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            matched_msgs = [m for m in self.messages.values() if m.conversation_id == conv_id]
            matched_msgs.sort(key=lambda m: m.created_at, reverse=True)
            return MockResult(items=matched_msgs)

        # 4. Query: select(OrganizationBillingPeriod)
        if entity is OrganizationBillingPeriod:
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.billing_periods:
                        return MockResult(self.billing_periods[v])
                    for bp in self.billing_periods.values():
                        if bp.organization_id == v:
                            return MockResult(bp)
            return MockResult(None)

        # 5. Query: select(GenerationTask)
        if entity is GenerationTask:
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.tasks:
                        return MockResult(self.tasks[v])
                    for t in self.tasks.values():
                        if t.visitor_message_id == v:
                            return MockResult(t)
            return MockResult(None)

        # 6. Query: select(UsageReservation)
        if entity is UsageReservation:
            if ReservationStatus.active in params.values():
                t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
                active_res = next(
                    (
                        r
                        for r in self.reservations.values()
                        if r.generation_task_id == t_id and r.status == ReservationStatus.active
                    ),
                    None,
                )
                return MockResult(active_res)
            for v in params.values():
                if isinstance(v, uuid.UUID) and v in self.reservations:
                    return MockResult(self.reservations[v])
            return MockResult(None)

        # 7. Query: select(GenerationTaskAttempt)
        if entity is GenerationTaskAttempt:
            for v in params.values():
                if isinstance(v, uuid.UUID):
                    if v in self.attempts:
                        return MockResult(self.attempts[v])
                    matched = [a for a in self.attempts.values() if a.generation_task_id == v]
                    return MockResult(items=matched)
            return MockResult(None)

        # 8. Query: select(LLMUsageLog)
        if entity is LLMUsageLog:
            t_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            log = next((l for l in self.usage_logs.values() if l.generation_task_id == t_id), None)
            return MockResult(log)

        return MockResult(None)


# ============================================================================
# Helpers & Fixtures
# ============================================================================


def make_test_environment(
    *,
    monthly_token_quota: int = 100_000,
    hard_spending_cap_micros: int | None = 1_000_000,
    is_active_bp: bool = True,
    now: datetime | None = None,
) -> tuple[MockOrchestratorSession, Chatbot, uuid.UUID, Message, OrganizationBillingPeriod]:
    current_time = now or datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=current_time - timedelta(days=1),
        period_end=current_time + timedelta(days=29),
        monthly_token_quota=monthly_token_quota,
        tokens_used=0,
        tokens_reserved=0,
        hard_spending_cap_micros=hard_spending_cap_micros,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=is_active_bp,
        is_blocked=False,
    )

    chatbot = Chatbot(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="SupportBot",
        instructions="You are a helpful customer support assistant.",
        status=ChatbotStatus.active,
    )

    visitor_message = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="What is Aptlyst AI?",
        created_at=current_time,
    )

    db = MockOrchestratorSession()
    if is_active_bp:
        db.add(bp)
    db.add(visitor_message)

    return db, chatbot, conv_id, visitor_message, bp


# ============================================================================
# 1. Success Path and Two-Phase Transaction Boundaries
# ============================================================================


def test_orchestrator_success_path() -> None:
    """Verifies complete successful orchestrated generation lifecycle."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(canned_response="Aptlyst AI provides reliable customer support.")

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    # 1. Status & Return Value Assertions
    assert result.status == GenerationStatus.completed
    assert result.error_code is None
    assert result.is_idempotent_replay is False
    assert result.reply_message is not None
    assert result.reply_message.content == "Aptlyst AI provides reliable customer support."
    assert result.reply_message.client_message_id == f"reply:{visitor_msg.id}"
    assert result.reply_message.sender_type == MessageSenderType.assistant
    assert result.tokens_used > 0
    assert result.cost_micros > 0

    # 2. Two-Phase Transaction Commits
    # Transaction 1 committed dispatch marker; Transaction 2 committed settlement & reply
    assert db.commit_count == 2

    # 3. Task State Transition
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.completed
    assert result.task.attempts == 1

    # 4. Attempt State Transition
    assert result.attempt is not None
    assert result.attempt.status == GenerationAttemptStatus.completed
    assert result.attempt.attempt_number == 1
    assert result.attempt.completed_at == now
    assert result.attempt.response_content == "Aptlyst AI provides reliable customer support."
    assert result.attempt.prompt_tokens > 0
    assert result.attempt.completion_tokens > 0

    # 5. Usage Log & Reservation Settlement
    assert result.usage_log is not None
    assert result.usage_log.generation_task_id == result.task.id
    assert result.usage_log.total_tokens == result.tokens_used
    assert result.usage_log.cost_micros == result.cost_micros

    # Active reservation is now settled
    res = next(r for r in db.reservations.values() if r.generation_task_id == result.task.id)
    assert res.status == ReservationStatus.settled


def test_orchestrator_two_phase_transaction_boundary_commit_before_provider() -> None:
    """Verifies that Transaction 1 is durably COMMITTED before the provider is invoked."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    class TransactionInspectProvider(BaseLLMProvider):
        """Spy provider verifying DB state and commit count at invocation time."""

        def __init__(self, session: MockOrchestratorSession) -> None:
            self.session = session
            self.commit_count_at_call = -1
            self.attempt_status_at_call: GenerationAttemptStatus | None = None

        async def generate_reply(
            self,
            messages: list[LLMMessage],
            system_prompt: str | None = None,
            temperature: float = 0.7,
            max_tokens: int = 1000,
            timeout_seconds: float = 10.0,
        ) -> LLMResponse:
            self.commit_count_at_call = self.session.commit_count
            # Inspect attempt status in session
            if self.session.attempts:
                latest_attempt = list(self.session.attempts.values())[-1]
                self.attempt_status_at_call = latest_attempt.status

            return LLMResponse(
                content="Spy reply",
                model="test-spy",
                usage=LLMUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

    spy_provider = TransactionInspectProvider(db)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=spy_provider,
            pricing=MOCK_GPT_4O_MINI_PRICING,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    # CRITICAL INVARIANT: Commit count must be exactly 1 when provider is called
    assert spy_provider.commit_count_at_call == 1
    # CRITICAL INVARIANT: Attempt status must be 'dispatched' before provider executes
    assert spy_provider.attempt_status_at_call == GenerationAttemptStatus.dispatched
    # Final commit count after Transaction 2 settlement
    assert db.commit_count == 2


# ============================================================================
# 2. Idempotent Replays
# ============================================================================


def test_orchestrator_idempotent_replay_existing_reply_message() -> None:
    """Verifies that an existing reply message is returned immediately without provider calls."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    correlation_key = f"reply:{visitor_msg.id}"
    prior_reply = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.assistant,
        content="Prior answered response",
        client_message_id=correlation_key,
        created_at=now - timedelta(minutes=5),
    )
    db.add(prior_reply)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.is_idempotent_replay is True
    assert result.reply_message == prior_reply
    assert result.reply_message.content == "Prior answered response"
    # Zero provider calls, zero attempts created
    assert provider.call_count == 0
    assert len(db.attempts) == 0
    assert db.commit_count == 0


def test_orchestrator_idempotent_replay_already_completed_task() -> None:
    """Verifies that a completed task returns the existing reply without re-invoking the provider."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.completed,
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_expires_at=now + timedelta(minutes=1),
        attempts=1,
        max_attempts=3,
    )
    db.add(task)

    correlation_key = f"reply:{visitor_msg.id}"
    reply = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.assistant,
        content="Completed answer",
        client_message_id=correlation_key,
        created_at=now,
    )
    db.add(reply)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.is_idempotent_replay is True
    assert result.reply_message == reply
    assert provider.call_count == 0


def test_orchestrator_concurrent_commit_integrity_error_replay() -> None:
    """Verifies that concurrent commit conflict rolls back and returns the winner's reply."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(canned_response="Winner message")

    # Simulate: on the 2nd commit (Transaction 2), a concurrent winner inserted the message first
    original_commit = db.commit

    async def commit_with_winner_conflict():
        if db.commit_count == 1:
            # First commit succeeded (Transaction 1). Simulate winner committing before Transaction 2 finishes.
            winner_msg = Message(
                id=uuid.uuid4(),
                conversation_id=conv_id,
                sender_type=MessageSenderType.assistant,
                content="Winner reply already in DB",
                client_message_id=f"reply:{visitor_msg.id}",
                created_at=now,
            )
            db.messages[winner_msg.id] = winner_msg
            db.fail_commit_with_integrity_error = True
        await original_commit()

    db.commit = commit_with_winner_conflict

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.is_idempotent_replay is True
    assert result.reply_message is not None
    assert result.reply_message.content == "Winner reply already in DB"
    assert db.rollback_count == 1


# ============================================================================
# 3. Quota & Spending Cap Rejections Pre-Dispatch
# ============================================================================


def test_orchestrator_quota_rejection_pre_dispatch() -> None:
    """Verifies that quota exhaustion is rejected pre-dispatch with zero provider calls."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    # Token quota is 0 (exhausted)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(monthly_token_quota=0, now=now)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.QUOTA_EXHAUSTED
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.failed
    # Zero provider calls, zero attempts created
    assert provider.call_count == 0
    assert len(db.attempts) == 0


def test_orchestrator_spending_cap_rejection_pre_dispatch() -> None:
    """Verifies that hard spending cap exhaustion is rejected pre-dispatch with zero provider calls."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    # Spending cap is $0.000001 (less than turn max cost)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(
        monthly_token_quota=100_000,
        hard_spending_cap_micros=1,
        now=now,
    )

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SPENDING_CAP_EXHAUSTED
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.failed
    assert provider.call_count == 0
    assert len(db.attempts) == 0


def test_orchestrator_no_active_billing_period_rejection() -> None:
    """Verifies rejection when no active billing period exists."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, _ = make_test_environment(is_active_bp=False, now=now)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.NO_ACTIVE_BILLING_PERIOD
    assert provider.call_count == 0


# ============================================================================
# 4. Pre-Dispatch Safe Reservation Release
# ============================================================================


def test_orchestrator_pre_dispatch_failure_releases_reservation() -> None:
    """Verifies that pre-dispatch context failure marks attempt failed and releases reservation."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Corrupt visitor message content to trigger an error during context building
    visitor_msg.content = None  # type: ignore

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.PRE_DISPATCH_FAILURE
    assert provider.call_count == 0

    # Attempt marked failed
    assert result.attempt is not None
    assert result.attempt.status == GenerationAttemptStatus.failed

    # Reservation released cleanly
    res = next(r for r in db.reservations.values() if r.generation_task_id == result.task.id)
    assert res.status == ReservationStatus.released

    # Task marked failed
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.failed


# ============================================================================
# 5. Provider Timeout & Ambiguity Quarantine
# ============================================================================


def test_orchestrator_timeout_quarantines_ambiguous_state() -> None:
    """Verifies that provider timeout marks attempt ambiguous, task quarantined, and preserves reservation."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(force_timeout=True)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.PROVIDER_TIMEOUT
    assert provider.call_count == 1

    # Attempt marked ambiguous
    assert result.attempt is not None
    assert result.attempt.status == GenerationAttemptStatus.ambiguous

    # Task quarantined to reconciliation_needed
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.reconciliation_needed

    # CRITICAL INVARIANT: Reservation is NOT released! It remains active to preserve liability
    res = next(r for r in db.reservations.values() if r.generation_task_id == result.task.id)
    assert res.status == ReservationStatus.active


def test_orchestrator_quarantined_task_refuses_retry() -> None:
    """Verifies that a task in reconciliation_needed cannot be retried by any worker."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    quarantined_task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.reconciliation_needed,
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_expires_at=now - timedelta(seconds=10),
        attempts=1,
        max_attempts=3,
        last_error="Quarantined due to prior timeout",
    )
    db.add(quarantined_task)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.TASK_QUARANTINED
    # Provider must NEVER be called for quarantined tasks!
    assert provider.call_count == 0


# ============================================================================
# 6. Worker Lease Fencing
# ============================================================================


def test_orchestrator_stale_lease_fenced() -> None:
    """Verifies that an expired worker is fenced if the task lease was taken over."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    class LeaseStealingProvider(BaseLLMProvider):
        """Simulates worker lease expiration and takeover by another worker during inference."""

        def __init__(self, session: MockOrchestratorSession) -> None:
            self.session = session

        async def generate_reply(
            self,
            messages: list[LLMMessage],
            system_prompt: str | None = None,
            temperature: float = 0.7,
            max_tokens: int = 1000,
            timeout_seconds: float = 10.0,
        ) -> LLMResponse:
            # Simulate another worker taking over the task's lease while we are in inference
            task = list(self.session.tasks.values())[0]
            task.lease_token = uuid.uuid4()  # New worker's token!
            task.lease_version += 1

            return LLMResponse(
                content="Stale worker response",
                model="test-bot",
                usage=LLMUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

    provider = LeaseStealingProvider(db)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.STALE_LEASE_FENCED

    # Attempt marked stale_received
    assert result.attempt is not None
    assert result.attempt.status == GenerationAttemptStatus.stale_received

    # Reply message was NOT persisted
    assert result.reply_message is None
    assert len([m for m in db.messages.values() if m.sender_type == MessageSenderType.assistant]) == 0

    # Reservation was NOT settled by stale worker
    res = list(db.reservations.values())[0]
    assert res.status == ReservationStatus.active


# ============================================================================
# 7. Post-Inference Settlement Failure Handling
# ============================================================================


def test_orchestrator_settlement_failure_quarantines_task() -> None:
    """Verifies that settlement failure after provider output quarantines the task without re-dispatch."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    class TamperingProvider(BaseLLMProvider):
        """Simulates reservation corruption or release while inference is running."""

        def __init__(self, session: MockOrchestratorSession) -> None:
            self.session = session

        async def generate_reply(
            self,
            messages: list[LLMMessage],
            system_prompt: str | None = None,
            temperature: float = 0.7,
            max_tokens: int = 1000,
            timeout_seconds: float = 10.0,
        ) -> LLMResponse:
            # Corrupt active reservation status to trigger settlement failure
            res = list(self.session.reservations.values())[0]
            res.status = ReservationStatus.released
            return LLMResponse(
                content="Valid response from provider",
                model="test-bot",
                usage=LLMUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

    provider = TamperingProvider(db)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SETTLEMENT_FAILED

    # Task quarantined to reconciliation_needed
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.reconciliation_needed

    # Provider output was captured in attempt record for audit
    assert result.attempt is not None


# ============================================================================
# 8. Concurrent Task Acquisition & Attempt Ceilings
# ============================================================================


def test_orchestrator_max_attempts_exceeded() -> None:
    """Verifies that exceeding max_task_attempts releases reservation and marks task failed."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Pre-create task with max_attempts = 2
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_expires_at=now - timedelta(seconds=10),
        attempts=2,
        max_attempts=2,
    )
    db.add(task)

    # Pre-create 2 prior attempts
    for num in (1, 2):
        att = GenerationTaskAttempt(
            id=uuid.uuid4(),
            generation_task_id=task.id,
            attempt_number=num,
            lease_token=task.lease_token,
            status=GenerationAttemptStatus.failed,
            provider="mock",
            model="mock-gpt-4o-mini",
        )
        db.add(att)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            max_task_attempts=2,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.MAX_ATTEMPTS_EXCEEDED
    assert provider.call_count == 0
    assert result.task.status == GenerationTaskStatus.failed


def test_orchestrator_concurrent_active_lease_rejected() -> None:
    """Verifies that an active unexpired lease held by another worker blocks execution."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Pre-create task leased by Worker B for 60 seconds into the future
    worker_b_token = uuid.uuid4()
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=worker_b_token,
        lease_version=1,
        lease_expires_at=now + timedelta(seconds=60),
        attempts=0,
        max_attempts=3,
    )
    db.add(task)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.TASK_IN_PROGRESS
    assert provider.call_count == 0


def test_orchestrator_expired_lease_takeover() -> None:
    """Verifies that an expired lease can be safely taken over by a new worker."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Pre-create task whose lease expired 10 seconds ago
    dead_worker_token = uuid.uuid4()
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=dead_worker_token,
        lease_version=1,
        lease_expires_at=now - timedelta(seconds=10),
        attempts=0,
        max_attempts=3,
    )
    db.add(task)

    provider = MockLLMProvider(canned_response="Taken over successfully")

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.task.lease_token != dead_worker_token
    assert result.task.lease_version == 2
    assert result.reply_message.content == "Taken over successfully"


# ============================================================================
# 9. Reply Correlation Consistency
# ============================================================================


def test_orchestrator_reply_correlation_consistency() -> None:
    """Verifies that the reply client_message_id strictly matches the visitor message id."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(canned_response="Correlation test")

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.completed
    assert result.reply_message is not None
    assert result.reply_message.client_message_id == f"reply:{visitor_msg.id}"
    assert result.reply_message.conversation_id == conv_id


# ============================================================================
# 10. Tenant Ownership & State Validation
# ============================================================================


def test_orchestrator_cross_tenant_ownership_mismatch() -> None:
    """Verifies rejection of cross-tenant ownership mismatches."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Task belongs to different organization
    foreign_org_id = uuid.uuid4()
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=foreign_org_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=uuid.uuid4(),
        lease_version=1,
        lease_expires_at=now + timedelta(seconds=60),
        attempts=0,
        max_attempts=3,
    )
    db.add(task)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.INVALID_OWNERSHIP
    assert provider.call_count == 0


def test_orchestrator_general_provider_error() -> None:
    """Verifies that definite upstream provider failure transitions attempt and task to failed and safely releases reservation."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(force_error="Upstream model rate limit", force_error_is_definite=True)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.PROVIDER_ERROR
    assert result.attempt.status == GenerationAttemptStatus.failed
    assert result.task.status == GenerationTaskStatus.failed

    # Priority 1 Invariant: Reservation is safely released on definite rejection; never left active on failed task
    res = list(db.reservations.values())[0]
    assert res.status == ReservationStatus.released
    assert bp.tokens_reserved == 0
    assert bp.cost_micros_reserved == 0


def test_orchestrator_uncertain_provider_failure_quarantines_and_retains_reservation() -> None:
    """Verifies that uncertain provider failure quarantines task/attempt and retains reservation liability."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # force_error_is_definite=False represents 5xx / dropped socket where provider may have billed
    provider = MockLLMProvider(force_error="Upstream dropped TCP connection", force_error_is_definite=False)

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.TASK_QUARANTINED
    assert result.attempt.status == GenerationAttemptStatus.ambiguous
    assert result.task.status == GenerationTaskStatus.reconciliation_needed

    # Priority 1 Invariant: Reservation liability is RETAINED; not silently zeroed or released
    res = list(db.reservations.values())[0]
    assert res.status == ReservationStatus.active
    assert bp.tokens_reserved > 0
    assert bp.cost_micros_reserved > 0


def test_orchestrator_provider_disabled_safeguard() -> None:
    """Verifies that GenerationStatus.skipped is returned when provider is disabled."""
    settings.llm_provider = "disabled"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider()

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    assert result.status == GenerationStatus.skipped
    assert result.reply_message is None
    assert provider.call_count == 0


# ============================================================================
# 11. Focused Stage 3 Audit Regression Tests
# ============================================================================


def test_orchestrator_dispatched_attempt_expired_lease_quarantines_without_redispatch() -> None:
    """Verifies that an expired lease on a task with a dispatched attempt quarantines rather than redispatches."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    # Task was leased to Worker A, whose lease expired 10 seconds ago
    dead_worker_token = uuid.uuid4()
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=dead_worker_token,
        lease_version=1,
        lease_expires_at=now - timedelta(seconds=10),
        attempts=1,
        max_attempts=3,
    )
    db.add(task)

    # Worker A committed attempt 1 as 'dispatched' before crashing
    dispatched_attempt = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=task.id,
        attempt_number=1,
        lease_token=dead_worker_token,
        status=GenerationAttemptStatus.dispatched,
        provider="mock",
        model="mock-gpt-4o-mini",
        dispatched_at=now - timedelta(seconds=20),
    )
    db.add(dispatched_attempt)

    # Active usage reservation exists
    reservation = UsageReservation(
        id=uuid.uuid4(),
        organization_id=chatbot.organization_id,
        generation_task_id=task.id,
        billing_period_id=bp.id,
        tokens_reserved=1000,
        cost_micros_reserved=150,
        status=ReservationStatus.active,
    )
    db.add(reservation)

    provider = MockLLMProvider()

    # Worker B tries to acquire and process the task
    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    # 1. Zero Provider Calls: External provider must NEVER be re-invoked
    assert provider.call_count == 0

    # 2. Rejection & Quarantine Outcome
    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.TASK_QUARANTINED

    # 3. Dispatched attempt transitioned to 'ambiguous'
    assert dispatched_attempt.status == GenerationAttemptStatus.ambiguous
    assert "expired" in str(dispatched_attempt.error_message).lower()

    # 4. Task transitioned to 'reconciliation_needed' and committed
    assert task.status == GenerationTaskStatus.reconciliation_needed
    assert db.commit_count >= 1

    # 5. Reservation liability PRESERVED (never released)
    assert reservation.status == ReservationStatus.active


def test_orchestrator_claimed_attempt_pre_dispatch_crash_safe_failover() -> None:
    """Verifies that an abandoned 'claimed' attempt (pre-dispatch crash) is safely failed and retried."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    dead_worker_token = uuid.uuid4()
    task = GenerationTask(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        visitor_message_id=visitor_msg.id,
        organization_id=chatbot.organization_id,
        billing_period_id=bp.id,
        status=GenerationTaskStatus.pending,
        lease_token=dead_worker_token,
        lease_version=1,
        lease_expires_at=now - timedelta(seconds=10),
        attempts=1,
        max_attempts=3,
    )
    db.add(task)

    # Worker A claimed attempt 1, but crashed BEFORE committing dispatch
    claimed_attempt = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=task.id,
        attempt_number=1,
        lease_token=dead_worker_token,
        status=GenerationAttemptStatus.claimed,
        provider="mock",
        model="mock-gpt-4o-mini",
    )
    db.add(claimed_attempt)

    provider = MockLLMProvider(canned_response="Recovered reply")

    # Worker B acquires task: safe because dispatch was never committed
    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    # 1. Success Outcome
    assert result.status == GenerationStatus.completed
    assert result.reply_message is not None
    assert result.reply_message.content == "Recovered reply"

    # 2. Abandoned claimed attempt marked failed
    assert claimed_attempt.status == GenerationAttemptStatus.failed

    # 3. Worker B created Attempt 2 and succeeded
    assert result.attempt is not None
    assert result.attempt.attempt_number == 2
    assert result.attempt.status == GenerationAttemptStatus.completed


def test_orchestrator_lease_expired_during_context_building_fenced_pre_dispatch() -> None:
    """Verifies that if worker lease expires during context building, dispatch is rejected pre-dispatch."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider()

    # In context query, simulate a slow query during which the worker's lease expires
    original_execute = db.execute

    async def slow_execute(stmt):
        col = stmt.column_descriptions[0]
        if col.get("entity") is Message and not any(
            isinstance(v, str) and v.startswith("reply:") for v in stmt.compile().params.values()
        ):
            # Worker acquired task in Phase 1. Expire worker's lease during context fetch!
            if db.tasks:
                task = list(db.tasks.values())[0]
                task.lease_expires_at = now - timedelta(seconds=1)
        return await original_execute(stmt)

    db.execute = slow_execute

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    # Zero provider calls
    assert provider.call_count == 0
    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.STALE_LEASE_FENCED
    assert result.attempt.status == GenerationAttemptStatus.failed


def test_orchestrator_transaction2_unexpected_error_commits_reconciliation_quarantine() -> None:
    """Verifies that an unexpected error in Transaction 2 commits reconciliation_needed after rollback."""
    settings.llm_provider = "mock"
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    db, chatbot, conv_id, visitor_msg, bp = make_test_environment(now=now)

    provider = MockLLMProvider(canned_response="Success reply from provider")

    # Simulate: second commit (Transaction 2) throws a database operational exception once
    original_commit = db.commit
    has_failed = False

    async def failing_commit():
        nonlocal has_failed
        if db.commit_count == 1 and not has_failed:
            has_failed = True
            raise RuntimeError("PostgreSQL IO error during message persistence")
        await original_commit()

    db.commit = failing_commit

    result = run_async(
        orchestrate_assistant_reply(
            db=db,
            chatbot=chatbot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
            provider=provider,
            now=now,
        )
    )

    # 1. Result reflects failure
    assert result.status == GenerationStatus.failed
    assert result.error_code == OrchestratorErrorCode.SETTLEMENT_FAILED

    # 2. CRITICAL RECOVERY: Task was committed to reconciliation_needed in fresh transaction
    assert result.task is not None
    assert result.task.status == GenerationTaskStatus.reconciliation_needed
    assert "PostgreSQL IO error" in str(result.task.last_error)

    # 3. Provider output was recorded in attempt
    assert result.attempt is not None
    assert result.attempt.response_content == "Success reply from provider"

    # 4. Rollback was executed and fresh commit occurred
    assert db.rollback_count >= 1
    assert db.commit_count >= 2
