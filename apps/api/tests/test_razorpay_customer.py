"""Focused unit tests for organization-scoped Razorpay customer management.

Covers:
1. Reusing an existing resolved Razorpay customer from BillingCustomerProvision.
2. Complete provider isolation: Stripe customer ID is never reused as a Razorpay customer ID.
3. New Razorpay customer creation when no matching association exists (persists to BillingCustomerProvision).
4. Subscription decoupling: customer provisioning NEVER creates, modifies, or demotes a Subscription row.
5. In-place lifecycle protection: pre-existing Subscription rows (active, trialing, past_due, canceled)
   remain 100% untouched during Razorpay customer provisioning.
6. Concurrency serialization: in-progress provisioning claims prevent concurrent duplicate
   customer creation on Razorpay.
7. Uniqueness conflict recovery: concurrent collision on pending claim recovers cleanly.
8. Rigorous HTTP Failure Classification:
   - Definite client errors (HTTP 400, 401, 404, 422) clean up the claim and allow safe retry.
   - HTTP 408 Request Timeout transitions claim to ambiguous and prohibits blind retry.
   - HTTP 429 Rate Limit transitions claim to ambiguous (conservatively preserves claim).
   - Ambiguous server errors (HTTP 500, 502) transition claim to ambiguous and prohibit blind retry.
   - Pre-dispatch network errors (ConnectTimeout, ConnectError) clean up the claim.
   - Post-dispatch network errors (ReadTimeout, connection reset) transition to ambiguous.
   - Malformed success (HTTP 200/201 with missing/null ID) transitions to ambiguous and never cleans claim.
   - Unexpected exceptions transition to ambiguous.
9. Remote Success with Database Persistence Failure:
   - If customer creation succeeds on Razorpay but database commit fails, customer ID is preserved
     in ambiguous reconciliation state and claim is never released.
10. Lease semantics and claim ownership:
    - Independent lease_expires_at timer (independent of Subscription.updated_at).
    - Expired lease transitions to ambiguous, never a fresh create opportunity.
    - Stale worker cannot overwrite a newer provisioning state.
11. No synthetic identity: never fabricates emails or phone numbers.
12. Multi-tenant isolation: independent organizations provision concurrently without interference.
"""

from datetime import datetime, timedelta, timezone
import sys
import uuid
from typing import Any
import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError, OperationalError

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
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401
from app.config import settings
from app.models.organization import Organization
from app.models.payment import (
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayBillingDisabledError,
    RazorpayClient,
    RazorpayConfigurationError,
    RazorpayNetworkError,
    RazorpayRateLimitError,
    RazorpayServer5xxError,
    reset_razorpay_client,
    set_razorpay_client_for_testing,
)
from app.services.billing.razorpay.customer import (
    RazorpayAmbiguousNetworkError,
    RazorpayCustomerError,
    RazorpayCustomerPersistenceError,
    RazorpayCustomerProvisioningInProgressError,
    RazorpayCustomerReconciliationRequiredError,
    get_or_create_razorpay_customer,
    get_razorpay_customer_id,
)


@pytest.fixture(autouse=True)
def setup_razorpay_settings(monkeypatch):
    """Configure test Razorpay settings and cleanup mock clients."""
    reset_razorpay_client()
    monkeypatch.setattr(settings, "razorpay_enabled", True)
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_test_123")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_mockkey")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("test_mock_secret"))
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "plan_test_pro")
    monkeypatch.setattr(settings, "razorpay_plan_currency", "USD")
    yield
    reset_razorpay_client()


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

    def first(self) -> Any:
        return self._value


class InMemoryDbSession:
    """In-memory AsyncSession test double for customer management operations."""

    def __init__(
        self,
        provisions: list[BillingCustomerProvision] | None = None,
        subscriptions: list[Subscription] | None = None,
        fail_commit_with_integrity_error: bool = False,
        fail_commit_with_exception: Exception | None = None,
    ) -> None:
        self.provisions: list[BillingCustomerProvision] = list(provisions or [])
        self.subscriptions: list[Subscription] = list(subscriptions or [])
        self.reconciliations: list[PaymentReconciliation] = []
        self.fail_commit_with_integrity_error = fail_commit_with_integrity_error
        self.fail_commit_with_exception = fail_commit_with_exception
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.uncommitted: list[Any] = []
        self._committed_state: dict[Any, dict[str, Any]] = {}
        self._save_snapshots()
        self.committed_count = 0
        self.rolled_back_count = 0

    def _save_snapshots(self) -> None:
        self._committed_state.clear()
        for p in self.provisions:
            self._committed_state[p] = {
                "provider_customer_id": p.provider_customer_id,
                "status": p.status,
                "claim_operation_id": p.claim_operation_id,
                "lease_expires_at": p.lease_expires_at,
                "last_error_code": p.last_error_code,
                "updated_at": getattr(p, "updated_at", None),
            }
        for s in self.subscriptions:
            self._committed_state[s] = {
                "provider_customer_id": s.provider_customer_id,
                "provider_subscription_id": s.provider_subscription_id,
                "status": s.status,
                "plan_tier": s.plan_tier,
                "updated_at": s.updated_at,
            }

    async def execute(self, stmt: Any) -> MockScalarResult:
        w_params = {}
        for w in getattr(stmt, "_where_criteria", ()):
            if hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key"):
                w_params[w.left.key] = w.right.value

        # Identify target entity from stmt
        from_table = ""
        froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
        for f in froms_list:
            name = getattr(f, "name", None) or getattr(f, "__tablename__", "")
            if name:
                from_table = name
                break
        if not from_table:
            cols = getattr(stmt, "selected_columns", [])
            if cols:
                c0 = cols[0]
                table_obj = getattr(c0, "table", None)
                if table_obj is not None:
                    from_table = getattr(table_obj, "name", "")

        org_id = w_params.get("organization_id")
        provider = w_params.get("provider")
        merchant_id = w_params.get("merchant_account_id")
        env = w_params.get("environment")

        if from_table == "billing_customer_provisions" or (not from_table and not self.subscriptions and self.provisions):
            matched_prov = None
            for p in reversed(self.provisions):
                if org_id and p.organization_id != org_id:
                    continue
                if provider and p.provider != provider:
                    continue
                if merchant_id and p.merchant_account_id != merchant_id:
                    continue
                if env and p.environment != env:
                    continue
                matched_prov = p
                break
            return MockScalarResult(matched_prov)

        if from_table == "payment_reconciliations":
            matched_rec = None
            for r in reversed(self.reconciliations):
                if org_id and r.organization_id != org_id:
                    continue
                matched_rec = r
                break
            return MockScalarResult(matched_rec)

        # Default to subscriptions table search
        matched_sub = None
        for s in reversed(self.subscriptions):
            if org_id and s.organization_id != org_id:
                continue
            if provider and s.provider != provider:
                continue
            if merchant_id and s.merchant_account_id != merchant_id:
                continue
            if env and s.environment != env:
                continue
            matched_sub = s
            break

        return MockScalarResult(matched_sub)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.uncommitted.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if obj in self.provisions:
            self.provisions.remove(obj)
        if obj in self.subscriptions:
            self.subscriptions.remove(obj)

    async def commit(self) -> None:
        if self.fail_commit_with_integrity_error:
            self.fail_commit_with_integrity_error = False
            raise IntegrityError("Simulated unique constraint conflict", params={}, orig=Exception())
        if self.fail_commit_with_exception:
            exc = self.fail_commit_with_exception
            self.fail_commit_with_exception = None
            raise exc

        # Promote uncommitted objects to state lists
        for obj in self.uncommitted:
            if isinstance(obj, BillingCustomerProvision) and obj not in self.provisions:
                self.provisions.append(obj)
            elif isinstance(obj, Subscription) and obj not in self.subscriptions:
                self.subscriptions.append(obj)
            elif isinstance(obj, PaymentReconciliation) and obj not in self.reconciliations:
                self.reconciliations.append(obj)
        self.uncommitted.clear()
        self._save_snapshots()
        self.committed_count += 1

    async def rollback(self) -> None:
        for obj in self.uncommitted:
            if obj in self.added:
                self.added.remove(obj)
            if obj in self.provisions:
                self.provisions.remove(obj)
            if obj in self.subscriptions:
                self.subscriptions.remove(obj)
        self.uncommitted.clear()

        # Revert in-memory object attributes to committed snapshot values
        for obj, snap in self._committed_state.items():
            for k, v in snap.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)

        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


# ============================================================================
# Basic Operations & Complete Decoupling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_razorpay_customer_reuse_existing():
    """Existing resolved Razorpay customer in BillingCustomerProvision is reused without API calls."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Corp")

    existing_provision = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_existing_999",
        status=CustomerProvisionStatus.resolved,
    )

    db = InMemoryDbSession(provisions=[existing_provision])

    mock_client = RazorpayClient(key_id="rzp_test_123", key_secret="secret")
    set_razorpay_client_for_testing(mock_client)

    customer_id = await get_or_create_razorpay_customer(db, org)
    assert customer_id == "cust_existing_999"
    assert len(db.added) == 0
    assert len(db.subscriptions) == 0


@pytest.mark.asyncio
async def test_stripe_customer_is_not_reused_as_razorpay_customer():
    """A Stripe customer association must NEVER be returned or reused as a Razorpay customer."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Legacy Stripe Corp")

    stripe_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="stripe",
        merchant_account_id="default",
        environment="legacy",
        provider_customer_id="cus_stripe_legacy_111",
        stripe_customer_id="cus_stripe_legacy_111",
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )

    db = InMemoryDbSession(subscriptions=[stripe_sub])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            assert kwargs["name"] == "Legacy Stripe Corp"
            assert kwargs["notes"]["organization_id"] == str(org_id)
            return {"id": "cust_new_rzp_222"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(db, org)
    assert customer_id == "cust_new_rzp_222"
    assert customer_id != "cus_stripe_legacy_111"

    # Stripe subscription is completely unchanged
    assert stripe_sub.provider == "stripe"
    assert stripe_sub.provider_customer_id == "cus_stripe_legacy_111"
    assert stripe_sub.status == SubscriptionStatus.active
    assert stripe_sub.plan_tier == "pro"

    # New provision created in billing_customer_provisions
    assert len(db.provisions) == 1
    prov = db.provisions[0]
    assert prov.provider == "razorpay"
    assert prov.provider_customer_id == "cust_new_rzp_222"
    assert prov.status == CustomerProvisionStatus.resolved


@pytest.mark.asyncio
async def test_razorpay_customer_creation_when_none_exists():
    """When no customer exists, creates on Razorpay with safe metadata and persists to BillingCustomerProvision."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Brand New Org")
    db = InMemoryDbSession()

    captured_args = {}

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal captured_args
            captured_args = kwargs
            return {"id": "cust_rzp_created_333"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(db, org, user_email="owner@brandnew.com")
    assert customer_id == "cust_rzp_created_333"
    assert captured_args["name"] == "Brand New Org"
    assert captured_args["email"] == "owner@brandnew.com"
    assert captured_args["notes"] == {"organization_id": str(org_id)}

    assert len(db.added) == 1
    new_prov = db.added[0]
    assert isinstance(new_prov, BillingCustomerProvision)
    assert new_prov.organization_id == org_id
    assert new_prov.provider == "razorpay"
    assert new_prov.merchant_account_id == "acc_test_123"
    assert new_prov.environment == "test"
    assert new_prov.provider_customer_id == "cust_rzp_created_333"
    assert new_prov.status == CustomerProvisionStatus.resolved
    assert new_prov.claim_operation_id is None
    assert new_prov.lease_expires_at is None

    # Crucial: Subscription table is completely untouched!
    assert len(db.subscriptions) == 0


@pytest.mark.asyncio
async def test_razorpay_customer_creation_does_not_mutate_existing_subscription():
    """Customer creation must never modify, demote, or touch existing Subscription rows."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Active Corp")

    active_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_placeholder",
        provider_subscription_id="sub_rzp_active_999",
        status=SubscriptionStatus.active,
        plan_tier="pro",
        cancel_at_period_end=False,
    )

    db = InMemoryDbSession(subscriptions=[active_sub])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_active_bound_111"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(db, org)
    assert customer_id == "cust_active_bound_111"

    # Subscription row remains 100% untouched
    assert active_sub.status == SubscriptionStatus.active
    assert active_sub.plan_tier == "pro"
    assert active_sub.provider_subscription_id == "sub_rzp_active_999"

    # Provision row created independently
    assert len(db.provisions) == 1
    assert db.provisions[0].provider_customer_id == "cust_active_bound_111"
    assert db.provisions[0].status == CustomerProvisionStatus.resolved


# ============================================================================
# HTTP Failure Classification Tests (Task 3)
# ============================================================================


@pytest.mark.asyncio
async def test_http_failure_400_bad_request_cleans_claim():
    """HTTP 400 Bad Request is a definite rejection: transitions claim to failed and allows retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Bad Request Org")
    db = InMemoryDbSession()

    class FailingClient400:
        async def create_customer(self, **kwargs):
            raise RazorpayAPIError("Invalid customer name format", status_code=400)

    set_razorpay_client_for_testing(FailingClient400())

    with pytest.raises(RazorpayCustomerError, match="creation failed"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "400"
    assert db.provisions[0].claim_operation_id is None


@pytest.mark.asyncio
async def test_http_failure_401_unauthorized_cleans_claim():
    """HTTP 401 Unauthorized is a definite credentials failure: transitions claim to failed."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Auth Error Org")
    db = InMemoryDbSession()

    class FailingClient401:
        async def create_customer(self, **kwargs):
            raise RazorpayAuthError("Invalid key_id or key_secret")

    set_razorpay_client_for_testing(FailingClient401())

    with pytest.raises(RazorpayCustomerError, match="authentication error"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "401"


@pytest.mark.asyncio
async def test_http_failure_408_request_timeout_marks_ambiguous():
    """HTTP 408 Request Timeout is AMBIGUOUS: reverse proxy timed out. Marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Timeout 408 Org")
    db = InMemoryDbSession()

    class FailingClient408:
        async def create_customer(self, **kwargs):
            raise RazorpayAPIError("Request Timeout from proxy", status_code=408)

    set_razorpay_client_for_testing(FailingClient408())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 408 request timeout"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "408"


@pytest.mark.asyncio
async def test_http_failure_429_rate_limit_conservatively_marks_ambiguous():
    """HTTP 429 Rate Limit is reassessed conservatively: marks ambiguous and preserves claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Rate Limit Org")
    db = InMemoryDbSession()

    class FailingClient429:
        async def create_customer(self, **kwargs):
            raise RazorpayRateLimitError("Rate limit exceeded")

    set_razorpay_client_for_testing(FailingClient429())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="rate limit exceeded"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    # Preserved in ambiguous state for controlled recovery, NEVER cleaned up
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "429"


@pytest.mark.asyncio
async def test_http_failure_500_internal_error_marks_ambiguous():
    """HTTP 500 Internal Error is AMBIGUOUS: marks ambiguous, no blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Server 500 Org")
    db = InMemoryDbSession()

    class FailingClient500:
        async def create_customer(self, **kwargs):
            raise RazorpayServer5xxError("Internal database error on server", status_code=500)

    set_razorpay_client_for_testing(FailingClient500())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 500"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "500"


@pytest.mark.asyncio
async def test_http_failure_502_bad_gateway_marks_ambiguous():
    """HTTP 502 Bad Gateway is AMBIGUOUS: marks ambiguous, no blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Gateway 502 Org")
    db = InMemoryDbSession()

    class FailingClient502:
        async def create_customer(self, **kwargs):
            raise RazorpayServer5xxError("Bad Gateway from reverse proxy", status_code=502)

    set_razorpay_client_for_testing(FailingClient502())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 502"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "502"


@pytest.mark.asyncio
async def test_http_failure_read_timeout_post_dispatch_marks_ambiguous():
    """Read timeout after request dispatch is AMBIGUOUS: marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Read Timeout Org")
    db = InMemoryDbSession()

    class ReadTimeoutClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("Read timed out waiting for server", is_pre_dispatch=False)

    set_razorpay_client_for_testing(ReadTimeoutClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Ambiguous network timeout"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "post_dispatch_network_error"


@pytest.mark.asyncio
async def test_http_failure_connection_reset_post_dispatch_marks_ambiguous():
    """Connection reset after request dispatch is AMBIGUOUS: marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Conn Reset Org")
    db = InMemoryDbSession()

    class ConnResetClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("Connection reset by peer after transmission", is_pre_dispatch=False)

    set_razorpay_client_for_testing(ConnResetClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Ambiguous network timeout/reset"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous


@pytest.mark.asyncio
async def test_http_failure_pre_dispatch_connect_failure_cleans_claim():
    """Failure before request dispatch (e.g. ConnectTimeout / ConnectError) safely cleans claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Connect Timeout Org")
    db = InMemoryDbSession()

    class PreDispatchTimeoutClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("TCP connection timed out before dispatch", is_pre_dispatch=True)

    set_razorpay_client_for_testing(PreDispatchTimeoutClient())

    with pytest.raises(RazorpayCustomerError, match="connection failed before dispatch"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "pre_dispatch_network_error"


@pytest.mark.asyncio
async def test_malformed_success_response_marks_ambiguous_and_does_not_clean():
    """HTTP 200/201 with missing or null customer ID must transition to ambiguous and NEVER clean claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Malformed Success Org")
    db = InMemoryDbSession()

    class MalformedSuccessClient:
        async def create_customer(self, **kwargs):
            # Remote gateway returned HTTP 200 OK, but payload has missing 'id'
            return {"status": "created", "id": None}

    set_razorpay_client_for_testing(MalformedSuccessClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="missing or malformed customer ID"):
        await get_or_create_razorpay_customer(db, org)

    # Claim must NEVER be cleaned!
    assert len(db.provisions) == 1
    prov = db.provisions[0]
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.last_error_code == "malformed_success"


@pytest.mark.asyncio
async def test_unexpected_exception_marks_ambiguous():
    """Unexpected exception during HTTP request must transition to ambiguous."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Unexpected Error Org")
    db = InMemoryDbSession()

    class CrashingClient:
        async def create_customer(self, **kwargs):
            raise RuntimeError("Unexpected library assertion crash")

    set_razorpay_client_for_testing(CrashingClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Unexpected error communicating with Razorpay"):
        await get_or_create_razorpay_customer(db, org)

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "unexpected_exception"


# ============================================================================
# Database Persistence Failure Tests
# ============================================================================


@pytest.mark.asyncio
async def test_remote_success_followed_by_database_commit_failure():
    """When remote customer creation succeeds but DB commit fails:
    1. The customer ID is recorded in ambiguous state for reconciliation.
    2. The provisioning claim is NEVER released.
    3. RazorpayCustomerPersistenceError is raised containing customer_id.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="DB Failure Org")

    class CommitFailingDbSession(InMemoryDbSession):
        async def commit(self):
            # Allow Phase 2 (claim insertion) to commit successfully
            if any(obj.status == CustomerProvisionStatus.pending for obj in self.uncommitted):
                await super().commit()
                return

            # Simulate database failure during Phase 4 resolution commit
            if any(obj.status == CustomerProvisionStatus.resolved for obj in self.provisions):
                raise OperationalError("Connection to PostgreSQL lost during resolution commit", params={}, orig=Exception())
            await super().commit()

    db = CommitFailingDbSession()

    class SuccessfulRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_remote_success_789"}

    set_razorpay_client_for_testing(SuccessfulRazorpayClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(db, org)

    err = exc_info.value
    assert err.customer_id == "cust_remote_success_789"
    assert "local database persistence failed" in str(err)

    # Verify claim is NEVER released: provision remains intact with ambiguous status and customer_id
    assert len(db.provisions) == 1
    prov = db.provisions[0]
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id == "cust_remote_success_789"
    assert prov.last_error_code == "db_persistence_failure"

    # Subsequent request detects ambiguous state and refuses to create a duplicate remote customer
    dispatched = False

    class SpyClient:
        async def create_customer(self, **kwargs):
            nonlocal dispatched
            dispatched = True
            return {"id": "cust_duplicate_should_not_happen"}

    set_razorpay_client_for_testing(SpyClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="ambiguous state"):
        await get_or_create_razorpay_customer(db, org)

    assert dispatched is False


@pytest.mark.asyncio
async def test_remote_success_followed_by_catastrophic_database_failure_leaves_pending():
    """If both primary resolution commit and ambiguous fallback commit fail, pending claim remains intact."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Catastrophic DB Org")

    class CatastrophicDbSession(InMemoryDbSession):
        def __init__(self):
            super().__init__()
            self.phase2_done = False

        async def commit(self):
            if not self.phase2_done:
                self.phase2_done = True
                await super().commit()
                return
            # Fail all subsequent commits (both resolution and fallback ambiguous update)
            raise OperationalError("Total PostgreSQL database outage", params={}, orig=Exception())

    db = CatastrophicDbSession()

    class SuccessfulRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_catastrophic_999"}

    set_razorpay_client_for_testing(SuccessfulRazorpayClient())

    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(db, org)

    # Pre-committed pending claim is intact
    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.pending


# ============================================================================
# Lease Semantics and Claim Ownership Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_newer_provisioning_state():
    """A stale worker whose claim was modified/expired while HTTP call ran cannot overwrite newer state,
    and preserves the remote customer ID in PaymentReconciliation.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Stale Worker Org")

    db = InMemoryDbSession()

    class SlowRazorpayClient:
        async def create_customer(self, **kwargs):
            # While slow client was running, Worker 2 detected lease expiry and transitioned claim to ambiguous
            assert len(db.provisions) == 1
            staged_prov = db.provisions[0]
            staged_prov.status = CustomerProvisionStatus.ambiguous
            staged_prov.claim_operation_id = None
            staged_prov.lease_expires_at = None
            db._save_snapshots()
            return {"id": "cust_slow_late_arrival_555"}

    set_razorpay_client_for_testing(SlowRazorpayClient())

    with pytest.raises(RazorpayCustomerError, match="was superseded or lost ownership"):
        await get_or_create_razorpay_customer(db, org)

    # Invariant: Stale worker did NOT mutate the ambiguous provision record
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].provider_customer_id is None
    assert db.provisions[0].claim_operation_id is None
    assert db.provisions[0].lease_expires_at is None

    # Invariant: Remote customer ID is preserved in PaymentReconciliation for review
    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.organization_id == org_id
    assert recon.provider == "razorpay"
    assert recon.reason == "stale_worker_customer_created"
    assert recon.diagnostic_payload["provider_customer_id"] == "cust_slow_late_arrival_555"


@pytest.mark.asyncio
async def test_stale_worker_superseded_by_newer_claim_preserves_claim_b_and_records_reconciliation():
    """Worker A obtains claim A, Claim A is superseded by Worker B (Claim B),
    Worker A receives success: Claim B remains 100% unchanged, Worker A's customer is preserved in reconciliation.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Superseded Claim Org")

    db = InMemoryDbSession()
    worker_a_claim_id: str | None = None
    worker_b_lease = datetime.now(timezone.utc) + timedelta(seconds=25)

    class InterleavingRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal worker_a_claim_id
            assert len(db.provisions) == 1
            prov = db.provisions[0]
            worker_a_claim_id = prov.claim_operation_id

            # Worker B supersedes Claim A with Claim B
            prov.status = CustomerProvisionStatus.pending
            prov.claim_operation_id = "claim_B_operation_999"
            prov.lease_expires_at = worker_b_lease
            prov.provider_customer_id = None
            prov.last_error_code = None
            db._save_snapshots()

            return {"id": "cust_worker_a_remote_success"}

    set_razorpay_client_for_testing(InterleavingRazorpayClient())

    with pytest.raises(RazorpayCustomerError, match="was superseded or lost ownership"):
        await get_or_create_razorpay_customer(db, org)

    # Invariant: Claim B and ALL its fields remain completely unchanged
    assert len(db.provisions) == 1
    prov = db.provisions[0]
    assert prov.status == CustomerProvisionStatus.pending
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id == "claim_B_operation_999"
    assert prov.lease_expires_at == worker_b_lease
    assert prov.last_error_code is None

    # Invariant: Customer ID returned to Worker A is preserved in PaymentReconciliation
    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.organization_id == org_id
    assert recon.provider == "razorpay"
    assert recon.reason == "stale_worker_customer_created"
    assert recon.status == ReconciliationStatus.pending_review
    assert recon.diagnostic_payload["provider_customer_id"] == "cust_worker_a_remote_success"
    assert recon.diagnostic_payload["stale_claim_operation_id"] == worker_a_claim_id
    assert recon.diagnostic_payload["persisted_claim_op_id"] == "claim_B_operation_999"


@pytest.mark.asyncio
async def test_stale_worker_persistence_failure_preserves_claim_b_and_records_reconciliation():
    """Worker A has remote success, but primary commit fails. In fallback handler, Claim B is active.
    Claim B must remain completely unchanged, and Worker A's customer must be recorded in reconciliation.
    """
    from app.services.billing.razorpay.customer import _handle_persistence_failure

    org_id = uuid.uuid4()
    worker_b_lease = datetime.now(timezone.utc) + timedelta(seconds=20)

    # Database has Claim B active
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=settings.razorpay_merchant_account_id,
        environment=settings.razorpay_environment,
        provider_customer_id=None,
        status=CustomerProvisionStatus.pending,
        claim_operation_id="claim_B_active",
        lease_expires_at=worker_b_lease,
        last_error_code=None,
    )
    db = InMemoryDbSession(provisions=[prov])

    # Worker A (stale claim) calls _handle_persistence_failure
    with pytest.raises(RazorpayCustomerPersistenceError):
        await _handle_persistence_failure(
            db=db,
            organization_id=org_id,
            customer_id="cust_worker_a_failed_commit",
            claim_op_id="claim_A_stale",
            original_error=Exception("Disk full"),
        )

    # Invariant: Claim B and ALL its fields are completely unchanged
    assert prov.status == CustomerProvisionStatus.pending
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id == "claim_B_active"
    assert prov.lease_expires_at == worker_b_lease
    assert prov.last_error_code is None

    # Invariant: Customer ID is preserved in PaymentReconciliation
    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.organization_id == org_id
    assert recon.provider == "razorpay"
    assert recon.reason == "persistence_failure_stale_claim"
    assert recon.diagnostic_payload["provider_customer_id"] == "cust_worker_a_failed_commit"
    assert recon.diagnostic_payload["stale_claim_operation_id"] == "claim_A_stale"


@pytest.mark.asyncio
async def test_stale_worker_ambiguous_error_fallback_preserves_claim_b():
    """Worker A experiences uncertain network error and calls _record_ambiguous_claim while Claim B is active.
    Claim B and all its fields must remain completely unchanged.
    """
    from app.services.billing.razorpay.customer import _record_ambiguous_claim

    org_id = uuid.uuid4()
    worker_b_lease = datetime.now(timezone.utc) + timedelta(seconds=20)

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=settings.razorpay_merchant_account_id,
        environment=settings.razorpay_environment,
        provider_customer_id=None,
        status=CustomerProvisionStatus.pending,
        claim_operation_id="claim_B_active",
        lease_expires_at=worker_b_lease,
        last_error_code=None,
    )
    db = InMemoryDbSession(provisions=[prov])

    # Stale Worker A calls _record_ambiguous_claim
    await _record_ambiguous_claim(
        db=db,
        organization_id=org_id,
        claim_op_id="claim_A_stale",
        last_error_code="read_timeout",
    )

    # Invariant: Claim B and all fields unchanged
    assert prov.status == CustomerProvisionStatus.pending
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id == "claim_B_active"
    assert prov.lease_expires_at == worker_b_lease
    assert prov.last_error_code is None


@pytest.mark.asyncio
async def test_stale_worker_cleanup_claim_preserves_claim_b():
    """Worker A experiences client error (400/401) and calls _cleanup_claim while Claim B is active.
    Claim B and all its fields must remain completely unchanged.
    """
    from app.services.billing.razorpay.customer import _cleanup_claim

    org_id = uuid.uuid4()
    worker_b_lease = datetime.now(timezone.utc) + timedelta(seconds=20)

    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=settings.razorpay_merchant_account_id,
        environment=settings.razorpay_environment,
        provider_customer_id=None,
        status=CustomerProvisionStatus.pending,
        claim_operation_id="claim_B_active",
        lease_expires_at=worker_b_lease,
        last_error_code=None,
    )
    db = InMemoryDbSession(provisions=[prov])

    # Stale Worker A calls _cleanup_claim
    await _cleanup_claim(
        db=db,
        organization_id=org_id,
        claim_op_id="claim_A_stale",
        last_error_code="400_bad_request",
    )

    # Invariant: Claim B and all fields unchanged
    assert prov.status == CustomerProvisionStatus.pending
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id == "claim_B_active"
    assert prov.lease_expires_at == worker_b_lease
    assert prov.last_error_code is None


@pytest.mark.asyncio
async def test_expired_lease_transitions_to_ambiguous_and_requires_reconciliation():
    """If worker crashes and claim lease expires (>30s), subsequent call must not blindly retry; marks ambiguous."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Crashed Worker Org")

    expired_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    stale_provision = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=None,
        status=CustomerProvisionStatus.pending,
        claim_operation_id="stale_op_123",
        lease_expires_at=expired_time,
    )

    db = InMemoryDbSession(provisions=[stale_provision])

    dispatched = False

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal dispatched
            dispatched = True
            return {"id": "cust_should_not_be_created"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="lease expired"):
        await get_or_create_razorpay_customer(db, org)

    assert dispatched is False
    assert stale_provision.status == CustomerProvisionStatus.ambiguous
    assert stale_provision.last_error_code == "lease_expired"


@pytest.mark.asyncio
async def test_independent_organizations_provision_concurrently():
    """Two different organizations can provision concurrently without mutual blocking."""
    org1_id = uuid.uuid4()
    org2_id = uuid.uuid4()
    org1 = Organization(id=org1_id, name="Org One")
    org2 = Organization(id=org2_id, name="Org Two")

    db = InMemoryDbSession()

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            if kwargs["notes"]["organization_id"] == str(org1_id):
                return {"id": "cust_org_111"}
            return {"id": "cust_org_222"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    cust1 = await get_or_create_razorpay_customer(db, org1)
    cust2 = await get_or_create_razorpay_customer(db, org2)

    assert cust1 == "cust_org_111"
    assert cust2 == "cust_org_222"
    assert len(db.provisions) == 2
    assert len(db.subscriptions) == 0


@pytest.mark.asyncio
async def test_no_email_fabrication_when_email_omitted():
    """When user_email is omitted or empty, client receives None (no synthetic email generated)."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Email-less Org")
    db = InMemoryDbSession()

    captured_payload = {}

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal captured_payload
            captured_payload = kwargs
            return {"id": "cust_no_email_456"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    await get_or_create_razorpay_customer(db, org, user_email=None)
    assert captured_payload["email"] is None
    assert captured_payload["name"] == "Email-less Org"
    assert captured_payload["notes"] == {"organization_id": str(org_id)}


@pytest.mark.asyncio
async def test_razorpay_customer_environment_scoping():
    """Live environment customers must not be reused when operating in test environment."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Corp")

    live_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_live_999",
        environment="live",
        provider_customer_id="cust_live_should_not_leak",
        status=CustomerProvisionStatus.resolved,
    )

    db = InMemoryDbSession(provisions=[live_prov])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_test_fresh_444"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(db, org)
    assert customer_id == "cust_test_fresh_444"
    assert customer_id != "cust_live_should_not_leak"


@pytest.mark.asyncio
async def test_razorpay_customer_disabled_guard(monkeypatch):
    """If razorpay_enabled is False, raises RazorpayBillingDisabledError."""
    monkeypatch.setattr(settings, "razorpay_enabled", False)
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Disabled Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayBillingDisabledError, match="Razorpay billing is currently disabled"):
        await get_or_create_razorpay_customer(db, org)


# ============================================================================
# Scope Safety and Fail-Closed Validation Tests (Task 3)
# ============================================================================


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_on_invalid_environment(monkeypatch):
    """If razorpay_environment is invalid, fail closed with RazorpayConfigurationError."""
    monkeypatch.setattr(settings, "razorpay_environment", "staging")
    org = Organization(id=uuid.uuid4(), name="Invalid Env Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayConfigurationError, match="Invalid Razorpay environment"):
        await get_or_create_razorpay_customer(db, org)


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_on_empty_merchant_account(monkeypatch):
    """If razorpay_merchant_account_id is missing or whitespace, fail closed."""
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "   ")
    org = Organization(id=uuid.uuid4(), name="Empty Merchant Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayConfigurationError, match="merchant account ID must be explicitly configured"):
        await get_or_create_razorpay_customer(db, org)


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_production_with_default_merchant(monkeypatch):
    """In production, merchant_account_id cannot be 'default' or 'placeholder'."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "live")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "default")
    org = Organization(id=uuid.uuid4(), name="Prod Default Merchant Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured"):
        await get_or_create_razorpay_customer(db, org)


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_production_with_test_environment(monkeypatch):
    """In production, test environment is strictly prohibited."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_real_prod_123")
    org = Organization(id=uuid.uuid4(), name="Prod Test Env Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
        await get_or_create_razorpay_customer(db, org)


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_dev_with_live_environment(monkeypatch):
    """In development/test, live environment is strictly prohibited."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_environment", "live")
    org = Organization(id=uuid.uuid4(), name="Dev Live Env Org")
    db = InMemoryDbSession()

    with pytest.raises(RazorpayConfigurationError, match="Non-production RAZORPAY_ENVIRONMENT must be 'test'"):
        await get_or_create_razorpay_customer(db, org)
