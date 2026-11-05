"""Tests for organization-scoped Stripe customer management.

Covers:
1. Customer creation for an organization without an existing association.
2. Reuse of an existing Stripe customer.
3. Stripe idempotency key usage.
4. Correct organization metadata.
5. Concurrent customer creation.
6. Database uniqueness conflict recovery.
7. Cross-tenant access rejection.
8. Unauthorized access rejection.
9. Stripe billing disabled behavior.
10. Stripe API failure handling.
11. Safe API response serialization.
12. Mock injection and cleanup.
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

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import stripe
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
from app.auth.dependencies import CurrentUser, get_current_user
from app.config import settings
from app.db.session import get_db
from app.main import app
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.billing import StripeCustomerOut
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    get_stripe_client,
    reset_stripe_client,
    set_stripe_client_for_testing,
)
from app.services.billing.stripe_customer import (
    StripeCustomerError,
    get_or_create_stripe_customer,
    get_stripe_customer_id,
)


@pytest.fixture(autouse=True)
def cleanup_stripe_client():
    """Ensure mock Stripe client is cleaned up after each test."""
    yield
    reset_stripe_client()


# ============================================================================
# In-Memory Database Session Test Double
# ============================================================================


class MockScalarResult:
    """Mock query result for scalar_one_or_none."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value


class InMemoryDbSession:
    """In-memory AsyncSession test double for billing customer operations."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        members: list[OrganizationMember] | None = None,
        subscriptions: list[Subscription] | None = None,
        fail_commit_with_integrity_error: bool = False,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            org.id: org for org in (organizations or [])
        }
        self.members: dict[tuple[uuid.UUID, uuid.UUID], OrganizationMember] = {
            (m.organization_id, m.user_id): m for m in (members or [])
        }
        self.subscriptions: dict[uuid.UUID, Subscription] = {
            s.organization_id: s for s in (subscriptions or [])
        }
        self.fail_commit_with_integrity_error = fail_commit_with_integrity_error
        self.added: list[Any] = []
        self.uncommitted: list[Any] = []
        self.committed_count = 0
        self.rolled_back_count = 0

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(pk)
        if entity_cls is Subscription:
            return self.subscriptions.get(pk)
        return None

    async def execute(self, stmt: Any) -> MockScalarResult:
        w_params = {
            w.left.key: w.right.value
            for w in getattr(stmt, "_where_criteria", ())
            if hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key")
        }

        cols = getattr(stmt, "selected_columns", [])
        col_names = [getattr(c, "key", getattr(c, "name", str(c))) for c in cols]

        # 1. OrganizationMember lookup by (organization_id, user_id)
        if "role" in col_names or any("member" in str(c) for c in col_names):
            org_id = w_params.get("organization_id")
            user_id = w_params.get("user_id")
            member = self.members.get((org_id, user_id)) if org_id and user_id else None
            return MockScalarResult(member)

        # 2. Subscription.stripe_customer_id lookup by organization_id
        if "stripe_customer_id" in col_names or any("subscription" in str(c) for c in col_names):
            org_id = w_params.get("organization_id")
            sub = self.subscriptions.get(org_id)
            if sub:
                return MockScalarResult(sub.stripe_customer_id)
            return MockScalarResult(None)

        return MockScalarResult(None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.uncommitted.append(obj)

    async def commit(self) -> None:
        if self.fail_commit_with_integrity_error:
            raise IntegrityError("Simulated unique constraint conflict", params={}, orig=Exception())
        for obj in self.uncommitted:
            if isinstance(obj, Subscription):
                self.subscriptions[obj.organization_id] = obj
        self.uncommitted.clear()
        self.committed_count += 1

    async def rollback(self) -> None:
        self.uncommitted.clear()
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


# ============================================================================
# Helpers
# ============================================================================


def setup_api_overrides(
    db: InMemoryDbSession, user_id: uuid.UUID, email: str = "admin@example.com"
) -> None:
    """Register FastAPI dependency overrides for in-memory DB and authenticated user."""
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


AUTH_HEADERS = {"Authorization": "Bearer test_token"}


def make_mock_stripe_client(customer_id: str = "cus_mock_123") -> MagicMock:
    """Create a mock StripeClient conforming to StripeClient.v1.customers."""
    mock_client = MagicMock(spec=stripe.StripeClient)
    mock_customers = MagicMock()
    mock_customer = MagicMock()
    mock_customer.id = customer_id
    mock_customers.create.return_value = mock_customer
    mock_customers.retrieve.return_value = mock_customer
    mock_client.v1 = MagicMock()
    mock_client.v1.customers = mock_customers
    mock_client.customers = mock_customers
    return mock_client


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ============================================================================
# 1. Customer creation for an organization without an existing association
# ============================================================================


async def test_customer_creation_without_existing_association(monkeypatch):
    """Verifies that an organization without a customer gets a new customer created and persisted."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Acme Corp")
    db = InMemoryDbSession(organizations=[org])

    mock_client = make_mock_stripe_client("cus_new_123")
    set_stripe_client_for_testing(mock_client)

    customer_id = await get_or_create_stripe_customer(
        db=db,
        organization=org,
        user_email="admin@acme.com",
    )

    assert customer_id == "cus_new_123"
    assert org.id in db.subscriptions
    sub = db.subscriptions[org.id]
    assert sub.stripe_customer_id == "cus_new_123"
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free"
    assert sub.stripe_subscription_id is None
    assert mock_client.v1.customers.create.call_count == 1


# ============================================================================
# 2. Reuse of an existing Stripe customer
# ============================================================================


async def test_reuse_existing_stripe_customer(monkeypatch):
    """Verifies that an organization with an existing customer reuses it without calling Stripe."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Existing Org")
    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_existing_999",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryDbSession(organizations=[org], subscriptions=[existing_sub])

    mock_client = make_mock_stripe_client("cus_unexpected")
    set_stripe_client_for_testing(mock_client)

    customer_id = await get_or_create_stripe_customer(
        db=db,
        organization=org,
        user_email="user@existing.com",
    )

    assert customer_id == "cus_existing_999"
    # Stripe API should NOT have been called
    assert mock_client.v1.customers.create.call_count == 0


# ============================================================================
# 3. Stripe idempotency key usage
# ============================================================================


async def test_stripe_idempotency_key_usage(monkeypatch):
    """Verifies that the customer creation request passes an organization-scoped idempotency key."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Idempotent Org")
    db = InMemoryDbSession(organizations=[org])

    mock_client = make_mock_stripe_client("cus_idem_1")
    set_stripe_client_for_testing(mock_client)

    await get_or_create_stripe_customer(db=db, organization=org)

    assert mock_client.v1.customers.create.call_count == 1
    call_kwargs = mock_client.v1.customers.create.call_args.kwargs
    options = call_kwargs.get("options")
    assert options is not None
    assert options.get("idempotency_key") == f"create-customer-{org.id}"


# ============================================================================
# 4. Correct organization metadata
# ============================================================================


async def test_correct_organization_metadata(monkeypatch):
    """Verifies that legitimate organization metadata is passed without exposing sensitive internal data."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Meta Org")
    db = InMemoryDbSession(organizations=[org])

    mock_client = make_mock_stripe_client("cus_meta_1")
    set_stripe_client_for_testing(mock_client)

    await get_or_create_stripe_customer(
        db=db,
        organization=org,
        user_email="meta_user@example.com",
    )

    call_kwargs = mock_client.v1.customers.create.call_args.kwargs
    params = call_kwargs.get("params")
    assert params is not None
    assert params["name"] == "Meta Org"
    assert params["email"] == "meta_user@example.com"
    assert params["metadata"] == {"organization_id": str(org.id)}
    # Ensure no secrets or sensitive internal fields are in metadata
    assert "secret" not in str(params).lower()
    assert "password" not in str(params).lower()


async def test_organization_metadata_without_email(monkeypatch):
    """Verifies that when email is None or blank, email field is omitted from Stripe params."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="No Email Org")
    db = InMemoryDbSession(organizations=[org])

    mock_client = make_mock_stripe_client("cus_no_email_1")
    set_stripe_client_for_testing(mock_client)

    await get_or_create_stripe_customer(db=db, organization=org, user_email=None)

    call_kwargs = mock_client.v1.customers.create.call_args.kwargs
    params = call_kwargs.get("params")
    assert "email" not in params


# ============================================================================
# 5. Concurrent customer creation
# ============================================================================


async def test_concurrent_customer_creation(monkeypatch):
    """Verifies that concurrent customer creation requests for the same organization safely resolve."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Concurrent Org")
    # Shared database session state
    db = InMemoryDbSession(organizations=[org])

    mock_client = make_mock_stripe_client("cus_concurrent_1")
    set_stripe_client_for_testing(mock_client)

    # Launch two concurrent requests
    res1, res2 = await asyncio.gather(
        get_or_create_stripe_customer(db=db, organization=org, user_email="admin1@test.com"),
        get_or_create_stripe_customer(db=db, organization=org, user_email="admin2@test.com"),
    )

    assert res1 == "cus_concurrent_1"
    assert res2 == "cus_concurrent_1"
    assert db.subscriptions[org.id].stripe_customer_id == "cus_concurrent_1"


# ============================================================================
# 6. Database uniqueness conflict recovery
# ============================================================================


async def test_database_uniqueness_conflict_recovery(monkeypatch):
    """Verifies that an IntegrityError during insert rolls back and safely reloads existing record."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Race Org")

    # Simulate: commit fails with IntegrityError on insert, but a concurrent worker committed the record
    class ConflictDbSession(InMemoryDbSession):
        async def commit(self):
            if self.uncommitted:
                # Concurrent worker wins and inserts the record in the background
                self.subscriptions[org.id] = Subscription(
                    id=uuid.uuid4(),
                    organization_id=org.id,
                    stripe_customer_id="cus_winner_race",
                    status=SubscriptionStatus.incomplete,
                    plan_tier="free",
                )
                self.uncommitted.clear()
                raise IntegrityError("duplicate key value violates unique constraint", params={}, orig=Exception())
            await super().commit()

    conflict_db = ConflictDbSession(organizations=[org])
    mock_client = make_mock_stripe_client("cus_loser_race")
    set_stripe_client_for_testing(mock_client)

    customer_id = await get_or_create_stripe_customer(
        db=conflict_db,
        organization=org,
    )

    # Should safely recover and return the winning committed customer ID
    assert customer_id == "cus_winner_race"
    assert conflict_db.rolled_back_count >= 1


async def test_database_uniqueness_conflict_unresolved_raises(monkeypatch):
    """Verifies that if an IntegrityError occurs and no record is found on reload, it is re-raised."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    org = Organization(id=uuid.uuid4(), name="Error Org")
    db = InMemoryDbSession(organizations=[org], fail_commit_with_integrity_error=True)

    mock_client = make_mock_stripe_client("cus_error_1")
    set_stripe_client_for_testing(mock_client)

    with pytest.raises(IntegrityError):
        await get_or_create_stripe_customer(db=db, organization=org)


# ============================================================================
# 7. Cross-tenant access rejection
# ============================================================================


async def test_cross_tenant_access_rejection(client: AsyncClient, monkeypatch):
    """Verifies that a user cannot access or create billing customer for an organization they don't belong to."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    user_id = uuid.uuid4()
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()

    # User belongs to Org A only
    member_a = OrganizationMember(organization_id=org_a_id, user_id=user_id, role=MemberRole.owner)
    org_a = Organization(id=org_a_id, name="Org A")
    org_b = Organization(id=org_b_id, name="Org B")

    mock_db = InMemoryDbSession(organizations=[org_a, org_b], members=[member_a])

    setup_api_overrides(mock_db, user_id=user_id, email="user@org-a.com")

    try:
        # User tries to access Org B's billing customer
        res_get = await client.get(
            f"/api/v1/organizations/{org_b_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_get.status_code == 404
        assert res_get.json()["detail"] == "Not found"

        res_post = await client.post(
            f"/api/v1/organizations/{org_b_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_post.status_code == 404
        assert res_post.json()["detail"] == "Not found"
    finally:
        clear_api_overrides()


# ============================================================================
# 8. Unauthorized access rejection
# ============================================================================


async def test_unauthorized_member_role_rejected(client: AsyncClient, monkeypatch):
    """Verifies that regular members (non-admin, non-owner) receive 403 Forbidden."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()

    # User is only a 'member'
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.member)
    org = Organization(id=org_id, name="Member Org")

    mock_db = InMemoryDbSession(organizations=[org], members=[member])

    setup_api_overrides(mock_db, user_id=user_id, email="member@test.com")

    try:
        res_get = await client.get(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_get.status_code == 403
        assert res_get.json()["detail"] == "Insufficient role"

        res_post = await client.post(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_post.status_code == 403
        assert res_post.json()["detail"] == "Insufficient role"
    finally:
        clear_api_overrides()


async def test_unauthenticated_request_rejected(client: AsyncClient):
    """Verifies that unauthenticated requests receive 401 or 403."""
    org_id = uuid.uuid4()
    # Missing authorization credentials
    res = await client.get(f"/api/v1/organizations/{org_id}/billing/customer")
    assert res.status_code in (401, 403)


# ============================================================================
# 9. Stripe billing disabled behavior
# ============================================================================


async def test_stripe_billing_disabled_without_existing_association(client: AsyncClient, monkeypatch):
    """Verifies behavior when Stripe billing is disabled and no customer association exists.

    Expected:
    - Service layer raises StripeBillingDisabledError.
    - GET returns 404 Not Found (local DB query finds nothing).
    - POST returns 503 Service Unavailable (creation required but disabled).
    - No Stripe API call occurs.
    """
    monkeypatch.setattr(settings, "stripe_enabled", False)

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Disabled Org No Cust")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)

    db = InMemoryDbSession(organizations=[org], members=[member])

    mock_client = make_mock_stripe_client("cus_never_called")
    set_stripe_client_for_testing(mock_client)

    # 1. Service layer check
    with pytest.raises(StripeBillingDisabledError) as exc_info:
        await get_or_create_stripe_customer(db=db, organization=org)
    assert "currently disabled" in str(exc_info.value).lower()

    # 2. API layer check
    setup_api_overrides(db, user_id=user_id, email="owner@test.com")

    try:
        # GET returns 404 because no association exists locally
        res_get = await client.get(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_get.status_code == 404
        assert res_get.json()["detail"] == "No billing customer found for this organization"

        # POST returns 503 because creating a new customer requires Stripe billing to be enabled
        res_post = await client.post(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_post.status_code == 503
        assert "disabled" in res_post.json()["detail"].lower()

        # Zero calls to external Stripe client
        assert mock_client.v1.customers.create.call_count == 0
    finally:
        clear_api_overrides()


async def test_stripe_billing_disabled_with_existing_association(client: AsyncClient, monkeypatch):
    """Verifies that an existing customer association is retrieved successfully even when Stripe is disabled.

    Expected:
    - Service layer returns existing customer ID without contacting Stripe.
    - GET returns 200 OK from local DB.
    - POST returns 200 OK from local DB (idempotent reuse).
    - No Stripe API call occurs.
    """
    monkeypatch.setattr(settings, "stripe_enabled", False)

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Disabled Org With Cust")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.owner)
    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_persisted_disabled_123",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )

    db = InMemoryDbSession(organizations=[org], members=[member], subscriptions=[existing_sub])

    mock_client = make_mock_stripe_client("cus_never_called")
    set_stripe_client_for_testing(mock_client)

    # 1. Service layer returns existing ID without error
    customer_id = await get_or_create_stripe_customer(db=db, organization=org)
    assert customer_id == "cus_persisted_disabled_123"

    # 2. API layer check
    setup_api_overrides(db, user_id=user_id, email="owner@test.com")

    try:
        # GET returns 200 OK from local database without requiring Stripe
        res_get = await client.get(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_get.status_code == 200
        get_data = res_get.json()
        assert get_data["organization_id"] == str(org_id)
        assert get_data["stripe_customer_id"] == "cus_persisted_disabled_123"

        # POST returns 200 OK (idempotent reuse) without requiring Stripe
        res_post = await client.post(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_post.status_code == 200
        post_data = res_post.json()
        assert post_data["organization_id"] == str(org_id)
        assert post_data["stripe_customer_id"] == "cus_persisted_disabled_123"

        # Zero calls to external Stripe client
        assert mock_client.v1.customers.create.call_count == 0
    finally:
        clear_api_overrides()


# ============================================================================
# 10. Stripe API failure handling
# ============================================================================


async def test_stripe_api_failure_handling(client: AsyncClient, monkeypatch):
    """Verifies that upstream Stripe API failures return 502 Bad Gateway without leaking secrets."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Failing Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])

    mock_client = make_mock_stripe_client()
    mock_client.v1.customers.create.side_effect = stripe.APIConnectionError("Connection timed out to api.stripe.com")
    set_stripe_client_for_testing(mock_client)

    # 1. Service layer
    with pytest.raises(StripeCustomerError) as exc_info:
        await get_or_create_stripe_customer(db=db, organization=org)
    assert "stripe api error" in str(exc_info.value).lower()

    # 2. API layer
    setup_api_overrides(db, user_id=user_id, email="admin@test.com")

    try:
        res = await client.post(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 502
        assert "payment provider error" in res.json()["detail"].lower()
        # Verify no secret or internal exception leaked in detail
        assert "sk_test" not in res.text
    finally:
        clear_api_overrides()


# ============================================================================
# 11. Safe API response serialization
# ============================================================================


async def test_safe_api_response_serialization(client: AsyncClient, monkeypatch):
    """Verifies that API responses strictly expose only safe customer fields."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Safe Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])

    mock_client = make_mock_stripe_client("cus_safe_123")
    set_stripe_client_for_testing(mock_client)

    setup_api_overrides(db, user_id=user_id, email="admin@test.com")

    try:
        res_post = await client.post(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_post.status_code == 200
        data = res_post.json()

        # Strict response schema verification
        assert set(data.keys()) == {"organization_id", "stripe_customer_id"}
        assert data["organization_id"] == str(org_id)
        assert data["stripe_customer_id"] == "cus_safe_123"

        # GET also returns the exact same safe schema
        res_get = await client.get(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res_get.status_code == 200
        assert res_get.json() == data
    finally:
        clear_api_overrides()


# ============================================================================
# 12. Mock injection and cleanup
# ============================================================================


def test_mock_injection_and_cleanup(monkeypatch):
    """Verifies that mock StripeClient injection routes correctly and reset cleans up."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    mock1 = make_mock_stripe_client("cus_test_1")
    set_stripe_client_for_testing(mock1)
    assert get_stripe_client() is mock1

    reset_stripe_client()

    mock2 = make_mock_stripe_client("cus_test_2")
    set_stripe_client_for_testing(mock2)
    assert get_stripe_client() is mock2

    reset_stripe_client()


# ============================================================================
# 13. GET customer when not found returns 404
# ============================================================================


async def test_get_customer_not_found(client: AsyncClient, monkeypatch):
    """Verifies that GET /billing/customer returns 404 when no customer exists."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret"))

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Empty Billing Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)

    db = InMemoryDbSession(organizations=[org], members=[member])

    setup_api_overrides(db, user_id=user_id, email="admin@test.com")

    try:
        res = await client.get(
            f"/api/v1/organizations/{org_id}/billing/customer",
            headers=AUTH_HEADERS,
        )
        assert res.status_code == 404
        assert res.json()["detail"] == "No billing customer found for this organization"
    finally:
        clear_api_overrides()
