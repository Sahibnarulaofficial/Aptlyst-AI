"""Comprehensive API Tests for Subscription Status and Billing Portal Endpoints.

Endpoints:
1. GET  /api/v1/organizations/{org_id}/billing/subscription
2. POST /api/v1/organizations/{org_id}/billing/portal

Covers:
Subscription Status:
- Unauthenticated request returns 401.
- Non-member request returns 404.
- Regular member receives 200.
- Admin and owner receive 200.
- Organization with no subscription and no billing period receives safe Free display response.
- Organization with active Free period returns correct quota, used, reserved, and remaining.
- Active Pro subscription returns correct subscription and billing-period info.
- Past-due and canceled subscription states are represented correctly.
- Blocked billing period includes its blocked state and reason.
- Historical subscriptions do not override current subscription.
- Stripe disabled still returns local billing info.
- GET does not mutate subscription or billing-period records.
- Response contains no secrets or internal diagnostic fields.

Billing Portal:
- Unauthenticated request returns 401.
- Non-member returns 404.
- Regular member returns 403.
- Admin and owner can create a portal session.
- Missing customer association returns safe 404 client error.
- Stripe disabled returns 503.
- Missing configuration returns 503.
- Stripe provider error returns sanitized 502.
- Invalid redirect URL (HTTP, non-Stripe domain, port, credentials) returns 502.
- Valid HTTPS billing.stripe.com URL returns 200.
- Client cannot override customer ID or return URL.
- No customer is created as a side effect.
- Unit tests for validate_stripe_portal_url.
"""

from datetime import datetime, timezone
import sys
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest

# Windows asyncio Proactor sandbox self-pipe fix
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self: Any) -> None:
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401
from app.api.billing import validate_stripe_portal_url
from app.auth.dependencies import CurrentUser, get_current_user
from app.config import Settings, settings
from app.db.session import get_db, get_session_factory
from app.main import app
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.payment import BillingCustomerProvision, CustomerProvisionStatus
from app.models.subscription import Subscription, SubscriptionStatus

AUTH_HEADERS = {"Authorization": "Bearer test_valid_token"}


# =============================================================================
# In-Memory Database Session Test Double
# =============================================================================


class MockScalarResult:
    """Mock query result wrapper."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value


class BillingApiMockDbSession:
    """In-memory AsyncSession test double for billing subscription and portal tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        members: list[OrganizationMember] | None = None,
        subscriptions: list[Subscription] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
        provisions: list[BillingCustomerProvision] | None = None,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            org.id: org for org in (organizations or [])
        }
        self.members: dict[tuple[uuid.UUID, uuid.UUID], OrganizationMember] = {
            (m.organization_id, m.user_id): m for m in (members or [])
        }
        self.subscriptions: list[Subscription] = list(subscriptions or [])
        self.billing_periods: list[OrganizationBillingPeriod] = list(billing_periods or [])
        self.provisions: list[BillingCustomerProvision] = list(provisions or [])
        self.committed_count = 0
        self.rolled_back_count = 0
        self.added: list[Any] = []

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(pk)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Subscription):
            self.subscriptions.append(obj)
        elif isinstance(obj, OrganizationBillingPeriod):
            self.billing_periods.append(obj)

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MockScalarResult:
        sql_str = str(stmt).lower()

        # 1. OrganizationMember query (membership check)
        if "organization_members" in sql_str:
            org_id, user_id = None, None
            for w in getattr(stmt, "_where_criteria", ()):
                col = getattr(getattr(w, "left", None), "name", None) or getattr(
                    getattr(w, "left", None), "key", None
                )
                val = getattr(getattr(w, "right", None), "value", None)
                if col == "organization_id":
                    org_id = val
                elif col == "user_id":
                    user_id = val
            if org_id and user_id:
                member = self.members.get((org_id, user_id))
                return MockScalarResult(member)
            return MockScalarResult(None)

        # 2. OrganizationBillingPeriod query
        if "organization_billing_periods" in sql_str:
            org_id = None
            is_active = True
            for w in getattr(stmt, "_where_criteria", ()):
                col = getattr(getattr(w, "left", None), "name", None) or getattr(
                    getattr(w, "left", None), "key", None
                )
                val = getattr(getattr(w, "right", None), "value", None)
                if col == "organization_id":
                    org_id = val
                elif col == "is_active":
                    is_active = val
            matching_bp = next(
                (
                    bp for bp in self.billing_periods
                    if (org_id is None or bp.organization_id == org_id)
                    and (is_active is None or bp.is_active == is_active)
                ),
                None,
            )
            return MockScalarResult(matching_bp)

        # 3. Subscriptions query
        if "subscriptions" in sql_str:
            org_id = None
            for w in getattr(stmt, "_where_criteria", ()):
                col = getattr(getattr(w, "left", None), "name", None) or getattr(
                    getattr(w, "left", None), "key", None
                )
                val = getattr(getattr(w, "right", None), "value", None)
                if col == "organization_id":
                    org_id = val

            matching_subs = [
                s for s in self.subscriptions
                if org_id is None or s.organization_id == org_id
            ]

            # Specialized check for get_stripe_customer_id: returns customer ID string
            if "select subscriptions.stripe_customer_id" in sql_str:
                matching_subs.sort(
                    key=lambda s: s.created_at or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                for s in matching_subs:
                    if s.stripe_customer_id:
                        return MockScalarResult(s.stripe_customer_id)
                return MockScalarResult(None)

            # Prioritize: active/trialing (1) > past_due (2) > paused/unpaid (3) > incomplete (4) > canceled (5)
            def sub_priority(s: Subscription) -> tuple[int, datetime]:
                p = 5
                if s.status in (SubscriptionStatus.active, SubscriptionStatus.trialing):
                    p = 1
                elif s.status == SubscriptionStatus.past_due:
                    p = 2
                elif s.status in (SubscriptionStatus.paused, SubscriptionStatus.unpaid):
                    p = 3
                elif s.status == SubscriptionStatus.incomplete:
                    p = 4
                dt = s.created_at or datetime.min.replace(tzinfo=timezone.utc)
                return (p, dt)

            matching_subs.sort(key=lambda s: (sub_priority(s)[0], -sub_priority(s)[1].timestamp()))
            top_sub = matching_subs[0] if matching_subs else None
            return MockScalarResult(top_sub)

        return MockScalarResult(None)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1


# =============================================================================
# Fixtures & Test Settings
# =============================================================================


def get_test_settings(
    stripe_enabled: bool = True,
    secret_key: str = "sk_test_mock_1234567890abcdef",
    billing_return_url: str = "http://localhost:3000/billing",
) -> Settings:
    """Build mock Settings for billing tests."""
    return Settings(
        stripe_enabled=stripe_enabled,
        stripe_secret_key=SecretStr(secret_key),
        stripe_webhook_secret=SecretStr("whsec_test_mock_1234567890abcdef"),
        stripe_publishable_key="pk_test_mock_1234567890abcdef",
        stripe_pro_price_id="price_pro_invoice_test_123",
        stripe_billing_return_url=billing_return_url,
        default_free_monthly_token_quota=100000,
        default_pro_monthly_token_quota=1000000,
    )


def setup_api_overrides(
    db: BillingApiMockDbSession,
    user_id: uuid.UUID,
    email: str = "user@example.com",
    session_factory_provider: Any = None,
) -> None:
    """Register FastAPI dependency overrides."""
    async def override_db():
        yield db

    async def override_user():
        return CurrentUser(id=user_id, email=email)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    if session_factory_provider is not None:
        app.dependency_overrides[get_session_factory] = session_factory_provider


def clear_api_overrides() -> None:
    """Clear FastAPI dependency overrides."""
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_session_factory, None)
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


# =============================================================================
# 1. GET /api/v1/organizations/{org_id}/billing/subscription Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_unauthenticated_returns_401(client: AsyncClient):
    """Verify that unauthenticated GET requests are rejected with 401 Unauthorized."""
    org_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/organizations/{org_id}/billing/subscription")
    assert resp.status_code == 401
    assert "Not authenticated" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_subscription_non_member_returns_404(client: AsyncClient):
    """Verify that non-members receive 404 Not Found to prevent tenant enumeration."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Foreign Org")
    db = BillingApiMockDbSession(organizations=[org], members=[])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


@pytest.mark.asyncio
async def test_subscription_regular_member_returns_200(client: AsyncClient):
    """Verify that a regular member can safely view the organization's subscription status."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Member Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.member)

    db = BillingApiMockDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["organization_id"] == str(org_id)
    assert data["plan_tier"] == "free"
    assert data["status"] == "active"
    assert data["billing_period"] is None


@pytest.mark.asyncio
async def test_subscription_admin_and_owner_receive_200(client: AsyncClient):
    """Verify that admin and owner roles receive 200 OK."""
    owner_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Org")
    member_owner = OrganizationMember(organization_id=org_id, user_id=owner_id, role=MemberRole.owner)
    member_admin = OrganizationMember(organization_id=org_id, user_id=admin_id, role=MemberRole.admin)

    db = BillingApiMockDbSession(organizations=[org], members=[member_owner, member_admin])

    # Test Owner
    setup_api_overrides(db, owner_id)
    resp_owner = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp_owner.status_code == 200

    # Test Admin
    setup_api_overrides(db, admin_id)
    resp_admin = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp_admin.status_code == 200


@pytest.mark.asyncio
async def test_subscription_no_sub_no_billing_period_safe_free_display(client: AsyncClient):
    """Verify organization with no subscription and no billing period receives safe Free display with null period."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Empty Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[], billing_periods=[])
    setup_api_overrides(db, user_id)

    with patch("app.api.billing.is_stripe_enabled", return_value=True):
        resp = await client.get(
            f"/api/v1/organizations/{org_id}/billing/subscription",
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["organization_id"] == str(org_id)
    assert data["plan_tier"] == "free"
    assert data["status"] == "active"
    assert data["current_period_start"] is None
    assert data["current_period_end"] is None
    assert data["cancel_at_period_end"] is False
    assert data["canceled_at"] is None
    assert data["past_due_since"] is None
    assert data["has_stripe_customer"] is False
    assert data["stripe_enabled"] is True
    assert data["billing_period"] is None  # Never fabricates an active period!


@pytest.mark.asyncio
async def test_subscription_active_free_period_correct_quota_and_usage(client: AsyncClient):
    """Verify organization with active Free period returns accurate usage and tokens remaining."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Free Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    t_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=100000,
        tokens_used=15000,
        tokens_reserved=5000,
        is_active=True,
        is_blocked=False,
    )

    db = BillingApiMockDbSession(organizations=[org], members=[member], billing_periods=[bp])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_tier"] == "free"
    assert data["status"] == "active"

    bp_data = data["billing_period"]
    assert bp_data is not None
    assert bp_data["id"] == str(bp.id)
    assert bp_data["monthly_token_quota"] == 100000
    assert bp_data["tokens_used"] == 15000
    assert bp_data["tokens_reserved"] == 5000
    assert bp_data["tokens_remaining"] == 80000  # 100000 - 15000 - 5000
    assert bp_data["is_blocked"] is False
    assert bp_data["blocked_reason"] is None


@pytest.mark.asyncio
async def test_subscription_active_pro_correct_info(client: AsyncClient):
    """Verify active Pro subscription returns correct subscription fields and billing period."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Pro Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    t_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_pro_123",
        stripe_subscription_id="sub_pro_123",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        current_period_start=t_start,
        current_period_end=t_end,
        cancel_at_period_end=True,
    )
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="pro",
        monthly_token_quota=1000000,
        tokens_used=200000,
        tokens_reserved=0,
        is_active=True,
        is_blocked=False,
    )

    db = BillingApiMockDbSession(
        organizations=[org], members=[member], subscriptions=[sub], billing_periods=[bp]
    )
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_tier"] == "pro"
    assert data["status"] == "active"
    assert data["cancel_at_period_end"] is True
    assert data["has_stripe_customer"] is True
    assert data["billing_period"]["plan_tier"] == "pro"
    assert data["billing_period"]["tokens_remaining"] == 800000


@pytest.mark.asyncio
async def test_subscription_past_due_state_represented_correctly(client: AsyncClient):
    """Verify that a past-due subscription returns status='past_due' and past_due_since timestamp."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Past Due Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    anchor = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_past_due",
        stripe_subscription_id="sub_past_due",
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=anchor,
    )

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "past_due"
    assert data["past_due_since"].startswith("2026-11-02T12:00:00")


@pytest.mark.asyncio
async def test_subscription_canceled_state_represented_correctly(client: AsyncClient):
    """Verify that a canceled subscription returns status='canceled' and canceled_at timestamp."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Canceled Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    canceled_time = datetime(2026, 10, 25, 8, 30, 0, tzinfo=timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_canceled",
        stripe_subscription_id="sub_canceled",
        status=SubscriptionStatus.canceled,
        plan_tier="pro",
        canceled_at=canceled_time,
    )

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "canceled"
    assert data["canceled_at"].startswith("2026-10-25T08:30:00")


@pytest.mark.asyncio
async def test_subscription_free_tier_with_razorpay_customer_provision(client: AsyncClient):
    """Verify that an organization without a paid subscription but with a Razorpay customer
    provisioning record returns plan_tier='free' and status='active', ensuring customer provisioning
    state is never interpreted as a subscription lifecycle state."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Free Tier With Razorpay Provision")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.member)

    # Customer provision in resolved state (customer created, but NO subscription created yet)
    provision = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_rzp_precheckout_999",
        status=CustomerProvisionStatus.resolved,
    )

    db = BillingApiMockDbSession(
        organizations=[org],
        members=[member],
        subscriptions=[],  # Zero subscription rows!
        provisions=[provision],
    )
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_tier"] == "free"
    assert data["status"] == "active"
    assert data["has_stripe_customer"] is False



@pytest.mark.asyncio
async def test_subscription_blocked_billing_period_includes_reason(client: AsyncClient):
    """Verify that a blocked billing period returns is_blocked=True and the blocked_reason."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Blocked Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    t_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=100000,
        tokens_used=100000,
        tokens_reserved=0,
        is_active=True,
        is_blocked=True,
        blocked_reason="Monthly quota exhausted",
    )

    db = BillingApiMockDbSession(organizations=[org], members=[member], billing_periods=[bp])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    bp_data = data["billing_period"]
    assert bp_data["is_blocked"] is True
    assert bp_data["blocked_reason"] == "Monthly quota exhausted"
    assert bp_data["tokens_remaining"] == 0  # Does not return negative balance!


@pytest.mark.asyncio
async def test_subscription_historical_subscriptions_do_not_override_current_subscription(
    client: AsyncClient,
):
    """Verify that a historical canceled subscription does not override the current active subscription."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Resubscribed Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    sub_old = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_old",
        status=SubscriptionStatus.canceled,
        plan_tier="pro",
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    sub_current = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_current",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        created_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
    )

    db = BillingApiMockDbSession(
        organizations=[org], members=[member], subscriptions=[sub_old, sub_current]
    )
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"  # Selected active over canceled!
    assert data["plan_tier"] == "pro"


@pytest.mark.asyncio
async def test_subscription_stripe_disabled_returns_local_billing_info(client: AsyncClient):
    """Verify that GET /subscription succeeds even when Stripe billing is disabled."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Local Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = BillingApiMockDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id)

    with patch("app.api.billing.is_stripe_enabled", return_value=False):
        resp = await client.get(
            f"/api/v1/organizations/{org_id}/billing/subscription",
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["stripe_enabled"] is False
    assert data["plan_tier"] == "free"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_subscription_get_does_not_mutate_records(client: AsyncClient):
    """Verify that GET /subscription is strictly read-only and adds no records."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Read Only Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = BillingApiMockDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert len(db.added) == 0
    assert db.committed_count == 0


@pytest.mark.asyncio
async def test_subscription_response_contains_no_secrets(client: AsyncClient):
    """Verify that the response contains no internal Stripe secrets, keys, or diagnostics."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Safe Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_safe_123",
        stripe_subscription_id="sub_safe_123",
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    resp = await client.get(
        f"/api/v1/organizations/{org_id}/billing/subscription",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    raw_text = resp.text.lower()
    for forbidden in ["secret", "payload", "diagnostic", "stripe_subscription_id", "sk_test", "whsec"]:
        assert f'"{forbidden}"' not in raw_text


# =============================================================================
# 2. POST /api/v1/organizations/{org_id}/billing/portal Tests
# =============================================================================


@pytest.mark.asyncio
async def test_portal_unauthenticated_returns_401(client: AsyncClient):
    """Verify that unauthenticated POST requests to /portal are rejected with 401."""
    org_id = uuid.uuid4()
    resp = await client.post(f"/api/v1/organizations/{org_id}/billing/portal")
    assert resp.status_code == 401
    assert "Not authenticated" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_portal_non_member_returns_404(client: AsyncClient):
    """Verify that non-members receive 404 Not Found on /portal."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Foreign Org")
    db = BillingApiMockDbSession(organizations=[org], members=[])
    setup_api_overrides(db, user_id)

    resp = await client.post(
        f"/api/v1/organizations/{org_id}/billing/portal",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


@pytest.mark.asyncio
async def test_portal_regular_member_returns_403(client: AsyncClient):
    """Verify that a member with role 'member' receives 403 Forbidden on /portal."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Member Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.member)

    db = BillingApiMockDbSession(organizations=[org], members=[member])
    setup_api_overrides(db, user_id)

    resp = await client.post(
        f"/api/v1/organizations/{org_id}/billing/portal",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 403
    assert "Insufficient role" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_portal_admin_and_owner_can_create_portal_session(client: AsyncClient):
    """Verify that admin and owner can successfully create a billing portal session."""
    owner_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Portal Org")
    member_owner = OrganizationMember(organization_id=org_id, user_id=owner_id, role=MemberRole.owner)
    member_admin = OrganizationMember(organization_id=org_id, user_id=admin_id, role=MemberRole.admin)

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_portal_123",
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    db = BillingApiMockDbSession(
        organizations=[org], members=[member_owner, member_admin], subscriptions=[sub]
    )

    mock_session = {"url": "https://billing.stripe.com/p/session/test_session_123"}

    # 1. Owner creation
    setup_api_overrides(db, owner_id)
    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch("app.api.billing._call_stripe_create_portal_session", return_value=mock_session),
    ):
        resp_owner = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp_owner.status_code == 200
    assert resp_owner.json()["portal_url"] == "https://billing.stripe.com/p/session/test_session_123"
    assert resp_owner.json()["organization_id"] == str(org_id)

    # 2. Admin creation
    setup_api_overrides(db, admin_id)
    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch("app.api.billing._call_stripe_create_portal_session", return_value=mock_session),
    ):
        resp_admin = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp_admin.status_code == 200
    assert resp_admin.json()["portal_url"] == "https://billing.stripe.com/p/session/test_session_123"


@pytest.mark.asyncio
async def test_portal_missing_customer_association_returns_404(client: AsyncClient):
    """Verify that an organization without a Stripe customer returns 404 client error without creating one."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="No Customer Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[])
    setup_api_overrides(db, user_id)

    resp = await client.post(
        f"/api/v1/organizations/{org_id}/billing/portal",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    assert "No billing customer found" in resp.json()["detail"]
    assert len(db.added) == 0  # No customer was created as a side-effect!


@pytest.mark.asyncio
async def test_portal_stripe_disabled_returns_503(client: AsyncClient):
    """Verify that disabled Stripe returns 503 Service Unavailable."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    with (
        patch("app.api.billing.is_stripe_enabled", return_value=False),
        patch("app.api.billing.settings", get_test_settings(stripe_enabled=False)),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_portal_missing_configuration_returns_503(client: AsyncClient):
    """Verify that missing return URL configuration returns 503."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch.object(settings, "stripe_billing_return_url", ""),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_portal_stripe_provider_error_returns_sanitized_502(client: AsyncClient):
    """Verify that Stripe provider errors return sanitized 502 Bad Gateway without leaking internals."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch(
            "app.api.billing._call_stripe_create_portal_session",
            side_effect=RuntimeError("Secret raw Stripe API failure stack trace with sk_test_mock_12345"),
        ),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Payment provider error while creating billing portal session"
    assert "sk_test" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://billing.stripe.com/p/session/test_insecure",
        "https://attacker.com/p/session/test",
        "https://billing.stripe.com.attacker.com/p/session/test",
        "https://evil-billing.stripe.com/p/session/test",
        "https://user:pass@billing.stripe.com/p/session/test",
        "https://billing.stripe.com:8443/p/session/test",
        "javascript:alert(1)",
        "",
    ],
)
async def test_portal_invalid_redirect_url_returns_502(client: AsyncClient, invalid_url: str):
    """Verify that invalid portal URLs returned by provider fail validation with 502 Bad Gateway."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch(
            "app.api.billing._call_stripe_create_portal_session",
            return_value={"url": invalid_url},
        ),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 502
    assert "invalid billing portal url" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_portal_valid_url_returns_200(client: AsyncClient):
    """Verify that a valid HTTPS billing.stripe.com URL succeeds with 200."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    valid_portal_url = "https://billing.stripe.com/p/session/test_valid_session_123"
    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch(
            "app.api.billing._call_stripe_create_portal_session",
            return_value={"url": valid_portal_url},
        ),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["portal_url"] == valid_portal_url


@pytest.mark.asyncio
async def test_portal_customer_id_and_return_url_sourced_server_side(client: AsyncClient):
    """Verify that client cannot override customer ID, return URL, or organization identity."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    sub = Subscription(id=uuid.uuid4(), organization_id=org_id, stripe_customer_id="cus_authentic_123")

    db = BillingApiMockDbSession(organizations=[org], members=[member], subscriptions=[sub])
    setup_api_overrides(db, user_id)

    captured_call: dict[str, Any] = {}

    def mock_create(customer_id: str, return_url: str) -> dict[str, str]:
        captured_call["customer_id"] = customer_id
        captured_call["return_url"] = return_url
        return {"url": "https://billing.stripe.com/p/session/test"}

    malicious_body = {
        "customer": "cus_evil_999",
        "customer_id": "cus_evil_999",
        "return_url": "https://attacker.com/steal-creds",
        "organization_id": str(uuid.uuid4()),
    }

    with (
        patch("app.api.billing.is_stripe_enabled", return_value=True),
        patch("app.api.billing.settings", get_test_settings()),
        patch("app.api.billing._call_stripe_create_portal_session", side_effect=mock_create),
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/portal",
            headers=AUTH_HEADERS,
            json=malicious_body,
        )

    assert resp.status_code == 200
    # Customer ID and return URL came exclusively from server-side database and settings!
    assert captured_call["customer_id"] == "cus_authentic_123"
    assert captured_call["return_url"] == "http://localhost:3000/billing"


# =============================================================================
# 3. Unit Tests for validate_stripe_portal_url
# =============================================================================


def test_validate_stripe_portal_url_valid():
    """Verify valid HTTPS billing.stripe.com URLs pass validation."""
    validate_stripe_portal_url("https://billing.stripe.com/p/session/test")
    validate_stripe_portal_url("https://billing.stripe.com:443/p/session/test")


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("", "non-empty string"),
        ("   ", "non-empty string"),
        ("http://billing.stripe.com/pay", "HTTPS is required"),
        ("ftp://billing.stripe.com/pay", "HTTPS is required"),
        ("https://stripe.com/pay", "Expected 'billing.stripe.com'"),
        ("https://checkout.stripe.com/pay", "Expected 'billing.stripe.com'"),
        ("https://billing.stripe.com.attacker.com/pay", "Expected 'billing.stripe.com'"),
        ("https://evil-billing.stripe.com/pay", "Expected 'billing.stripe.com'"),
        ("https://admin:pass@billing.stripe.com/pay", "credentials in URL are forbidden"),
        ("https://billing.stripe.com:8443/pay", "Invalid portal URL port"),
    ],
)
def test_validate_stripe_portal_url_invalid(url: str, match: str):
    """Verify invalid portal URLs fail with descriptive ValueError."""
    with pytest.raises(ValueError, match=match):
        validate_stripe_portal_url(url)


# =============================================================================
# 4. D-2 Stage 1 Razorpay Checkout Session Factory DI Integration Tests
# =============================================================================


class _RazorpayCheckoutDiSessionDouble:
    """Minimal AsyncSession double sharing committed tables across endpoint & provisioning sessions."""

    def __init__(
        self,
        *,
        name: str,
        organizations: dict[uuid.UUID, Organization],
        members: dict[tuple[uuid.UUID, uuid.UUID], OrganizationMember],
        provisions: list[BillingCustomerProvision],
        subscriptions: list[Subscription],
        attempts: list[Any],
    ) -> None:
        self.name = name
        self.organizations = organizations
        self.members = members
        self.provisions = provisions
        self.subscriptions = subscriptions
        self.attempts = attempts
        self._in_tx = False
        self.uncommitted: list[Any] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.executed_tables: list[str] = []

    def in_transaction(self) -> bool:
        return self._in_tx

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        self._in_tx = True
        if entity_cls is Organization:
            return self.organizations.get(pk)
        return None

    def add(self, obj: Any) -> None:
        self._in_tx = True
        self.uncommitted.append(obj)

    async def flush(self) -> None:
        self._in_tx = True

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MockScalarResult:
        from app.models.payment import BillingCheckoutAttempt

        self._in_tx = True
        w_params: dict[str, Any] = {}
        for w in getattr(stmt, "_where_criteria", ()):
            if hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key"):
                if hasattr(w.right, "value"):
                    w_params[w.left.key] = w.right.value

        from_table = ""
        froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
        for f in froms_list:
            tname = getattr(f, "name", None) or getattr(f, "__tablename__", "")
            if tname:
                from_table = tname
                break
        if from_table:
            self.executed_tables.append(from_table)

        org_id = w_params.get("organization_id")

        if from_table == "organizations":
            lookup_id = w_params.get("id") or org_id
            return MockScalarResult(self.organizations.get(lookup_id))

        if from_table == "organization_members":
            user_id = w_params.get("user_id")
            if org_id and user_id:
                return MockScalarResult(self.members.get((org_id, user_id)))
            return MockScalarResult(None)

        if from_table == "billing_customer_provisions":
            for p in reversed(self.provisions):
                if org_id and p.organization_id != org_id:
                    continue
                if "provider" in w_params and p.provider != w_params["provider"]:
                    continue
                if "merchant_account_id" in w_params and p.merchant_account_id != w_params["merchant_account_id"]:
                    continue
                if "environment" in w_params and p.environment != w_params["environment"]:
                    continue
                return MockScalarResult(p)
            return MockScalarResult(None)

        if from_table == "subscriptions":
            for s in reversed(self.subscriptions):
                if org_id and s.organization_id != org_id:
                    continue
                return MockScalarResult(s)
            return MockScalarResult(None)

        if from_table == "billing_checkout_attempts":
            aid = w_params.get("id")
            for a in reversed(self.attempts):
                if not isinstance(a, BillingCheckoutAttempt):
                    continue
                if aid and a.id != aid:
                    continue
                if org_id and a.organization_id != org_id:
                    continue
                if "status" in w_params:
                    sf = w_params["status"]
                    if isinstance(sf, (list, tuple, set)):
                        if a.status not in sf:
                            continue
                    elif a.status != sf:
                        continue
                return MockScalarResult(a)
            return MockScalarResult(None)

        return MockScalarResult(None)

    async def commit(self) -> None:
        from app.models.payment import BillingCheckoutAttempt

        self.commit_calls += 1
        for obj in self.uncommitted:
            if isinstance(obj, BillingCustomerProvision) and obj not in self.provisions:
                self.provisions.append(obj)
            elif isinstance(obj, BillingCheckoutAttempt) and obj not in self.attempts:
                self.attempts.append(obj)
        self.uncommitted.clear()
        self._in_tx = False

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self.uncommitted.clear()
        self._in_tx = False


@pytest.mark.asyncio
async def test_razorpay_checkout_endpoint_wires_get_session_factory_into_customer_provisioning(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """D-2 Stage 1 API DI: POST /razorpay/checkout-subscription uses FastAPI `get_session_factory`
    dependency override exclusively for customer provisioning (separate from the request `get_db`
    session and never falling back to `AsyncSessionLocal`).
    """
    from contextlib import asynccontextmanager
    from app.services.billing.razorpay.client import reset_razorpay_client, set_razorpay_client_for_testing

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="DI Billing Endpoint Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    shared_orgs = {org_id: org}
    shared_members = {(org_id, user_id): member}
    shared_provisions: list[BillingCustomerProvision] = []
    shared_subscriptions: list[Subscription] = []
    shared_attempts: list[Any] = []

    request_db = _RazorpayCheckoutDiSessionDouble(
        name="request_db",
        organizations=shared_orgs,
        members=shared_members,
        provisions=shared_provisions,
        subscriptions=shared_subscriptions,
        attempts=shared_attempts,
    )
    opened_factory_sessions: list[_RazorpayCheckoutDiSessionDouble] = []

    def custom_factory():
        @asynccontextmanager
        async def _ctx():
            sess = _RazorpayCheckoutDiSessionDouble(
                name=f"customer_factory_{len(opened_factory_sessions) + 1}",
                organizations=shared_orgs,
                members=shared_members,
                provisions=shared_provisions,
                subscriptions=shared_subscriptions,
                attempts=shared_attempts,
            )
            opened_factory_sessions.append(sess)
            try:
                yield sess
            finally:
                if sess.in_transaction():
                    await sess.rollback()

        return _ctx()

    async def provide_custom_factory():
        return custom_factory

    def _forbidden_default_factory():
        raise AssertionError("AsyncSessionLocal must not be called when get_session_factory is overridden")

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.AsyncSessionLocal",
        _forbidden_default_factory,
    )
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_enabled", True)
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_test_123")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_di_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("secret_di_123"))
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "plan_test_di")
    monkeypatch.setattr(settings, "razorpay_plan_currency", "USD")

    class _MockClient:
        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            assert all(not s.in_transaction() for s in opened_factory_sessions)
            assert not request_db.in_transaction()
            return {"id": "cust_di_endpoint_123", "name": kwargs.get("name")}

        async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs.get("customer_id") == "cust_di_endpoint_123"
            return {
                "id": "sub_di_endpoint_456",
                "entity": "subscription",
                "status": "created",
                "short_url": "https://rzp.io/i/sub_di_endpoint_456",
            }

    set_razorpay_client_for_testing(_MockClient())
    setup_api_overrides(
        request_db,  # type: ignore[arg-type]
        user_id,
        email="di-admin@example.com",
        session_factory_provider=provide_custom_factory,
    )

    try:
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/billing/razorpay/checkout-subscription",
            headers=AUTH_HEADERS,
        )
    finally:
        reset_razorpay_client()

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription_id"] == "sub_di_endpoint_456"
    assert len(opened_factory_sessions) == 2  # TX_A1 (claim) + TX_A2 (resolved)
    assert all(s.commit_calls == 1 for s in opened_factory_sessions)
    assert all(not s.in_transaction() for s in opened_factory_sessions)
    assert len(shared_provisions) == 1
    assert shared_provisions[0].provider_customer_id == "cust_di_endpoint_123"
    assert shared_provisions[0].status == CustomerProvisionStatus.resolved
    assert not request_db.in_transaction()
