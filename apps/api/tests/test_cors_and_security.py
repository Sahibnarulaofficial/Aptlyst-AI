"""CORS policy, preflight, and API authentication boundary tests."""

from collections.abc import AsyncGenerator
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import parse_and_validate_cors_origins
from app.db.session import get_db
from app.main import app
from app.security.rate_limiter import _in_memory_limiter
from tests.test_auth import TEST_JWT_SECRET, make_token

APPROVED_ORIGIN = "http://localhost:3000"
UNAPPROVED_ORIGIN = "https://evil.example.com"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clear_rate_limiter() -> None:
    _in_memory_limiter.clear()
    yield
    _in_memory_limiter.clear()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.parametrize(
    "raw_origins,is_prod,expected",
    [
        ("http://localhost:3000,http://127.0.0.1:3000", False, ["http://localhost:3000", "http://127.0.0.1:3000"]),
        ("https://app.aptlyst.ai, https://staging.aptlyst.ai", True, ["https://app.aptlyst.ai", "https://staging.aptlyst.ai"]),
    ],
)
def test_parse_and_validate_cors_origins_accepts_approved_origins(
    raw_origins: str, is_prod: bool, expected: list[str]
) -> None:
    assert parse_and_validate_cors_origins(raw_origins, is_production=is_prod) == expected


@pytest.mark.parametrize(
    "bad_origins,is_prod",
    [
        ("*", False),
        ("https://*.aptlyst.ai", True),
        ("http://app.aptlyst.ai", True),
        ("https://app.aptlyst.ai/dashboard", False),
        ("https://user:pass@app.aptlyst.ai", False),
        ("javascript:alert(1)", False),
    ],
)
def test_parse_and_validate_cors_origins_rejects_unsafe_origins(
    bad_origins: str, is_prod: bool
) -> None:
    with pytest.raises(ValueError):
        parse_and_validate_cors_origins(bad_origins, is_production=is_prod)


@pytest.mark.anyio
async def test_internal_api_cors_preflight_approved_vs_unapproved_origin(
    client: AsyncClient,
) -> None:
    """Internal credentialed CORS allows approved origins and denies unapproved origins."""
    res_ok = await client.options(
        "/api/v1/organizations",
        headers={
            "Origin": APPROVED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert res_ok.status_code == 200
    assert res_ok.headers.get("access-control-allow-origin") == APPROVED_ORIGIN
    assert res_ok.headers.get("access-control-allow-credentials") == "true"

    res_blocked = await client.options(
        "/api/v1/organizations",
        headers={
            "Origin": UNAPPROVED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert res_blocked.status_code == 400
    assert "access-control-allow-origin" not in res_blocked.headers


@pytest.mark.anyio
async def test_approved_cors_origin_does_not_bypass_authentication(
    client: AsyncClient,
) -> None:
    """CORS approval must never substitute for JWT authentication on protected endpoints."""
    org_id = uuid.uuid4()
    bot_id = uuid.uuid4()
    protected_paths = [
        ("GET", "/api/v1/organizations"),
        ("GET", f"/api/v1/organizations/{org_id}/chatbots"),
        ("POST", f"/api/v1/organizations/{org_id}/chatbots"),
        ("GET", f"/api/v1/organizations/{org_id}/billing/subscription"),
        ("DELETE", f"/api/v1/organizations/{org_id}/chatbots/{bot_id}"),
    ]

    for method, path in protected_paths:
        res_missing = await client.request(
            method,
            path,
            headers={"Origin": APPROVED_ORIGIN},
        )
        assert res_missing.status_code == 401
        assert res_missing.headers.get("access-control-allow-origin") == APPROVED_ORIGIN
        assert res_missing.headers.get("access-control-allow-credentials") == "true"

        res_invalid = await client.request(
            method,
            path,
            headers={
                "Origin": UNAPPROVED_ORIGIN,
                "Authorization": "Bearer invalid.jwt.token",
            },
        )
        assert res_invalid.status_code == 401
        assert "access-control-allow-origin" not in res_invalid.headers


@pytest.mark.anyio
async def test_authenticated_request_reflects_approved_origin_and_blocks_unapproved_origin(
    client: AsyncClient,
) -> None:
    """Valid Bearer JWT succeeds with approved origin CORS headers and omits headers for unapproved origins."""
    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_res)

    async def _override_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    token = make_token(user_id=str(uuid.uuid4()))

    try:
        with patch("app.auth.dependencies.settings") as mock_settings:
            mock_settings.supabase_jwt_algorithm = "HS256"
            mock_settings.supabase_jwt_secret = TEST_JWT_SECRET

            res_approved = await client.get(
                "/api/v1/organizations",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": APPROVED_ORIGIN,
                },
            )
            assert res_approved.status_code == 200
            assert res_approved.headers.get("access-control-allow-origin") == APPROVED_ORIGIN
            assert res_approved.headers.get("access-control-allow-credentials") == "true"

            res_unapproved = await client.get(
                "/api/v1/organizations",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": UNAPPROVED_ORIGIN,
                },
            )
            assert res_unapproved.status_code == 200
            assert "access-control-allow-origin" not in res_unapproved.headers
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_public_endpoint_cors_preflight_and_rate_limit_headers(
    client: AsyncClient,
) -> None:
    """Public widget routes expose public CORS headers without credentials on preflight and 429 responses."""
    bot_id = uuid.uuid4()
    res_preflight = await client.options(
        f"/api/v1/public/chatbots/{bot_id}/conversations",
        headers={
            "Origin": "https://customer-widget.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-Visitor-Session",
        },
    )
    assert res_preflight.status_code == 204
    assert res_preflight.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in res_preflight.headers
    assert "X-Visitor-Session" in res_preflight.headers.get("access-control-allow-headers", "")

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_res)

    async def _override_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with patch("app.api.public_chatbots.settings.rate_limit_public_config_per_minute", 1):
            first = await client.get(
                f"/api/v1/public/chatbots/{bot_id}",
                headers={"Origin": "https://customer-widget.example"},
            )
            assert first.status_code == 404
            assert first.headers.get("access-control-allow-origin") == "*"

            limited = await client.get(
                f"/api/v1/public/chatbots/{bot_id}",
                headers={"Origin": "https://customer-widget.example"},
            )
            assert limited.status_code == 429
            assert limited.headers.get("access-control-allow-origin") == "*"
            assert "Retry-After" in limited.headers.get("access-control-expose-headers", "")
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_public_endpoint_with_approved_internal_origin_never_combines_wildcard_with_credentials(
    client: AsyncClient,
) -> None:
    """GET /api/v1/public/chatbots/{bot_id} with an approved internal Origin must not emit ACAO: * with ACAC: true."""
    from app.models.chatbot import Chatbot, ChatbotStatus

    bot_id = uuid.uuid4()
    active_bot = MagicMock(spec=Chatbot)
    active_bot.id = bot_id
    active_bot.name = "Public Bot"
    active_bot.welcome_message = "Hi!"
    active_bot.primary_color = "#3b82f6"
    active_bot.status = ChatbotStatus.active

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = active_bot
    mock_session.execute = AsyncMock(return_value=mock_res)

    async def _override_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db] = _override_db
    try:
        res_get = await client.get(
            f"/api/v1/public/chatbots/{bot_id}",
            headers={"Origin": APPROVED_ORIGIN},
        )
        assert res_get.status_code == 200
        assert res_get.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in res_get.headers

        with patch("app.api.public_chatbots.settings.rate_limit_public_config_per_minute", 1):
            _in_memory_limiter.clear()
            first = await client.get(
                f"/api/v1/public/chatbots/{bot_id}",
                headers={"Origin": APPROVED_ORIGIN},
            )
            assert first.status_code == 200
            limited = await client.get(
                f"/api/v1/public/chatbots/{bot_id}",
                headers={"Origin": APPROVED_ORIGIN},
            )
            assert limited.status_code == 429
            assert limited.headers.get("access-control-allow-origin") == "*"
            assert "access-control-allow-credentials" not in limited.headers
    finally:
        app.dependency_overrides.pop(get_db, None)
