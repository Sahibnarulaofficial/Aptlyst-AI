"""Focused unit tests for the Async Razorpay HTTP client.

Covers:
1. HTTP Basic Authentication header formatting.
2. Successful GET and POST request responses.
3. HTTP 401 Unauthorized handling (RazorpayAuthError).
4. HTTP 429 Rate Limit handling (RazorpayRateLimitError).
5. HTTP 500 / 502 server/gateway error handling (RazorpayServer5xxError).
6. Request and connection timeout handling (RazorpayNetworkError, pre vs post dispatch).
7. Malformed JSON response handling.
8. Secret redaction in string representations and error messages.
9. Guard exceptions when Razorpay billing is disabled.
10. Test mock injection via set_razorpay_client_for_testing.
"""

import base64
import json
import sys
import pytest
import httpx
from pydantic import SecretStr

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from app.config import settings
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayBillingDisabledError,
    RazorpayClient,
    RazorpayConfigurationError,
    RazorpayNetworkError,
    RazorpayRateLimitError,
    RazorpayServer5xxError,
    get_razorpay_client,
    is_razorpay_enabled,
    reset_razorpay_client,
    sanitize_razorpay_error,
    set_razorpay_client_for_testing,
)


@pytest.fixture(autouse=True)
def cleanup_client():
    """Ensure global Razorpay client state is reset between tests."""
    reset_razorpay_client()
    yield
    reset_razorpay_client()


@pytest.mark.asyncio
async def test_razorpay_client_basic_auth_header():
    """Client must send HTTP Basic Auth header matching base64(key_id:key_secret)."""
    key_id = "rzp_test_mykey123"
    key_secret = "mysecret456"

    captured_auth = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_auth
        captured_auth = request.headers.get("Authorization")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(
        key_id=key_id,
        key_secret=key_secret,
        transport=transport,
    )

    resp = await client.get("/test")
    assert resp == {"status": "ok"}

    expected_creds = f"{key_id}:{key_secret}"
    expected_header = f"Basic {base64.b64encode(expected_creds.encode('ascii')).decode('ascii')}"
    assert captured_auth == expected_header
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_successful_post():
    """POST request sends JSON payload and returns parsed response."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["name"] == "Acme Corp"
        return httpx.Response(201, json={"id": "cust_12345", "name": "Acme Corp"})

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    resp = await client.create_customer(name="Acme Corp", email="acme@example.com")
    assert resp["id"] == "cust_12345"
    assert resp["name"] == "Acme Corp"
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_auth_failure_401():
    """HTTP 401 must raise RazorpayAuthError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "Invalid key"}})

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="bad_secret", transport=transport)

    with pytest.raises(RazorpayAuthError, match="Razorpay authentication failed"):
        await client.get("/customers")
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_rate_limit_429():
    """HTTP 429 must raise RazorpayRateLimitError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "RATE_LIMIT_EXCEEDED"}})

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayRateLimitError, match="Razorpay rate limit exceeded"):
        await client.get("/customers")
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_server_error_500():
    """HTTP 500 must raise RazorpayServer5xxError with parsed code and description."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "GATEWAY_ERROR", "description": "Internal gateway failure"}},
        )

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayServer5xxError) as exc_info:
        await client.get("/customers")

    err = exc_info.value
    assert err.status_code == 500
    assert err.error_code == "GATEWAY_ERROR"
    assert "Internal gateway failure" in str(err)
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_server_error_502():
    """HTTP 502 Bad Gateway must raise RazorpayServer5xxError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="<html><body>502 Bad Gateway</body></html>",
        )

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayServer5xxError) as exc_info:
        await client.get("/customers")

    err = exc_info.value
    assert err.status_code == 502
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_read_timeout_post_dispatch():
    """Read timeout after request dispatch raises RazorpayNetworkError with is_pre_dispatch=False."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out reading data")

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayNetworkError) as exc_info:
        await client.get("/customers")

    err = exc_info.value
    assert "timed out" in str(err)
    assert err.is_pre_dispatch is False
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_connect_timeout_pre_dispatch():
    """Connect timeout before request dispatch raises RazorpayNetworkError with is_pre_dispatch=True."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Failed to establish TCP connection")

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayNetworkError) as exc_info:
        await client.get("/customers")

    err = exc_info.value
    assert err.is_pre_dispatch is True
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_remote_protocol_error_post_dispatch():
    """Connection reset / protocol error after request dispatch raises RazorpayNetworkError with is_pre_dispatch=False."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without response")

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayNetworkError) as exc_info:
        await client.get("/customers")

    err = exc_info.value
    assert err.is_pre_dispatch is False
    await client.close()


@pytest.mark.asyncio
async def test_razorpay_client_malformed_json_200():
    """Non-JSON 200 response from Razorpay must raise RazorpayAPIError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Non-JSON</body></html>")

    transport = httpx.MockTransport(handler)
    client = RazorpayClient(key_id="rzp_test_123", key_secret="secret", transport=transport)

    with pytest.raises(RazorpayAPIError, match="Malformed JSON response from Razorpay"):
        await client.get("/customers")
    await client.close()


def test_razorpay_client_secret_redaction():
    """Client repr and sanitize_razorpay_error must never leak raw key_secret."""
    key_secret = "super_private_secret_9999"
    client = RazorpayClient(key_id="rzp_test_secretid123", key_secret=key_secret)

    client_repr = repr(client)
    assert key_secret not in client_repr
    assert "rzp_test_secretid123" not in client_repr  # Key ID masked to prefix

    leak_msg = f"Failed with header Basic {base64.b64encode(b'rzp_test_123:super_private_secret_9999').decode('ascii')}"
    sanitized = sanitize_razorpay_error(leak_msg)
    assert "super_private_secret_9999" not in sanitized
    assert "Basic [REDACTED]" in sanitized


def test_razorpay_client_disabled_guard(monkeypatch):
    """When razorpay_enabled is False, get_razorpay_client must raise RazorpayBillingDisabledError."""
    monkeypatch.setattr(settings, "razorpay_enabled", False)
    assert is_razorpay_enabled() is False

    with pytest.raises(RazorpayBillingDisabledError, match="Razorpay billing is currently disabled"):
        get_razorpay_client()


def test_razorpay_client_mock_injection():
    """set_razorpay_client_for_testing allows injecting mock clients."""
    mock_instance = RazorpayClient(key_id="rzp_test_mock", key_secret="secret")
    set_razorpay_client_for_testing(mock_instance)

    assert get_razorpay_client() is mock_instance
    reset_razorpay_client()
