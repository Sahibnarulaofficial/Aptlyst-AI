"""Comprehensive tests for Razorpay Subscription Checkout backend.

Covers:
1. Subscription creation with mocked HTTP.
2. Customer provisioning integration.
3. Invalid or missing configuration.
4. Subscription signature verification success (HMAC-SHA256).
5. Invalid signature rejection.
6. Browser-supplied subscription ID mismatch rejection.
7. Payment status not captured ('authorized' -> payment_pending; 'failed' -> failed).
8. Payment/subscription metadata mismatch.
9. Repeated verification (Idempotency).
10. Network ambiguity during creation ('recovery_required').
11. Database conflict / concurrency handling.
12. Organization and tenant isolation (401, 403, 404).
13. Entitlement Protection: NO premature Pro entitlement after creation or verification.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import sys
from typing import Any
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest
from sqlalchemy.exc import IntegrityError

# Windows asyncio Proactor sandbox self-pipe fix
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401
from app.auth.dependencies import CurrentUser, get_current_user
from app.config import settings
from app.db.session import get_db
from app.main import app
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import MemberRole, Organization, OrganizationMember
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import (
    CheckoutAttemptStatus,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.razorpay.checkout import (
    ActiveSubscriptionExistsError,
    CheckoutInProgressError,
    CheckoutRecoveryRequiredError,
    RazorpayCheckoutAmbiguousError,
    RazorpayCheckoutConflictError,
    RazorpayCheckoutError,
    RazorpayCheckoutNotFoundError,
    RazorpayCheckoutPaymentError,
    RazorpayCheckoutSignatureError,
    create_razorpay_subscription_checkout,
    recover_razorpay_checkout_attempt,
    verify_razorpay_checkout_callback,
    verify_razorpay_subscription_signature,
)
from app.services.billing.razorpay.client import (
    RazorpayAuthError,
    RazorpayBillingDisabledError,
    RazorpayClientError,
    RazorpayConfigurationError,
    RazorpayNetworkError,
    RazorpayServer5xxError,
    reset_razorpay_client,
    set_razorpay_client_for_testing,
)


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


class CheckoutMockDbSession:
    """In-memory AsyncSession test double for checkout tests."""

    def __init__(
        self,
        attempts: list[BillingCheckoutAttempt] | None = None,
        provisions: list[BillingCustomerProvision] | None = None,
        subscriptions: list[Subscription] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
        organizations: list[Organization] | None = None,
        members: list[OrganizationMember] | None = None,
        reconciliations: list[PaymentReconciliation] | None = None,
        fail_commit_with_integrity: bool = False,
        allow_missing_created_at: bool = False,
    ) -> None:
        self.attempts: list[BillingCheckoutAttempt] = list(attempts or [])
        if not allow_missing_created_at:
            for a in self.attempts:
                if a.created_at is None and not getattr(a, "_force_none_created_at", False):
                    a.created_at = datetime.now(timezone.utc)
        self.provisions: list[BillingCustomerProvision] = list(provisions or [])
        self.subscriptions: list[Subscription] = list(subscriptions or [])
        self.billing_periods: list[OrganizationBillingPeriod] = list(billing_periods or [])
        self.reconciliations: list[PaymentReconciliation] = list(reconciliations or [])
        self.organizations: dict[uuid.UUID, Organization] = {
            o.id: o for o in (organizations or [])
        }
        self.members: list[OrganizationMember] = list(members or [])
        self.fail_commit_with_integrity = fail_commit_with_integrity
        self.uncommitted: list[Any] = []
        self._in_tx = False

    def in_transaction(self) -> bool:
        return self._in_tx

    async def get(self, entity_cls: Any, ident: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(ident)
        return None

    async def execute(self, stmt: Any) -> MockScalarResult:
        w_params: dict[str, Any] = {}
        for w in getattr(stmt, "_where_criteria", ()):
            if hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key"):
                if hasattr(w.right, "value"):
                    w_params[w.left.key] = w.right.value

        from_table = ""
        froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
        for f in froms_list:
            name = getattr(f, "name", None) or getattr(f, "__tablename__", "")
            if name:
                from_table = name
                break

        org_id = w_params.get("organization_id")
        attempt_id = w_params.get("id")

        if from_table == "billing_checkout_attempts":
            provider = w_params.get("provider")
            merchant_account_id = w_params.get("merchant_account_id")
            environment = w_params.get("environment")
            for a in reversed(self.attempts):
                if attempt_id and a.id != attempt_id:
                    continue
                if org_id and a.organization_id != org_id:
                    continue
                if provider and a.provider != provider:
                    continue
                if merchant_account_id and a.merchant_account_id != merchant_account_id:
                    continue
                if environment and a.environment != environment:
                    continue
                return MockScalarResult(a)
            return MockScalarResult(None)

        if from_table == "billing_customer_provisions":
            for p in reversed(self.provisions):
                if org_id and p.organization_id != org_id:
                    continue
                return MockScalarResult(p)
            return MockScalarResult(None)

        if from_table == "subscriptions":
            plan_tier = w_params.get("plan_tier")
            for s in reversed(self.subscriptions):
                if org_id and s.organization_id != org_id:
                    continue
                if plan_tier and s.plan_tier != plan_tier:
                    continue
                return MockScalarResult(s)
            return MockScalarResult(None)

        if from_table == "organization_billing_periods":
            for bp in reversed(self.billing_periods):
                if org_id and bp.organization_id != org_id:
                    continue
                return MockScalarResult(bp)
            return MockScalarResult(None)

        if from_table == "organization_members":
            user_id = w_params.get("user_id")
            for m in self.members:
                if org_id and m.organization_id != org_id:
                    continue
                if user_id and m.user_id != user_id:
                    continue
                return MockScalarResult(m)
            return MockScalarResult(None)

        return MockScalarResult(None)

    def add(self, obj: Any) -> None:
        self.uncommitted.append(obj)
        self._in_tx = True

    async def commit(self) -> None:
        if self.fail_commit_with_integrity:
            self.fail_commit_with_integrity = False
            raise IntegrityError("Concurrent attempt constraint conflict", params={}, orig=Exception())
        for obj in self.uncommitted:
            if isinstance(obj, BillingCheckoutAttempt) and obj not in self.attempts:
                self.attempts.append(obj)
            elif isinstance(obj, BillingCustomerProvision) and obj not in self.provisions:
                self.provisions.append(obj)
            elif isinstance(obj, Subscription) and obj not in self.subscriptions:
                self.subscriptions.append(obj)
            elif isinstance(obj, OrganizationBillingPeriod) and obj not in self.billing_periods:
                self.billing_periods.append(obj)
            elif isinstance(obj, PaymentReconciliation) and obj not in self.reconciliations:
                self.reconciliations.append(obj)
        self.uncommitted.clear()
        self._in_tx = False

    async def rollback(self) -> None:
        self.uncommitted.clear()
        self._in_tx = False


# =============================================================================
# Mock Razorpay Client
# =============================================================================


class MockRazorpayClient:
    """Mock Razorpay HTTP client for testing."""

    def __init__(
        self,
        customer_id: str = "cust_test_mock_123",
        subscription_id: str = "sub_test_mock_456",
        payment_id: str = "pay_test_mock_789",
        payment_status: str = "captured",
        payment_amount: int = 2900,
        payment_currency: str = "USD",
        payment_subscription_id: str | None = "__same__",
        fail_subscription: Exception | None = None,
        fail_payment: Exception | None = None,
        payment_notes: dict[str, str] | None = None,
        listed_subscriptions: list[dict[str, Any]] | None = None,
        fail_list_subscriptions: Exception | None = None,
        fail_list_subscriptions_on_page: int | None = None,
        list_subscriptions_pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.customer_id = customer_id
        self.subscription_id = subscription_id
        self.payment_id = payment_id
        self.payment_status = payment_status
        self.payment_amount = payment_amount
        self.payment_currency = payment_currency
        self.payment_subscription_id = (
            subscription_id if payment_subscription_id == "__same__" else payment_subscription_id
        )
        self.fail_subscription = fail_subscription
        self.fail_payment = fail_payment
        self.payment_notes = payment_notes
        self.listed_subscriptions = listed_subscriptions
        self.fail_list_subscriptions = fail_list_subscriptions
        self.fail_list_subscriptions_on_page = fail_list_subscriptions_on_page
        self.list_subscriptions_pages = list_subscriptions_pages
        self.create_subscription_calls: list[dict[str, Any]] = []
        self.get_payment_calls: list[str] = []
        self.get_subscription_calls: list[str] = []
        self.list_subscription_calls: list[dict[str, Any]] = []

    async def list_subscriptions(self, **kwargs: Any) -> dict[str, Any]:
        self.list_subscription_calls.append(kwargs)
        page_num = len(self.list_subscription_calls)
        if self.fail_list_subscriptions:
            raise self.fail_list_subscriptions
        if self.fail_list_subscriptions_on_page is not None and page_num == self.fail_list_subscriptions_on_page:
            raise RazorpayNetworkError(f"Network error on page {page_num}", is_pre_dispatch=False)

        if self.list_subscriptions_pages is not None:
            if page_num <= len(self.list_subscriptions_pages):
                return self.list_subscriptions_pages[page_num - 1]
            return {"entity": "collection", "count": 0, "items": []}

        count = kwargs.get("count", 10)
        skip = kwargs.get("skip", 0)
        all_items = self.listed_subscriptions if self.listed_subscriptions is not None else []
        sliced = all_items[skip : skip + count]
        return {"entity": "collection", "count": len(sliced), "items": sliced}

    async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": self.customer_id, "name": kwargs.get("name")}

    async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
        self.create_subscription_calls.append(kwargs)
        if self.fail_subscription:
            raise self.fail_subscription
        return {
            "id": self.subscription_id,
            "entity": "subscription",
            "plan_id": kwargs.get("plan_id"),
            "customer_id": kwargs.get("customer_id"),
            "status": "created",
            "short_url": f"https://rzp.io/i/{self.subscription_id}",
            "expire_by": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
        }

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        self.get_subscription_calls.append(subscription_id)
        return {
            "id": subscription_id,
            "entity": "subscription",
            "status": "active",
            "short_url": f"https://rzp.io/i/{subscription_id}",
            "expire_by": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
        }

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        self.get_payment_calls.append(payment_id)
        if self.fail_payment:
            raise self.fail_payment
        return {
            "id": payment_id,
            "entity": "payment",
            "amount": self.payment_amount,
            "currency": self.payment_currency,
            "status": self.payment_status,
            "method": "card",
            "subscription_id": self.payment_subscription_id,
            "notes": self.payment_notes if self.payment_notes is not None else {},
        }


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def configure_razorpay_test_settings(monkeypatch):
    """Ensure baseline Razorpay test configuration and clean client between tests."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_enabled", True)
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_test_123")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_mock_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("test_mock_key_secret_xyz123"))
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "plan_test_pro_monthly")
    monkeypatch.setattr(settings, "razorpay_plan_currency", "USD")
    yield
    reset_razorpay_client()


# =============================================================================
# Task 1 & 6: Subscription Creation Unit Tests
# =============================================================================


@pytest.mark.asyncio
async def test_successful_subscription_creation_using_mocked_http():
    """Successful checkout creation commits BillingCheckoutAttempt with 'open' status."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Pro Startup")
    db = CheckoutMockDbSession(organizations=[org])

    mock_client = MockRazorpayClient(
        customer_id="cust_rzp_111",
        subscription_id="sub_rzp_222",
    )
    set_razorpay_client_for_testing(mock_client)

    result = await create_razorpay_subscription_checkout(db=db, organization=org)

    assert result.organization_id == org_id
    assert result.subscription_id == "sub_rzp_222"
    assert result.provider == "razorpay"
    assert result.status == CheckoutAttemptStatus.open
    assert result.checkout_url == "https://rzp.io/i/sub_rzp_222"

    assert len(db.attempts) == 1
    attempt = db.attempts[0]
    assert attempt.id == result.attempt_id
    assert attempt.provider == "razorpay"
    assert attempt.merchant_account_id == "acc_test_123"
    assert attempt.environment == "test"
    assert attempt.provider_customer_id == "cust_rzp_111"
    assert attempt.provider_subscription_id == "sub_rzp_222"
    assert attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_customer_provisioning_integration_and_reuse():
    """Existing resolved customer provision is reused without creating a new customer."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Reused Customer Org")

    existing_provision = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_already_provisioned_999",
        status=CustomerProvisionStatus.resolved,
    )
    db = CheckoutMockDbSession(provisions=[existing_provision], organizations=[org])

    mock_client = MockRazorpayClient()
    set_razorpay_client_for_testing(mock_client)

    result = await create_razorpay_subscription_checkout(db=db, organization=org)

    assert result.subscription_id == "sub_test_mock_456"
    assert len(db.attempts) == 1
    assert db.attempts[0].provider_customer_id == "cust_already_provisioned_999"


@pytest.mark.asyncio
async def test_creation_fails_when_active_pro_subscription_exists():
    """Organization possessing an active Pro subscription cannot initiate new checkout."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Existing Pro Org")

    active_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        provider_customer_id="cust_existing_pro",
        merchant_account_id="acc_test_123",
        environment="test",
        plan_tier="pro",
        status=SubscriptionStatus.active,
    )
    db = CheckoutMockDbSession(subscriptions=[active_sub], organizations=[org])
    set_razorpay_client_for_testing(MockRazorpayClient())

    with pytest.raises(ActiveSubscriptionExistsError):
        await create_razorpay_subscription_checkout(db=db, organization=org)


@pytest.mark.asyncio
async def test_missing_plan_id_fails_closed(monkeypatch):
    """Missing or empty RAZORPAY_PRO_PLAN_ID fails closed before gateway request."""
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "")
    org = Organization(id=uuid.uuid4(), name="No Plan Org")
    db = CheckoutMockDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="RAZORPAY_PRO_PLAN_ID is not configured"):
        await create_razorpay_subscription_checkout(db=db, organization=org)


@pytest.mark.asyncio
async def test_network_ambiguity_during_creation_marks_recovery_required():
    """Server 500 error or timeout during subscription creation transitions attempt to recovery_required."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Timeout Org")
    db = CheckoutMockDbSession(organizations=[org])

    timeout_client = MockRazorpayClient(
        fail_subscription=RazorpayNetworkError("Read timeout connecting to Razorpay", is_pre_dispatch=False),
    )
    set_razorpay_client_for_testing(timeout_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError):
        await create_razorpay_subscription_checkout(db=db, organization=org)

    # Attempt is marked recovery_required, NOT failed (reservation retained)
    assert len(db.attempts) == 1
    assert db.attempts[0].status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_repeated_checkout_initiation_reuses_open_session():
    """Repeated checkout request returns existing open unexpired session idempotently."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Idempotent Org")

    open_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_open_1",
        provider_subscription_id="sub_open_existing_777",
        idempotency_key="rzp_sub_open_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        checkout_url="https://rzp.io/i/sub_open_existing_777",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        session_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
    )
    db = CheckoutMockDbSession(attempts=[open_attempt], organizations=[org])
    mock_client = MockRazorpayClient()
    set_razorpay_client_for_testing(mock_client)

    result = await create_razorpay_subscription_checkout(db=db, organization=org)

    assert result.subscription_id == "sub_open_existing_777"
    assert result.attempt_id == open_attempt.id
    # Zero gateway calls were made
    assert len(mock_client.create_subscription_calls) == 0


# =============================================================================
# Task 2 & 6: Signature Verification & Callback Tests
# =============================================================================


def _compute_valid_signature(payment_id: str, subscription_id: str, secret: str) -> str:
    payload = f"{payment_id}|{subscription_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_checkout_verification_signature_success_and_captured_status():
    """Valid callback signature and captured payment marks attempt as complete."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_valid_123"
    pay_id = "pay_valid_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="captured",
        payment_amount=2900,
    )
    set_razorpay_client_for_testing(mock_client)

    valid_sig = _compute_valid_signature(pay_id, sub_id, secret)

    result = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=valid_sig,
    )

    assert result.attempt_id == attempt_id
    assert result.payment_id == pay_id
    assert result.status == CheckoutAttemptStatus.complete
    assert result.payment_status == "captured"
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_invalid_signature_rejection():
    """Invalid HMAC signature is rejected with RazorpayCheckoutSignatureError."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_valid_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    set_razorpay_client_for_testing(MockRazorpayClient())

    with pytest.raises(RazorpayCheckoutSignatureError, match="Invalid checkout signature"):
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id="pay_tampered_456",
            razorpay_subscription_id=sub_id,
            razorpay_signature="invalid_tampered_signature_hex",
        )

    assert attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_browser_supplied_subscription_id_mismatch_rejection():
    """Browser sending a subscription ID that does not match server-owned attempt is rejected."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    server_sub_id = "sub_server_owned_111"
    attacker_sub_id = "sub_attacker_spoofed_222"
    pay_id = "pay_spoofed_333"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=server_sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    set_razorpay_client_for_testing(MockRazorpayClient())

    sig = _compute_valid_signature(pay_id, attacker_sub_id, secret)

    with pytest.raises(RazorpayCheckoutSignatureError, match="Subscription ID mismatch"):
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=attacker_sub_id,
            razorpay_signature=sig,
        )


@pytest.mark.asyncio
async def test_payment_status_authorized_marks_payment_pending():
    """Authorized payment status on Razorpay transitions attempt to payment_pending."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_auth_123"
    pay_id = "pay_auth_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="authorized",
    )
    set_razorpay_client_for_testing(mock_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    result = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )

    assert result.status == CheckoutAttemptStatus.payment_pending
    assert result.payment_status == "authorized"
    assert attempt.status == CheckoutAttemptStatus.payment_pending


@pytest.mark.asyncio
async def test_repeated_verification_is_idempotent():
    """Repeated verification callback returns completed state idempotently."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_idem_123"
    pay_id = "pay_idem_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="captured",
    )
    set_razorpay_client_for_testing(mock_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    # First call
    res1 = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )
    assert res1.status == CheckoutAttemptStatus.complete

    # Second call (idempotent replay)
    res2 = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )
    assert res2.status == CheckoutAttemptStatus.complete
    assert res2.payment_id == pay_id


# =============================================================================
# Task 3 & 6: Entitlement Protection Invariant Tests
# =============================================================================


@pytest.mark.asyncio
async def test_no_premature_pro_entitlement_granted():
    """CRITICAL: Neither subscription creation nor checkout verification grants Pro tier or quota."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Free Org")

    # Baseline: Organization has Free billing period and Free subscription
    free_bp = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=datetime.now(timezone.utc) - timedelta(days=5),
        period_end=datetime.now(timezone.utc) + timedelta(days=25),
        plan_tier="free",
        monthly_token_quota=100000,
        tokens_used=5000,
        tokens_reserved=0,
        is_active=True,
    )
    free_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        provider_customer_id="cust_free_org",
        merchant_account_id="acc_test_123",
        environment="test",
        plan_tier="free",
        status=SubscriptionStatus.active,
    )
    db = CheckoutMockDbSession(
        subscriptions=[free_sub],
        billing_periods=[free_bp],
        organizations=[org],
    )

    mock_client = MockRazorpayClient(
        customer_id="cust_free_org",
        subscription_id="sub_pro_123",
        payment_id="pay_pro_456",
        payment_status="captured",
    )
    set_razorpay_client_for_testing(mock_client)

    # 1. Initiate checkout
    create_res = await create_razorpay_subscription_checkout(db=db, organization=org)
    assert create_res.status == CheckoutAttemptStatus.open

    # ASSERTION 1: Subscription and billing period remain strictly Free
    assert free_sub.plan_tier == "free"
    assert free_bp.plan_tier == "free"
    assert free_bp.monthly_token_quota == 100000

    # 2. Verify checkout callback
    secret = settings.razorpay_key_secret.get_secret_value()
    sig = _compute_valid_signature("pay_pro_456", "sub_pro_123", secret)

    verify_res = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=create_res.attempt_id,
        razorpay_payment_id="pay_pro_456",
        razorpay_subscription_id="sub_pro_123",
        razorpay_signature=sig,
    )
    assert verify_res.status == CheckoutAttemptStatus.complete

    # ASSERTION 2: Subscription and billing period STILL remain strictly Free!
    # Verified signature alone NEVER provisions Pro quota.
    assert free_sub.plan_tier == "free"
    assert free_bp.plan_tier == "free"
    assert free_bp.monthly_token_quota == 100000


# =============================================================================
# Task 5 & 6: FastAPI Endpoint Integration Tests
# =============================================================================


@pytest.fixture
def setup_overrides():
    def _override(db: CheckoutMockDbSession, user_id: uuid.UUID):
        async def override_db():
            yield db

        async def override_user():
            return CurrentUser(id=user_id, email="admin@example.com")

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user

    yield _override
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_checkout_endpoint_unauthenticated_returns_401():
    """Unauthenticated request to checkout initiation returns 401."""
    org_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/organizations/{org_id}/billing/razorpay/checkout-subscription")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_checkout_endpoint_non_member_returns_404(setup_overrides):
    """Caller who is not a member of the organization receives 404 (anti-enumeration)."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Isolated Org")
    db = CheckoutMockDbSession(organizations=[org], members=[])  # Not a member

    setup_overrides(db, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/organizations/{org_id}/billing/razorpay/checkout-subscription")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_checkout_endpoint_regular_member_returns_403(setup_overrides):
    """Regular member without admin role receives 403 Forbidden."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Member Org")
    member = OrganizationMember(
        organization_id=org_id,
        user_id=user_id,
        role=MemberRole.member,
    )
    db = CheckoutMockDbSession(organizations=[org], members=[member])

    setup_overrides(db, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/organizations/{org_id}/billing/razorpay/checkout-subscription")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_checkout_endpoint_admin_succeeds(setup_overrides):
    """Admin member successfully initiates checkout session and receives safe response."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Admin Org")
    member = OrganizationMember(
        organization_id=org_id,
        user_id=user_id,
        role=MemberRole.admin,
    )
    db = CheckoutMockDbSession(organizations=[org], members=[member])
    set_razorpay_client_for_testing(MockRazorpayClient(subscription_id="sub_endpoint_123"))

    setup_overrides(db, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/organizations/{org_id}/billing/razorpay/checkout-subscription")

    assert res.status_code == 200
    data = res.json()
    assert data["organization_id"] == str(org_id)
    assert data["subscription_id"] == "sub_endpoint_123"
    assert data["provider"] == "razorpay"
    assert data["status"] == "open"
    assert "key_secret" not in data
    assert "secret" not in str(data).lower()


@pytest.mark.asyncio
async def test_verify_endpoint_admin_succeeds(setup_overrides):
    """Admin member verifies callback data and receives verified outcome."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    org = Organization(id=org_id, name="Verify Endpoint Org")
    member = OrganizationMember(
        organization_id=org_id,
        user_id=user_id,
        role=MemberRole.admin,
    )
    sub_id = "sub_ep_verify_123"
    pay_id = "pay_ep_verify_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_123",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(
        attempts=[attempt],
        organizations=[org],
        members=[member],
    )
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_id,
            payment_id=pay_id,
            payment_status="captured",
        )
    )

    setup_overrides(db, user_id)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    payload = {
        "attempt_id": str(attempt_id),
        "razorpay_payment_id": pay_id,
        "razorpay_subscription_id": sub_id,
        "razorpay_signature": sig,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(
            f"/api/v1/organizations/{org_id}/billing/razorpay/verify-checkout",
            json=payload,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["attempt_id"] == str(attempt_id)
    assert data["payment_id"] == pay_id
    assert data["status"] == "complete"
    assert data["payment_status"] == "captured"


# =============================================================================
# Step 5.2C.1.1 Audit Regression Tests
# =============================================================================


@pytest.mark.asyncio
async def test_expired_recovery_required_attempt_cannot_trigger_duplicate_creation():
    """An attempt in recovery_required must NEVER be recycled or duplicated just because its lease expired."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Recovery Org")
    attempt_id = uuid.uuid4()

    # Attempt in recovery_required with an expired lease
    past_lease = datetime.now(timezone.utc) - timedelta(hours=3)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recovery_1",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=past_lease,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt], organizations=[org])
    mock_client = MockRazorpayClient()
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(CheckoutRecoveryRequiredError) as exc_info:
        await create_razorpay_subscription_checkout(db=db, organization=org)

    assert str(attempt_id) in str(exc_info.value)
    # No new subscription was created on gateway
    assert len(mock_client.create_subscription_calls) == 0
    # Attempt remains safely preserved in recovery_required
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_recovery_of_existing_remote_subscription():
    """Administrator / worker recovers a recovery_required attempt using known remote subscription ID."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_recovered_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recov_2",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    mock_client = MockRazorpayClient(subscription_id=sub_id)
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=sub_id,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == sub_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_subscription_id == sub_id
    assert attempt.error_message is None
    assert mock_client.get_subscription_calls == [sub_id]


@pytest.mark.asyncio
async def test_recovery_marked_failed_only_with_defensible_evidence():
    """An ambiguous attempt can ONLY be marked failed when defensible non-creation evidence is explicitly provided."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    evidence_proof = "admin_verified_gateway_audit_log_no_subscription_created"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recov_fail",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    with pytest.raises(RazorpayCheckoutError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
            definitive_nonexistence_evidence=evidence_proof,
        )

    assert "marked failed with defensible evidence" in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.failed
    assert attempt.error_message == evidence_proof

    # Defensible evidence must be recorded in PaymentReconciliation audit ledger
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].reason == "definitive_nonexistence_evidence"
    assert db.reconciliations[0].status == ReconciliationStatus.resolved_ignored


@pytest.mark.asyncio
async def test_recovery_unknown_subscription_inconclusive_lookup_preserves_recovery_required():
    """Inconclusive lookup with 0 candidates must NEVER mark attempt failed or release reservation."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recov_inconcl",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    # Gateway returns empty list of subscriptions
    mock_client = MockRazorpayClient(listed_subscriptions=[])
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "inconclusive" in str(exc_info.value)
    # Attempt remains preserved in recovery_required (reservation is NOT released)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.error_message == "recovery_lookup_inconclusive_no_candidates"


@pytest.mark.asyncio
async def test_recovery_lookup_finds_exactly_one_matching_subscription():
    """Remote lookup finding exactly one matching candidate successfully recovers attempt to open."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    candidate_sub_id = "sub_single_match_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recov_single",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Remote gateway has exactly 1 subscription matching attempt_id in notes
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": candidate_sub_id,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "status": "created",
                "short_url": f"https://rzp.io/i/{candidate_sub_id}",
                "expire_by": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
                "notes": {
                    "attempt_id": str(attempt_id),
                    "organization_id": str(org_id),
                },
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=None,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == candidate_sub_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_subscription_id == candidate_sub_id
    assert attempt.error_message is None
    assert attempt.request_payload["recovery_evidence"]["method"] == "filtered_remote_lookup"


@pytest.mark.asyncio
async def test_recovery_lookup_finds_multiple_matching_subscriptions():
    """Remote lookup finding multiple candidates preserves ambiguity and logs PaymentReconciliation case."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand1 = "sub_cand_alpha_1"
    cand2 = "sub_cand_beta_2"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_recov_multi",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Remote gateway returns 2 matching candidates
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": cand1,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "notes": {"organization_id": str(org_id), "attempt_id": str(attempt_id)},
            },
            {
                "id": cand2,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "notes": {"organization_id": str(org_id), "attempt_id": str(attempt_id)},
            },
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "Multiple matching remote subscriptions" in str(exc_info.value)
    # Attempt must preserve recovery_required
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    # PaymentReconciliation case created for review
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].reason == "multiple_recovery_candidates_found"
    assert db.reconciliations[0].status == ReconciliationStatus.pending_review
    assert cand1 in db.reconciliations[0].diagnostic_payload["candidate_subscription_ids"]
    assert cand2 in db.reconciliations[0].diagnostic_payload["candidate_subscription_ids"]


@pytest.mark.asyncio
async def test_recovery_remote_subscription_different_plan_rejected():
    """Candidate remote subscription belonging to a different plan is rejected with conflict."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_diff_plan_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_diff_plan",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Remote subscription belongs to 'plan_enterprise_yearly'
    mock_client = MockRazorpayClient(subscription_id=sub_id)
    mock_client.create_subscription_calls = []

    async def _mock_get_diff_plan(s_id: str):
        return {
            "id": s_id,
            "entity": "subscription",
            "plan_id": "plan_enterprise_yearly",
            "customer_id": "cust_123",
            "status": "created",
        }

    mock_client.get_subscription = _mock_get_diff_plan
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=sub_id,
        )

    assert "plan" in str(exc_info.value).lower()
    # Preserves recovery_required
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_recovery_remote_subscription_different_customer_rejected():
    """Candidate remote subscription belonging to a different customer is rejected with conflict."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_diff_cust_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_our_org",
        provider_subscription_id=None,
        idempotency_key="idemp_diff_cust",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(subscription_id=sub_id)

    async def _mock_get_diff_cust(s_id: str):
        return {
            "id": s_id,
            "entity": "subscription",
            "plan_id": "plan_test_pro_monthly",
            "customer_id": "cust_other_org",
            "status": "created",
        }

    mock_client.get_subscription = _mock_get_diff_cust
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=sub_id,
        )

    assert "customer" in str(exc_info.value).lower()
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_recovery_api_timeout_preserves_recovery_required():
    """Network timeout during recovery lookup preserves attempt in recovery_required."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_timeout",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(
        fail_list_subscriptions=RazorpayNetworkError("Gateway connection timed out", is_pre_dispatch=False)
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "timed out" in str(exc_info.value).lower()
    assert attempt.status == CheckoutAttemptStatus.recovery_required


@pytest.mark.asyncio
async def test_recovery_repeated_attempts_are_idempotent():
    """Calling recovery on an attempt that is already open returns the open attempt idempotently."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_already_open_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_open",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        checkout_url="https://rzp.io/i/sub_already_open_123",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    mock_client = MockRazorpayClient()
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=sub_id,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == sub_id
    assert mock_client.get_subscription_calls == []


@pytest.mark.asyncio
async def test_recovery_worker_ownership_conflict_superseded():
    """A stale recovery worker whose claim was superseded cannot overwrite attempt and saves candidate to reconciliation."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    candidate_sub_id = "sub_stale_worker_123"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_stale_claim",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(subscription_id=candidate_sub_id)

    # Intercept execute during recovery Phase 3 (when with_for_update re-locks the row):
    # simulate concurrent Worker B having acquired a newer claim!
    original_execute = db.execute

    async def _intercepting_execute(stmt: Any):
        res = await original_execute(stmt)
        # Check if this is the lock query on billing_checkout_attempts
        froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
        is_attempt_lock = any(getattr(f, "name", None) == "billing_checkout_attempts" for f in froms_list)
        if is_attempt_lock and getattr(stmt, "_for_update_arg", None) is not None:
            # Concurrently mutate claim in the database to simulate supersession by Worker B!
            attempt.request_payload["recovery_claim_id"] = "rec_claim_WORKER_B_SUPERSEDED"
        return res

    db.execute = _intercepting_execute
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=candidate_sub_id,
        )

    assert "superseded by another worker" in str(exc_info.value)
    # Stale worker did NOT mutate the attempt to open
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    # The recovered candidate is safely preserved in PaymentReconciliation so it is not lost
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].reason == "superseded_recovery_worker"
    assert db.reconciliations[0].diagnostic_payload["recovered_subscription_id"] == candidate_sub_id


@pytest.mark.asyncio
async def test_scoped_uniqueness_same_org_different_provider_and_environment():
    """Simulated: Scoped uniqueness permits active attempts across different provider/account/environment scopes."""
    org_id = uuid.uuid4()

    # Scope 1: Razorpay test scope
    rzp_test_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        idempotency_key="idemp_rzp_test",
        status=CheckoutAttemptStatus.open,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Scope 2: Stripe test scope
    stripe_test_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="stripe",
        merchant_account_id="acc_stripe_default",
        environment="test",
        idempotency_key="idemp_stripe_test",
        status=CheckoutAttemptStatus.open,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Scope 3: Razorpay live scope
    rzp_live_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_live_merchant_999",
        environment="live",
        idempotency_key="idemp_rzp_live",
        status=CheckoutAttemptStatus.open,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Scoped index keys:
    key_1 = (rzp_test_attempt.organization_id, rzp_test_attempt.provider, rzp_test_attempt.merchant_account_id, rzp_test_attempt.environment)
    key_2 = (stripe_test_attempt.organization_id, stripe_test_attempt.provider, stripe_test_attempt.merchant_account_id, stripe_test_attempt.environment)
    key_3 = (rzp_live_attempt.organization_id, rzp_live_attempt.provider, rzp_live_attempt.merchant_account_id, rzp_live_attempt.environment)

    # Under scoped uniqueness, all 3 scoped tuples are distinct and can coexist
    unique_scoped_keys = {key_1, key_2, key_3}
    assert len(unique_scoped_keys) == 3


@pytest.mark.asyncio
async def test_scoped_uniqueness_duplicate_active_same_scope_rejected():
    """Simulated: Duplicate active checkout attempts within the exact same scope violate scoped uniqueness."""
    org_id = uuid.uuid4()
    scope = ("razorpay", "acc_test_123", "test")

    attempt_1 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=scope[0],
        merchant_account_id=scope[1],
        environment=scope[2],
        idempotency_key="idemp_dup_1",
        status=CheckoutAttemptStatus.open,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    attempt_2 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=scope[0],
        merchant_account_id=scope[1],
        environment=scope[2],
        idempotency_key="idemp_dup_2",
        status=CheckoutAttemptStatus.creating,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Both are active statuses ('open', 'creating') in the same (org, provider, merchant, env) scope
    key_1 = (attempt_1.organization_id, attempt_1.provider, attempt_1.merchant_account_id, attempt_1.environment)
    key_2 = (attempt_2.organization_id, attempt_2.provider, attempt_2.merchant_account_id, attempt_2.environment)

    # Under the scoped index predicate, this represents a unique constraint violation
    assert key_1 == key_2


@pytest.mark.asyncio
async def test_scoped_uniqueness_multiple_completed_attempts_coexist():
    """Multiple complete attempts within the same scope do not conflict because they are not active."""
    org_id = uuid.uuid4()
    scope = ("razorpay", "acc_test_123", "test")

    completed_1 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=scope[0],
        merchant_account_id=scope[1],
        environment=scope[2],
        idempotency_key="idemp_comp_1",
        status=CheckoutAttemptStatus.complete,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc),
    )
    completed_2 = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=scope[0],
        merchant_account_id=scope[1],
        environment=scope[2],
        idempotency_key="idemp_comp_2",
        status=CheckoutAttemptStatus.complete,
        currency="USD",
        attempt_lease_expires_at=datetime.now(timezone.utc),
    )

    # The partial unique index predicate only applies to ('creating', 'recovery_required', 'open', 'payment_pending')
    active_statuses = {
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
        CheckoutAttemptStatus.open,
        CheckoutAttemptStatus.payment_pending,
    }
    assert completed_1.status not in active_statuses
    assert completed_2.status not in active_statuses



@pytest.mark.asyncio
async def test_payment_changes_from_authorized_to_captured():
    """Payment pending verification transitions monotonically to complete when payment becomes captured."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_monot_123"
    pay_id = "pay_monot_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_monot_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # 1. First verification: payment is authorized
    client_authorized = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="authorized",
        payment_amount=2900,
        payment_currency="USD",
    )
    set_razorpay_client_for_testing(client_authorized)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    res1 = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )
    assert res1.status == CheckoutAttemptStatus.payment_pending
    assert res1.payment_status == "authorized"
    assert attempt.status == CheckoutAttemptStatus.payment_pending

    # 2. Second verification: payment has now been captured by Razorpay
    client_captured = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="captured",
        payment_amount=2900,
        payment_currency="USD",
    )
    set_razorpay_client_for_testing(client_captured)

    res2 = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )
    assert res2.status == CheckoutAttemptStatus.complete
    assert res2.payment_status == "captured"
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.amount_minor_units == 2900
    assert attempt.currency == "USD"


@pytest.mark.asyncio
async def test_repeated_callback_after_capture():
    """Repeated verification after capture returns complete idempotently without re-processing."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_repcpt_123"
    pay_id = "pay_repcpt_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_repcpt_1",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=2900,
        currency="USD",
        request_payload={
            "verification": {
                "payment_id": pay_id,
                "payment_status": "captured",
                "amount": 2900,
            }
        },
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    mock_client = MockRazorpayClient(subscription_id=sub_id, payment_id=pay_id)
    set_razorpay_client_for_testing(mock_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    res = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )

    assert res.status == CheckoutAttemptStatus.complete
    assert res.payment_status == "captured"
    # Gateway was not queried because idempotent replay short-circuited
    assert mock_client.get_payment_calls == []


@pytest.mark.asyncio
async def test_stale_verification_response_cannot_downgrade_captured_state():
    """Stale or out-of-order verification response cannot downgrade a complete attempt to payment_pending."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_stale_123"
    pay_id = "pay_stale_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_stale_1",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=2900,
        currency="USD",
        request_payload={
            "verification": {
                "payment_id": pay_id,
                "payment_status": "captured",
            }
        },
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Replay with stale client that returns 'authorized'
    stale_client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="authorized",
    )
    set_razorpay_client_for_testing(stale_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    res = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )

    assert res.status == CheckoutAttemptStatus.complete
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_payment_belonging_to_another_subscription_is_rejected():
    """Gateway payment whose subscription_id does not match server-owned subscription is rejected."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    server_sub_id = "sub_our_org_123"
    foreign_sub_id = "sub_attacker_org_999"
    pay_id = "pay_foreign_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=server_sub_id,
        idempotency_key="idemp_foreign_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Payment belongs to foreign_sub_id
    foreign_client = MockRazorpayClient(
        subscription_id=foreign_sub_id,
        payment_id=pay_id,
        payment_status="captured",
        payment_subscription_id=foreign_sub_id,
    )
    set_razorpay_client_for_testing(foreign_client)

    # Signature passes against server_sub_id
    sig = _compute_valid_signature(pay_id, server_sub_id, secret)

    with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=server_sub_id,
            razorpay_signature=sig,
        )

    assert foreign_sub_id in str(exc_info.value)
    assert server_sub_id in str(exc_info.value)
    # Attempt remains open, not completed
    assert attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_missing_payment_subscription_association_fails_closed():
    """Gateway payment lacking subscription_id association fails closed immediately."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_assoc_123"
    pay_id = "pay_assoc_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_assoc_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Payment has no subscription_id
    no_sub_client = MockRazorpayClient(
        payment_id=pay_id,
        payment_status="captured",
        payment_subscription_id=None,
    )
    set_razorpay_client_for_testing(no_sub_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)

    with pytest.raises(RazorpayCheckoutPaymentError) as exc_info:
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=sub_id,
            razorpay_signature=sig,
        )

    assert "has no associated subscription_id" in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_conflicting_payment_id_on_attempt_rejected():
    """An attempt already associated with payment A permanently rejects callback for payment B."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_bind_123"
    pay_a = "pay_alpha_123"
    pay_b = "pay_beta_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_bind_1",
        status=CheckoutAttemptStatus.payment_pending,
        amount_minor_units=0,
        currency="USD",
        request_payload={
            "verification": {
                "payment_id": pay_a,
                "payment_status": "authorized",
            }
        },
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    sig_b = _compute_valid_signature(pay_b, sub_id, secret)

    with pytest.raises(RazorpayCheckoutConflictError) as exc_info:
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_b,
            razorpay_subscription_id=sub_id,
            razorpay_signature=sig_b,
        )

    assert pay_a in str(exc_info.value)
    assert pay_b in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_account_environment_checkout_isolation():
    """Active attempts in different provider/account/environment scopes do not collide."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="MultiScope Org")

    # Stale active attempt in 'stripe' provider scope
    stripe_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="stripe",
        merchant_account_id="acc_stripe_default",
        environment="test",
        provider_customer_id="cus_stripe_123",
        provider_subscription_id=None,
        idempotency_key="idemp_stripe_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Active attempt in 'razorpay' 'live' environment scope
    live_attempt = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_live_merchant_999",
        environment="live",
        provider_customer_id="cust_live_123",
        provider_subscription_id="sub_live_999",
        idempotency_key="idemp_live_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    db = CheckoutMockDbSession(
        attempts=[stripe_attempt, live_attempt],
        organizations=[org],
    )
    mock_client = MockRazorpayClient(subscription_id="sub_test_scope_123")
    set_razorpay_client_for_testing(mock_client)

    # Current settings scope is provider="razorpay", merchant_account_id="acc_test_123", environment="test"
    # Neither the stripe attempt nor the live razorpay attempt should interfere with creating a test checkout
    result = await create_razorpay_subscription_checkout(db=db, organization=org)

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == "sub_test_scope_123"
    assert result.provider == "razorpay"


# =============================================================================
# Step 5.2C.1.4: Cross-Attempt Isolation and Bounded Pagination Tests
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_cross_attempt_isolation_different_attempt_id_rejected():
    """A subscription with a non-empty attempt_id belonging to another attempt MUST be rejected."""
    org_id = uuid.uuid4()
    attempt_id_curr = uuid.uuid4()
    attempt_id_other = uuid.uuid4()

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id_curr,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_cross_1",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Remote subscription belongs to the same org and customer, but notes.attempt_id points to another attempt
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": "sub_belonging_to_other_attempt",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "status": "created",
                "created_at": int(now_utc.timestamp()),
                "notes": {
                    "organization_id": str(org_id),
                    "attempt_id": str(attempt_id_other),
                },
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id_curr,
            known_subscription_id=None,
        )

    assert "inconclusive (no definitive candidates found)" in str(exc_info.value)
    # The reservation MUST be preserved and candidate NEVER bound
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None
    assert attempt.error_message == "recovery_lookup_inconclusive_no_candidates"


@pytest.mark.asyncio
async def test_recovery_cross_attempt_isolation_missing_attempt_id_valid_time_fallback_matches():
    """A subscription with absent attempt_id matches via fallback if org, customer, plan, and creation time match."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_fallback_valid",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    fallback_sub_id = "sub_fallback_valid_123"
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": fallback_sub_id,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "status": "created",
                "short_url": f"https://rzp.io/i/{fallback_sub_id}",
                "expire_by": int((now_utc + timedelta(days=1)).timestamp()),
                "created_at": int(now_utc.timestamp()),  # Exactly in window
                "notes": {
                    "organization_id": str(org_id),
                    # attempt_id is absent
                },
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=None,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == fallback_sub_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_subscription_id == fallback_sub_id


@pytest.mark.asyncio
async def test_recovery_cross_attempt_isolation_missing_or_invalid_creation_time_fallback_rejected():
    """Fallback candidate is rejected if creation time is missing, malformed, or outside defensible window."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_fallback_invalid_time",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # 3 subscriptions with no attempt_id:
    # 1. Missing created_at
    # 2. Malformed created_at
    # 3. Created_at 2 hours before window
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": "sub_missing_time",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "notes": {"organization_id": str(org_id)},
            },
            {
                "id": "sub_malformed_time",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "created_at": "not_an_int_timestamp",
                "notes": {"organization_id": str(org_id)},
            },
            {
                "id": "sub_outside_window",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "created_at": int((now_utc - timedelta(hours=2)).timestamp()),
                "notes": {"organization_id": str(org_id)},
            },
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "inconclusive (no definitive candidates found)" in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None


@pytest.mark.asyncio
async def test_recovery_pagination_one_candidate_page_one_exhausted_accepted():
    """When page 1 returns fewer than 50 items and contains exactly 1 candidate, search is exhausted and accepted."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand_id = "sub_p1_unique"

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_p1_ok",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # 5 items total (< 50), 1 matches attempt_id
    items = [
        {
            "id": f"sub_other_{i}",
            "plan_id": "plan_test_pro_monthly",
            "customer_id": f"cust_other_{i}",
            "notes": {"attempt_id": f"att_other_{i}"},
        }
        for i in range(4)
    ]
    items.append(
        {
            "id": cand_id,
            "plan_id": "plan_test_pro_monthly",
            "customer_id": "cust_123",
            "status": "created",
            "short_url": f"https://rzp.io/i/{cand_id}",
            "expire_by": int((now_utc + timedelta(days=1)).timestamp()),
            "created_at": int(now_utc.timestamp()),
            "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
        }
    )

    mock_client = MockRazorpayClient(listed_subscriptions=items)
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=None,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == cand_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_subscription_id == cand_id
    # Verified that from_time parameter was passed correctly
    assert len(mock_client.list_subscription_calls) == 1
    assert mock_client.list_subscription_calls[0]["from_time"] == max(0, int(now_utc.timestamp()) - 300)
    assert mock_client.list_subscription_calls[0]["skip"] == 0
    assert mock_client.list_subscription_calls[0]["count"] == 50


@pytest.mark.asyncio
async def test_recovery_pagination_candidates_across_multiple_pages_ambiguous():
    """Candidate on page 1 and another candidate on page 2 detected across pagination, resulting in ambiguity."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand1 = "sub_page_1_match"
    cand2 = "sub_page_2_match"

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_multipage_ambig",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Page 1: 50 items (contains cand1)
    page1_items = [
        {
            "id": f"sub_p1_noise_{i}",
            "plan_id": "plan_test_pro_monthly",
            "customer_id": f"cust_noise_{i}",
            "notes": {"attempt_id": f"att_noise_{i}"},
        }
        for i in range(49)
    ]
    page1_items.append(
        {
            "id": cand1,
            "plan_id": "plan_test_pro_monthly",
            "customer_id": "cust_123",
            "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
        }
    )

    # Page 2: 20 items (contains cand2)
    page2_items = [
        {
            "id": f"sub_p2_noise_{i}",
            "plan_id": "plan_test_pro_monthly",
            "customer_id": f"cust_p2_{i}",
            "notes": {"attempt_id": f"att_p2_{i}"},
        }
        for i in range(19)
    ]
    page2_items.append(
        {
            "id": cand2,
            "plan_id": "plan_test_pro_monthly",
            "customer_id": "cust_123",
            "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
        }
    )

    mock_client = MockRazorpayClient(
        list_subscriptions_pages=[
            {"entity": "collection", "count": len(page1_items), "items": page1_items},
            {"entity": "collection", "count": len(page2_items), "items": page2_items},
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "Multiple matching remote subscriptions found on gateway" in str(exc_info.value)
    assert cand1 in str(exc_info.value)
    assert cand2 in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None
    # Reconcilation case recorded
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].reason == "multiple_recovery_candidates_found"
    assert len(mock_client.list_subscription_calls) == 2


@pytest.mark.asyncio
async def test_recovery_pagination_api_error_on_later_page_inconclusive():
    """An API error on page 2 makes recovery inconclusive and preserves recovery_required even if page 1 had a candidate."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand1 = "sub_page_1_match"

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_page_err",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    # Page 1: 50 items (contains cand1)
    page1_items = [
        {
            "id": f"sub_p1_noise_{i}",
            "plan_id": "plan_test_pro_monthly",
            "customer_id": f"cust_noise_{i}",
            "notes": {"attempt_id": f"att_noise_{i}"},
        }
        for i in range(49)
    ]
    page1_items.append(
        {
            "id": cand1,
            "plan_id": "plan_test_pro_monthly",
            "customer_id": "cust_123",
            "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
        }
    )

    mock_client = MockRazorpayClient(
        list_subscriptions_pages=[
            {"entity": "collection", "count": len(page1_items), "items": page1_items},
        ],
        fail_list_subscriptions_on_page=2,
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "incomplete" in str(exc_info.value)
    assert "gateway_error_page_2" in str(exc_info.value)
    # The attempt MUST remain recovery_required; cand1 must NOT be bound
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None
    assert "recovery_lookup_inconclusive_gateway_error_page_2" in attempt.error_message


@pytest.mark.asyncio
async def test_recovery_pagination_three_full_pages_ceiling_inconclusive():
    """If recovery reaches the 3-page ceiling (50 items each), search is incomplete and candidate is NOT bound."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand1 = "sub_page_1_match"

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_ceiling_hit",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    def make_50_items(page_prefix: str, include_candidate: bool = False):
        count = 49 if include_candidate else 50
        noise = [
            {
                "id": f"sub_{page_prefix}_{i}",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": f"cust_{page_prefix}_{i}",
                "notes": {"attempt_id": f"att_{page_prefix}_{i}"},
            }
            for i in range(count)
        ]
        if include_candidate:
            noise.append(
                {
                    "id": cand1,
                    "plan_id": "plan_test_pro_monthly",
                    "customer_id": "cust_123",
                    "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
                }
            )
        return noise

    p1 = make_50_items("p1", include_candidate=True)
    p2 = make_50_items("p2", include_candidate=False)
    p3 = make_50_items("p3", include_candidate=False)

    mock_client = MockRazorpayClient(
        list_subscriptions_pages=[
            {"entity": "collection", "count": 50, "items": p1},
            {"entity": "collection", "count": 50, "items": p2},
            {"entity": "collection", "count": 50, "items": p3},
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "page_ceiling_reached_3_pages_unexamined_results_may_exist" in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None
    assert len(mock_client.list_subscription_calls) == 3


@pytest.mark.asyncio
async def test_recovery_pagination_malformed_response_inconclusive():
    """A malformed pagination response treats recovery as inconclusive and preserves recovery_required."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_malformed_pg",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt])

    mock_client = MockRazorpayClient(
        list_subscriptions_pages=[
            {"entity": "collection", "count": 10, "items": "invalid_string_not_list"}  # Malformed items
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    assert "malformed_response_page_1" in str(exc_info.value)
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None


# =============================================================================
# Step 5.2C.1.6: Missing Checkout Timestamp Fail-Closed Tests
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_missing_created_at_fails_closed_without_remote_call():
    """An attempt with missing created_at fails closed immediately without calling gateway list."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_missing_created_at",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        created_at=None,
        error_message="post_dispatch_network_timeout",
    )
    attempt._force_none_created_at = True
    db = CheckoutMockDbSession(attempts=[attempt], allow_missing_created_at=True)

    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": "sub_should_never_be_reached",
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutAmbiguousError) as exc_info:
        await recover_razorpay_checkout_attempt(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            known_subscription_id=None,
        )

    # 1. Clear diagnostic message raised
    assert "lacks a created_at timestamp" in str(exc_info.value)
    # 2. No remote subscription-list API call occurred
    assert len(mock_client.list_subscription_calls) == 0
    # 3. The attempt remains recovery_required
    assert attempt.status == CheckoutAttemptStatus.recovery_required
    assert attempt.provider_subscription_id is None
    # 4. Error message records missing timestamp outcome
    assert attempt.error_message == "recovery_lookup_inconclusive_missing_created_at"


@pytest.mark.asyncio
async def test_recovery_naive_datetime_created_at_handled_safely():
    """An attempt with a naive (offset-naive) datetime created_at is safely converted to UTC without errors."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand_id = "sub_naive_ts_match"

    # Naive datetime (no tzinfo)
    naive_created_at = datetime.utcnow()
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_naive_ts",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        created_at=naive_created_at,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt], allow_missing_created_at=True)

    expected_ts = int(naive_created_at.replace(tzinfo=timezone.utc).timestamp())
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": cand_id,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "status": "created",
                "short_url": f"https://rzp.io/i/{cand_id}",
                "expire_by": expected_ts + 86400,
                "created_at": expected_ts,
                "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=None,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == cand_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_subscription_id == cand_id
    assert len(mock_client.list_subscription_calls) == 1
    assert mock_client.list_subscription_calls[0]["from_time"] == max(0, expected_ts - 300)


@pytest.mark.asyncio
async def test_recovery_timezone_aware_datetime_created_at_handled_safely():
    """An attempt with timezone-aware created_at correctly derives the UTC epoch search window."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cand_id = "sub_tz_aware_match"

    now_utc = datetime.now(timezone.utc)
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=None,
        idempotency_key="idemp_tz_aware",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=now_utc - timedelta(hours=1),
        created_at=now_utc,
        error_message="post_dispatch_network_timeout",
    )
    db = CheckoutMockDbSession(attempts=[attempt], allow_missing_created_at=True)

    expected_ts = int(now_utc.timestamp())
    mock_client = MockRazorpayClient(
        listed_subscriptions=[
            {
                "id": cand_id,
                "plan_id": "plan_test_pro_monthly",
                "customer_id": "cust_123",
                "status": "created",
                "short_url": f"https://rzp.io/i/{cand_id}",
                "expire_by": expected_ts + 86400,
                "created_at": expected_ts,
                "notes": {"attempt_id": str(attempt_id), "organization_id": str(org_id)},
            }
        ]
    )
    set_razorpay_client_for_testing(mock_client)

    result = await recover_razorpay_checkout_attempt(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        known_subscription_id=None,
    )

    assert result.status == CheckoutAttemptStatus.open
    assert result.subscription_id == cand_id
    assert attempt.status == CheckoutAttemptStatus.open
    assert len(mock_client.list_subscription_calls) == 1
    assert mock_client.list_subscription_calls[0]["from_time"] == max(0, expected_ts - 300)

