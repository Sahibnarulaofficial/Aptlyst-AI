"""Tests for the Stripe Checkout Session API endpoint.

Endpoint: POST /api/v1/organizations/{org_id}/billing/checkout-session

Covers:
1. Successful checkout for an authorized organization owner.
2. Successful checkout for an authorized organization admin.
3. Unauthenticated request rejection (401).
4. Non-member organization access rejection (404).
5. Member without sufficient role rejection (403).
6. Stripe billing disabled response (503).
7. Active subscription conflict response (409).
8. Checkout already in progress response (409).
9. Recovery-required response (409) with safe message.
10. Invalid Stripe redirect URL rejected (502).
11. Valid Stripe Checkout URL returned (200).
12. Client cannot override price ID, customer ID, or return URLs.
13. Organization ID and session ID in the response are correct.
14. Internal Stripe exceptions do not leak secrets or raw error details (502/500).
15. Unit tests for validate_stripe_redirect_url security criteria.
"""

import sys

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
from app.api.billing import validate_stripe_redirect_url
from app.auth.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.main import app
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.subscription import CheckoutAttemptStatus
from app.services.billing.stripe_checkout import (
    ActiveSubscriptionExistsError,
    CheckoutInProgressError,
    CheckoutRecoveryRequiredError,
    StripeCheckoutError,
    StripeCheckoutSessionResult,
)
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
)

AUTH_HEADERS = {"Authorization": "Bearer test_valid_token"}


# =============================================================================
# In-Memory Database Session Test Double
# =============================================================================


class MockScalarResult:
    """Mock query result for scalar_one_or_none."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value


class InMemoryDbSession:
    """In-memory AsyncSession test double for checkout API authorization tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        members: list[OrganizationMember] | None = None,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            org.id: org for org in (organizations or [])
        }
        self.members: dict[tuple[uuid.UUID, uuid.UUID], OrganizationMember] = {
            (m.organization_id, m.user_id): m for m in (members or [])
        }
        self.committed_count = 0
        self.rolled_back_count = 0

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(pk)
        return None

    async def execute(self, stmt: Any) -> MockScalarResult:
        w_params: dict[str, Any] = {}
        for w in getattr(stmt, "_where_criteria", ()):
            col_name = getattr(getattr(w, "left", None), "key", None) or getattr(
                getattr(w, "left", None), "name", None
            )
            val = getattr(getattr(w, "right", None), "value", None)
            if col_name is not None and val is not None:
                w_params[col_name] = val

        # OrganizationMember lookup by (organization_id, user_id)
        org_id = w_params.get("organization_id")
        user_id = w_params.get("user_id")
        if org_id and user_id:
            member = self.members.get((org_id, user_id))
            return MockScalarResult(member)

        return MockScalarResult(None)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1


# =============================================================================
# Fixtures & Helpers
# =============================================================================


def setup_api_overrides(
    db: InMemoryDbSession,
    user_id: uuid.UUID,
    email: str = "user@example.com",
) -> None:
    """Register FastAPI dependency overrides."""
    async def override_db():
        yield db

    async def override_user():
        return CurrentUser(id=user_id, email=email)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user


def clear_api_overrides() -> None:
    """Clear FastAPI dependency overrides."""
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def clean_overrides():
    """Ensure dependency overrides are always cleared after each test."""
    yield
    clear_api_overrides()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def make_sample_checkout_result(
    org_id: uuid.UUID,
    session_id: str = "cs_test_session_123",
    checkout_url: str = "https://checkout.stripe.com/c/pay/cs_test_session_123",
) -> StripeCheckoutSessionResult:
    """Helper to build a sample successful StripeCheckoutSessionResult."""
    return StripeCheckoutSessionResult(
        attempt_id=uuid.uuid4(),
        organization_id=org_id,
        stripe_session_id=session_id,
        checkout_url=checkout_url,
        session_expires_at=datetime.now(timezone.utc),
        status=CheckoutAttemptStatus.open,
    )


# =============================================================================
# 1. Successful checkout for an authorized organization owner
# =============================================================================


@pytest.mark.asyncio
async def test_successful_checkout_authorized_owner(client: AsyncClient):
    """Verify that an organization owner can successfully initiate a checkout session."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Owner Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="owner@example.com")

    sample_result = make_sample_checkout_result(
        org_id=org_id,
        session_id="cs_test_owner_abc",
        checkout_url="https://checkout.stripe.com/c/pay/cs_test_owner_abc",
    )

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ) as mock_domain_call:
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 200
        data = res.json()
        assert data["organization_id"] == str(org_id)
        assert data["session_id"] == "cs_test_owner_abc"
        assert data["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_owner_abc"

        # Verify domain service was called with authenticated user's email
        mock_domain_call.assert_awaited_once_with(
            db=db,
            organization=org,
            user_email="owner@example.com",
        )


# =============================================================================
# 2. Successful checkout for an authorized organization admin
# =============================================================================


@pytest.mark.asyncio
async def test_successful_checkout_authorized_admin(client: AsyncClient):
    """Verify that an organization admin can successfully initiate a checkout session."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Admin Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    sample_result = make_sample_checkout_result(
        org_id=org_id,
        session_id="cs_test_admin_xyz",
        checkout_url="https://checkout.stripe.com/c/pay/cs_test_admin_xyz",
    )

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ) as mock_domain_call:
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 200
        data = res.json()
        assert data["organization_id"] == str(org_id)
        assert data["session_id"] == "cs_test_admin_xyz"
        assert data["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_admin_xyz"

        mock_domain_call.assert_awaited_once_with(
            db=db,
            organization=org,
            user_email="admin@example.com",
        )


# =============================================================================
# 3. Unauthenticated request
# =============================================================================


@pytest.mark.asyncio
async def test_unauthenticated_request(client: AsyncClient):
    """Verify that unauthenticated requests are strictly rejected with HTTP 401 Unauthorized."""
    org_id = uuid.uuid4()

    # 1. Missing Authorization header -> strictly 401 Unauthorized
    res_missing = await client.post(
        f"/api/v1/organizations/{org_id}/billing/checkout-session",
    )
    assert res_missing.status_code == 401
    assert "not authenticated" in res_missing.json()["detail"].lower()
    assert res_missing.headers.get("www-authenticate") == "Bearer"

    # 2. Invalid / forged Bearer token -> strictly 401 Unauthorized
    res_invalid = await client.post(
        f"/api/v1/organizations/{org_id}/billing/checkout-session",
        headers={"Authorization": "Bearer invalid.malformed.jwt"},
    )
    assert res_invalid.status_code == 401
    assert "detail" in res_invalid.json()


# =============================================================================
# 4. Non-member organization access
# =============================================================================


@pytest.mark.asyncio
async def test_non_member_organization_access(client: AsyncClient):
    """Verify that requesting checkout for a non-member organization returns 404."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Other Org")

    # User is NOT added as a member
    db = InMemoryDbSession(organizations=[org], members=[])
    setup_api_overrides(db, user_id=user_id, email="outsider@example.com")

    with patch("app.api.billing.create_stripe_checkout_session", new_callable=AsyncMock) as mock_domain:
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        # Invariant: 404 to avoid leaking whether the organization exists
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()
        mock_domain.assert_not_called()


# =============================================================================
# 5. Member without sufficient role
# =============================================================================


@pytest.mark.asyncio
async def test_member_without_sufficient_role(client: AsyncClient):
    """Verify that a regular member (non-admin, non-owner) is rejected with 403 Forbidden."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Member Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.member)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="regular_member@example.com")

    with patch("app.api.billing.create_stripe_checkout_session", new_callable=AsyncMock) as mock_domain:
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 403
        assert "insufficient role" in res.json()["detail"].lower()
        mock_domain.assert_not_called()


# =============================================================================
# 6. Stripe billing disabled
# =============================================================================


@pytest.mark.asyncio
async def test_stripe_billing_disabled(client: AsyncClient):
    """Verify that when Stripe billing is disabled or misconfigured, 503 is returned."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Disabled Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=StripeBillingDisabledError("Stripe billing is disabled in settings."),
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 503
        assert "stripe billing is currently disabled or unavailable" in res.json()["detail"].lower()


# =============================================================================
# 7. Active subscription conflict
# =============================================================================


@pytest.mark.asyncio
async def test_active_subscription_conflict(client: AsyncClient):
    """Verify that an active or trialing subscription raises 409 Conflict."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Active Sub Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=ActiveSubscriptionExistsError("Organization already has an active subscription."),
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 409
        assert "organization already has an active subscription" in res.json()["detail"].lower()


# =============================================================================
# 8. Checkout already in progress
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_already_in_progress(client: AsyncClient):
    """Verify that an in-flight checkout attempt raises 409 Conflict."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="In Progress Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=CheckoutInProgressError("A checkout attempt is currently in-flight."),
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 409
        assert "checkout session or payment is already in progress" in res.json()["detail"].lower()


# =============================================================================
# 9. Recovery-required response
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_required_response(client: AsyncClient):
    """Verify that an ambiguous attempt requiring recovery returns 409 Conflict with a safe message."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Recovery Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=CheckoutRecoveryRequiredError("Ambiguous Stripe outcome. Retaining reservation."),
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 409
        detail = res.json()["detail"]
        # Invariant: Safe message that does NOT imply a fresh session can be created
        assert "reconciliation" in detail.lower()
        assert "previous checkout attempt requires reconciliation" in detail.lower()


# =============================================================================
# 10. Invalid Stripe redirect URL rejected
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://checkout.stripe.com/c/pay/cs_test_http",  # Insecure HTTP
        "https://attacker.com/c/pay/cs_test",  # Wrong host
        "https://checkout.stripe.com.attacker.com/c/pay/cs_test",  # Hostname suffix spoofing
        "https://attacker-checkout.stripe.com/c/pay/cs_test",  # Hostname prefix spoofing
        "https://stripe.com/c/pay/cs_test",  # Main domain instead of checkout
        "https://user:pass@checkout.stripe.com/c/pay/cs_test",  # Credentials in netloc
        "https://checkout.stripe.com:8443/c/pay/cs_test",  # Non-standard port
        "javascript:alert(1)",  # Malicious scheme
        "",  # Empty string
    ],
)
async def test_invalid_stripe_redirect_url_rejected(client: AsyncClient, invalid_url: str):
    """Verify that any non-genuine Stripe Checkout redirect URL is rejected with 502 Bad Gateway."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="URL Validation Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    sample_result = make_sample_checkout_result(
        org_id=org_id,
        session_id="cs_test_invalid_url",
        checkout_url=invalid_url,
    )

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 502
        assert "invalid checkout url" in res.json()["detail"].lower()
        # Invariant: Invalid URL is never reflected back to the client
        assert invalid_url not in res.text if invalid_url else True


# =============================================================================
# 11. Valid Stripe Checkout URL returned
# =============================================================================


@pytest.mark.asyncio
async def test_valid_stripe_checkout_url_returned(client: AsyncClient):
    """Verify that a valid HTTPS checkout.stripe.com redirect URL is accepted and returned."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Valid URL Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    expected_url = "https://checkout.stripe.com/c/pay/cs_live_or_test_valid123#frag"
    sample_result = make_sample_checkout_result(
        org_id=org_id,
        session_id="cs_valid_123",
        checkout_url=expected_url,
    )

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 200
        assert res.json()["checkout_url"] == expected_url


# =============================================================================
# 12. Client cannot override price ID, customer ID, or return URLs
# =============================================================================


@pytest.mark.asyncio
async def test_client_cannot_override_price_id_customer_id_or_return_urls(client: AsyncClient):
    """Verify that client body parameters attempting to override billing settings are ignored or rejected."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="No Override Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    sample_result = make_sample_checkout_result(org_id=org_id)

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ) as mock_domain_call:
        # Malicious payload attempting client-controlled pricing or redirects
        malicious_body = {
            "price_id": "price_hacked_free",
            "customer_id": "cus_stolen_123",
            "success_url": "https://attacker.com/pwned",
            "cancel_url": "https://attacker.com/cancel",
            "idempotency_key": "custom-key-override",
        }
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session?price_id=price_hacked_query",
            json=malicious_body,
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 200

        # Invariant: Domain service only receives db, org, and user_email.
        # Malicious body and query parameters are never forwarded.
        mock_domain_call.assert_awaited_once_with(
            db=db,
            organization=org,
            user_email="admin@example.com",
        )


# =============================================================================
# 13. Organization ID and session ID in the response are correct
# =============================================================================


@pytest.mark.asyncio
async def test_organization_id_and_session_id_correct_and_minimal_schema(client: AsyncClient):
    """Verify exact organization_id, session_id matching and minimal safe schema."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Schema Test Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="owner@example.com")

    expected_session_id = f"cs_test_{uuid.uuid4().hex}"
    sample_result = make_sample_checkout_result(
        org_id=org_id,
        session_id=expected_session_id,
    )

    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        return_value=sample_result,
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 200
        data = res.json()

        # Strict whitelist of keys exposed
        assert set(data.keys()) == {"organization_id", "session_id", "checkout_url"}
        assert data["organization_id"] == str(org_id)
        assert data["session_id"] == expected_session_id
        assert data["checkout_url"] == sample_result.checkout_url


# =============================================================================
# 14. Internal Stripe exceptions do not leak secrets or raw error details
# =============================================================================


@pytest.mark.asyncio
async def test_internal_stripe_exceptions_do_not_leak_secrets(client: AsyncClient):
    """Verify that upstream Stripe errors and internal exceptions do not leak secrets to clients."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Secret Leak Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id=user_id, email="admin@example.com")

    # 1. StripeCheckoutError containing secret string
    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=StripeCheckoutError("Failed with secret sk_test_secret_key_12345 and sensitive payload"),
    ):
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res.status_code == 502
        assert "payment provider error" in res.json()["detail"].lower()
        # Invariant: Secret is NOT leaked
        assert "sk_test" not in res.text
        assert "sensitive payload" not in res.text

    # 2. Unexpected Internal Exception containing database secret
    with patch(
        "app.api.billing.create_stripe_checkout_session",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Database crash: postgres://user:super_secret_pw@db.internal:5432"),
    ):
        res_500 = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers=AUTH_HEADERS,
        )

        assert res_500.status_code == 500
        assert res_500.json()["detail"] == "Internal server error"
        assert "super_secret_pw" not in res_500.text


# =============================================================================
# 15. Unit tests for validate_stripe_redirect_url
# =============================================================================


def test_validate_stripe_redirect_url_valid():
    """Verify valid HTTPS checkout.stripe.com URLs pass validation."""
    validate_stripe_redirect_url("https://checkout.stripe.com/c/pay/cs_test_123")
    validate_stripe_redirect_url("https://checkout.stripe.com:443/c/pay/cs_test_123")
    validate_stripe_redirect_url("https://checkout.stripe.com/pay?session_id=cs_1#step1")


@pytest.mark.parametrize(
    "invalid_url,expected_match",
    [
        ("", "non-empty string"),
        ("   ", "non-empty string"),
        ("http://checkout.stripe.com/pay", "HTTPS is required"),
        ("ftp://checkout.stripe.com/pay", "HTTPS is required"),
        ("https://stripe.com/pay", "Expected 'checkout.stripe.com'"),
        ("https://connect.stripe.com/pay", "Expected 'checkout.stripe.com'"),
        ("https://evil-checkout.stripe.com/pay", "Expected 'checkout.stripe.com'"),
        ("https://checkout.stripe.com.attacker.com/pay", "Expected 'checkout.stripe.com'"),
        ("https://admin:pass@checkout.stripe.com/pay", "credentials in URL are forbidden"),
        ("https://checkout.stripe.com:8443/pay", "Invalid checkout URL port"),
    ],
)
def test_validate_stripe_redirect_url_invalid(invalid_url: str, expected_match: str):
    """Verify that all invalid redirect URLs raise ValueError with descriptive security messages."""
    with pytest.raises(ValueError, match=expected_match):
        validate_stripe_redirect_url(invalid_url)
