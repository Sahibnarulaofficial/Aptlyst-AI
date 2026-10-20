"""Tests for JWT auth dependency, supporting both ES256 (JWKS) and HS256 verification."""

import base64
import time
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import JWKSCache, default_jwks_cache
from app.main import app

# ─── Test Keys & Fixtures ─────────────────────────────────────────────────────

# A test secret for legacy/symmetric HS256 tests
TEST_JWT_SECRET = "test-secret-for-unit-tests-only"
TEST_SUPABASE_URL = "https://test-project.supabase.co"

# EC key pairs for asymmetric ES256 tests
TEST_EC_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
TEST_EC_PUBLIC_KEY = TEST_EC_PRIVATE_KEY.public_key()
TEST_EC_KID = "test-ec-kid-1"


def _int_to_b64url(val: int) -> str:
    byte_len = 32
    return base64.urlsafe_b64encode(val.to_bytes(byte_len, "big")).decode().rstrip("=")


def _make_jwk(kid: str, public_key) -> dict:
    numbers = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "kid": kid,
        "x": _int_to_b64url(numbers.x),
        "y": _int_to_b64url(numbers.y),
    }


TEST_JWKS = {"keys": [_make_jwk(TEST_EC_KID, TEST_EC_PUBLIC_KEY)]}


def make_es256_token(
    user_id: str | None = None,
    email: str = "test@example.com",
    expired: bool = False,
    private_key=TEST_EC_PRIVATE_KEY,
    kid: str | None = TEST_EC_KID,
    aud: str | None = "authenticated",
    role: str | None = "authenticated",
    algorithm: str = "ES256",
    include_sub: bool = True,
    include_exp: bool = True,
    app_metadata: dict | None = None,
) -> str:
    payload: dict = {
        "email": email,
        "iat": int(time.time()) - 10,
    }
    if app_metadata is not None:
        payload["app_metadata"] = app_metadata
    if include_sub:
        payload["sub"] = user_id if user_id is not None else str(uuid.uuid4())
    if aud is not None:
        payload["aud"] = aud
    if role is not None:
        payload["role"] = role
    if include_exp:
        if expired:
            payload["exp"] = int(time.time()) - 3600
        else:
            payload["exp"] = int(time.time()) + 3600

    headers = {}
    if kid is not None:
        headers["kid"] = kid

    return jwt.encode(payload, private_key, algorithm=algorithm, headers=headers)


def make_token(
    user_id: str | None = None,
    email: str = "test@example.com",
    expired: bool = False,
    secret: str = TEST_JWT_SECRET,
    aud: str | None = "authenticated",
    role: str | None = "authenticated",
    algorithm: str = "HS256",
    include_sub: bool = True,
    include_exp: bool = True,
    headers: dict | None = None,
    app_metadata: dict | None = None,
) -> str:
    """Helper for generating symmetric HS256 tokens."""
    payload: dict = {
        "email": email,
        "iat": int(time.time()) - 10,
    }
    if app_metadata is not None:
        payload["app_metadata"] = app_metadata
    if include_sub:
        payload["sub"] = user_id if user_id is not None else str(uuid.uuid4())
    if aud is not None:
        payload["aud"] = aud
    if role is not None:
        payload["role"] = role
    if include_exp:
        if expired:
            payload["exp"] = int(time.time()) - 3600
        else:
            payload["exp"] = int(time.time()) + 3600

    return jwt.encode(payload, secret, algorithm=algorithm, headers=headers)


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
def reset_jwks_cache():
    default_jwks_cache.clear()
    yield
    default_jwks_cache.clear()


# ─── Generic / Common Auth Tests ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_missing_bearer_token_returns_403(client: AsyncClient) -> None:
    """Unauthenticated request to a protected route should return 403."""
    response = await client.get("/api/v1/organizations")
    assert response.status_code == 403


@pytest.mark.anyio
async def test_invalid_token_returns_401(client: AsyncClient) -> None:
    """Malformed JWT should return 401."""
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        )
    assert response.status_code == 401


# ─── ES256 (Asymmetric JWKS) Verification Tests ───────────────────────────────


@pytest.mark.anyio
async def test_es256_valid_token_reaches_endpoint(client: AsyncClient) -> None:
    """A valid ES256 token verified against cached JWKS should pass authentication."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token()

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code in (200, 503, 500)


@pytest.mark.anyio
async def test_es256_invalid_signature_returns_401(client: AsyncClient) -> None:
    """Token signed with an untrusted EC key (wrong signature) must be rejected with 401."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    untrusted_key = ec.generate_private_key(ec.SECP256R1())
    token = make_es256_token(private_key=untrusted_key)

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_es256_unknown_kid_returns_401(client: AsyncClient) -> None:
    """Token referencing a key ID not in the JWKS must be rejected with 401."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(kid="unknown-kid-999")

    with patch("app.auth.dependencies.settings") as mock_settings, \
         patch.object(default_jwks_cache, "_fetch_jwks", new_callable=AsyncMock, return_value=TEST_JWKS):
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "Unknown key identifier" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_missing_kid_returns_401(client: AsyncClient) -> None:
    """ES256 token without a 'kid' header claim must be rejected with 401."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(kid=None)

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "missing key identifier" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_rejects_hs256_token(client: AsyncClient) -> None:
    """When configured for ES256, an HS256 token must be rejected (no algorithm confusion/fallback)."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_token(algorithm="HS256")

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "Invalid token algorithm" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_rejects_none_algorithm(client: AsyncClient) -> None:
    """Token with 'none' algorithm must be rejected with 401."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "authenticated", "role": "authenticated"},
        key="",
        algorithm="none",
        headers={"kid": TEST_EC_KID},
    )

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_es256_wrong_audience_returns_401(client: AsyncClient) -> None:
    """ES256 token with wrong audience must be rejected."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(aud="untrusted-audience")

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("wrong_role", ["anon", "service_role", "guest"])
async def test_es256_wrong_role_returns_401(client: AsyncClient, wrong_role: str) -> None:
    """ES256 token with role other than 'authenticated' must be rejected."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(role=wrong_role)

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert response.json().get("detail") == "Invalid token role"


@pytest.mark.anyio
async def test_es256_expired_token_returns_401(client: AsyncClient) -> None:
    """Expired ES256 token must be rejected."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(expired=True)

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "expired" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_missing_sub_returns_401(client: AsyncClient) -> None:
    """ES256 token missing 'sub' claim must be rejected."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(include_sub=False)

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_es256_invalid_sub_uuid_returns_401(client: AsyncClient) -> None:
    """ES256 token with non-UUID 'sub' claim must be rejected."""
    default_jwks_cache.set_jwks_data(TEST_JWKS)
    token = make_es256_token(user_id="invalid-uuid")

    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert response.json().get("detail") == "Invalid subject format"


@pytest.mark.anyio
async def test_es256_missing_supabase_url_fails_closed_with_500(client: AsyncClient) -> None:
    """If SUPABASE_URL is missing/blank when ES256 is configured, fail closed with 500."""
    token = make_es256_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = ""
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 500
    assert "missing Supabase URL" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_jwks_unavailable_fails_closed_with_401(client: AsyncClient) -> None:
    """If JWKS endpoint fails (e.g. 500 or connection error), request fails closed with 401."""
    token = make_es256_token()

    with patch("app.auth.dependencies.settings") as mock_settings, \
         patch.object(default_jwks_cache, "_fetch_jwks", new_callable=AsyncMock, side_effect=httpx.ConnectError("Network unreachable")):
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "Authentication service unavailable" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_jwks_malformed_fails_closed_with_401(client: AsyncClient) -> None:
    """If JWKS endpoint returns malformed data, request fails closed with 401."""
    token = make_es256_token()

    with patch("app.auth.dependencies.settings") as mock_settings, \
         patch.object(default_jwks_cache, "_fetch_jwks", new_callable=AsyncMock, return_value={"keys": [{"kty": "UNKNOWN_KTY"}]}):
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "Authentication service unavailable" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_es256_key_rotation_handling(client: AsyncClient) -> None:
    """Verifies that an unknown kid triggers a JWKS refresh and succeeds if new key is in updated JWKS."""
    # Start with cache containing Key 1
    default_jwks_cache.set_jwks_data(TEST_JWKS)

    # Key 2 (rotated key)
    new_private_key = ec.generate_private_key(ec.SECP256R1())
    new_kid = "test-ec-kid-rotated-2"
    rotated_token = make_es256_token(private_key=new_private_key, kid=new_kid)

    # Remote JWKS endpoint has both Key 1 and Key 2
    updated_jwks = {
        "keys": [
            _make_jwk(TEST_EC_KID, TEST_EC_PUBLIC_KEY),
            _make_jwk(new_kid, new_private_key.public_key()),
        ]
    }

    with patch("app.auth.dependencies.settings") as mock_settings, \
         patch.object(default_jwks_cache, "_fetch_jwks", new_callable=AsyncMock, return_value=updated_jwks):
        mock_settings.supabase_jwt_algorithm = "ES256"
        mock_settings.supabase_url = TEST_SUPABASE_URL
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {rotated_token}"},
        )
    # Passed authentication
    assert response.status_code in (200, 503, 500)


# ─── Legacy / Symmetric HS256 Support Tests ───────────────────────────────────


@pytest.mark.anyio
async def test_hs256_valid_token_reaches_endpoint(client: AsyncClient) -> None:
    """When configured for HS256, a valid HS256 token passes auth."""
    token = make_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code in (200, 503, 500)


@pytest.mark.anyio
async def test_hs256_wrong_secret_returns_401(client: AsyncClient) -> None:
    """HS256 JWT signed with wrong secret must be rejected."""
    token = make_token(secret="wrong-secret")
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_hs256_expired_token_returns_401(client: AsyncClient) -> None:
    """Expired HS256 JWT should return 401."""
    token = make_token(expired=True)
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("blank_secret", ["", "   "])
async def test_hs256_missing_jwt_secret_fails_closed_with_500(
    client: AsyncClient, blank_secret: str
) -> None:
    """If SUPABASE_JWT_SECRET is missing/blank when HS256 is configured, fail closed with 500."""
    token = make_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = blank_secret
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 500
    assert "missing JWT secret" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_hs256_rejects_es256_token(client: AsyncClient) -> None:
    """When configured for HS256, an ES256 token must be rejected with 401."""
    token = make_es256_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401
    assert "Invalid token algorithm" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_server_rejects_none_algorithm_configuration(client: AsyncClient) -> None:
    """If server is somehow configured with algorithm='none', it must fail closed with 500."""
    token = make_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "none"
        response = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 500
    assert "Insecure JWT algorithm" in response.json().get("detail", "")


@pytest.mark.anyio
async def test_verify_token_defaults_to_free_plan() -> None:
    """When token has no app_metadata, plan defaults to free and is_paid is False."""
    from app.auth.dependencies import _verify_token

    token = make_token()
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        user = await _verify_token(token)

    assert user.plan == "free"
    assert user.is_paid is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "app_metadata,expected_plan,expected_is_paid",
    [
        ({"plan": "pro"}, "pro", True),
        ({"plan": "enterprise"}, "enterprise", True),
        ({"plan": "pro", "subscription_status": "active"}, "pro", True),
        ({"plan": "enterprise", "subscription_status": "trialing"}, "enterprise", True),
        ({"is_paid": True}, "free", True),
        ({"plan": "pro", "subscription_status": "canceled"}, "pro", False),
        ({"plan": "pro", "subscription_status": "past_due"}, "pro", False),
        ({"plan": "pro", "is_paid": False}, "pro", False),
        ({"plan": "free", "subscription_status": "active"}, "free", False),
        ({"plan": "growth"}, "growth", False),
        ({"plan": "free"}, "free", False),
        ({"plan": "free", "subscription_status": "canceled"}, "free", False),
        ({}, "free", False),
    ],
)
async def test_verify_token_extracts_subscription_tier_from_jwt_claims(
    app_metadata: dict,
    expected_plan: str,
    expected_is_paid: bool,
) -> None:
    """Extracts plan tier and paid status strictly from verified JWT app_metadata."""
    from app.auth.dependencies import _verify_token

    token = make_token(app_metadata=app_metadata)
    with patch("app.auth.dependencies.settings") as mock_settings:
        mock_settings.supabase_jwt_algorithm = "HS256"
        mock_settings.supabase_jwt_secret = TEST_JWT_SECRET
        user = await _verify_token(token)

    assert user.plan == expected_plan
    assert user.is_paid is expected_is_paid
