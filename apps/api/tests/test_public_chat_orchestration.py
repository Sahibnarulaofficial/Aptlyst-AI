"""Comprehensive unit test suite verifying integration of billing-aware orchestrator into public chat.

Verifies:
1. The public visitor message endpoint invokes orchestrate_assistant_reply, never the legacy unmetered service.
2. A quota-exhausted or inactive-billing response rejects inference without dispatching the provider.
3. Successful generation preserves the exact public API response contract (VisitorMessageResponse).
4. Duplicate visitor-message submissions (idempotent replay) return existing reply without duplicate provider calls or billable tasks.
5. Generation failures preserve the visitor message and safely manage reservations.
6. Ambiguous outcomes (timeouts) quarantine task liability and block automatic retries.
7. Multi-tenant boundaries, bot status checks, and visitor session security remain airtight.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app.auth.visitor_session import (
    VisitorSession,
    create_visitor_token,
    verify_visitor_token,
)
from app.config import settings
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import (
    Conversation,
    ConversationStatus,
    Message,
    MessageSenderType,
)
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.schemas.conversation import (
    GenerationStatus,
    VisitorMessageCreate,
    VisitorMessageResponse,
)
from app.api.public_chatbots import (
    _execute_orchestrated_generation,
    _map_orchestrator_error_to_public_message,
    create_visitor_message,
)
from app.services.billing.pricing import MOCK_GPT_4O_MINI_PRICING
from app.services.llm.base import LLMResponse, LLMTimeoutError, LLMUsage
from app.services.llm.mock import MockLLMProvider
from app.services.llm.orchestrator import (
    OrchestrationResult,
    OrchestratorErrorCode,
    orchestrate_assistant_reply,
)
from tests.test_orchestrator import MockOrchestratorSession, MockResult

T = TypeVar("T")


@pytest.fixture(autouse=True)
def configure_mock_provider():
    """Ensure mock LLM provider is active for unit tests."""
    original = settings.llm_provider
    settings.llm_provider = "mock"
    yield
    settings.llm_provider = original


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to immediate completion without requiring an OS event loop."""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise RuntimeError(f"Coroutine {coro} yielded unexpectedly before completing")


class MockPublicSession(MockOrchestratorSession):
    """In-memory async session extending MockOrchestratorSession with Chatbot and Conversation models."""

    def __init__(
        self,
        *,
        chatbots: dict[uuid.UUID, Chatbot] | None = None,
        conversations: dict[uuid.UUID, Conversation] | None = None,
        billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] | None = None,
        tasks: dict[uuid.UUID, GenerationTask] | None = None,
        reservations: dict[uuid.UUID, UsageReservation] | None = None,
        usage_logs: dict[uuid.UUID, LLMUsageLog] | None = None,
        attempts: dict[uuid.UUID, GenerationTaskAttempt] | None = None,
        messages: dict[uuid.UUID, Message] | None = None,
    ) -> None:
        super().__init__(
            billing_periods=billing_periods,
            tasks=tasks,
            reservations=reservations,
            usage_logs=usage_logs,
            attempts=attempts,
            messages=messages,
        )
        self.chatbots = chatbots or {}
        self.conversations = conversations or {}

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        super().add(obj)
        if isinstance(obj, Chatbot):
            self.chatbots[obj.id] = obj
        elif isinstance(obj, Conversation):
            self.conversations[obj.id] = obj

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)

    async def execute(self, stmt: Any) -> MockResult:
        col = stmt.column_descriptions[0]
        entity = col.get("entity")
        params = stmt.compile().params

        # Query: select(Chatbot)
        if entity is Chatbot:
            b_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            bot = self.chatbots.get(b_id)
            # Filter status if required
            status_param = next((v for v in params.values() if isinstance(v, ChatbotStatus)), None)
            if status_param is not None and bot and bot.status != status_param:
                return MockResult(None)
            return MockResult(bot)

        # Query: select(Conversation)
        if entity is Conversation:
            c_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
            conv = self.conversations.get(c_id)
            return MockResult(conv)

        # Query: select(Message)
        if entity is Message:
            client_msg_id = next(
                (v for v in params.values() if isinstance(v, str)),
                None,
            )
            conv_id = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)

            if client_msg_id is not None:
                matching_msg = next(
                    (
                        m for m in self.messages.values()
                        if m.client_message_id == client_msg_id
                        and (conv_id is None or m.conversation_id == conv_id)
                    ),
                    None,
                )
                return MockResult(matching_msg)

            if conv_id is not None:
                matched_msgs = [m for m in self.messages.values() if m.conversation_id == conv_id]
                matched_msgs.sort(key=lambda m: m.created_at, reverse=True)
                return MockResult(items=matched_msgs)

        return await super().execute(stmt)


def _make_dummy_request(body_bytes: bytes = b'{"content": "test"}') -> Request:
    """Create a minimal Starlette Request object for testing."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/public/chatbots/dummy/conversations/dummy/messages",
        "headers": [(b"content-length", str(len(body_bytes)).encode("latin1"))],
        "client": ("127.0.0.1", 54321),
    }
    req = Request(scope)
    # Cache body for request.body()
    req._body = body_bytes
    return req


def _create_test_environment():
    """Build a coherent, active multi-tenant test setup in memory."""
    org_id = uuid.uuid4()
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    bot = Chatbot(
        id=bot_id,
        organization_id=org_id,
        name="Support Assistant",
        instructions="Be polite and concise.",
        status=ChatbotStatus.active,
    )

    conv = Conversation(
        id=conv_id,
        chatbot_id=bot_id,
        organization_id=org_id,
        visitor_id="visitor_test_1",
        status=ConversationStatus.active,
    )

    now = datetime.now(timezone.utc)
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=now - timedelta(days=1),
        period_end=now + timedelta(days=29),
        is_active=True,
        is_blocked=False,
        monthly_token_quota=100_000,
        tokens_used=0,
        tokens_reserved=0,
        hard_spending_cap_micros=10_000_000,  # $10.00
        cost_micros_used=0,
        cost_micros_reserved=0,
    )

    session = MockPublicSession(
        chatbots={bot_id: bot},
        conversations={conv_id: conv},
        billing_periods={bp.id: bp},
    )

    visitor_session = VisitorSession(
        visitor_id="visitor_test_1",
        bot_id=bot_id,
        conversation_id=conv_id,
        expires_at=int(now.timestamp()) + 3600,
    )

    return bot, conv, bp, session, visitor_session


# ============================================================================
# Invariant 1: Public endpoint invokes orchestrator, never legacy service
# ============================================================================


def test_public_endpoint_invokes_orchestrator_not_legacy_service():
    """Verify that create_visitor_message routes through orchestrate_assistant_reply."""
    bot, conv, bp, db, v_session = _create_test_environment()
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Hello AI", client_message_id="msg_001")

    with patch(
        "app.api.public_chatbots.orchestrate_assistant_reply",
        wraps=orchestrate_assistant_reply,
    ) as mock_orch, patch(
        "app.services.llm.service.generate_and_persist_assistant_reply"
    ) as mock_legacy:
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        assert mock_orch.called
        assert not mock_legacy.called
        assert res.generation_status == GenerationStatus.completed
        assert res.assistant_reply is not None
        assert "Hello AI" in res.assistant_reply


def test_disabled_provider_skips_generation_and_preserves_visitor_message():
    """Verify that when llm_provider is 'disabled', generation is skipped without error."""
    bot, conv, bp, db, v_session = _create_test_environment()
    settings.llm_provider = "disabled"
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Hello when disabled")

    res = run_async(
        create_visitor_message(
            bot_id=bot.id,
            conversation_id=conv.id,
            body=body,
            request=req,
            response=resp,
            db=db,
            session=v_session,
        )
    )

    assert res.message.content == "Hello when disabled"
    assert res.assistant_reply is None
    assert res.assistant_message is None
    assert res.generation_status == GenerationStatus.skipped
    assert res.generation_error is None


# ============================================================================
# Invariant 2: Quota-exhausted or inactive billing does not dispatch provider
# ============================================================================


def test_inactive_billing_period_blocks_provider_dispatch():
    """Verify that an inactive billing period prevents provider calls and preserves visitor message."""
    bot, conv, bp, db, v_session = _create_test_environment()
    # Remove billing period to simulate no active subscription
    db.billing_periods.clear()

    mock_provider = MockLLMProvider()
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Will this be answered?")

    with patch("app.services.llm.orchestrator.get_llm_provider", return_value=mock_provider):
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        # Provider must NOT have been called
        assert mock_provider.call_count == 0
        # Visitor message must be preserved
        assert res.message.content == "Will this be answered?"
        assert res.assistant_reply is None
        assert res.assistant_message is None
        assert res.generation_status == GenerationStatus.failed
        assert res.generation_error == (
            "The AI assistant is currently unavailable due to organization service limits. Your message was saved."
        )


def test_exhausted_quota_blocks_provider_dispatch():
    """Verify that exhausted token quota rejects turn before provider dispatch."""
    bot, conv, bp, db, v_session = _create_test_environment()
    # Exhaust monthly quota
    bp.tokens_used = bp.monthly_token_quota

    mock_provider = MockLLMProvider()
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Need an answer")

    with patch("app.services.llm.orchestrator.get_llm_provider", return_value=mock_provider):
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        assert mock_provider.call_count == 0
        assert res.message.content == "Need an answer"
        assert res.assistant_reply is None
        assert res.generation_status == GenerationStatus.failed
        assert res.generation_error == (
            "The AI assistant is currently unavailable due to usage limits. Your message was saved."
        )


# ============================================================================
# Invariant 3: Successful generation preserves API response format
# ============================================================================


def test_successful_generation_preserves_response_format():
    """Verify the exact VisitorMessageResponse contract when generation succeeds."""
    bot, conv, bp, db, v_session = _create_test_environment()
    mock_provider = MockLLMProvider(canned_response="Accurate response.")
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="What are the rates?", client_message_id="msg_fmt")

    with patch("app.services.llm.orchestrator.get_llm_provider", return_value=mock_provider):
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        assert isinstance(res, VisitorMessageResponse)
        assert res.message.content == "What are the rates?"
        assert res.message.client_message_id == "msg_fmt"
        assert res.assistant_reply == "Accurate response."
        assert res.assistant_message is not None
        assert res.assistant_message.content == "Accurate response."
        assert res.assistant_message.sender_type == MessageSenderType.assistant
        assert res.assistant_message.client_message_id == f"reply:{res.message.id}"
        assert res.generation_status == GenerationStatus.completed
        assert res.generation_error is None

        # Verify underlying accounting artifacts were recorded
        assert len(db.tasks) == 1
        task = next(iter(db.tasks.values()))
        assert task.status == GenerationTaskStatus.completed
        assert len(db.usage_logs) == 1


# ============================================================================
# Invariant 4: Duplicate submissions do not duplicate tasks or provider calls
# ============================================================================


def test_duplicate_visitor_message_submission_is_idempotent():
    """Verify that re-submitting with identical client_message_id does not re-invoke provider."""
    bot, conv, bp, db, v_session = _create_test_environment()
    mock_provider = MockLLMProvider(canned_response="Single answer.")
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Initial submission", client_message_id="idemp_key_1")

    with patch("app.services.llm.orchestrator.get_llm_provider", return_value=mock_provider):
        # 1. First submission
        res1 = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )
        assert res1.generation_status == GenerationStatus.completed
        assert mock_provider.call_count == 1
        assert len(db.tasks) == 1

        # 2. Second submission with same client_message_id
        res2 = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        # Zero additional provider calls or generation tasks
        assert mock_provider.call_count == 1
        assert len(db.tasks) == 1
        assert res2.assistant_reply == "Single answer."
        assert res2.generation_status == GenerationStatus.completed
        assert resp.status_code == 200


# ============================================================================
# Invariant 5: Generation failure preserves visitor message and handles reservations safely
# ============================================================================


def test_pre_dispatch_failure_preserves_message_and_releases_reservation():
    """Verify that when pre-dispatch context building fails, reservation is released cleanly."""
    bot, conv, bp, db, v_session = _create_test_environment()
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Context will fail")

    with patch(
        "app.services.llm.orchestrator.build_conversation_context",
        side_effect=RuntimeError("Context build exploded"),
    ):
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        assert res.message.content == "Context will fail"
        assert res.generation_status == GenerationStatus.failed
        assert res.assistant_reply is None
        assert "temporarily unavailable" in res.generation_error

        # Reservation should be released (zero active reservations)
        active_res = [r for r in db.reservations.values() if r.status == ReservationStatus.active]
        assert len(active_res) == 0
        assert bp.tokens_reserved == 0


# ============================================================================
# Invariant 6: Ambiguous outcomes quarantine and block second provider call
# ============================================================================


def test_provider_timeout_quarantines_task_and_blocks_retry():
    """Verify provider timeout retains reservation liability and marks task reconciliation_needed."""
    bot, conv, bp, db, v_session = _create_test_environment()
    mock_provider = MockLLMProvider(force_timeout=True)
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Will timeout", client_message_id="timeout_key")

    with patch("app.services.llm.orchestrator.get_llm_provider", return_value=mock_provider):
        res = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )

        assert res.generation_status == GenerationStatus.failed
        assert res.generation_error == (
            "The AI assistant timed out while generating a response. Your message was saved."
        )

        # Task MUST be quarantined
        task = next(iter(db.tasks.values()))
        assert task.status == GenerationTaskStatus.reconciliation_needed
        assert task.attempts == 1

        # Attempt MUST be marked ambiguous
        attempt = next(iter(db.attempts.values()))
        assert attempt.status == GenerationAttemptStatus.ambiguous

        # Reservation liability is PRESERVED (never released)
        reservation = next(iter(db.reservations.values()))
        assert reservation.status == ReservationStatus.active
        assert bp.tokens_reserved > 0

        # Attempting to re-execute for the same visitor message must NOT trigger a second provider call
        res_retry = run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )
        assert mock_provider.call_count == 1
        assert res_retry.generation_status == GenerationStatus.failed


# ============================================================================
# Invariant 7: Tenant isolation and visitor token validation intact
# ============================================================================


def test_inactive_or_missing_bot_returns_404():
    """Verify that a nonexistent or inactive bot raises 404."""
    bot, conv, bp, db, v_session = _create_test_environment()
    bot.status = ChatbotStatus.inactive
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Hello")

    with pytest.raises(HTTPException) as exc_info:
        run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )
    assert exc_info.value.status_code == 404


def test_inactive_conversation_returns_400():
    """Verify that an inactive conversation raises 400."""
    bot, conv, bp, db, v_session = _create_test_environment()
    conv.status = ConversationStatus.archived
    req = _make_dummy_request()
    resp = Response()
    body = VisitorMessageCreate(content="Hello")

    with pytest.raises(HTTPException) as exc_info:
        run_async(
            create_visitor_message(
                bot_id=bot.id,
                conversation_id=conv.id,
                body=body,
                request=req,
                response=resp,
                db=db,
                session=v_session,
            )
        )
    assert exc_info.value.status_code == 400


def test_visitor_token_mismatched_conversation_fails_validation():
    """Verify that a visitor token bound to conversation A cannot access conversation B."""
    bot_id = uuid.uuid4()
    conv_a = uuid.uuid4()
    conv_b = uuid.uuid4()

    token_a, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_a)

    with pytest.raises(HTTPException) as exc_info:
        verify_visitor_token(
            token=token_a,
            expected_bot_id=bot_id,
            expected_conversation_id=conv_b,
        )
    assert exc_info.value.status_code == 403
    assert "does not match" in exc_info.value.detail.lower()


def test_reserved_reply_prefix_rejected_by_schema():
    """Verify that client_message_id cannot impersonate system reply prefix."""
    with pytest.raises(ValueError) as exc_info:
        VisitorMessageCreate(
            content="Attempted spoofing",
            client_message_id="reply:fake_assistant_id",
        )
    assert "reserved 'reply:' prefix" in str(exc_info.value)
