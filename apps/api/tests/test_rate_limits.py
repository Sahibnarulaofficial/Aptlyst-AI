"""Comprehensive test suite for rate limiting, proxy resolution, and abuse protection."""

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.auth.visitor_session import create_visitor_token
from app.config import Settings, settings
from app.db.session import get_db
from app.main import app
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSenderType
from app.security.rate_limiter import (
    InMemoryRateLimiter,
    RateLimitExceeded,
    RedisRateLimiter,
    _in_memory_limiter,
    _sanitize_redis_error,
    get_client_ip,
    mask_ip_for_logging,
    validate_rate_limiter_configuration,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_limiter_storage_and_mock_db() -> None:
    """Clear in-memory limiter state and override get_db to prevent TCP connections."""
    _in_memory_limiter.clear()

    async def _mock_get_db() -> Any:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result
        yield mock_session

    app.dependency_overrides[get_db] = _mock_get_db
    yield
    app.dependency_overrides.pop(get_db, None)
    _in_memory_limiter.clear()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _make_mock_request(
    client_host: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> Request:
    """Helper to build a Starlette Request with custom client and headers."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": raw_headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


# ============================================================================
# 1. Client IP Resolution & Proxy Spoofing Tests
# ============================================================================


def test_client_ip_untrusted_direct_peer_ignores_forwarding_headers() -> None:
    """An untrusted direct socket peer cannot spoof its IP via X-Forwarded-For or X-Real-IP."""
    req = _make_mock_request(
        client_host="198.51.100.25",  # Public / untrusted IP
        headers={
            "X-Forwarded-For": "203.0.113.195, 10.0.0.1",
            "X-Real-IP": "8.8.8.8",
        },
    )
    resolved_ip = get_client_ip(req)
    assert resolved_ip == "198.51.100.25"


def test_client_ip_trusted_proxy_extracts_rightmost_untrusted_ip() -> None:
    """A request via a trusted proxy parses X-Forwarded-For right-to-left skipping trusted hops."""
    req = _make_mock_request(
        client_host="127.0.0.1",  # Trusted loopback proxy
        headers={
            "X-Forwarded-For": "spoofed.client.ip, 203.0.113.50, 10.0.0.2",
        },
    )
    # 10.0.0.2 is in trusted 10.0.0.0/8 network; next untrusted from right is 203.0.113.50
    resolved_ip = get_client_ip(req)
    assert resolved_ip == "203.0.113.50"


def test_client_ip_trusted_proxy_handles_invalid_or_malformed_ips() -> None:
    """Malformed strings in X-Forwarded-For or X-Real-IP do not crash the resolver."""
    req = _make_mock_request(
        client_host="127.0.0.1",
        headers={
            "X-Forwarded-For": "malicious-injection-string; drop table, not-an-ip",
            "X-Real-IP": "also-not-an-ip",
        },
    )
    resolved_ip = get_client_ip(req)
    assert resolved_ip == "127.0.0.1"


def test_client_ip_trusted_proxy_uses_real_ip_when_xff_absent() -> None:
    """When X-Forwarded-For is absent but X-Real-IP is present from a trusted proxy, use X-Real-IP."""
    req = _make_mock_request(
        client_host="127.0.0.1",
        headers={"X-Real-IP": "198.51.100.77"},
    )
    resolved_ip = get_client_ip(req)
    assert resolved_ip == "198.51.100.77"


def test_mask_ip_for_logging() -> None:
    """Verifies that IP addresses are masked to satisfy GDPR/CCPA privacy standards."""
    assert mask_ip_for_logging("192.168.1.55") == "192.168.1.***"
    assert mask_ip_for_logging("2001:db8:85a3:8d3:1319:8a2e:370:7348") == "2001:db8:85a3:****"


# ============================================================================
# 2. Sliding Window & In-Memory Rate Limiter Unit Tests
# ============================================================================


@pytest.mark.anyio
async def test_in_memory_sliding_window_enforcement_and_reset() -> None:
    """Verifies that requests up to limit succeed and subsequent requests are rejected."""
    limiter = InMemoryRateLimiter()

    # Limit: 3 requests per 2 seconds
    key = "test_window"
    limit = 3
    window = 2

    # First 3 requests at t=100.0, 100.5, 101.0 must succeed
    allowed, remaining, retry_after = await limiter.check_rate_limit(key, limit, window, now=100.0)
    assert allowed is True
    assert remaining == 2
    assert retry_after == 0

    allowed, remaining, retry_after = await limiter.check_rate_limit(key, limit, window, now=100.5)
    assert allowed is True
    assert remaining == 1

    allowed, remaining, retry_after = await limiter.check_rate_limit(key, limit, window, now=101.0)
    assert allowed is True
    assert remaining == 0

    # 4th request at t=101.5 must be rejected
    allowed, remaining, retry_after = await limiter.check_rate_limit(key, limit, window, now=101.5)
    assert allowed is False
    assert remaining == 0
    # Oldest timestamp is 100.0, window is 2, now is 101.5 -> retry_after = 100.0 + 2 - 101.5 = 0.5 -> max(1, 0) = 1
    assert retry_after >= 1

    # At t=102.1 (> 100.0 + 2.0), the first request falls out of the sliding window
    allowed, remaining, retry_after = await limiter.check_rate_limit(key, limit, window, now=102.1)
    assert allowed is True
    assert remaining == 0  # 100.5 and 101.0 are still in window [100.1, 102.1], so this is the 3rd request


@pytest.mark.anyio
async def test_in_memory_concurrency_atomicity() -> None:
    """Concurrent requests must not bypass the configured limit."""
    limiter = InMemoryRateLimiter()
    key = "concurrent_key"
    limit = 10
    window = 60

    # Fire 30 concurrent tasks
    async def worker() -> bool:
        allowed, _, _ = await limiter.check_rate_limit(key, limit, window)
        return allowed

    results = await asyncio.gather(*(worker() for _ in range(30)))
    allowed_count = sum(1 for r in results if r is True)
    rejected_count = sum(1 for r in results if r is False)

    assert allowed_count == limit
    assert rejected_count == 20


@pytest.mark.anyio
async def test_in_memory_bounded_storage_and_lru_eviction() -> None:
    """When max_keys is reached, expired keys are cleaned up and oldest active keys are evicted."""
    limiter = InMemoryRateLimiter(max_keys=3, overflow_policy="evict_oldest")

    # Add 3 distinct keys
    await limiter.check_rate_limit("k1", 5, 60)
    await limiter.check_rate_limit("k2", 5, 60)
    await limiter.check_rate_limit("k3", 5, 60)
    assert len(limiter._storage) == 3

    # Add 4th key: should evict oldest (k1)
    await limiter.check_rate_limit("k4", 5, 60)
    assert len(limiter._storage) == 3
    assert "k1" not in limiter._storage
    assert "k4" in limiter._storage


@pytest.mark.anyio
async def test_in_memory_bounded_storage_drop_new_policy() -> None:
    """When overflow_policy is 'drop_new', new keys beyond capacity are rejected."""
    limiter = InMemoryRateLimiter(max_keys=2, overflow_policy="drop_new")

    await limiter.check_rate_limit("k1", 5, 60)
    await limiter.check_rate_limit("k2", 5, 60)

    # 3rd key should be dropped
    allowed, _, _ = await limiter.check_rate_limit("k3", 5, 60)
    assert allowed is False
    assert "k3" not in limiter._storage


@pytest.mark.anyio
async def test_in_memory_fractional_retry_after_uses_ceil() -> None:
    """Fractional remaining seconds must round UP via math.ceil to prevent premature retries."""
    limiter = InMemoryRateLimiter()
    key = "frac_key"
    limit = 1
    window = 60

    # First request at t=100.0
    await limiter.check_rate_limit(key, limit, window, now=100.0)

    # Rejected at t=100.2 -> remaining = 100.0 + 60 - 100.2 = 59.8 -> ceil is 60
    allowed, _, retry_after = await limiter.check_rate_limit(key, limit, window, now=100.2)
    assert allowed is False
    assert retry_after == 60

    # Rejected at t=159.9 -> remaining = 100.0 + 60 - 159.9 = 0.1 -> ceil is 1
    allowed, _, retry_after = await limiter.check_rate_limit(key, limit, window, now=159.9)
    assert allowed is False
    assert retry_after == 1


@pytest.mark.anyio
async def test_in_memory_heterogeneous_cross_window_cleanup() -> None:
    """A 60s window cleanup must NOT prematurely evict an active hourly (3600s) counter."""
    limiter = InMemoryRateLimiter(max_keys=2, overflow_policy="evict_oldest")

    # 1. Hourly key recorded at t=1000.0 with 3600s window
    await limiter.check_rate_limit("hourly_key", 50, 3600, now=1000.0)

    # 2. Short 60s key recorded at t=1050.0
    await limiter.check_rate_limit("short_key_1", 10, 60, now=1050.0)
    assert len(limiter._storage) == 2

    # 3. At t=1120.0 (120s after hourly_key was recorded, 70s after short_key_1):
    # short_key_1 is genuinely expired (> 60s).
    # hourly_key is STILL active (120s < 3600s).
    # A new 60s request triggers cleanup pass:
    await limiter.check_rate_limit("short_key_2", 10, 60, now=1120.0)

    # short_key_1 should have been cleaned up as expired
    assert "short_key_1" not in limiter._storage
    # hourly_key MUST STILL EXIST because its 3600s window is active!
    assert "hourly_key" in limiter._storage
    assert "short_key_2" in limiter._storage

    # 4. If we jump past 3600s (e.g. t=4700.0), hourly_key is genuinely expired and pruned
    await limiter.check_rate_limit("short_key_3", 10, 60, now=4700.0)
    assert "hourly_key" not in limiter._storage


@pytest.mark.anyio
async def test_in_memory_lru_preserves_active_rejected_keys_under_high_cardinality() -> None:
    """Actively rejected abusers must NOT be prioritized for eviction over inactive keys."""
    limiter = InMemoryRateLimiter(max_keys=3, overflow_policy="evict_oldest")

    # Inactive key k_idle is recorded first at t=100.0
    await limiter.check_rate_limit("k_idle", 10, 60, now=100.0)

    # Abusive key k_abuser reaches limit (2 reqs)
    await limiter.check_rate_limit("k_abuser", 2, 60, now=101.0)
    await limiter.check_rate_limit("k_abuser", 2, 60, now=102.0)

    # 3rd request from k_abuser is REJECTED at t=103.0
    allowed, _, _ = await limiter.check_rate_limit("k_abuser", 2, 60, now=103.0)
    assert allowed is False

    # Now add k_new_1 (storage is now at capacity 3: k_idle, k_abuser, k_new_1)
    await limiter.check_rate_limit("k_new_1", 10, 60, now=104.0)
    assert len(limiter._storage) == 3

    # Now add k_new_2 at t=105.0. Storage exceeds max_keys.
    # Because k_abuser was actively touched on its rejection, k_idle is the oldest LRU key!
    await limiter.check_rate_limit("k_new_2", 10, 60, now=105.0)

    # k_idle was evicted, NOT k_abuser!
    assert "k_idle" not in limiter._storage
    assert "k_abuser" in limiter._storage
    assert "k_new_2" in limiter._storage

    # k_abuser is STILL blocked and its rate limit was NOT bypassed
    allowed, _, _ = await limiter.check_rate_limit("k_abuser", 2, 60, now=106.0)
    assert allowed is False


# ============================================================================
# 3. Redis Rate Limiter & Fallback Policy Unit Tests
# ============================================================================


@pytest.mark.anyio
async def test_redis_limiter_lua_execution() -> None:
    """Simulate successful Redis Lua script evaluation."""
    mock_redis = MagicMock()
    # Mock Lua script register and execution returning [allowed, remaining, retry_after]
    mock_script = MagicMock()

    async def mock_exec(*args: Any, **kwargs: Any) -> list[int]:
        return [1, 9, 0]

    mock_script.side_effect = mock_exec
    mock_redis.register_script.return_value = mock_script

    redis_limiter = RedisRateLimiter(redis_client=mock_redis)
    allowed, remaining, retry_after = await redis_limiter.check_rate_limit("rk", 10, 60)

    assert allowed is True
    assert remaining == 9
    assert retry_after == 0


@pytest.mark.anyio
async def test_redis_failure_policy_fallback_to_memory() -> None:
    """When Redis fails and fallback policy is 'memory', requests are enforced in-memory."""
    mock_redis = MagicMock()
    mock_script = MagicMock()

    async def mock_fail(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Redis server unreachable")

    mock_script.side_effect = mock_fail
    mock_redis.register_script.return_value = mock_script

    mem_limiter = InMemoryRateLimiter()
    redis_limiter = RedisRateLimiter(
        redis_client=mock_redis,
        fallback_limiter=mem_limiter,
        fallback_policy="memory",
    )

    allowed, remaining, _ = await redis_limiter.check_rate_limit("k_fail", 2, 60)
    assert allowed is True
    assert remaining == 1

    # Second request through in-memory fallback
    allowed2, remaining2, _ = await redis_limiter.check_rate_limit("k_fail", 2, 60)
    assert allowed2 is True
    assert remaining2 == 0

    # Third request blocked
    allowed3, remaining3, retry_after = await redis_limiter.check_rate_limit("k_fail", 2, 60)
    assert allowed3 is False
    assert retry_after >= 1


@pytest.mark.anyio
async def test_redis_failure_policy_fail_open() -> None:
    """When Redis fails and fallback policy is 'fail_open', request is allowed."""
    mock_redis = MagicMock()
    mock_script = MagicMock()

    async def mock_fail(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("Redis timeout")

    mock_script.side_effect = mock_fail
    mock_redis.register_script.return_value = mock_script

    redis_limiter = RedisRateLimiter(
        redis_client=mock_redis,
        fallback_policy="fail_open",
    )
    allowed, _, _ = await redis_limiter.check_rate_limit("k_open", 1, 60)
    assert allowed is True


@pytest.mark.anyio
async def test_redis_failure_policy_fail_closed() -> None:
    """When Redis fails and fallback policy is 'fail_closed', request is rejected."""
    mock_redis = MagicMock()
    mock_script = MagicMock()

    async def mock_fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Redis error")

    mock_script.side_effect = mock_fail
    mock_redis.register_script.return_value = mock_script

    redis_limiter = RedisRateLimiter(
        redis_client=mock_redis,
        fallback_policy="fail_closed",
    )
    allowed, _, retry_after = await redis_limiter.check_rate_limit("k_closed", 1, 60)
    assert allowed is False
    assert retry_after == 60


def test_redis_startup_validation_missing_url() -> None:
    """When RATE_LIMIT_STORAGE=redis but REDIS_URL is blank, startup fails fast."""
    with patch.object(settings, "rate_limit_storage", "redis"), patch.object(
        settings, "redis_url", ""
    ):
        with pytest.raises(RuntimeError, match="REDIS_URL is not configured"):
            validate_rate_limiter_configuration()


def test_redis_startup_validation_missing_package() -> None:
    """When RATE_LIMIT_STORAGE=redis but redis package is not installed, fail fast."""
    with patch.object(settings, "rate_limit_storage", "redis"), patch.object(
        settings, "redis_url", "redis://localhost:6379/0"
    ), patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
        with pytest.raises(RuntimeError, match=r"[Tt]he 'redis' Python package is not installed"):
            validate_rate_limiter_configuration()


def test_redis_error_sanitization_masks_credentials() -> None:
    """Redis error messages containing auth credentials must have passwords masked."""
    raw_err = ConnectionError(
        "Failed to connect to redis://app_user:super_secret_pw@cache.prod.internal:6379/0: timeout"
    )
    sanitized = _sanitize_redis_error(raw_err)
    assert "super_secret_pw" not in sanitized
    assert "redis://app_user:***@cache.prod.internal:6379/0" in sanitized


@pytest.mark.anyio
async def test_redis_client_close_lifecycle() -> None:
    """Closing RedisRateLimiter awaits client.aclose() and clears state."""
    mock_redis = AsyncMock()
    limiter = RedisRateLimiter(redis_client=mock_redis)
    await limiter.close()
    mock_redis.aclose.assert_awaited_once()
    assert limiter._redis is None


# ============================================================================
# 4. HTTP 429 Response Format & Integration Tests
# ============================================================================


@pytest.mark.anyio
async def test_http_429_response_format_and_headers(client: AsyncClient) -> None:
    """Verify that an exceeded limit returns HTTP 429 with Retry-After and consistent JSON."""
    bot_id = uuid.uuid4()

    # Artificially set public config rate limit to 2 for this test
    with patch.object(settings, "rate_limit_public_config_per_minute", 2):
        # 1st request -> 404 (bot doesn't exist, but limiter counted it)
        r1 = await client.get(f"/api/v1/public/chatbots/{bot_id}")
        assert r1.status_code == 404

        # 2nd request -> 404
        r2 = await client.get(f"/api/v1/public/chatbots/{bot_id}")
        assert r2.status_code == 404

        # 3rd request -> 429 Too Many Requests
        r3 = await client.get(f"/api/v1/public/chatbots/{bot_id}")
        assert r3.status_code == 429

        # Headers check
        assert "Retry-After" in r3.headers
        assert int(r3.headers["Retry-After"]) >= 1
        assert "X-RateLimit-Limit" in r3.headers
        assert r3.headers["X-RateLimit-Limit"] == "2/minute"
        assert r3.headers["X-RateLimit-Remaining"] == "0"
        assert "X-RateLimit-Reset" in r3.headers

        # CORS header check on public 429
        assert r3.headers.get("Access-Control-Allow-Origin") == "*"
        assert "Retry-After" in r3.headers.get("Access-Control-Expose-Headers", "")

        # Body format check
        body = r3.json()
        assert body["detail"] == "Rate limit exceeded. Please wait before submitting another request."
        assert body["retry_after"] >= 1
        assert body["limit"] == "2/minute"


@pytest.mark.anyio
async def test_conversation_creation_hourly_limit(client: AsyncClient) -> None:
    """Verify that conversation creation enforces the hourly limit."""
    bot_id = uuid.uuid4()

    with patch.object(settings, "rate_limit_conversation_create_per_minute", 100), patch.object(
        settings, "rate_limit_conversation_create_per_hour", 2
    ):
        # 1st request
        await client.post(f"/api/v1/public/chatbots/{bot_id}/conversations")
        # 2nd request
        await client.post(f"/api/v1/public/chatbots/{bot_id}/conversations")

        # 3rd request should hit hourly limit
        r3 = await client.post(f"/api/v1/public/chatbots/{bot_id}/conversations")
        assert r3.status_code == 429
        assert r3.json()["limit"] == "2/hour"
        assert int(r3.headers["Retry-After"]) >= 1


@pytest.mark.anyio
async def test_create_message_rate_limiting_pre_db_rejection(client: AsyncClient) -> None:
    """Verify POST /messages enforces rate limiting and rejects before executing DB writes."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id, visitor_id="v_test")

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_bot = MagicMock(spec=Chatbot)
    mock_bot.id = bot_id
    mock_bot.status = ChatbotStatus.active

    mock_conv = MagicMock(spec=Conversation)
    mock_conv.id = conv_id
    mock_conv.chatbot_id = bot_id
    mock_conv.status = ConversationStatus.active

    async def _mock_execute(statement: Any) -> Any:
        stmt_str = str(statement).lower()
        res = MagicMock()
        if "from chatbots" in stmt_str:
            res.scalar_one_or_none.return_value = mock_bot
        elif "from conversations" in stmt_str:
            res.scalar_one_or_none.return_value = mock_conv
        else:
            res.scalar_one_or_none.return_value = None
        return res

    mock_session.execute.side_effect = _mock_execute

    async def _mock_refresh(instance: Any) -> None:
        from datetime import datetime, timezone
        instance.id = uuid.uuid4()
        instance.created_at = datetime.now(timezone.utc)

    mock_session.refresh.side_effect = _mock_refresh

    async def _custom_db() -> Any:
        yield mock_session

    app.dependency_overrides[get_db] = _custom_db

    try:
        with patch.object(settings, "rate_limit_message_create_per_visitor_minute", 2):
            # Request 1 -> 201 Created
            r1 = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": "Message 1"},
            )
            assert r1.status_code == 201
            writes_after_r1 = mock_session.add.call_count
            assert writes_after_r1 == 1

            # Request 2 -> 201 Created
            r2 = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": "Message 2"},
            )
            assert r2.status_code == 201
            writes_after_r2 = mock_session.add.call_count
            assert writes_after_r2 == 2

            # Request 3 -> 429 Too Many Requests
            r3 = await client.post(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": "Message 3"},
            )
            assert r3.status_code == 429
            assert "Retry-After" in r3.headers
            assert int(r3.headers["Retry-After"]) >= 1
            assert r3.json()["limit"] == "2/minute"

            # Crucial assertion: 3rd rejected request MUST NOT have executed any DB write!
            assert mock_session.add.call_count == writes_after_r2
            assert mock_session.commit.call_count == writes_after_r2
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_list_messages_rate_limiting_pre_db_rejection(client: AsyncClient) -> None:
    """Verify GET /messages enforces rate limiting and rejects before querying messages."""
    bot_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    token, _ = create_visitor_token(bot_id=bot_id, conversation_id=conv_id, visitor_id="v_test")

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_bot = MagicMock(spec=Chatbot)
    mock_bot.id = bot_id
    mock_bot.status = ChatbotStatus.active

    mock_conv = MagicMock(spec=Conversation)
    mock_conv.id = conv_id
    mock_conv.chatbot_id = bot_id
    mock_conv.status = ConversationStatus.active

    mock_result_bot = MagicMock()
    mock_result_bot.scalar_one_or_none.return_value = mock_bot
    mock_result_conv = MagicMock()
    mock_result_conv.scalar_one_or_none.return_value = mock_conv
    mock_result_msgs = MagicMock()
    mock_result_msgs.scalars.return_value.all.return_value = []

    mock_session.execute.side_effect = [
        mock_result_bot,
        mock_result_conv,
        mock_result_msgs,
        # Repeat for 2nd request
        mock_result_bot,
        mock_result_conv,
        mock_result_msgs,
    ]

    async def _custom_db() -> Any:
        yield mock_session

    app.dependency_overrides[get_db] = _custom_db

    try:
        with patch.object(settings, "rate_limit_messages_list_per_visitor_minute", 2):
            # Request 1 -> 200
            r1 = await client.get(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r1.status_code == 200
            queries_after_r1 = mock_session.execute.call_count
            assert queries_after_r1 == 3

            # Request 2 -> 200
            r2 = await client.get(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 200
            queries_after_r2 = mock_session.execute.call_count
            assert queries_after_r2 == 6

            # Request 3 -> 429 Too Many Requests
            r3 = await client.get(
                f"/api/v1/public/chatbots/{bot_id}/conversations/{conv_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r3.status_code == 429
            assert "Retry-After" in r3.headers
            assert int(r3.headers["Retry-After"]) >= 1
            assert r3.json()["limit"] == "2/minute"

            # Crucial assertion: 3rd rejected request MUST NOT have executed any DB queries!
            assert mock_session.execute.call_count == queries_after_r2
    finally:
        app.dependency_overrides.pop(get_db, None)
