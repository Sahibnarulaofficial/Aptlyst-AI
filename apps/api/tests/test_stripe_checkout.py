"""Tests for organization-scoped Stripe Checkout domain service.
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
from datetime import datetime, timedelta, timezone
from typing import Any
import unittest.mock as mock
import uuid

from pydantic import SecretStr
import pytest
import stripe
from sqlalchemy.exc import IntegrityError

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
from app.config import settings
from app.models.organization import Organization
from app.models.subscription import (
    CheckoutAttemptStatus,
    StripeCheckoutAttempt,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.stripe_checkout import (
    IDEMPOTENCY_SAFE_REPLAY_CUTOFF_HOURS,
    ActiveSubscriptionExistsError,
    CheckoutInProgressError,
    CheckoutRecoveryRequiredError,
    StripeCheckoutError,
    StripeCheckoutSessionResult,
    _update_attempt_status,
    build_checkout_return_url,
    classify_stripe_error,
    create_stripe_checkout_session,
    get_active_checkout_attempt,
    recover_checkout_attempt,
)
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
    reset_stripe_client,
    set_stripe_client_for_testing,
)


@pytest.fixture(autouse=True)
def configure_test_settings(monkeypatch):
    """Ensure baseline Stripe test settings and clean test client between tests."""
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_mock_secret_key_12345"))
    monkeypatch.setattr(settings, "stripe_pro_price_id", "price_test_pro_monthly")
    monkeypatch.setattr(settings, "stripe_billing_return_url", "https://app.example.com/billing")
    yield
    reset_stripe_client()


# =============================================================================
# In-Memory Database Session Test Double
# =============================================================================


class MockScalars:
    """Mock query result for .scalars()."""

    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)

    def first(self) -> Any:
        return self._values[0] if self._values else None


class MockScalarResult:
    """Mock query result for scalar_one_or_none and scalars().all()."""

    def __init__(self, value: Any, values: list[Any] | None = None) -> None:
        self._value = value
        self._values = values if values is not None else ([value] if value is not None else [])

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value

    def scalars(self) -> MockScalars:
        return MockScalars(self._values)


class InMemoryCheckoutDbSession:
    """In-memory AsyncSession test double for Stripe checkout domain tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        subscriptions: list[Subscription] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        fail_commit_with_integrity_error: bool = False,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            org.id: org for org in (organizations or [])
        }
        self.subscriptions: list[Subscription] = list(subscriptions or [])
        self.attempts: list[StripeCheckoutAttempt] = list(attempts or [])
        self.fail_commit_with_integrity_error = fail_commit_with_integrity_error
        self.uncommitted: list[Any] = []
        self._in_transaction = False
        self.committed_count = 0
        self.rolled_back_count = 0

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def execute(self, stmt: Any) -> MockScalarResult:
        self._in_transaction = True
        stmt_str = str(stmt)

        # Extract where criteria attributes
        criteria: dict[str, Any] = {}
        for clause in getattr(stmt, "_where_criteria", ()):
            col_name = getattr(getattr(clause, "left", None), "name", None)
            val = getattr(getattr(clause, "right", None), "value", None)
            if col_name is not None and val is not None:
                criteria[col_name] = val

        def _sort_key(att: StripeCheckoutAttempt) -> datetime:
            dt = getattr(att, "created_at", None)
            if dt is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        # 1. Organization row lock / lookup
        if "organizations" in stmt_str and "organizations.id" in stmt_str:
            target_org_id = criteria.get("id")
            if target_org_id and target_org_id in self.organizations:
                return MockScalarResult(target_org_id)
            for org_id in self.organizations:
                if str(org_id) in stmt_str:
                    return MockScalarResult(org_id)
            if self.organizations:
                return MockScalarResult(list(self.organizations.keys())[0])
            return MockScalarResult(None)

        # 2. Subscription active/trialing check
        if "subscriptions" in stmt_str and "status" in stmt_str:
            target_org_id = criteria.get("organization_id")
            for sub in self.subscriptions:
                if target_org_id is None or sub.organization_id == target_org_id:
                    if sub.status in (SubscriptionStatus.active, SubscriptionStatus.trialing):
                        return MockScalarResult(sub.id)
            return MockScalarResult(None)

        # 3. Subscription by stripe_subscription_id (_is_attempt_reconciled)
        if "subscriptions" in stmt_str and "stripe_subscription_id" in stmt_str:
            target_org_id = criteria.get("organization_id")
            target_sub_id = criteria.get("stripe_subscription_id")
            if not target_sub_id:
                for clause in getattr(stmt, "_where_criteria", ()):
                    right = getattr(clause, "right", None)
                    val = getattr(right, "value", None)
                    if val and isinstance(val, str) and val.startswith("sub_"):
                        target_sub_id = val
                        break
            for sub in self.subscriptions:
                if target_sub_id and sub.stripe_subscription_id == target_sub_id:
                    if target_org_id is None or sub.organization_id == target_org_id:
                        return MockScalarResult(sub.id)
            return MockScalarResult(None)

        # 4. Subscription stripe_customer_id lookup (get_stripe_customer_id)
        if "subscriptions" in stmt_str and "stripe_customer_id" in stmt_str:
            target_org_id = criteria.get("organization_id")
            for sub in self.subscriptions:
                if (target_org_id is None or sub.organization_id == target_org_id) and sub.stripe_customer_id:
                    return MockScalarResult(sub.stripe_customer_id)
            return MockScalarResult(None)

        # 5. StripeCheckoutAttempt by attempt_id (_update_attempt_status and reload in _handle_locally_expired_open_attempt)
        if "stripe_checkout_attempts" in stmt_str and ("stripe_checkout_attempts.id =" in stmt_str or "id" in criteria):
            all_attempts = self.attempts + [
                obj for obj in self.uncommitted if isinstance(obj, StripeCheckoutAttempt)
            ]
            target_attempt_id = criteria.get("id")
            if target_attempt_id:
                for att in all_attempts:
                    if att.id == target_attempt_id:
                        return MockScalarResult(att)
                return MockScalarResult(None)
            if all_attempts:
                return MockScalarResult(all_attempts[-1])
            return MockScalarResult(None)

        # 6. StripeCheckoutAttempt completed attempts query (step 5.5 in create_stripe_checkout_session)
        if "stripe_checkout_attempts" in stmt_str and criteria.get("status") in (
            CheckoutAttemptStatus.complete,
            "complete",
        ):
            target_org_id = criteria.get("organization_id")
            all_attempts = self.attempts + [
                obj for obj in self.uncommitted if isinstance(obj, StripeCheckoutAttempt)
            ]
            completed = [
                att
                for att in all_attempts
                if att.status == CheckoutAttemptStatus.complete
                and (target_org_id is None or att.organization_id == target_org_id)
            ]
            completed.sort(key=_sort_key, reverse=True)
            return MockScalarResult(completed[0] if completed else None, values=completed)

        # 7. StripeCheckoutAttempt active attempts query (get_active_checkout_attempt)
        if "stripe_checkout_attempts" in stmt_str:
            target_org_id = criteria.get("organization_id")
            all_attempts = self.attempts + [
                obj for obj in self.uncommitted if isinstance(obj, StripeCheckoutAttempt)
            ]
            active_statuses = {
                CheckoutAttemptStatus.creating,
                CheckoutAttemptStatus.recovery_required,
                CheckoutAttemptStatus.open,
                CheckoutAttemptStatus.payment_pending,
            }
            matching = [
                att
                for att in all_attempts
                if att.status in active_statuses
                and (target_org_id is None or att.organization_id == target_org_id)
            ]
            matching.sort(key=_sort_key, reverse=True)
            if matching:
                return MockScalarResult(matching[0])
            return MockScalarResult(None)

        return MockScalarResult(None)

    def add(self, obj: Any) -> None:
        self.uncommitted.append(obj)

    async def commit(self) -> None:
        if self.fail_commit_with_integrity_error and any(
            isinstance(obj, StripeCheckoutAttempt) for obj in self.uncommitted
        ):
            self._in_transaction = False
            raise IntegrityError(
                "Simulated unique constraint conflict: uq_org_active_checkout_attempt",
                params={},
                orig=Exception("duplicate key value violates unique constraint"),
            )
        for obj in self.uncommitted:
            if isinstance(obj, StripeCheckoutAttempt):
                if obj not in self.attempts:
                    self.attempts.append(obj)
            elif isinstance(obj, Subscription):
                if obj not in self.subscriptions:
                    self.subscriptions.append(obj)
        self.uncommitted.clear()
        self._in_transaction = False
        self.committed_count += 1

    async def rollback(self) -> None:
        self.uncommitted.clear()
        self._in_transaction = False
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


# =============================================================================
# Mock Stripe Client Setup Helper
# =============================================================================


def create_mock_stripe_client():
    """Create a configured mock Stripe client conforming to SDK v16.1.0."""
    client = mock.MagicMock()
    v1 = mock.MagicMock()
    checkout = mock.MagicMock()
    sessions = mock.MagicMock()
    customers = mock.MagicMock()

    sessions.create = mock.MagicMock()
    sessions.retrieve = mock.MagicMock()
    customers.create = mock.MagicMock()

    client.v1 = v1
    v1.checkout = checkout
    checkout.sessions = sessions
    v1.customers = customers

    client.checkout = checkout
    client.customers = customers

    return client, sessions, customers


# =============================================================================
# 1. URL Helper Tests
# =============================================================================


def test_build_checkout_return_url_clean():
    """Verify appending query parameters to a clean base URL."""
    base_url = "https://app.example.com/settings/billing"
    res = build_checkout_return_url(
        base_url,
        session_id="{CHECKOUT_SESSION_ID}",
        status="success",
    )
    assert res == "https://app.example.com/settings/billing?session_id={CHECKOUT_SESSION_ID}&status=success"


def test_build_checkout_return_url_merges_existing_query_and_preserves_fragment():
    """Verify existing query parameters and fragments are preserved when merging."""
    base_url = "https://app.example.com/billing?tab=plans&ref=banner#pricing"
    res = build_checkout_return_url(base_url, status="cancelled")
    assert "tab=plans" in res
    assert "ref=banner" in res
    assert "status=cancelled" in res
    assert res.endswith("#pricing")


def test_build_checkout_return_url_overwrites_existing_param():
    """Verify newly passed parameters overwrite existing query parameters of the same name."""
    base_url = "https://app.example.com/billing?status=old"
    res = build_checkout_return_url(base_url, status="success")
    assert res == "https://app.example.com/billing?status=success"


def test_build_checkout_return_url_unencoded_session_placeholder():
    """Verify {CHECKOUT_SESSION_ID} is preserved verbatim without %7B / %7D encoding."""
    base_url = "https://app.example.com/billing"
    res = build_checkout_return_url(base_url, session_id="{CHECKOUT_SESSION_ID}")
    assert "{CHECKOUT_SESSION_ID}" in res
    assert "%7B" not in res
    assert "%7D" not in res


def test_build_checkout_return_url_invalid():
    """Verify invalid URL schemes, missing hosts, or empty URLs raise ValueError."""
    with pytest.raises(ValueError, match="base_url must not be empty"):
        build_checkout_return_url("")

    with pytest.raises(ValueError, match="Invalid URL scheme"):
        build_checkout_return_url("ftp://app.example.com/billing")

    with pytest.raises(ValueError, match="missing host"):
        build_checkout_return_url("https:///billing")


# =============================================================================
# 2. Error Classification Tests
# =============================================================================


def test_classify_stripe_error_definitive():
    """Verify definitive client/configuration errors are classified as 'failed'."""
    invalid_req = stripe.InvalidRequestError("No such price", "price_id", http_status=400)
    auth_err = stripe.AuthenticationError("Invalid API key")
    perm_err = stripe.PermissionError("Permission denied")

    assert classify_stripe_error(invalid_req) == "failed"
    assert classify_stripe_error(auth_err) == "failed"
    assert classify_stripe_error(perm_err) == "failed"


def test_classify_stripe_error_ambiguous():
    """Verify network, server, and unexpected errors are classified as 'recovery_required'."""
    conn_err = stripe.APIConnectionError("Connection timeout")
    api_err = stripe.APIError("Internal server error", http_status=500)
    py_timeout = TimeoutError("Socket timed out")
    async_timeout = asyncio.TimeoutError()
    generic_err = RuntimeError("Worker crashed")

    assert classify_stripe_error(conn_err) == "recovery_required"
    assert classify_stripe_error(api_err) == "recovery_required"
    assert classify_stripe_error(py_timeout) == "recovery_required"
    assert classify_stripe_error(async_timeout) == "recovery_required"
    assert classify_stripe_error(generic_err) == "recovery_required"


# =============================================================================
# 3. Configuration & Billing Disabled Safeguards
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_stripe_disabled_raises(monkeypatch):
    """Verify create_stripe_checkout_session raises StripeBillingDisabledError when billing is disabled."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    db = InMemoryCheckoutDbSession(organizations=[org])

    monkeypatch.setattr(settings, "stripe_enabled", False)
    with pytest.raises(StripeBillingDisabledError, match="Stripe billing is currently disabled"):
        await create_stripe_checkout_session(db, org)


@pytest.mark.asyncio
async def test_checkout_missing_pro_price_id_raises(monkeypatch):
    """Verify missing or empty STRIPE_PRO_PRICE_ID raises StripeConfigurationError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    db = InMemoryCheckoutDbSession(organizations=[org])

    monkeypatch.setattr(settings, "stripe_pro_price_id", "")
    with pytest.raises(StripeConfigurationError, match="Stripe Pro price ID is not configured"):
        await create_stripe_checkout_session(db, org)


@pytest.mark.asyncio
async def test_checkout_missing_return_url_raises(monkeypatch):
    """Verify missing or empty STRIPE_BILLING_RETURN_URL raises StripeConfigurationError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    db = InMemoryCheckoutDbSession(organizations=[org])

    monkeypatch.setattr(settings, "stripe_billing_return_url", "")
    with pytest.raises(StripeConfigurationError, match="Stripe billing return URL is not configured"):
        await create_stripe_checkout_session(db, org)


# =============================================================================
# 4. Subscription State Guards
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_active_subscription_raises():
    """Verify an organization with an active subscription is rejected with ActiveSubscriptionExistsError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_test_1",
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    with pytest.raises(ActiveSubscriptionExistsError, match="already has an active or trialing subscription"):
        await create_stripe_checkout_session(db, org)


@pytest.mark.asyncio
async def test_checkout_trialing_subscription_raises():
    """Verify an organization with a trialing subscription is rejected with ActiveSubscriptionExistsError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_test_1",
        status=SubscriptionStatus.trialing,
        plan_tier="pro",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    with pytest.raises(ActiveSubscriptionExistsError, match="already has an active or trialing subscription"):
        await create_stripe_checkout_session(db, org)


@pytest.mark.asyncio
async def test_checkout_incomplete_subscription_permitted():
    """Verify an organization with an incomplete subscription is permitted to create a checkout session."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_test_ok"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_ok"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    assert result.stripe_session_id == "cs_test_ok"
    assert result.status == CheckoutAttemptStatus.open


# =============================================================================
# 5. Reuse & Expiration of Open Attempts
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_reuse_unexpired_open_attempt():
    """Verify an unexpired open attempt returns the existing checkout URL immediately without calling Stripe."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    future_exp = datetime.now(timezone.utc) + timedelta(hours=2)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_test_existing",
        checkout_url="https://checkout.stripe.com/c/pay/cs_test_existing",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=future_exp,
    )
    # Provide a pre-existing subscription with stripe_customer_id so get_or_create_stripe_customer
    # returns immediately without calling Stripe (Correction 3 moves customer resolution first).
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    assert result.attempt_id == attempt.id
    assert result.stripe_session_id == "cs_test_existing"
    assert result.checkout_url == "https://checkout.stripe.com/c/pay/cs_test_existing"
    assert result.session_expires_at == future_exp
    # Assert ZERO external calls were made to Stripe
    assert sessions.create.call_count == 0


@pytest.mark.asyncio
async def test_checkout_sweeps_expired_open_attempt():
    """Verify an expired open attempt is marked 'expired' and a fresh attempt is initiated.

    With Correction 1, the service now verifies the actual Stripe session status before
    releasing the reservation. When Stripe confirms the session is expired, the local
    attempt is marked expired and a fresh attempt is created.
    """
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=5)
    expired_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_test_old",
        checkout_url="https://checkout.stripe.com/c/pay/cs_test_old",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[expired_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    # Stripe confirms the session is genuinely expired
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "expired"
    mock_retrieved.payment_status = None
    sessions.retrieve.return_value = mock_retrieved

    mock_session = mock.MagicMock()
    mock_session.id = "cs_test_fresh"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_fresh"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    assert expired_attempt.status == CheckoutAttemptStatus.expired
    assert result.stripe_session_id == "cs_test_fresh"
    assert result.attempt_id != expired_attempt.id
    assert sessions.create.call_count == 1
    # Verify retrieve was called with the old session ID
    sessions.retrieve.assert_called_once_with("cs_test_old")


# =============================================================================
# 6. Payment Pending & In-Flight Lease Protections
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_payment_pending_raises():
    """Verify an attempt in payment_pending rejects new session creation with CheckoutInProgressError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_test_pending",
        checkout_url="https://checkout.stripe.com/c/pay/cs_test_pending",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.payment_pending,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    # Pre-existing subscription provides stripe_customer_id so get_or_create_stripe_customer
    # returns immediately (Correction 3 moves customer resolution before the lock).
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    with pytest.raises(CheckoutInProgressError, match="payment is currently pending confirmation"):
        await create_stripe_checkout_session(db, org)


@pytest.mark.asyncio
async def test_checkout_creating_active_lease_raises():
    """Verify an attempt in 'creating' with a valid in-flight lease raises CheckoutInProgressError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    active_lease = datetime.now(timezone.utc) + timedelta(seconds=45)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.creating,
        attempt_lease_expires_at=active_lease,
        session_expires_at=None,
    )
    # Pre-existing subscription provides stripe_customer_id so get_or_create_stripe_customer
    # returns immediately (Correction 3 moves customer resolution before the lock).
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    with pytest.raises(CheckoutInProgressError, match="currently being created"):
        await create_stripe_checkout_session(db, org)


# =============================================================================
# 7. End-to-End Success & Payload Invariants
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_success_creates_attempt_and_open_session():
    """Verify successful checkout flow: customer creation, payload parameters, idempotency key, and open status."""
    org = Organization(id=uuid.uuid4(), name="Acme Corp")
    db = InMemoryCheckoutDbSession(organizations=[org])

    client, sessions, customers = create_mock_stripe_client()
    mock_cust = mock.MagicMock()
    mock_cust.id = "cus_acme_123"
    customers.create.return_value = mock_cust

    exp_unix = int(datetime.now(timezone.utc).timestamp()) + 86400
    mock_session = mock.MagicMock()
    mock_session.id = "cs_test_acme_checkout"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_acme_checkout"
    mock_session.expires_at = exp_unix
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org, user_email="owner@acme.com")

    # 1. Assert result fields
    assert result.organization_id == org.id
    assert result.stripe_session_id == "cs_test_acme_checkout"
    assert result.checkout_url == "https://checkout.stripe.com/c/pay/cs_test_acme_checkout"
    assert result.status == CheckoutAttemptStatus.open
    assert result.session_expires_at is not None

    # 2. Assert Stripe customer creation params
    customers.create.assert_called_once()
    cust_kwargs = customers.create.call_args.kwargs
    assert cust_kwargs["params"]["name"] == "Acme Corp"
    assert cust_kwargs["params"]["email"] == "owner@acme.com"
    assert cust_kwargs["params"]["metadata"]["organization_id"] == str(org.id)

    # 3. Assert Stripe checkout session params
    sessions.create.assert_called_once()
    call_kwargs = sessions.create.call_args.kwargs
    payload = call_kwargs["params"]
    options = call_kwargs["options"]

    assert payload["mode"] == "subscription"
    assert payload["customer"] == "cus_acme_123"
    assert payload["line_items"] == [{"price": "price_test_pro_monthly", "quantity": 1}]
    assert "session_id={CHECKOUT_SESSION_ID}" in payload["success_url"]
    assert "status=success" in payload["success_url"]
    assert "status=cancelled" in payload["cancel_url"]
    assert payload["metadata"]["organization_id"] == str(org.id)
    assert payload["metadata"]["attempt_id"] == str(result.attempt_id)
    assert payload["subscription_data"]["metadata"]["organization_id"] == str(org.id)
    assert options["idempotency_key"] == f"checkout-attempt-{result.attempt_id}"

    # 4. Assert durable attempt record in database
    created_attempt = next(a for a in db.attempts if a.id == result.attempt_id)
    assert created_attempt.status == CheckoutAttemptStatus.open
    assert created_attempt.stripe_session_id == "cs_test_acme_checkout"
    assert created_attempt.checkout_url == "https://checkout.stripe.com/c/pay/cs_test_acme_checkout"
    assert created_attempt.error_message is None


# =============================================================================
# 8. Error Handling & State Transitions
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_definitive_stripe_error_marks_failed():
    """Verify definitive Stripe failure (e.g. 400 InvalidRequestError) marks attempt failed and releases reservation."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    sessions.create.side_effect = stripe.InvalidRequestError(
        message="No such price: 'price_invalid'",
        param="line_items",
        http_status=400,
    )
    set_stripe_client_for_testing(client)

    with pytest.raises(StripeCheckoutError, match="No such price"):
        await create_stripe_checkout_session(db, org)

    # Verify attempt was recorded and marked failed
    assert len(db.attempts) == 1
    attempt = db.attempts[0]
    assert attempt.status == CheckoutAttemptStatus.failed
    assert "No such price" in str(attempt.error_message)


@pytest.mark.asyncio
async def test_checkout_ambiguous_stripe_error_marks_recovery_required():
    """Verify ambiguous failure (e.g. APIConnectionError) marks attempt recovery_required and retains reservation."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    sessions.create.side_effect = stripe.APIConnectionError("Network connection reset by peer")
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError, match="Ambiguous Stripe checkout outcome"):
        await create_stripe_checkout_session(db, org)

    # Verify attempt is marked recovery_required, retaining the tenant reservation
    assert len(db.attempts) == 1
    attempt = db.attempts[0]
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert "Network connection reset" in str(attempt.error_message)


# =============================================================================
# 9. Recovery Replay Tests
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_creating_expired_lease_triggers_recovery():
    """Verify an attempt in 'creating' with an expired lease transitions to recovery_required and triggers replay."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_lease = datetime.now(timezone.utc) - timedelta(seconds=10)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.creating,
        attempt_lease_expires_at=past_lease,
        session_expires_at=None,
    )
    # Set created_at to a recent time so the idempotency cutoff guard allows replay.
    attempt.created_at = datetime.now(timezone.utc)
    # Pre-existing subscription with stripe_customer_id so get_or_create_stripe_customer
    # returns immediately without calling Stripe (Correction 3).
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_recovered_123"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_recovered_123"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    assert result.attempt_id == attempt.id
    assert result.stripe_session_id == "cs_recovered_123"
    assert result.status == CheckoutAttemptStatus.open
    assert attempt.status == CheckoutAttemptStatus.open

    # Verify replay used stored payload and idempotency key
    call_kwargs = sessions.create.call_args.kwargs
    assert call_kwargs["params"] == attempt.request_payload
    assert call_kwargs["options"]["idempotency_key"] == f"checkout-attempt-{attempt.id}"


@pytest.mark.asyncio
async def test_recover_checkout_attempt_success():
    """Verify explicit recover_checkout_attempt call recovers an ambiguous attempt to 'open'."""
    org_id = uuid.uuid4()
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
        error_message="Prior network failure",
    )
    # Set created_at to a recent time so the idempotency cutoff guard allows replay.
    attempt.created_at = datetime.now(timezone.utc)
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_recovered_explicit"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_recovered_explicit"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await recover_checkout_attempt(db, attempt)

    assert result.stripe_session_id == "cs_recovered_explicit"
    assert result.status == CheckoutAttemptStatus.open
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.error_message is None


@pytest.mark.asyncio
async def test_recover_checkout_attempt_definitive_failure():
    """Verify recovery replay encountering a definitive 400 error marks attempt 'failed'."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    # Set created_at to a recent time so the idempotency cutoff guard allows replay.
    attempt.created_at = datetime.now(timezone.utc)
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    sessions.create.side_effect = stripe.InvalidRequestError("Customer deleted", "customer", http_status=400)
    set_stripe_client_for_testing(client)

    with pytest.raises(StripeCheckoutError, match="Customer deleted"):
        await recover_checkout_attempt(db, attempt)

    assert attempt.status == CheckoutAttemptStatus.failed
    assert "Customer deleted" in str(attempt.error_message)


@pytest.mark.asyncio
async def test_recover_checkout_attempt_ambiguous_failure():
    """Verify recovery replay encountering an ambiguous timeout error KEEPS 'recovery_required'."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    # Set created_at to a recent time so the idempotency cutoff guard allows replay.
    attempt.created_at = datetime.now(timezone.utc)
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    sessions.create.side_effect = stripe.APIConnectionError("Timeout connecting to Stripe")
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError, match="Ambiguous Stripe checkout recovery outcome"):
        await recover_checkout_attempt(db, attempt)

    # Invariant: Must remain recovery_required
    assert attempt.status == CheckoutAttemptStatus.recovery_required


# =============================================================================
# 10. Concurrency Conflict & Security Invariants
# =============================================================================


@pytest.mark.asyncio
async def test_checkout_concurrency_race_conflict_recovery():
    """Verify database unique constraint conflict (IntegrityError) raises CheckoutInProgressError."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        fail_commit_with_integrity_error=True,
    )

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="already in progress"):
        await create_stripe_checkout_session(db, org)

    # Ensure Stripe API was NEVER invoked
    assert sessions.create.call_count == 0

@pytest.mark.asyncio
async def test_checkout_invalid_returned_url_rejected():
    """Verify checkout fails and marks attempt 'failed' if Stripe returns an invalid URL format."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_test_bad_url"
    mock_session.url = "javascript:alert(1)"  # Malicious/invalid scheme
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    with pytest.raises(StripeCheckoutError, match="invalid scheme"):
        await create_stripe_checkout_session(db, org)

    attempt = db.attempts[0]
    assert attempt.status == CheckoutAttemptStatus.failed


@pytest.mark.asyncio
async def test_checkout_does_not_modify_subscription_entitlements():
    """Verify checkout session creation never grants Pro entitlements or touches local subscription tier."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_test_entitlement_check"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_entitlement_check"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    await create_stripe_checkout_session(db, org)

    # Crucial invariant: Subscription must remain incomplete and free!
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free"
    assert sub.stripe_subscription_id is None


# =============================================================================
# 11. Step 2.2 Safety Corrections — New Tests
# =============================================================================


# ---------------------------------------------------------------------------
# 11.1 Stripe Verification for Locally-Expired Open Sessions (Correction 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_open_session_stripe_complete_paid_raises_inprogress():
    """Verify that when Stripe reports complete+paid for a locally-expired open attempt,
    the attempt transitions to 'complete' and CheckoutInProgressError is raised."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=10)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_old_paid",
        checkout_url="https://checkout.stripe.com/c/pay/cs_old_paid",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "complete"
    mock_retrieved.payment_status = "paid"
    sessions.retrieve.return_value = mock_retrieved
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="checkout session was recently completed"):
        await create_stripe_checkout_session(db, org)

    # Attempt must be transitioned to 'complete'
    assert attempt.status == CheckoutAttemptStatus.complete
    # No new session should have been created
    sessions.create.assert_not_called()


@pytest.mark.asyncio
async def test_expired_open_session_stripe_complete_unpaid_raises_inprogress():
    """Verify that when Stripe reports complete with non-paid payment_status for a locally-expired
    open attempt, the attempt transitions to 'payment_pending' and CheckoutInProgressError is raised."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=10)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_old_unpaid",
        checkout_url="https://checkout.stripe.com/c/pay/cs_old_unpaid",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "complete"
    mock_retrieved.payment_status = "unpaid"
    sessions.retrieve.return_value = mock_retrieved
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="checkout session was recently completed"):
        await create_stripe_checkout_session(db, org)

    # Attempt must be transitioned to 'payment_pending' (not 'complete')
    assert attempt.status == CheckoutAttemptStatus.payment_pending
    sessions.create.assert_not_called()


@pytest.mark.asyncio
async def test_expired_open_session_stripe_still_open_reuses_session():
    """Verify that when Stripe reports the session is still open for a locally-expired attempt,
    the existing session URL is returned and no new Stripe session is created."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_local_exp = datetime.now(timezone.utc) - timedelta(minutes=5)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_still_open",
        checkout_url="https://checkout.stripe.com/c/pay/cs_still_open",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_local_exp,
        session_expires_at=past_local_exp,  # Local timestamp expired, but Stripe says open
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "open"
    mock_retrieved.payment_status = "unpaid"
    sessions.retrieve.return_value = mock_retrieved
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    # Existing session must be reused — no new Stripe session created
    assert result.stripe_session_id == "cs_still_open"
    assert result.checkout_url == "https://checkout.stripe.com/c/pay/cs_still_open"
    assert result.attempt_id == attempt.id
    sessions.create.assert_not_called()
    sessions.retrieve.assert_called_once_with("cs_still_open")


@pytest.mark.asyncio
async def test_expired_open_session_stripe_confirmed_expired_creates_fresh():
    """Verify that when Stripe confirms a locally-expired open attempt is truly expired,
    the attempt is marked 'expired' and a fresh Stripe session is created."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(hours=2)
    old_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_truly_expired",
        checkout_url="https://checkout.stripe.com/c/pay/cs_truly_expired",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        attempts=[old_attempt],
        subscriptions=[sub],
    )

    client, sessions, _ = create_mock_stripe_client()
    # Stripe confirms the session is expired
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "expired"
    mock_retrieved.payment_status = None
    sessions.retrieve.return_value = mock_retrieved

    # New fresh session
    mock_new_session = mock.MagicMock()
    mock_new_session.id = "cs_fresh_after_expired"
    mock_new_session.url = "https://checkout.stripe.com/c/pay/cs_fresh_after_expired"
    mock_new_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_new_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    # Old attempt must be expired
    assert old_attempt.status == CheckoutAttemptStatus.expired
    # A fresh session must be created
    assert result.stripe_session_id == "cs_fresh_after_expired"
    assert result.attempt_id != old_attempt.id
    sessions.create.assert_called_once()
    sessions.retrieve.assert_called_once_with("cs_truly_expired")


@pytest.mark.asyncio
async def test_expired_open_session_stripe_retrieval_fails_marks_recovery_required():
    """Verify that when Stripe session retrieval fails with an ambiguous error for a locally-expired
    open attempt, the attempt transitions to 'recovery_required' and CheckoutRecoveryRequiredError
    is raised (the reservation is retained)."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=5)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_network_fail",
        checkout_url="https://checkout.stripe.com/c/pay/cs_network_fail",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    sessions.retrieve.side_effect = stripe.APIConnectionError("Network failure during retrieval")
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError, match="Could not verify session status"):
        await create_stripe_checkout_session(db, org)

    # Attempt must remain reserved as recovery_required (not expired/failed)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    sessions.create.assert_not_called()


# ---------------------------------------------------------------------------
# 11.2 Idempotency Retention Window Guard (Correction 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_older_than_22h_blocks_replay():
    """Verify that a recovery_required attempt older than 22 hours blocks Stripe replay
    and raises CheckoutRecoveryRequiredError without calling sessions.create."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    # Attempt is 23 hours old — beyond the 22-hour safe cutoff.
    attempt.created_at = datetime.now(timezone.utc) - timedelta(hours=23)
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError, match="safe idempotency replay window"):
        await recover_checkout_attempt(db, attempt)

    # Stripe must NOT have been called
    sessions.create.assert_not_called()
    # Attempt must remain recovery_required (reservation retained)
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_recovery_unknown_creation_time_blocks_replay():
    """Verify that a recovery_required attempt with None created_at blocks replay
    and raises CheckoutRecoveryRequiredError without calling sessions.create."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    # Explicitly set created_at to None (server_default doesn't fire in unit tests).
    attempt.created_at = None
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError, match="creation time is unknown"):
        await recover_checkout_attempt(db, attempt)

    sessions.create.assert_not_called()
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_idempotency_error_retains_reservation():
    """Verify that a stripe.IdempotencyError from sessions.create is classified as
    'recovery_required' (not 'failed') and the attempt reservation is retained."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id=None,
        checkout_url=None,
        request_payload={"mode": "subscription", "customer": "cus_test_1"},
        status=CheckoutAttemptStatus.recovery_required,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    attempt.created_at = datetime.now(timezone.utc)
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    client, sessions, _ = create_mock_stripe_client()
    # stripe.IdempotencyError is a subclass of stripe.InvalidRequestError.
    # It must be classified as 'recovery_required', NOT 'failed'.
    sessions.create.side_effect = stripe.IdempotencyError(
        "Keys for idempotent requests can only be used for the same endpoint",
        http_status=400,
    )
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutRecoveryRequiredError):
        await recover_checkout_attempt(db, attempt)

    # Reservation must be retained as recovery_required (NOT failed)
    assert attempt.status == CheckoutAttemptStatus.recovery_required


def test_classify_stripe_idempotency_error_is_recovery_required():
    """Verify classify_stripe_error treats stripe.IdempotencyError as 'recovery_required',
    not 'failed', even though IdempotencyError is a subclass of InvalidRequestError."""
    idempotency_err = stripe.IdempotencyError(
        "Idempotency key already used",
        http_status=400,
    )
    # Crucial: must NOT classify as 'failed' just because it's an InvalidRequestError subclass
    assert classify_stripe_error(idempotency_err) == "recovery_required"


# ---------------------------------------------------------------------------
# 11.3 State Transition Guard in _update_attempt_status (Correction 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_attempt_status_does_not_overwrite_complete():
    """Verify that _update_attempt_status refuses to regress a 'complete' attempt to 'open'.
    The 'complete' state is terminal and must never be overwritten."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_done",
        checkout_url="https://checkout.stripe.com/c/pay/cs_done",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    # Attempt to regress from complete → open (illegal transition)
    result = await _update_attempt_status(
        db,
        attempt_id=attempt.id,
        status=CheckoutAttemptStatus.open,
    )

    # Must be blocked — attempt stays 'complete'
    assert result is not None
    assert result.status == CheckoutAttemptStatus.complete
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_update_attempt_status_does_not_overwrite_payment_pending_with_open():
    """Verify that _update_attempt_status refuses to regress a 'payment_pending' attempt to 'open'.
    Payment-pending is webhook-advanced and must not be overwritten by stale checkout-service writes."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_pending",
        checkout_url="https://checkout.stripe.com/c/pay/cs_pending",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.payment_pending,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    # Attempt to regress from payment_pending → open (illegal transition)
    result = await _update_attempt_status(
        db,
        attempt_id=attempt.id,
        status=CheckoutAttemptStatus.open,
    )

    # Must be blocked — attempt stays 'payment_pending'
    assert result is not None
    assert result.status == CheckoutAttemptStatus.payment_pending
    assert attempt.status == CheckoutAttemptStatus.payment_pending


@pytest.mark.asyncio
async def test_payment_pending_to_complete_transition_allowed():
    """Verify that _update_attempt_status allows the legitimate payment_pending → complete transition.
    This is the forward transition applied when async payment confirmation arrives."""
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_pending_ok",
        checkout_url="https://checkout.stripe.com/c/pay/cs_pending_ok",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.payment_pending,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    db = InMemoryCheckoutDbSession(attempts=[attempt])

    result = await _update_attempt_status(
        db,
        attempt_id=attempt.id,
        status=CheckoutAttemptStatus.complete,
    )

    # Legitimate forward transition must succeed
    assert result is not None
    assert result.status == CheckoutAttemptStatus.complete
    assert attempt.status == CheckoutAttemptStatus.complete


# ---------------------------------------------------------------------------
# 11.4 Customer Resolution Ordering (Correction 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_resolution_occurs_before_org_lock():
    """Verify get_or_create_stripe_customer is called before the org row lock is acquired.

    Regression guard for Correction 3: customer resolution (which may commit its own
    transaction) must complete before we acquire the org FOR UPDATE lock, so that
    customer commits cannot release the lock prematurely.
    """
    org = Organization(id=uuid.uuid4(), name="Test Org")
    db = InMemoryCheckoutDbSession(organizations=[org])

    client, sessions, customers = create_mock_stripe_client()
    mock_cust = mock.MagicMock()
    mock_cust.id = "cus_before_lock"
    customers.create.return_value = mock_cust

    call_order: list[str] = []

    original_execute = db.execute

    async def tracking_execute(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "organizations" in stmt_str and "organizations.id" in stmt_str:
            call_order.append("org_lock")
        return await original_execute(stmt)

    db.execute = tracking_execute  # type: ignore[method-assign]

    from app.services.billing.stripe_customer import get_or_create_stripe_customer as real_goc
    from app.services.billing import stripe_customer as sc_module

    original_goc = sc_module.get_or_create_stripe_customer

    async def tracking_goc(db_: Any, org_: Any, email_: Any = None) -> str:
        call_order.append("customer_resolution")
        return await original_goc(db_, org_, email_)

    import app.services.billing.stripe_checkout as checkout_module
    original_module_goc = checkout_module.get_or_create_stripe_customer

    exp_unix = int(datetime.now(timezone.utc).timestamp()) + 86400
    mock_session = mock.MagicMock()
    mock_session.id = "cs_order_test"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_order_test"
    mock_session.expires_at = exp_unix
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    try:
        checkout_module.get_or_create_stripe_customer = tracking_goc  # type: ignore[attr-defined]
        result = await create_stripe_checkout_session(db, org, user_email="user@test.com")
    finally:
        checkout_module.get_or_create_stripe_customer = original_module_goc  # type: ignore[attr-defined]

    # customer_resolution must appear before org_lock in the call sequence
    assert "customer_resolution" in call_order, "get_or_create_stripe_customer was not tracked"
    assert "org_lock" in call_order, "org row lock was not tracked"
    customer_idx = call_order.index("customer_resolution")
    first_lock_idx = call_order.index("org_lock")
    assert customer_idx < first_lock_idx, (
        f"Customer resolution (index {customer_idx}) must occur before org lock "
        f"(index {first_lock_idx}). Call order: {call_order}"
    )
    assert result.stripe_session_id == "cs_order_test"


# =============================================================================
# 12. Step 2.4 Completed-Unreconciled Guard & Lock Order Tests
# =============================================================================


@pytest.mark.asyncio
async def test_recent_completed_unreconciled_attempt_blocks_second_checkout():
    """Verify that a recent completed attempt whose subscription has not yet been reconciled
    into the local database blocks creation of a second checkout session."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    completed_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_completed_recent",
        stripe_subscription_id="sub_recent_123",
        checkout_url="https://checkout.stripe.com/c/pay/cs_completed_recent",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    completed_attempt.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    # Local subscription has NOT been reconciled yet (no matching stripe_subscription_id)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[completed_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="Subscription activation is pending confirmation"):
        await create_stripe_checkout_session(db, org)

    # Ensure NO second checkout session was created in Stripe
    sessions.create.assert_not_called()


@pytest.mark.asyncio
async def test_completed_unreconciled_attempt_older_than_24h_still_blocks():
    """Verify that a completed but unreconciled attempt older than 24 hours continues
    to block new checkouts. Age alone must NOT release the reservation."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    old_completed_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_completed_old",
        stripe_subscription_id="sub_stale_456",
        checkout_url="https://checkout.stripe.com/c/pay/cs_completed_old",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=36),
        session_expires_at=None,
    )
    # Attempt was created 36 hours ago — beyond the 24-hour checkout session window
    old_completed_attempt.created_at = datetime.now(timezone.utc) - timedelta(hours=36)

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[old_completed_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="Subscription activation is pending confirmation"):
        await create_stripe_checkout_session(db, org)

    # Invariant: 36h age does NOT permit a second checkout session
    sessions.create.assert_not_called()


@pytest.mark.asyncio
async def test_completed_attempt_with_matching_subscription_is_reconciled():
    """Verify that a completed attempt whose stripe_subscription_id reliably matches
    a local Subscription record is recognized as reconciled, allowing checkout to proceed
    if the subscription is canceled."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    reconciled_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_reconciled_ok",
        stripe_subscription_id="sub_matching_789",
        checkout_url="https://checkout.stripe.com/c/pay/cs_reconciled_ok",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    reconciled_attempt.created_at = datetime.now(timezone.utc) - timedelta(days=30)

    # Local Subscription matches the attempt's stripe_subscription_id and is canceled
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_matching_789",  # Exact match proves reconciliation!
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[reconciled_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_fresh_resub"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_fresh_resub"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    # Checkout session creation succeeds for resubscription
    assert result.stripe_session_id == "cs_fresh_resub"
    assert result.status == CheckoutAttemptStatus.open
    assert sessions.create.call_count == 1


@pytest.mark.asyncio
async def test_previously_reconciled_subscription_canceled_permits_resubscription():
    """Verify that an organization whose previously reconciled subscription was later canceled
    is not permanently blocked and can legitimately resubscribe."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    prior_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_prior_sub",
        stripe_subscription_id="sub_prior_reconciled",
        checkout_url="https://checkout.stripe.com/c/pay/cs_prior_sub",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    prior_attempt.created_at = datetime.now(timezone.utc) - timedelta(days=60)

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_prior_reconciled",
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[prior_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    mock_session = mock.MagicMock()
    mock_session.id = "cs_resub_2"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_resub_2"
    mock_session.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_session
    set_stripe_client_for_testing(client)

    result = await create_stripe_checkout_session(db, org)

    assert result.stripe_session_id == "cs_resub_2"
    assert sessions.create.call_count == 1


@pytest.mark.asyncio
async def test_unrelated_local_subscription_does_not_count_as_reconciliation():
    """Verify that an unrelated local subscription (mismatched stripe_subscription_id)
    does not satisfy reconciliation for a completed attempt."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    completed_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_target",
        stripe_subscription_id="sub_target_999",
        checkout_url="https://checkout.stripe.com/c/pay/cs_target",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )

    # Local Subscription has a DIFFERENT stripe_subscription_id
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_other_unrelated_000",
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[completed_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    with pytest.raises(CheckoutInProgressError, match="Subscription activation is pending confirmation"):
        await create_stripe_checkout_session(db, org)

    sessions.create.assert_not_called()


@pytest.mark.asyncio
async def test_stripe_retrieval_is_outside_db_transaction():
    """Verify that Stripe session retrieval during _handle_locally_expired_open_attempt
    occurs when db.in_transaction() is strictly False."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=10)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_tx_check",
        checkout_url="https://checkout.stripe.com/c/pay/cs_tx_check",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()

    tx_state_during_retrieve = None

    def retrieve_with_tx_check(session_id: str) -> mock.MagicMock:
        nonlocal tx_state_during_retrieve
        tx_state_during_retrieve = db.in_transaction()
        mock_res = mock.MagicMock()
        mock_res.status = "expired"
        mock_res.payment_status = None
        mock_res.subscription = None
        return mock_res

    sessions.retrieve.side_effect = retrieve_with_tx_check

    mock_fresh = mock.MagicMock()
    mock_fresh.id = "cs_fresh_tx"
    mock_fresh.url = "https://checkout.stripe.com/c/pay/cs_fresh_tx"
    mock_fresh.expires_at = int(datetime.now(timezone.utc).timestamp()) + 86400
    sessions.create.return_value = mock_fresh
    set_stripe_client_for_testing(client)

    await create_stripe_checkout_session(db, org)

    # Invariant: Database transaction must NOT be held during the external network call
    assert tx_state_during_retrieve is False, "Stripe retrieval must occur outside DB transaction"


@pytest.mark.asyncio
async def test_org_lock_reacquired_before_attempt_lock_after_retrieval():
    """Verify that in _handle_locally_expired_open_attempt, the parent Organization row lock
    is strictly reacquired BEFORE the checkout attempt lock."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    past_exp = datetime.now(timezone.utc) - timedelta(minutes=10)
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_lock_order",
        checkout_url="https://checkout.stripe.com/c/pay/cs_lock_order",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=past_exp,
        session_expires_at=past_exp,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(organizations=[org], attempts=[attempt], subscriptions=[sub])

    client, sessions, _ = create_mock_stripe_client()
    mock_retrieved = mock.MagicMock()
    mock_retrieved.status = "complete"
    mock_retrieved.payment_status = "paid"
    mock_retrieved.subscription = "sub_lock_test"
    sessions.retrieve.return_value = mock_retrieved
    set_stripe_client_for_testing(client)

    execution_log: list[str] = []
    original_execute = db.execute

    async def logging_execute(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "organizations" in stmt_str and "organizations.id" in stmt_str and "FOR UPDATE" in stmt_str:
            execution_log.append("org_lock")
        elif "stripe_checkout_attempts" in stmt_str and "stripe_checkout_attempts.id =" in stmt_str and "FOR UPDATE" in stmt_str:
            execution_log.append("attempt_lock")
        return await original_execute(stmt)

    db.execute = logging_execute  # type: ignore[method-assign]

    with pytest.raises(CheckoutInProgressError):
        await create_stripe_checkout_session(db, org)

    # In _handle_locally_expired_open_attempt:
    # After Stripe retrieve returns, org_lock must be reacquired before attempt_lock.
    # The first org_lock is from the entry into create_stripe_checkout_session.
    # The second org_lock is the reacquisition after Stripe retrieve!
    assert execution_log.count("org_lock") >= 2, f"Org lock must be reacquired after retrieve. Log: {execution_log}"
    assert "attempt_lock" in execution_log, f"Attempt lock must be acquired. Log: {execution_log}"
    second_org_lock_idx = [i for i, x in enumerate(execution_log) if x == "org_lock"][1]
    attempt_lock_idx = execution_log.index("attempt_lock")
    assert second_org_lock_idx < attempt_lock_idx, (
        f"Parent org lock (idx {second_org_lock_idx}) must precede attempt lock (idx {attempt_lock_idx}). "
        f"Execution log: {execution_log}"
    )


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_bypass_completed_unreconciled_guard():
    """Verify that multiple concurrent or sequential checkout requests for the same organization
    cannot bypass the completed-unreconciled guard to create duplicate sessions."""
    org = Organization(id=uuid.uuid4(), name="Test Org")
    completed_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_session_id="cs_unreconciled_concurrent",
        stripe_subscription_id="sub_pending_concurrent",
        checkout_url="https://checkout.stripe.com/c/pay/cs_unreconciled_concurrent",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.complete,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        session_expires_at=None,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_test_1",
        stripe_subscription_id=None,  # Not reconciled
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = InMemoryCheckoutDbSession(
        organizations=[org],
        subscriptions=[sub],
        attempts=[completed_attempt],
    )

    client, sessions, _ = create_mock_stripe_client()
    set_stripe_client_for_testing(client)

    # Worker 1 attempts checkout
    with pytest.raises(CheckoutInProgressError, match="Subscription activation is pending confirmation"):
        await create_stripe_checkout_session(db, org)

    # Worker 2 attempts checkout immediately after
    with pytest.raises(CheckoutInProgressError, match="Subscription activation is pending confirmation"):
        await create_stripe_checkout_session(db, org)

    # Both workers were blocked; Stripe session creation was NEVER invoked
    assert sessions.create.call_count == 0
    # No new attempts were added
    assert len(db.attempts) == 1
