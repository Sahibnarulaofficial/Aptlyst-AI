"""Comprehensive test suite for Milestone 3 Phase 1: Reliable AI Generation Foundation.

Covers:
1. Provider abstraction & mock implementation:
   - Deterministic structured output
   - Explicit timeout behavior (LLMTimeoutError)
   - Explicit provider error behavior (LLMProviderError)
   - Production safeguard (rejection in production environments)
2. Conversation context builder:
   - History window bounding
   - Strict role separation (system instructions vs user content)
   - Tenant & conversation isolation (zero cross-conversation data)
3. Lifecycle & durability integration:
   - Successful visitor submission & assistant reply persistence
   - Explicit 1:1 reply correlation via client_message_id = f"reply:{msg.id}"
   - Visitor message preservation on timeout / provider error
   - Clear failure representation (generation_status == 'failed')
   - Idempotent replay without duplicate inference
   - Concurrent submission safety
   - Zero provider calls on invalid session or inactive bot
   - Disabled provider backward compatibility (Milestone 1 contract)
"""

import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.visitor_session import create_visitor_token
from app.config import Settings, settings
from app.db.session import get_db
import app.models.subscription  # noqa: F401
from app.main import app
from app.models.billing import OrganizationBillingPeriod
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSenderType
from app.models.generation import GenerationTaskStatus
from app.schemas.conversation import GenerationStatus, VisitorMessageCreate
from app.services.llm import (
    BaseLLMProvider,
    LLMConfigurationError,
    LLMMessage,
    LLMProviderError,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
    MockLLMProvider,
    build_conversation_context,
    generate_and_persist_assistant_reply,
    set_llm_provider_for_testing,
)
from tests.test_public_chat_orchestration import MockPublicSession


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def reset_test_provider():
    """Reset the global LLM provider between tests and configure mock provider by default."""
    original_provider = settings.llm_provider
    settings.llm_provider = "mock"
    set_llm_provider_for_testing(None)
    yield
    settings.llm_provider = original_provider
    set_llm_provider_for_testing(None)


# ============================================================================
# 1. Provider Abstraction & Mock Provider Unit Tests
# ============================================================================


@pytest.mark.anyio
async def test_mock_provider_deterministic_output() -> None:
    provider = MockLLMProvider(model_name="mock-test-bot")
    messages = [
        LLMMessage(role="user", content="How much does your plan cost?")
    ]
    response = await provider.generate_reply(
        messages=messages,
        system_prompt="You are a helpful sales assistant.",
        max_tokens=100,
    )

    assert isinstance(response, LLMResponse)
    assert response.model == "mock-test-bot"
    assert "[MOCK AI]" in response.content
    assert "How much does your plan cost?" in response.content
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens > 0
    assert response.usage.completion_tokens > 0
    assert response.usage.total_tokens == response.usage.prompt_tokens + response.usage.completion_tokens
    assert provider.call_count == 1


@pytest.mark.anyio
async def test_mock_provider_timeout_behavior() -> None:
    provider = MockLLMProvider(force_timeout=True)
    messages = [LLMMessage(role="user", content="Hello")]

    with pytest.raises(LLMTimeoutError) as exc_info:
        await provider.generate_reply(messages=messages, timeout_seconds=5.0)

    assert "timed out" in str(exc_info.value).lower()
    assert provider.call_count == 1


@pytest.mark.anyio
async def test_mock_provider_error_behavior() -> None:
    provider = MockLLMProvider(force_error="Rate limit exceeded on upstream")
    messages = [LLMMessage(role="user", content="Hello")]

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_reply(messages=messages)

    assert "upstream failure" in str(exc_info.value).lower()
    assert "Rate limit exceeded" in str(exc_info.value)


def test_mock_provider_production_safeguard() -> None:
    """Verifies that MockLLMProvider refuses to instantiate in production without permission."""
    with patch("app.services.llm.mock.settings") as mock_settings:
        mock_settings.environment = "production"
        mock_settings.llm_allow_mock_in_production = False

        with pytest.raises(LLMConfigurationError) as exc_info:
            MockLLMProvider()

        assert "prohibited from running in production" in str(exc_info.value)

        # Explicit allow_in_production succeeds
        allowed_provider = MockLLMProvider(allow_in_production=True)
        assert allowed_provider is not None


def test_production_settings_validation_blocks_mock() -> None:
    """Settings startup validation rejects llm_provider='mock' in production."""
    with pytest.raises(ValueError) as exc_info:
        Settings(
            environment="production",
            visitor_session_secret="a-very-long-production-secret-with-plenty-of-entropy-123456",
            llm_provider="mock",
            llm_allow_mock_in_production=False,
        )
    assert "Mock LLM provider is strictly for development" in str(exc_info.value)


def test_settings_supported_llm_providers() -> None:
    """Settings accepts 'disabled' and 'mock' (in non-production)."""
    s1 = Settings(llm_provider="disabled")
    assert s1.llm_provider == "disabled"

    s2 = Settings(llm_provider="mock", environment="development")
    assert s2.llm_provider == "mock"


def test_settings_llm_provider_normalization() -> None:
    """Settings normalizes case and whitespace for llm_provider."""
    s1 = Settings(llm_provider="  DISABLED  ")
    assert s1.llm_provider == "disabled"

    s2 = Settings(llm_provider="  Mock  ", environment="development")
    assert s2.llm_provider == "mock"


@pytest.mark.parametrize("invalid_provider", ["openai", "mokc", "anthropic", "", "llm"])
def test_settings_rejects_unsupported_llm_providers(invalid_provider: str) -> None:
    """Settings rejects unsupported or misspelled providers at initialization."""
    with pytest.raises(ValueError) as exc_info:
        Settings(llm_provider=invalid_provider)
    assert "Unsupported LLM provider" in str(exc_info.value)
    assert "Phase 1 accepts only 'disabled' or 'mock'" in str(exc_info.value)


def test_production_settings_validation_blocks_mock_with_whitespace() -> None:
    """Settings startup validation rejects llm_provider='  MOCK  ' in production."""
    with pytest.raises(ValueError) as exc_info:
        Settings(
            environment="production",
            visitor_session_secret="a-very-long-production-secret-with-plenty-of-entropy-123456",
            llm_provider="  MOCK  ",
            llm_allow_mock_in_production=False,
        )
    assert "Mock LLM provider is strictly for development" in str(exc_info.value)


# ============================================================================
# 1b. Schema Validation Unit Tests (Reply Namespace Protection)
# ============================================================================


def test_visitor_message_create_rejects_reserved_reply_prefix() -> None:
    """VisitorMessageCreate rejects IDs starting with 'reply:' (case-insensitive, trimmed)."""
    # 1. Exact "reply:"
    with pytest.raises(ValueError) as exc:
        VisitorMessageCreate(content="Hello", client_message_id="reply:msg-123")
    assert "reserved 'reply:' prefix" in str(exc.value)

    # 2. Case-insensitive "Reply:"
    with pytest.raises(ValueError) as exc:
        VisitorMessageCreate(content="Hello", client_message_id="Reply:msg-456")
    assert "reserved 'reply:' prefix" in str(exc.value)

    # 3. Leading whitespace "   reply:msg-789"
    with pytest.raises(ValueError) as exc:
        VisitorMessageCreate(content="Hello", client_message_id="   reply:msg-789")
    assert "reserved 'reply:' prefix" in str(exc.value)

    # 4. Mixed casing and leading/trailing whitespace "  REPLY:msg-abc  "
    with pytest.raises(ValueError) as exc:
        VisitorMessageCreate(content="Hello", client_message_id="  REPLY:msg-abc  ")
    assert "reserved 'reply:' prefix" in str(exc.value)


def test_visitor_message_create_accepts_valid_id_and_none() -> None:
    """VisitorMessageCreate accepts valid normal IDs, whitespace-trimmed, and None."""
    # Valid normal ID
    msg1 = VisitorMessageCreate(content="Hello", client_message_id="msg-12345")
    assert msg1.client_message_id == "msg-12345"

    # Leading/trailing whitespace on valid ID is trimmed without mismatch
    msg2 = VisitorMessageCreate(content="Hello", client_message_id="  msg-67890  ")
    assert msg2.client_message_id == "msg-67890"

    # None is preserved
    msg3 = VisitorMessageCreate(content="Hello", client_message_id=None)
    assert msg3.client_message_id is None

    # Empty / whitespace-only string is treated as None
    msg4 = VisitorMessageCreate(content="Hello", client_message_id="   ")
    assert msg4.client_message_id is None

    # Normal non-reserved prefix like "reply_to" is allowed
    msg5 = VisitorMessageCreate(content="Hello", client_message_id="reply_to_msg_1")
    assert msg5.client_message_id == "reply_to_msg_1"


# ============================================================================
# 2. Context Window & Tenant Isolation Unit Tests
# ============================================================================


@pytest.mark.anyio
async def test_context_window_bounds_and_role_separation() -> None:
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Support Bot",
        instructions="System Prompt: Speak like a pirate.",
        status=ChatbotStatus.active,
    )

    # Create 6 historical messages
    mock_messages: list[Message] = []
    for i in range(6):
        sender = MessageSenderType.visitor if i % 2 == 0 else MessageSenderType.assistant
        m = Message(
            id=uuid.uuid4(),
            conversation_id=conv_id,
            sender_type=sender,
            content=f"Message turn {i}",
        )
        mock_messages.append(m)

    curr_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Final current user question",
    )

    # Mock AsyncSession to return history
    mock_session = AsyncMock()
    mock_result = MagicMock()
    # Return 3 most recent preceding turns (Message turn 3, 4, 5)
    mock_result.scalars.return_value.all.return_value = list(reversed(mock_messages[-3:]))
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Request max 4 messages in context (3 history + 1 current)
    context = await build_conversation_context(
        db=mock_session,
        chatbot=bot,
        conversation_id=conv_id,
        current_message=curr_msg,
        max_context_messages=4,
    )

    assert len(context) == 4
    # History mapped correctly
    assert context[0].content == "Message turn 3"
    assert context[0].role == "assistant"
    assert context[1].content == "Message turn 4"
    assert context[1].role == "user"
    assert context[2].content == "Message turn 5"
    assert context[2].role == "assistant"
    # Final turn is current message with role 'user'
    assert context[3].content == "Final current user question"
    assert context[3].role == "user"


@pytest.mark.anyio
async def test_context_tenant_and_conversation_isolation() -> None:
    """Verifies that the SQL query for conversation context strictly filters by conversation_id."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Support Bot",
        instructions="Isolated bot instructions",
        status=ChatbotStatus.active,
    )

    curr_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Hello in Conv A",
    )

    captured_stmts = []

    async def mock_execute(stmt):
        captured_stmts.append(stmt)
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    mock_session = AsyncMock()
    mock_session.execute = mock_execute

    await build_conversation_context(
        db=mock_session,
        chatbot=bot,
        conversation_id=conv_id,
        current_message=curr_msg,
        max_context_messages=10,
    )

    assert len(captured_stmts) == 1
    compiled_sql = str(captured_stmts[0])
    assert "messages.conversation_id =" in compiled_sql


# ============================================================================
# 3. Lifecycle, Idempotency & Failure Integration Tests
# ============================================================================


@pytest.mark.anyio
async def test_ai_generation_service_success() -> None:
    """Unit integration test for generate_and_persist_assistant_reply."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        instructions="Be concise.",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="What is Aptlyst?",
    )

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    # 1. No existing reply
    first_result = MagicMock()
    first_result.scalar_one_or_none.return_value = None
    # 2. Context query
    context_result = MagicMock()
    context_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[first_result, context_result])

    provider = MockLLMProvider(canned_response="Aptlyst is an autonomous AI customer support platform.")

    result = await generate_and_persist_assistant_reply(
        db=mock_db,
        chatbot=bot,
        conversation_id=conv_id,
        visitor_message=visitor_msg,
        provider=provider,
    )

    assert result.status == GenerationStatus.completed
    assert result.reply_message is not None
    assert result.reply_message.sender_type == MessageSenderType.assistant
    assert result.reply_message.content == "Aptlyst is an autonomous AI customer support platform."
    assert result.reply_message.client_message_id == f"reply:{visitor_msg.id}"
    assert result.error is None
    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()


@pytest.mark.anyio
async def test_ai_generation_service_idempotency_returns_existing() -> None:
    """Verifies that an existing reply is returned without re-invoking the provider."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="What is Aptlyst?",
    )
    existing_assistant_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.assistant,
        content="Cached prior answer",
        client_message_id=f"reply:{visitor_msg.id}",
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_assistant_msg
    mock_db.execute = AsyncMock(return_value=mock_result)

    provider = MockLLMProvider()

    result = await generate_and_persist_assistant_reply(
        db=mock_db,
        chatbot=bot,
        conversation_id=conv_id,
        visitor_message=visitor_msg,
        provider=provider,
    )

    assert result.status == GenerationStatus.completed
    assert result.reply_message == existing_assistant_msg
    assert provider.call_count == 0  # Zero calls to provider!


@pytest.mark.anyio
async def test_ai_generation_service_timeout_handling() -> None:
    """Verifies timeout handling: visitor message is untouched, failure status is returned."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Will this time out?",
    )

    mock_db = AsyncMock()
    first_result = MagicMock()
    first_result.scalar_one_or_none.return_value = None
    context_result = MagicMock()
    context_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[first_result, context_result])

    provider = MockLLMProvider(force_timeout=True)

    result = await generate_and_persist_assistant_reply(
        db=mock_db,
        chatbot=bot,
        conversation_id=conv_id,
        visitor_message=visitor_msg,
        provider=provider,
    )

    assert result.status == GenerationStatus.failed
    assert result.reply_message is None
    assert result.error is not None
    assert "timed out" in result.error.lower()
    mock_db.add.assert_not_called()


@pytest.mark.anyio
async def test_ai_generation_service_provider_error_handling() -> None:
    """Verifies provider 500 error handling: visitor message is untouched, failure status is returned."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Will this fail?",
    )

    mock_db = AsyncMock()
    first_result = MagicMock()
    first_result.scalar_one_or_none.return_value = None
    context_result = MagicMock()
    context_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[first_result, context_result])

    provider = MockLLMProvider(force_error="Upstream model overloaded")

    result = await generate_and_persist_assistant_reply(
        db=mock_db,
        chatbot=bot,
        conversation_id=conv_id,
        visitor_message=visitor_msg,
        provider=provider,
    )

    assert result.status == GenerationStatus.failed
    assert result.reply_message is None
    assert result.error is not None
    assert "temporarily unavailable" in result.error.lower()
    mock_db.add.assert_not_called()


@pytest.mark.anyio
async def test_ai_generation_service_disabled_provider_skips() -> None:
    """Verifies that if llm_provider='disabled', generation is skipped."""
    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Hello",
    )

    mock_db = AsyncMock()
    first_result = MagicMock()
    first_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=first_result)

    with patch("app.services.llm.service.settings") as mock_settings:
        mock_settings.llm_provider = "disabled"
        result = await generate_and_persist_assistant_reply(
            db=mock_db,
            chatbot=bot,
            conversation_id=conv_id,
            visitor_message=visitor_msg,
        )

        assert result.status == GenerationStatus.skipped
        assert result.reply_message is None
        assert result.error is None


# ============================================================================
# 4. HTTP API Public Chatbots Integration Tests
# ============================================================================


@pytest.mark.anyio
async def test_http_api_message_submission_generates_mock_reply(client: AsyncClient) -> None:
    """End-to-end integration: Visitor posts message -> assistant reply generated and returned."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session_token, visitor_id = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    mock_bot = Chatbot(
        id=bot_id,
        organization_id=org_id,
        name="Live Assistant",
        instructions="Assist courteously.",
        status=ChatbotStatus.active,
    )
    mock_conv = Conversation(
        id=conv_id,
        organization_id=org_id,
        chatbot_id=bot_id,
        visitor_id=visitor_id,
        status=ConversationStatus.active,
    )

    now = datetime.now(timezone.utc)
    mock_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=now - timedelta(days=1),
        period_end=now + timedelta(days=29),
        is_active=True,
        is_blocked=False,
        monthly_token_quota=100_000,
        tokens_used=0,
        tokens_reserved=0,
        hard_spending_cap_micros=10_000_000,
        cost_micros_used=0,
        cost_micros_reserved=0,
    )

    custom_mock = MockLLMProvider(canned_response="Hello! I am your AI assistant.")
    set_llm_provider_for_testing(custom_mock)

    mock_session = MockPublicSession(
        chatbots={bot_id: mock_bot},
        conversations={conv_id: mock_conv},
        billing_periods={mock_bp.id: mock_bp},
    )
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        client_msg_id = f"cli_{uuid.uuid4().hex[:10]}"
        res = await client.post(
            f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"content": "What are your features?", "client_message_id": client_msg_id},
        )

        assert res.status_code == 201
        data = res.json()
        assert data["message"]["content"] == "What are your features?"
        assert data["message"]["client_message_id"] == client_msg_id
        assert data["assistant_reply"] == "Hello! I am your AI assistant."
        assert data["assistant_message"] is not None
        assert data["assistant_message"]["sender_type"] == "assistant"
        assert data["assistant_message"]["content"] == "Hello! I am your AI assistant."
        assert data["generation_status"] == "completed"
        assert data["generation_error"] is None
        assert custom_mock.call_count == 1

        # Verify orchestrator task and usage accounting occurred
        assert len(mock_session.tasks) == 1
        assert len(mock_session.usage_logs) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_http_api_timeout_preserves_visitor_message(client: AsyncClient) -> None:
    """Verifies that provider timeout returns 201 with preserved visitor message and failure status."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session_token, visitor_id = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    mock_bot = Chatbot(
        id=bot_id,
        organization_id=org_id,
        name="Live Assistant",
        status=ChatbotStatus.active,
    )
    mock_conv = Conversation(
        id=conv_id,
        organization_id=org_id,
        chatbot_id=bot_id,
        visitor_id=visitor_id,
        status=ConversationStatus.active,
    )

    now = datetime.now(timezone.utc)
    mock_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=now - timedelta(days=1),
        period_end=now + timedelta(days=29),
        is_active=True,
        is_blocked=False,
        monthly_token_quota=100_000,
        tokens_used=0,
        tokens_reserved=0,
        hard_spending_cap_micros=10_000_000,
        cost_micros_used=0,
        cost_micros_reserved=0,
    )

    custom_mock = MockLLMProvider(force_timeout=True)
    set_llm_provider_for_testing(custom_mock)

    mock_session = MockPublicSession(
        chatbots={bot_id: mock_bot},
        conversations={conv_id: mock_conv},
        billing_periods={mock_bp.id: mock_bp},
    )
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        res = await client.post(
            f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"content": "This will trigger an LLM timeout."},
        )

        assert res.status_code == 201
        data = res.json()
        # Visitor message MUST be preserved
        assert data["message"]["content"] == "This will trigger an LLM timeout."
        assert data["assistant_reply"] is None
        assert data["assistant_message"] is None
        assert data["generation_status"] == "failed"
        assert "timed out" in data["generation_error"].lower()

        # Orchestrator invariant: timeout preserves liability and quarantines task
        assert len(mock_session.tasks) == 1
        task = next(iter(mock_session.tasks.values()))
        assert task.status == GenerationTaskStatus.reconciliation_needed
        assert mock_bp.tokens_reserved > 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_http_api_idempotent_replay_without_duplicate_generation(client: AsyncClient) -> None:
    """Verifies that re-posting identical client_message_id returns existing reply without provider call."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session_token, visitor_id = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    mock_bot = Chatbot(
        id=bot_id,
        organization_id=org_id,
        name="Live Assistant",
        status=ChatbotStatus.active,
    )
    mock_conv = Conversation(
        id=conv_id,
        organization_id=org_id,
        chatbot_id=bot_id,
        visitor_id=visitor_id,
        status=ConversationStatus.active,
    )

    client_msg_id = "replay_key_abc"
    now = datetime.now(timezone.utc)
    v_msg_id = uuid.uuid4()
    existing_visitor_msg = Message(
        id=v_msg_id,
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Original question",
        client_message_id=client_msg_id,
        created_at=now,
    )
    existing_assistant_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.assistant,
        content="Original answer",
        client_message_id=f"reply:{v_msg_id}",
        created_at=now,
    )

    custom_mock = MockLLMProvider()
    set_llm_provider_for_testing(custom_mock)

    mock_session = MockPublicSession(
        chatbots={bot_id: mock_bot},
        conversations={conv_id: mock_conv},
        messages={
            existing_visitor_msg.id: existing_visitor_msg,
            existing_assistant_msg.id: existing_assistant_msg,
        },
    )
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        res = await client.post(
            f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"content": "Original question", "client_message_id": client_msg_id},
        )

        assert res.status_code == 200
        data = res.json()
        assert data["message"]["client_message_id"] == client_msg_id
        assert data["assistant_reply"] == "Original answer"
        assert data["assistant_message"]["content"] == "Original answer"
        assert data["generation_status"] == "completed"
        # Provider must NOT have been called!
        assert custom_mock.call_count == 0
        assert len(mock_session.tasks) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_inactive_bot_triggers_zero_provider_calls(client: AsyncClient) -> None:
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    session_token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    custom_mock = MockLLMProvider()
    set_llm_provider_for_testing(custom_mock)

    mock_session = AsyncMock()
    bot_res = MagicMock(); bot_res.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=bot_res)
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        res = await client.post(
            f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"content": "Are you active?"},
        )
        assert res.status_code == 404
        assert custom_mock.call_count == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_invalid_visitor_session_triggers_zero_provider_calls(client: AsyncClient) -> None:
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    custom_mock = MockLLMProvider()
    set_llm_provider_for_testing(custom_mock)

    # Submitting with an invalid signature
    res = await client.post(
        f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
        headers={"Authorization": "Bearer invalid.token.value"},
        json={"content": "Are you active?"},
    )
    assert res.status_code == 401
    assert custom_mock.call_count == 0


@pytest.mark.anyio
async def test_api_rejects_reserved_reply_id_before_db_or_provider(client: AsyncClient) -> None:
    """Submitting a client_message_id with reserved 'reply:' prefix fails 422 before DB/provider."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    session_token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    custom_mock = MockLLMProvider()
    set_llm_provider_for_testing(custom_mock)

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_session

    try:
        res = await client.post(
            f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {session_token}"},
            json={
                "content": "Trying to inject reply prefix",
                "client_message_id": "reply:malicious-id",
            },
        )
        assert res.status_code == 422
        # Zero database writes
        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_called()
        # Zero provider calls
        assert custom_mock.call_count == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_concurrent_duplicate_submission_race_safety() -> None:
    """Verifies that concurrent race collisions on client_message_id safely resolve to the winner."""
    from sqlalchemy.exc import IntegrityError

    conv_id = uuid.uuid4()
    bot = Chatbot(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Test Bot",
        status=ChatbotStatus.active,
    )
    visitor_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.visitor,
        content="Concurrent question",
    )
    winning_reply = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_type=MessageSenderType.assistant,
        content="Winner reply",
        client_message_id=f"reply:{visitor_msg.id}",
    )

    # Simulate Task B:
    # 1. No reply initially
    # 2. Context query succeeds
    # 3. commit() raises IntegrityError (Task A committed first)
    # 4. rollback() executed
    # 5. Query for winner succeeds
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock(side_effect=IntegrityError("duplicate key", params=None, orig=Exception("unique constraint")))

    first_res = MagicMock(); first_res.scalar_one_or_none.return_value = None
    ctx_res = MagicMock(); ctx_res.scalars.return_value.all.return_value = []
    winner_res = MagicMock(); winner_res.scalar_one_or_none.return_value = winning_reply

    mock_db.execute = AsyncMock(side_effect=[first_res, ctx_res, winner_res])

    provider = MockLLMProvider()

    result = await generate_and_persist_assistant_reply(
        db=mock_db,
        chatbot=bot,
        conversation_id=conv_id,
        visitor_message=visitor_msg,
        provider=provider,
    )

    assert result.status == GenerationStatus.completed
    assert result.reply_message == winning_reply
    mock_db.rollback.assert_called_once()
