"""Comprehensive test suite for Conversation Foundation and Persistence (Milestone 1).

Covers:
- Visitor session token cryptographic signing, verification, and tamper detection.
- Cross-visitor and cross-bot isolation.
- Public conversation initialization on active vs draft/inactive bots.
- Message submission with size limit (<= 16 KB) and character limit (<= 2000).
- Message submission idempotency via client_message_id.
- Explicit API contract (assistant_reply is None).
- Chronological transcript retrieval.
- Authenticated dashboard conversation listing and role protection.
- Database cascade deletion (deleting chatbot cascades to conversations and messages).
- Multi-tenant database invariant (composite foreign key rejects cross-org associations).
"""

import os
import time
import uuid
from unittest.mock import patch

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.auth.visitor_session import (
    VISITOR_ALGORITHM,
    VISITOR_AUDIENCE,
    VISITOR_ISSUER,
    create_visitor_token,
    verify_visitor_token,
)
from app.db.session import AsyncSessionLocal
from app.main import app
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSenderType
from app.models.organization import Organization
from tests.test_auth import TEST_JWT_SECRET, make_token

RUN_INTEGRATION_TESTS = os.getenv("RUN_SUPABASE_INTEGRATION_TESTS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ============================================================================
# 1. Visitor Session Cryptographic Signing & Verification Unit Tests
# ============================================================================


def test_visitor_token_create_and_verify_success() -> None:
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    visitor_id = "test_visitor_123"

    token, returned_visitor_id = create_visitor_token(
        bot_id=bot_id, conversation_id=conv_id, visitor_id=visitor_id
    )
    assert returned_visitor_id == visitor_id
    assert isinstance(token, str)

    session = verify_visitor_token(
        token=token,
        expected_bot_id=bot_id,
        expected_conversation_id=conv_id,
    )
    assert session.visitor_id == visitor_id
    assert session.bot_id == bot_id
    assert session.conversation_id == conv_id
    assert session.expires_at > int(time.time())


def test_visitor_token_tampered_signature_fails() -> None:
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id)

    # Tamper with the token characters
    tampered = token[:-4] + "AAAA"

    with pytest.raises(Exception) as exc_info:
        verify_visitor_token(
            token=tampered,
            expected_bot_id=bot_id,
            expected_conversation_id=conv_id,
        )
    assert "Invalid visitor session token" in str(exc_info.value)


def test_visitor_token_expired_fails() -> None:
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    # Create expired token directly with secret
    from app.config import settings

    past_time = int(time.time()) - 3600
    claims = {
        "sub": "expired_visitor",
        "bot_id": str(bot_id),
        "conv_id": str(conv_id),
        "iat": past_time - 60,
        "exp": past_time,
        "aud": VISITOR_AUDIENCE,
        "iss": VISITOR_ISSUER,
    }
    expired_token = jwt.encode(
        claims,
        settings.resolved_visitor_session_secret,
        algorithm=VISITOR_ALGORITHM,
    )

    with pytest.raises(Exception) as exc_info:
        verify_visitor_token(
            token=expired_token,
            expected_bot_id=bot_id,
            expected_conversation_id=conv_id,
        )
    assert "Visitor session expired" in str(exc_info.value)


def test_visitor_token_mismatched_bot_fails() -> None:
    bot_id_1 = uuid.uuid4()
    bot_id_2 = uuid.uuid4()
    conv_id = uuid.uuid4()

    token, _ = create_visitor_token(bot_id=bot_id_1, conversation_id=conv_id)

    with pytest.raises(Exception) as exc_info:
        verify_visitor_token(
            token=token,
            expected_bot_id=bot_id_2,
            expected_conversation_id=conv_id,
        )
    assert "Session token does not match requested chatbot" in str(exc_info.value)


def test_visitor_token_mismatched_conversation_fails() -> None:
    bot_id = uuid.uuid4()
    conv_id_1 = uuid.uuid4()
    conv_id_2 = uuid.uuid4()

    token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id_1)

    with pytest.raises(Exception) as exc_info:
        verify_visitor_token(
            token=token,
            expected_bot_id=bot_id,
            expected_conversation_id=conv_id_2,
        )
    assert "Session token does not match requested conversation" in str(exc_info.value)


def test_visitor_token_dual_secret_rotation() -> None:
    """Verifies that secondary secret verifies existing tokens during secret rotation."""
    from app.config import settings

    old_secret = "old-secret-at-least-32-chars-long-12345"
    new_secret = "new-secret-at-least-32-chars-long-67890"

    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    # Generate token using old_secret
    claims = {
        "sub": "rotating_visitor",
        "bot_id": str(bot_id),
        "conv_id": str(conv_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "aud": VISITOR_AUDIENCE,
        "iss": VISITOR_ISSUER,
    }
    old_token = jwt.encode(claims, old_secret, algorithm=VISITOR_ALGORITHM)

    # When new_secret is primary and old_secret is secondary, verification must succeed
    with patch.object(settings, "visitor_session_secret", new_secret), patch.object(
        settings, "visitor_session_secret_secondary", old_secret
    ):
        session = verify_visitor_token(
            token=old_token,
            expected_bot_id=bot_id,
            expected_conversation_id=conv_id,
        )
        assert session.visitor_id == "rotating_visitor"

    # When secondary secret is retired (empty), old_token must be rejected
    with patch.object(settings, "visitor_session_secret", new_secret), patch.object(
        settings, "visitor_session_secret_secondary", ""
    ):
        with pytest.raises(Exception) as exc_info:
            verify_visitor_token(
                token=old_token,
                expected_bot_id=bot_id,
                expected_conversation_id=conv_id,
            )
        assert "Invalid visitor session token" in str(exc_info.value)


def test_production_secret_enforcement() -> None:
    """Confirms that production cannot start or operate with a missing or insecure secret."""
    from app.config import Settings

    # Missing secret in production
    with pytest.raises(ValueError) as exc_info:
        Settings(environment="production", visitor_session_secret="")
    assert "VISITOR_SESSION_SECRET must be securely configured" in str(exc_info.value)

    # Short secret (<32 chars) in production
    with pytest.raises(ValueError) as exc_info:
        Settings(environment="production", visitor_session_secret="too-short")
    assert "at least 32 characters long" in str(exc_info.value)

    # Insecure default in production
    with pytest.raises(ValueError) as exc_info:
        Settings(
            environment="production",
            visitor_session_secret="dev-insecure-visitor-session-secret-change-in-production",
        )
    assert "Insecure default or empty secrets are prohibited" in str(exc_info.value)

    # Valid secret in production succeeds
    valid_prod_settings = Settings(
        environment="production",
        visitor_session_secret="a-very-secure-production-secret-with-plenty-of-entropy-123456",
    )
    assert len(valid_prod_settings.resolved_visitor_session_secret) >= 32


# ============================================================================
# 2. Integration Tests: Public Conversations, Messages & Dashboard Listing
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(
    not RUN_INTEGRATION_TESTS,
    reason="Conversation lifecycle integration test requires real PostgreSQL with RUN_SUPABASE_INTEGRATION_TESTS=1",
)
async def test_conversation_lifecycle_and_security(client: AsyncClient) -> None:
    """Full lifecycle integration test:

    1. Org setup and chatbot creation (draft -> active).
    2. Inactive/draft bot blocks conversation initialization.
    3. Active bot allows conversation initialization, returning signed session token.
    4. Visitor submits message with session token; verifies assistant_reply is null.
    5. Message idempotency: re-submitting with identical client_message_id returns existing message.
    6. Content limits: message > 2000 chars rejected (422), body > 16 KB rejected (413).
    7. Cross-visitor and cross-bot isolation (attacker cannot write/read other convs).
    8. Chronological transcript retrieval.
    9. Authenticated dashboard endpoint lists conversation with message count.
    10. Bot deletion cascades to conversation and messages.
    """
    user_id = uuid.uuid4()
    auth_token = make_token(user_id=str(user_id))
    org_id = None

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            # Step 1: Setup organization
            res_org = await client.post(
                "/api/v1/organizations/setup",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"name": "Conversations Test Org"},
            )
            assert res_org.status_code == 201
            org_id = uuid.UUID(res_org.json()["id"])

            # Create chatbot in draft mode
            res_bot = await client.post(
                f"/api/v1/organizations/{org_id}/chatbots",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={
                    "name": "Live Support Bot",
                    "welcome_message": "Hello! How can we help?",
                    "status": "draft",
                },
            )
            assert res_bot.status_code == 201
            bot_id = res_bot.json()["id"]

            # Step 2: Attempt conversation creation while chatbot is draft -> 404
            res_init_draft = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations",
                json={"visitor_id": "visitor_alice"},
            )
            assert res_init_draft.status_code == 404
            assert res_init_draft.json()["detail"] == "Chatbot not found"

            # Activate chatbot
            res_active = await client.patch(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"status": "active"},
            )
            assert res_active.status_code == 200

            # Step 3: Initialize conversation on active chatbot -> 201
            res_conv = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations",
                json={"visitor_id": "visitor_alice"},
            )
            assert res_conv.status_code == 201
            conv_data = res_conv.json()
            conv_id = conv_data["id"]
            session_token = conv_data["session_token"]
            assert conv_data["chatbot_id"] == bot_id
            assert conv_data["visitor_id"] == "visitor_alice"
            assert conv_data["status"] == "active"
            assert isinstance(session_token, str)

            # Step 4: Submit a message using the visitor session token
            client_msg_id = f"cli_{uuid.uuid4().hex[:12]}"
            res_msg = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {session_token}"},
                json={
                    "content": "Hello, I have a question about pricing.",
                    "client_message_id": client_msg_id,
                },
            )
            assert res_msg.status_code == 201
            msg_data = res_msg.json()
            persisted_msg = msg_data["message"]
            assert persisted_msg["conversation_id"] == conv_id
            assert persisted_msg["sender_type"] == "visitor"
            assert persisted_msg["content"] == "Hello, I have a question about pricing."
            assert persisted_msg["client_message_id"] == client_msg_id
            # Explicit API contract check: assistant_reply must be None
            assert msg_data["assistant_reply"] is None

            # Step 5: Test message idempotency (re-post identical client_message_id)
            res_msg_replay = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {session_token}"},
                json={
                    "content": "Hello, I have a question about pricing.",
                    "client_message_id": client_msg_id,
                },
            )
            assert res_msg_replay.status_code == 200
            replay_data = res_msg_replay.json()
            assert replay_data["message"]["id"] == persisted_msg["id"]
            assert replay_data["message"]["client_message_id"] == client_msg_id

            # Post a second unique message via X-Visitor-Session header
            res_msg2 = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"X-Visitor-Session": session_token},
                json={"content": "Can you provide a discount for annual plans?"},
            )
            assert res_msg2.status_code == 201

            # Step 6: Test size & content limits
            # 6a. Character limit (>2000 chars) -> 422
            long_content = "A" * 2001
            res_too_long = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {session_token}"},
                json={"content": long_content},
            )
            assert res_too_long.status_code == 422

            # 6b. Payload size limit (>16 KB) -> 413
            oversized_padding = "x" * (17 * 1024)
            res_oversized = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={
                    "Authorization": f"Bearer {session_token}",
                    "Content-Type": "application/json",
                },
                content=f'{{"content": "valid", "extra": "{oversized_padding}"}}'.encode("utf-8"),
            )
            assert res_oversized.status_code == 413
            assert "16 KB" in res_oversized.json()["detail"]

            # 6c. Conversation creation payload size limit (>16 KB) -> 413
            res_conv_oversized = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations",
                headers={"Content-Type": "application/json"},
                content=f'{{"visitor_id": "valid", "extra": "{oversized_padding}"}}'.encode("utf-8"),
            )
            assert res_conv_oversized.status_code == 413

            # Step 7: Cross-visitor and cross-bot isolation
            # Initialize a second conversation (Bob)
            res_conv_bob = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations",
                json={"visitor_id": "visitor_bob"},
            )
            assert res_conv_bob.status_code == 201
            bob_conv_id = res_conv_bob.json()["id"]
            bob_token = res_conv_bob.json()["session_token"]

            # Bob tries to write to Alice's conversation using Bob's token -> 403
            res_bob_tamper_write = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {bob_token}"},
                json={"content": "Bob trying to hijack Alice's conversation"},
            )
            assert res_bob_tamper_write.status_code == 403

            # Bob tries to read Alice's conversation transcript -> 403
            res_bob_tamper_read = await client.get(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert res_bob_tamper_read.status_code == 403

            # Step 8: Chronological transcript retrieval for authorized visitor
            res_transcript = await client.get(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            assert res_transcript.status_code == 200
            transcript = res_transcript.json()
            assert len(transcript) == 2
            assert transcript[0]["content"] == "Hello, I have a question about pricing."
            assert transcript[1]["content"] == "Can you provide a discount for annual plans?"

            # Step 9: Authenticated dashboard endpoint lists conversations with message counts
            res_dash_convs = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}/conversations",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            assert res_dash_convs.status_code == 200
            dash_convs = res_dash_convs.json()
            assert len(dash_convs) == 2
            # Find Alice's conversation
            alice_entry = next((c for c in dash_convs if c["id"] == conv_id), None)
            assert alice_entry is not None
            assert alice_entry["visitor_id"] == "visitor_alice"
            assert alice_entry["messages_count"] == 2
            assert alice_entry["last_message_at"] is not None

            # Non-member dashboard request returns 404
            stranger_token = make_token(user_id=str(uuid.uuid4()))
            res_stranger = await client.get(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}/conversations",
                headers={"Authorization": f"Bearer {stranger_token}"},
            )
            assert res_stranger.status_code == 404

            # Step 10: Bot deletion cascades to conversations and messages
            res_del_bot = await client.delete(
                f"/api/v1/organizations/{org_id}/chatbots/{bot_id}",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            assert res_del_bot.status_code == 204

            # Verify cascading deletion in the database
            async with AsyncSessionLocal() as session:
                remaining_convs = (
                    await session.execute(
                        select(Conversation).where(Conversation.id == uuid.UUID(conv_id))
                    )
                ).scalars().all()
                assert len(remaining_convs) == 0

                remaining_msgs = (
                    await session.execute(
                        select(Message).where(Message.conversation_id == uuid.UUID(conv_id))
                    )
                ).scalars().all()
                assert len(remaining_msgs) == 0
    finally:
        if org_id:
            async with AsyncSessionLocal() as session:
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.anyio
@pytest.mark.skipif(
    not RUN_INTEGRATION_TESTS,
    reason="Composite foreign key test requires real PostgreSQL with RUN_SUPABASE_INTEGRATION_TESTS=1",
)
async def test_composite_foreign_key_cross_org_invariant() -> None:
    """Verifies that the database composite FK rejects conversations referencing a bot from another org."""
    user_id = uuid.uuid4()
    auth_token = make_token(user_id=str(user_id))
    org_a_id = None
    org_b_id = None

    async with AsyncSessionLocal() as session:
        try:
            # Create Org A and Org B
            org_a = Organization(name="Org A Invariant Test")
            org_b = Organization(name="Org B Invariant Test")
            session.add_all([org_a, org_b])
            await session.commit()
            await session.refresh(org_a)
            await session.refresh(org_b)
            org_a_id = org_a.id
            org_b_id = org_b.id

            # Create Bot belonging to Org A
            bot_a = Chatbot(
                organization_id=org_a.id,
                name="Bot belonging to Org A",
                status=ChatbotStatus.active,
            )
            session.add(bot_a)
            await session.commit()
            await session.refresh(bot_a)

            # Attempt to create Conversation claiming to belong to Org B while referencing Bot A (Org A)
            illegal_conv = Conversation(
                organization_id=org_b.id,  # Mismatch!
                chatbot_id=bot_a.id,
                visitor_id="malicious_cross_org_visitor",
                status=ConversationStatus.active,
            )
            session.add(illegal_conv)

            with pytest.raises(IntegrityError) as exc_info:
                await session.commit()

            # Must fail the fk_conversations_chatbot_org constraint
            assert "fk_conversations_chatbot_org" in str(exc_info.value)
            await session.rollback()
        finally:
            if org_a_id or org_b_id:
                await session.execute(
                    delete(Organization).where(Organization.id.in_([org_a_id, org_b_id]))
                )
                await session.commit()
