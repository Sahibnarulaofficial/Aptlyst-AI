"""Async HTTP client for Razorpay API v1.

Guarantees:
1. Server-side only: Uses HTTP Basic Authentication with server-side key ID and key secret.
2. Zero secret leakage: Credentials and auth headers are never logged or exposed in exceptions.
3. Test isolation: Supports transport injection (httpx.MockTransport) and mock overrides.
4. Structured error handling: Distinct exception types for auth, rate limits, and network errors.
5. Resource management: Configurable timeouts and async client lifecycle.
"""

import json
import logging
import re
from typing import Any
import httpx
from pydantic import SecretStr

from app.config import settings

logger = logging.getLogger(__name__)


class RazorpayClientError(Exception):
    """Base exception for all Razorpay client operations."""


class RazorpayBillingDisabledError(RazorpayClientError):
    """Raised when attempting to use Razorpay client while billing is disabled."""


class RazorpayConfigurationError(RazorpayClientError):
    """Raised when Razorpay billing is enabled but credentials or settings are invalid."""


class RazorpayAuthError(RazorpayClientError):
    """Raised when authentication with Razorpay fails (HTTP 401)."""


class RazorpayRateLimitError(RazorpayClientError):
    """Raised when Razorpay rate limits are exceeded (HTTP 429)."""


class RazorpayAPIError(RazorpayClientError):
    """Raised when Razorpay returns a non-2xx status code."""

    def __init__(
        self,
        message: str,
        status_code: int,
        error_code: str | None = None,
        error_description: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_description = error_description


class RazorpayServer5xxError(RazorpayAPIError):
    """Raised when Razorpay returns an HTTP 5xx server or gateway error.

    Unlike 4xx client errors, 5xx server errors represent an uncertain / ambiguous outcome
    where the upstream gateway or server may have performed the requested mutation before
    the error response was generated.
    """


class RazorpayNetworkError(RazorpayClientError):
    """Raised when network connectivity, DNS, or timeouts fail.

    Attributes:
        is_pre_dispatch: True if the failure demonstrably occurred before the HTTP request
            was sent to Razorpay (e.g. ConnectTimeout, ConnectError, PoolTimeout).
            False if the failure occurred during or after dispatch (e.g. ReadTimeout,
            RemoteProtocolError, ReadError, connection reset), meaning the remote outcome
            is ambiguous.
    """

    def __init__(self, message: str, is_pre_dispatch: bool = False) -> None:
        super().__init__(message)
        self.is_pre_dispatch = is_pre_dispatch


def sanitize_razorpay_error(text: str) -> str:
    """Sanitize error messages to prevent credential or secret leakage."""
    if not text:
        return ""
    # Redact basic auth headers or key secrets if present
    sanitized = re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", text)
    sanitized = re.sub(r"rzp_(test|live)_[A-Za-z0-9]+", "rzp_\\1_[REDACTED]", sanitized)
    return sanitized


class RazorpayClient:
    """Async HTTP client for Razorpay REST API."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = "https://api.razorpay.com/v1",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not key_id or not key_id.strip():
            raise RazorpayConfigurationError("Razorpay key_id must be non-empty.")
        if not key_secret or not key_secret.strip():
            raise RazorpayConfigurationError("Razorpay key_secret must be non-empty.")

        self.key_id = key_id.strip()
        self._key_secret = key_secret.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout, connect=5.0)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        masked_key = self.key_id[:8] + "..." if len(self.key_id) > 8 else "***"
        return f"<RazorpayClient key_id='{masked_key}' base_url='{self.base_url}'>"

    async def _get_client(self) -> httpx.AsyncClient:
        """Obtain or initialize the underlying httpx AsyncClient."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=httpx.BasicAuth(self.key_id, self._key_secret),
                timeout=self.timeout,
                transport=self._transport,
                headers={
                    "User-Agent": "Aptlyst-AI/1.0",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "RazorpayClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request to Razorpay.

        Args:
            method: HTTP verb ('GET', 'POST', etc.).
            path: Relative URL path (e.g. '/customers').
            params: Optional query parameters.
            json_data: Optional JSON payload.
            headers: Optional additional headers.

        Returns:
            Parsed JSON dictionary from Razorpay response.

        Raises:
            RazorpayAuthError: If authentication fails (401).
            RazorpayRateLimitError: If rate limited (429).
            RazorpayAPIError: For other 4xx/5xx responses.
            RazorpayNetworkError: On connection or timeout failure.
        """
        client = await self._get_client()
        url = path if path.startswith("/") else f"/{path}"

        try:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
            )
        except httpx.ConnectTimeout as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay connect timeout (pre-dispatch) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay connection timed out before request dispatch: {sanitized_msg}",
                is_pre_dispatch=True,
            ) from exc
        except (httpx.ConnectError, httpx.PoolTimeout) as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay connection error (pre-dispatch) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay connection failed before request dispatch: {sanitized_msg}",
                is_pre_dispatch=True,
            ) from exc
        except httpx.ReadTimeout as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay read timeout (post-dispatch, ambiguous) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay read timed out after request dispatch (ambiguous): {sanitized_msg}",
                is_pre_dispatch=False,
            ) from exc
        except (httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay network error (post-dispatch, ambiguous) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay connection error after request dispatch (ambiguous): {sanitized_msg}",
                is_pre_dispatch=False,
            ) from exc
        except httpx.TimeoutException as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay generic timeout (ambiguous) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay request timed out (ambiguous): {sanitized_msg}",
                is_pre_dispatch=False,
            ) from exc
        except httpx.NetworkError as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Razorpay generic network error (ambiguous) for %s %s: %s", method, url, sanitized_msg)
            raise RazorpayNetworkError(
                f"Razorpay network error (ambiguous): {sanitized_msg}",
                is_pre_dispatch=False,
            ) from exc
        except Exception as exc:
            sanitized_msg = sanitize_razorpay_error(str(exc))
            logger.error("Unexpected error connecting to Razorpay: %s", sanitized_msg)
            raise RazorpayNetworkError(
                f"Unexpected connection error (ambiguous): {sanitized_msg}",
                is_pre_dispatch=False,
            ) from exc

        # Handle HTTP status codes
        if response.status_code == 401:
            raise RazorpayAuthError("Razorpay authentication failed: invalid key_id or key_secret")
        if response.status_code == 429:
            raise RazorpayRateLimitError("Razorpay rate limit exceeded; please retry later")

        # Parse JSON safely
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            if response.status_code >= 500:
                raise RazorpayServer5xxError(
                    f"Razorpay server returned non-JSON error (HTTP {response.status_code})",
                    status_code=response.status_code,
                ) from exc
            if not response.is_success:
                raise RazorpayAPIError(
                    f"Razorpay returned non-JSON error ({response.status_code})",
                    status_code=response.status_code,
                ) from exc
            raise RazorpayAPIError(
                "Malformed JSON response from Razorpay",
                status_code=response.status_code,
            ) from exc

        if not response.is_success:
            err_dict = data.get("error", {}) if isinstance(data, dict) else {}
            code = err_dict.get("code") if isinstance(err_dict, dict) else None
            desc = err_dict.get("description") if isinstance(err_dict, dict) else None
            message = desc or f"Razorpay API error ({response.status_code})"

            if response.status_code >= 500:
                raise RazorpayServer5xxError(
                    message=message,
                    status_code=response.status_code,
                    error_code=code,
                    error_description=desc,
                )

            raise RazorpayAPIError(
                message=message,
                status_code=response.status_code,
                error_code=code,
                error_description=desc,
            )

        if not isinstance(data, dict):
            raise RazorpayAPIError(
                "Unexpected response structure from Razorpay: expected JSON object",
                status_code=response.status_code,
            )

        return data

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated GET request."""
        return await self.request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated POST request."""
        return await self.request("POST", path, params=params, json_data=json_data, headers=headers)

    async def create_customer(
        self,
        name: str,
        email: str | None = None,
        contact: str | None = None,
        notes: dict[str, str] | None = None,
        fail_existing: int = 0,
    ) -> dict[str, Any]:
        """Create or fetch a customer on Razorpay.

        Args:
            name: Customer / organization name.
            email: Optional customer email.
            contact: Optional customer phone number.
            notes: Organization-scoped safe metadata dictionary.
            fail_existing: If 0, Razorpay returns existing customer on matching email/phone.

        Returns:
            Razorpay customer object dictionary (containing 'id', etc.).
        """
        payload: dict[str, Any] = {
            "name": name.strip(),
            "fail_existing": fail_existing,
        }
        if email and email.strip():
            payload["email"] = email.strip()
        if contact and contact.strip():
            payload["contact"] = contact.strip()
        if notes:
            payload["notes"] = notes

        return await self.post("/customers", json_data=payload)

    async def create_subscription(
        self,
        plan_id: str,
        total_count: int = 120,
        customer_id: str | None = None,
        quantity: int = 1,
        customer_notify: int = 1,
        notes: dict[str, str] | None = None,
        start_at: int | None = None,
        expire_by: int | None = None,
        addons: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a subscription on Razorpay.

        Args:
            plan_id: Razorpay plan identifier (e.g. 'plan_xxx').
            total_count: Total billing cycles (default 120 for 10-year monthly plan).
            customer_id: Optional Razorpay customer identifier (e.g. 'cust_xxx').
            quantity: Quantity of the subscription.
            customer_notify: 1 if Razorpay should notify customer, 0 otherwise.
            notes: Organization-scoped safe metadata dictionary.
            start_at: Optional Unix timestamp for future start.
            expire_by: Optional Unix timestamp for checkout expiry.
            addons: Optional list of add-on objects.

        Returns:
            Razorpay subscription object dictionary (containing 'id', 'short_url', etc.).
        """
        payload: dict[str, Any] = {
            "plan_id": plan_id.strip(),
            "total_count": total_count,
            "quantity": quantity,
            "customer_notify": customer_notify,
        }
        if customer_id and customer_id.strip():
            payload["customer_id"] = customer_id.strip()
        if notes:
            payload["notes"] = notes
        if start_at is not None:
            payload["start_at"] = start_at
        if expire_by is not None:
            payload["expire_by"] = expire_by
        if addons:
            payload["addons"] = addons

        return await self.post("/subscriptions", json_data=payload)

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Fetch subscription details from Razorpay."""
        if not subscription_id or not subscription_id.strip():
            raise RazorpayClientError("subscription_id must be non-empty.")
        return await self.get(f"/subscriptions/{subscription_id.strip()}")

    async def list_subscriptions(
        self,
        plan_id: str | None = None,
        from_time: int | None = None,
        to_time: int | None = None,
        count: int = 10,
        skip: int = 0,
    ) -> dict[str, Any]:
        """Fetch a list of subscriptions from Razorpay.

        Note: Razorpay GET /v1/subscriptions accepts 'plan_id', 'from', 'to', 'count', 'skip'.
        It does NOT natively support filtering by 'customer_id' or arbitrary 'notes' keys.
        """
        params: dict[str, Any] = {"count": count, "skip": skip}
        if plan_id and plan_id.strip():
            params["plan_id"] = plan_id.strip()
        if from_time is not None:
            params["from"] = from_time
        if to_time is not None:
            params["to"] = to_time
        return await self.get("/subscriptions", params=params)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch payment details from Razorpay."""
        if not payment_id or not payment_id.strip():
            raise RazorpayClientError("payment_id must be non-empty.")
        return await self.get(f"/payments/{payment_id.strip()}")


# Module-level client cache & test override
_TEST_RAZORPAY_CLIENT: Any | None = None
_CACHED_RAZORPAY_CLIENT: RazorpayClient | None = None


def is_razorpay_enabled() -> bool:
    """Return True if Razorpay billing is enabled and configured."""
    return bool(
        settings.razorpay_enabled
        and settings.razorpay_key_id.strip()
        and settings.razorpay_key_secret.get_secret_value().strip()
    )


def get_razorpay_client() -> RazorpayClient:
    """Retrieve or initialize the centralized RazorpayClient instance.

    Raises:
        RazorpayBillingDisabledError: If settings.razorpay_enabled is False.
        RazorpayConfigurationError: If key_id or key_secret is missing.

    Returns:
        RazorpayClient: Configured async client.
    """
    global _TEST_RAZORPAY_CLIENT, _CACHED_RAZORPAY_CLIENT

    if _TEST_RAZORPAY_CLIENT is not None:
        return _TEST_RAZORPAY_CLIENT

    if not settings.razorpay_enabled:
        raise RazorpayBillingDisabledError(
            "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
        )

    key_id = settings.razorpay_key_id.strip()
    key_secret = settings.razorpay_key_secret.get_secret_value().strip()

    if not key_id or not key_secret:
        raise RazorpayConfigurationError(
            "Razorpay credentials are incomplete. Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET."
        )

    if _CACHED_RAZORPAY_CLIENT is None:
        _CACHED_RAZORPAY_CLIENT = RazorpayClient(
            key_id=key_id,
            key_secret=key_secret,
        )

    return _CACHED_RAZORPAY_CLIENT


def set_razorpay_client_for_testing(client: Any | None) -> None:
    """Inject a test or mock RazorpayClient for testing."""
    global _TEST_RAZORPAY_CLIENT
    _TEST_RAZORPAY_CLIENT = client


def reset_razorpay_client() -> None:
    """Reset cached Razorpay client instances between tests."""
    global _TEST_RAZORPAY_CLIENT, _CACHED_RAZORPAY_CLIENT
    _TEST_RAZORPAY_CLIENT = None
    _CACHED_RAZORPAY_CLIENT = None
