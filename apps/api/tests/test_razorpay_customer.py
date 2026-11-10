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

import asyncio
import asyncpg
import copy
from datetime import datetime, timedelta, timezone
import operator
import sys
import uuid
from typing import Any, Callable
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, insert, select, update
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
from app.db import session as db_session_module
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
    CORRUPTED_RESOLVED_ERROR_CODE,
    PROVIDER_CUSTOMER_UNIQUE_INDEX,
    RazorpayAmbiguousNetworkError,
    RazorpayCustomerError,
    RazorpayCustomerPersistenceError,
    RazorpayCustomerProvisioningInProgressError,
    RazorpayCustomerReconciliationRequiredError,
    _handle_corrupted_resolved_provision,
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


def _matches_stmt_where(obj: Any, stmt: Any) -> bool:
    """Evaluate SQLAlchemy WHERE BinaryExpressions (==, !=, IS NULL, IS NOT NULL) in memory."""
    for w in getattr(stmt, "_where_criteria", ()):
        if not (hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key")):
            continue
        col_key = w.left.key
        op = getattr(w, "operator", operator.eq)
        target_val = getattr(w.right, "value", None)
        actual_val = getattr(obj, col_key, None)
        op_name = getattr(op, "__name__", "")
        if op is operator.eq:
            if actual_val != target_val:
                return False
        elif op is operator.ne:
            if actual_val == target_val:
                return False
        elif op_name == "is_":
            if actual_val is not target_val:
                return False
        elif op_name == "is_not":
            if actual_val is target_val:
                return False
    return True


def _make_pg_unique_error(
    constraint_name: str,
    detail: str = "duplicate key value violates unique constraint",
) -> IntegrityError:
    """Construct a PostgreSQL-shaped psycopg/SQLAlchemy IntegrityError."""

    class _FakeDiag:
        def __init__(self) -> None:
            self.sqlstate = "23505"
            self.constraint_name = constraint_name
            self.message_detail = detail

    class _FakeOrig(Exception):
        def __init__(self) -> None:
            super().__init__(f'duplicate key value violates unique constraint "{constraint_name}"')
            self.pgcode = "23505"
            self.sqlstate = "23505"
            self.constraint_name = constraint_name
            self.diag = _FakeDiag()

    return IntegrityError(
        statement="COMMIT",
        params={},
        orig=_FakeOrig(),
    )


class InMemoryDbSession:
    """In-memory AsyncSession test double for customer management operations."""

    def __init__(
        self,
        provisions: list[BillingCustomerProvision] | None = None,
        subscriptions: list[Subscription] | None = None,
        organizations: list[Organization] | None = None,
        reconciliations: list[PaymentReconciliation] | None = None,
        fail_commit_with_integrity_error: bool = False,
        fail_commit_with_exception: Exception | None = None,
    ) -> None:
        self.provisions: list[BillingCustomerProvision] = list(provisions or [])
        self.subscriptions: list[Subscription] = list(subscriptions or [])
        self.organizations: dict[uuid.UUID, Organization] = {
            o.id: o for o in (organizations or [])
        }
        self.reconciliations: list[PaymentReconciliation] = list(reconciliations or [])
        self.fail_commit_with_integrity_error = fail_commit_with_integrity_error
        self.fail_commit_with_exception = fail_commit_with_exception
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.uncommitted: list[Any] = []
        self._in_tx = False
        self._committed_state: dict[Any, dict[str, Any]] = {}
        self._save_snapshots()
        self.committed_count = 0
        self.rolled_back_count = 0
        self.session_scopes_opened = 0
        self.session_scopes_closed = 0

    def as_session_factory(self):
        """Return an AsyncSessionFactory yielding this mock session across sequential phases."""
        session = self

        class _MockSessionScope:
            async def __aenter__(self_scope) -> "InMemoryDbSession":
                session.session_scopes_opened += 1
                return session

            async def __aexit__(self_scope, exc_type: Any, exc: Any, tb: Any) -> bool:
                session.session_scopes_closed += 1
                return False

        def _factory() -> _MockSessionScope:
            return _MockSessionScope()

        return _factory

    def in_transaction(self) -> bool:
        return self._in_tx

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
        for r in self.reconciliations:
            self._committed_state[r] = {
                "organization_id": r.organization_id,
                "reason": r.reason,
                "status": r.status,
                "diagnostic_payload": copy.deepcopy(r.diagnostic_payload),
                "updated_at": getattr(r, "updated_at", None),
            }

    async def execute(self, stmt: Any) -> MockScalarResult:
        self._in_tx = True
        w_params = {}
        for w in getattr(stmt, "_where_criteria", ()):
            if (
                hasattr(w, "left")
                and hasattr(w, "right")
                and hasattr(w.left, "key")
                and getattr(w, "operator", operator.eq) is operator.eq
            ):
                w_params[w.left.key] = getattr(w.right, "value", None)

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

        if from_table == "organizations":
            lookup_org_id = w_params.get("id") or w_params.get("organization_id")
            return MockScalarResult(self.organizations.get(lookup_org_id))

        if from_table == "billing_customer_provisions" or (not from_table and not self.subscriptions and self.provisions):
            all_provs = list(self.provisions) + [
                o for o in self.uncommitted if isinstance(o, BillingCustomerProvision) and o not in self.provisions
            ]
            matched_prov = None
            for p in reversed(all_provs):
                if _matches_stmt_where(p, stmt):
                    matched_prov = p
                    break
            return MockScalarResult(matched_prov)

        if from_table == "payment_reconciliations":
            all_recs = list(self.reconciliations) + [
                o for o in self.uncommitted if isinstance(o, PaymentReconciliation) and o not in self.reconciliations
            ]
            matched_rec = None
            for r in reversed(all_recs):
                if _matches_stmt_where(r, stmt):
                    matched_rec = r
                    break
            return MockScalarResult(matched_rec)

        # Default to subscriptions table search
        matched_sub = None
        for s in reversed(self.subscriptions):
            if _matches_stmt_where(s, stmt):
                matched_sub = s
                break

        return MockScalarResult(matched_sub)

    def add(self, obj: Any) -> None:
        self._in_tx = True
        self.added.append(obj)
        self.uncommitted.append(obj)

    async def flush(self) -> None:
        self._in_tx = True

    async def delete(self, obj: Any) -> None:
        self._in_tx = True
        self.deleted.append(obj)
        if obj in self.provisions:
            self.provisions.remove(obj)
        if obj in self.subscriptions:
            self.subscriptions.remove(obj)
        if obj in self.reconciliations:
            self.reconciliations.remove(obj)

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
        self._in_tx = False
        self.committed_count += 1

    async def rollback(self) -> None:
        for obj in self.uncommitted:
            if obj in self.added:
                self.added.remove(obj)
            if obj in self.provisions:
                self.provisions.remove(obj)
            if obj in self.subscriptions:
                self.subscriptions.remove(obj)
            if obj in self.reconciliations:
                self.reconciliations.remove(obj)
        self.uncommitted.clear()

        # Revert in-memory object attributes to committed snapshot values
        for obj, snap in self._committed_state.items():
            for k, v in snap.items():
                if hasattr(obj, k):
                    setattr(obj, k, copy.deepcopy(v) if isinstance(v, (dict, list)) else v)

        self._in_tx = False
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


class ConstraintEnforcingDbSession(InMemoryDbSession):
    """Test double that enforces PostgreSQL-modeled unique/FK constraints and logs every TX commit/rollback.

    NOTE: Emulates PostgreSQL constraint behavior in-memory for unit tests; does not replace
    live PostgreSQL integration testing.
    """

    def __init__(
        self,
        provisions: list[BillingCustomerProvision] | None = None,
        subscriptions: list[Subscription] | None = None,
        organizations: list[Organization] | None = None,
        reconciliations: list[PaymentReconciliation] | None = None,
        reconciliation_commit_errors: list[Exception] | None = None,
        fail_commit_predicate: Callable[["ConstraintEnforcingDbSession", list[BillingCustomerProvision], list[PaymentReconciliation]], Exception | None] | None = None,
        generic_collision_error: bool = False,
    ) -> None:
        super().__init__(
            provisions=provisions,
            subscriptions=subscriptions,
            organizations=organizations,
            reconciliations=reconciliations,
        )
        self.tx_log: list[dict[str, Any]] = []
        self.reconciliation_commit_errors: list[Exception] = list(reconciliation_commit_errors or [])
        self.fail_commit_predicate = fail_commit_predicate
        self.generic_collision_error = generic_collision_error

    def _dirty_provisions(self) -> list[BillingCustomerProvision]:
        dirty: list[BillingCustomerProvision] = [
            o for o in self.uncommitted if isinstance(o, BillingCustomerProvision)
        ]
        for p in self.provisions:
            if p in dirty:
                continue
            snap = self._committed_state.get(p)
            if snap is None or any(
                getattr(p, k) != snap[k]
                for k in ("provider_customer_id", "status", "claim_operation_id", "lease_expires_at", "last_error_code")
            ):
                dirty.append(p)
        return dirty

    def _dirty_reconciliations(self) -> list[PaymentReconciliation]:
        dirty: list[PaymentReconciliation] = [
            o for o in self.uncommitted if isinstance(o, PaymentReconciliation)
        ]
        for r in self.reconciliations:
            if r in dirty:
                continue
            snap = self._committed_state.get(r)
            if snap is None or r.diagnostic_payload != snap["diagnostic_payload"] or r.reason != snap["reason"]:
                dirty.append(r)
        return dirty

    async def commit(self) -> None:
        dirty_provs = self._dirty_provisions()
        dirty_recs = self._dirty_reconciliations()
        dirty_cust_ids = [p.provider_customer_id for p in dirty_provs]

        if self.fail_commit_predicate is not None:
            pred_err = self.fail_commit_predicate(self, dirty_provs, dirty_recs)
            if pred_err is not None:
                self.tx_log.append({
                    "action": "commit_failed",
                    "dirty_customer_ids": dirty_cust_ids,
                    "reconciliations": len(dirty_recs),
                    "error": f"{type(pred_err).__name__}: {pred_err}",
                })
                raise pred_err

        if dirty_recs and self.reconciliation_commit_errors:
            rec_err = self.reconciliation_commit_errors.pop(0)
            self.tx_log.append({
                "action": "commit_failed",
                "dirty_customer_ids": dirty_cust_ids,
                "reconciliations": len(dirty_recs),
                "error": f"{type(rec_err).__name__}: {rec_err}",
            })
            raise rec_err

        # Enforce BillingCustomerProvision constraints on combined set
        all_provs = list(self.provisions) + [o for o in dirty_provs if o not in self.provisions]
        seen_org_keys: set[tuple[uuid.UUID, str, str, str]] = set()
        seen_customer_keys: set[tuple[str, str, str, str]] = set()
        for p in all_provs:
            org_key = (p.organization_id, p.provider, p.merchant_account_id, p.environment)
            if org_key in seen_org_keys:
                err = _make_pg_unique_error("uq_billing_customer_provisions_org_provider")
                self.tx_log.append({
                    "action": "commit_failed",
                    "dirty_customer_ids": dirty_cust_ids,
                    "reconciliations": len(dirty_recs),
                    "error": PROVIDER_CUSTOMER_UNIQUE_INDEX,
                })
                raise err
            seen_org_keys.add(org_key)

            # Partial unique index: WHERE provider_customer_id IS NOT NULL
            if p.provider_customer_id is not None:
                cust_key = (p.provider, p.merchant_account_id, p.environment, p.provider_customer_id)
                if cust_key in seen_customer_keys:
                    if self.generic_collision_error:
                        err = IntegrityError("duplicate key value violates unique constraint", params={}, orig=Exception("duplicate"))
                    else:
                        err = _make_pg_unique_error(
                            PROVIDER_CUSTOMER_UNIQUE_INDEX,
                            f"Key (provider, merchant_account_id, environment, provider_customer_id)="
                            f"({p.provider}, {p.merchant_account_id}, {p.environment}, {p.provider_customer_id}) already exists.",
                        )
                    self.tx_log.append({
                        "action": "commit_failed",
                        "dirty_customer_ids": dirty_cust_ids,
                        "reconciliations": len(dirty_recs),
                        "error": str(err),
                    })
                    raise err
                seen_customer_keys.add(cust_key)

        # Enforce PaymentReconciliation constraints
        all_recs = list(self.reconciliations) + [o for o in dirty_recs if o not in self.reconciliations]
        seen_case_keys: set[tuple[str, str, str, str]] = set()
        for r in all_recs:
            if not r.case_key or not isinstance(r.case_key, str) or len(r.case_key) > 255:
                raise IntegrityError("invalid PaymentReconciliation case_key", params={}, orig=Exception())
            if r.organization_id is not None and r.organization_id not in self.organizations:
                raise IntegrityError(
                    "fk_payment_reconciliations_organization_id",
                    params={},
                    orig=Exception("Key (organization_id) is not present in table organizations"),
                )
            ck = (r.provider, r.merchant_account_id, r.environment, r.case_key)
            if ck in seen_case_keys:
                raise _make_pg_unique_error("uq_payment_reconciliations_case_key")
            seen_case_keys.add(ck)

        await super().commit()
        self.tx_log.append({
            "action": "commit",
            "dirty_customer_ids": dirty_cust_ids,
            "provisions_statuses": [p.status.value for p in dirty_provs],
            "reconciliations": [r.case_key for r in dirty_recs],
        })

    async def rollback(self) -> None:
        await super().rollback()
        self.tx_log.append({"action": "rollback"})


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

    db = InMemoryDbSession(provisions=[existing_provision], organizations=[org])

    mock_client = RazorpayClient(key_id="rzp_test_123", key_secret="secret")
    set_razorpay_client_for_testing(mock_client)

    customer_id = await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
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

    db = InMemoryDbSession(subscriptions=[stripe_sub], organizations=[org])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            assert kwargs["name"] == "Legacy Stripe Corp"
            assert kwargs["notes"]["organization_id"] == str(org_id)
            return {"id": "cust_new_rzp_222"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
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
    db = InMemoryDbSession(organizations=[org])

    captured_args = {}

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal captured_args
            captured_args = kwargs
            return {"id": "cust_rzp_created_333"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(organization_id=org.id, user_email="owner@brandnew.com", organization_name=org.name, session_factory=db.as_session_factory())
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

    db = InMemoryDbSession(subscriptions=[active_sub], organizations=[org])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_active_bound_111"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
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
    db = InMemoryDbSession(organizations=[org])

    class FailingClient400:
        async def create_customer(self, **kwargs):
            raise RazorpayAPIError("Invalid customer name format", status_code=400)

    set_razorpay_client_for_testing(FailingClient400())

    with pytest.raises(RazorpayCustomerError, match="creation failed"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "400"
    assert db.provisions[0].claim_operation_id is None


@pytest.mark.asyncio
async def test_http_failure_401_unauthorized_cleans_claim():
    """HTTP 401 Unauthorized is a definite credentials failure: transitions claim to failed."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Auth Error Org")
    db = InMemoryDbSession(organizations=[org])

    class FailingClient401:
        async def create_customer(self, **kwargs):
            raise RazorpayAuthError("Invalid key_id or key_secret")

    set_razorpay_client_for_testing(FailingClient401())

    with pytest.raises(RazorpayCustomerError, match="authentication error"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "401"


@pytest.mark.asyncio
async def test_http_failure_408_request_timeout_marks_ambiguous():
    """HTTP 408 Request Timeout is AMBIGUOUS: reverse proxy timed out. Marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Timeout 408 Org")
    db = InMemoryDbSession(organizations=[org])

    class FailingClient408:
        async def create_customer(self, **kwargs):
            raise RazorpayAPIError("Request Timeout from proxy", status_code=408)

    set_razorpay_client_for_testing(FailingClient408())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 408 request timeout"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "408"


@pytest.mark.asyncio
async def test_http_failure_429_rate_limit_conservatively_marks_ambiguous():
    """HTTP 429 Rate Limit is reassessed conservatively: marks ambiguous and preserves claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Rate Limit Org")
    db = InMemoryDbSession(organizations=[org])

    class FailingClient429:
        async def create_customer(self, **kwargs):
            raise RazorpayRateLimitError("Rate limit exceeded")

    set_razorpay_client_for_testing(FailingClient429())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="rate limit exceeded"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    # Preserved in ambiguous state for controlled recovery, NEVER cleaned up
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "429"


@pytest.mark.asyncio
async def test_http_failure_500_internal_error_marks_ambiguous():
    """HTTP 500 Internal Error is AMBIGUOUS: marks ambiguous, no blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Server 500 Org")
    db = InMemoryDbSession(organizations=[org])

    class FailingClient500:
        async def create_customer(self, **kwargs):
            raise RazorpayServer5xxError("Internal database error on server", status_code=500)

    set_razorpay_client_for_testing(FailingClient500())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 500"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "500"


@pytest.mark.asyncio
async def test_http_failure_502_bad_gateway_marks_ambiguous():
    """HTTP 502 Bad Gateway is AMBIGUOUS: marks ambiguous, no blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Gateway 502 Org")
    db = InMemoryDbSession(organizations=[org])

    class FailingClient502:
        async def create_customer(self, **kwargs):
            raise RazorpayServer5xxError("Bad Gateway from reverse proxy", status_code=502)

    set_razorpay_client_for_testing(FailingClient502())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="HTTP 502"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "502"


@pytest.mark.asyncio
async def test_http_failure_read_timeout_post_dispatch_marks_ambiguous():
    """Read timeout after request dispatch is AMBIGUOUS: marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Read Timeout Org")
    db = InMemoryDbSession(organizations=[org])

    class ReadTimeoutClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("Read timed out waiting for server", is_pre_dispatch=False)

    set_razorpay_client_for_testing(ReadTimeoutClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Ambiguous network timeout"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous
    assert db.provisions[0].last_error_code == "post_dispatch_network_error"


@pytest.mark.asyncio
async def test_http_failure_connection_reset_post_dispatch_marks_ambiguous():
    """Connection reset after request dispatch is AMBIGUOUS: marks ambiguous, prohibits blind retry."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Conn Reset Org")
    db = InMemoryDbSession(organizations=[org])

    class ConnResetClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("Connection reset by peer after transmission", is_pre_dispatch=False)

    set_razorpay_client_for_testing(ConnResetClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Ambiguous network timeout/reset"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.ambiguous


@pytest.mark.asyncio
async def test_http_failure_pre_dispatch_connect_failure_cleans_claim():
    """Failure before request dispatch (e.g. ConnectTimeout / ConnectError) safely cleans claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Connect Timeout Org")
    db = InMemoryDbSession(organizations=[org])

    class PreDispatchTimeoutClient:
        async def create_customer(self, **kwargs):
            raise RazorpayNetworkError("TCP connection timed out before dispatch", is_pre_dispatch=True)

    set_razorpay_client_for_testing(PreDispatchTimeoutClient())

    with pytest.raises(RazorpayCustomerError, match="connection failed before dispatch"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert len(db.provisions) == 1
    assert db.provisions[0].status == CustomerProvisionStatus.failed
    assert db.provisions[0].last_error_code == "pre_dispatch_network_error"


@pytest.mark.asyncio
async def test_malformed_success_response_marks_ambiguous_and_does_not_clean():
    """HTTP 200/201 with missing or null customer ID must transition to ambiguous and NEVER clean claim."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Malformed Success Org")
    db = InMemoryDbSession(organizations=[org])

    class MalformedSuccessClient:
        async def create_customer(self, **kwargs):
            # Remote gateway returned HTTP 200 OK, but payload has missing 'id'
            return {"status": "created", "id": None}

    set_razorpay_client_for_testing(MalformedSuccessClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="missing or malformed customer ID"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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
    db = InMemoryDbSession(organizations=[org])

    class CrashingClient:
        async def create_customer(self, **kwargs):
            raise RuntimeError("Unexpected library assertion crash")

    set_razorpay_client_for_testing(CrashingClient())

    with pytest.raises(RazorpayAmbiguousNetworkError, match="Unexpected error communicating with Razorpay"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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

    db = CommitFailingDbSession(organizations=[org])

    class SuccessfulRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_remote_success_789"}

    set_razorpay_client_for_testing(SuccessfulRazorpayClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert dispatched is False


@pytest.mark.asyncio
async def test_remote_success_followed_by_catastrophic_database_failure_leaves_pending():
    """If both primary resolution commit and ambiguous fallback commit fail, pending claim remains intact."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Catastrophic DB Org")

    class CatastrophicDbSession(InMemoryDbSession):
        def __init__(self, organizations: list[Organization] | None = None):
            super().__init__(organizations=organizations)
            self.phase2_done = False

        async def commit(self):
            if not self.phase2_done:
                self.phase2_done = True
                await super().commit()
                return
            # Fail all subsequent commits (both resolution and fallback ambiguous update)
            raise OperationalError("Total PostgreSQL database outage", params={}, orig=Exception())

    db = CatastrophicDbSession(organizations=[org])

    class SuccessfulRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_catastrophic_999"}

    set_razorpay_client_for_testing(SuccessfulRazorpayClient())

    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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

    db = InMemoryDbSession(organizations=[org])

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
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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

    db = InMemoryDbSession(organizations=[org])
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
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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
    org = Organization(id=org_id, name="Claim B Persistence Org")
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
    db = InMemoryDbSession(provisions=[prov], organizations=[org])

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
    org = Organization(id=org_id, name="Claim B Ambiguous Org")
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
    db = InMemoryDbSession(provisions=[prov], organizations=[org])

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
    org = Organization(id=org_id, name="Claim B Cleanup Org")
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
    db = InMemoryDbSession(provisions=[prov], organizations=[org])

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

    db = InMemoryDbSession(provisions=[stale_provision], organizations=[org])

    dispatched = False

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal dispatched
            dispatched = True
            return {"id": "cust_should_not_be_created"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="lease expired"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

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

    db = InMemoryDbSession(organizations=[org1, org2])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            if kwargs["notes"]["organization_id"] == str(org1_id):
                return {"id": "cust_org_111"}
            return {"id": "cust_org_222"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    cust1 = await get_or_create_razorpay_customer(organization_id=org1.id, organization_name=org1.name, session_factory=db.as_session_factory())
    cust2 = await get_or_create_razorpay_customer(organization_id=org2.id, organization_name=org2.name, session_factory=db.as_session_factory())

    assert cust1 == "cust_org_111"
    assert cust2 == "cust_org_222"
    assert len(db.provisions) == 2
    assert len(db.subscriptions) == 0


@pytest.mark.asyncio
async def test_no_email_fabrication_when_email_omitted():
    """When user_email is omitted or empty, client receives None (no synthetic email generated)."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Email-less Org")
    db = InMemoryDbSession(organizations=[org])

    captured_payload = {}

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            nonlocal captured_payload
            captured_payload = kwargs
            return {"id": "cust_no_email_456"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    await get_or_create_razorpay_customer(organization_id=org.id, user_email=None, organization_name=org.name, session_factory=db.as_session_factory())
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

    db = InMemoryDbSession(provisions=[live_prov], organizations=[org])

    class MockRazorpayClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_test_fresh_444"}

    set_razorpay_client_for_testing(MockRazorpayClient())

    customer_id = await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
    assert customer_id == "cust_test_fresh_444"
    assert customer_id != "cust_live_should_not_leak"


@pytest.mark.asyncio
async def test_razorpay_customer_disabled_guard(monkeypatch):
    """If razorpay_enabled is False, raises RazorpayBillingDisabledError."""
    monkeypatch.setattr(settings, "razorpay_enabled", False)
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Disabled Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayBillingDisabledError, match="Razorpay billing is currently disabled"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


# ============================================================================
# Scope Safety and Fail-Closed Validation Tests (Task 3)
# ============================================================================


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_on_invalid_environment(monkeypatch):
    """If razorpay_environment is invalid, fail closed with RazorpayConfigurationError."""
    monkeypatch.setattr(settings, "razorpay_environment", "staging")
    org = Organization(id=uuid.uuid4(), name="Invalid Env Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="Invalid Razorpay environment"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_on_empty_merchant_account(monkeypatch):
    """If razorpay_merchant_account_id is missing or whitespace, fail closed."""
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "   ")
    org = Organization(id=uuid.uuid4(), name="Empty Merchant Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="merchant account ID must be explicitly configured"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_production_with_default_merchant(monkeypatch):
    """In production, merchant_account_id cannot be 'default' or 'placeholder'."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "live")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "default")
    org = Organization(id=uuid.uuid4(), name="Prod Default Merchant Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_production_with_test_environment(monkeypatch):
    """In production, test environment is strictly prohibited."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "razorpay_environment", "test")
    monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_real_prod_123")
    org = Organization(id=uuid.uuid4(), name="Prod Test Env Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


@pytest.mark.asyncio
async def test_razorpay_scope_fails_closed_in_dev_with_live_environment(monkeypatch):
    """In development/test, live environment is strictly prohibited."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "razorpay_environment", "live")
    org = Organization(id=uuid.uuid4(), name="Dev Live Env Org")
    db = InMemoryDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="Non-production RAZORPAY_ENVIRONMENT must be 'test'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())


# ============================================================================
# C-1 Phase A Isolation & Concurrency Tests
# ============================================================================


@pytest.mark.asyncio
async def test_phase_a_reuse_existing_commits_read_transaction_cleanly():
    """Reusing a resolved customer provision must run in an owned service session and leave caller's read TX untouched."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Reuse Commit Org")
    existing_provision = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_existing_clean_tx",
        status=CustomerProvisionStatus.resolved,
    )
    caller_db = InMemoryDbSession(provisions=[existing_provision], organizations=[org])
    service_db = InMemoryDbSession(provisions=[existing_provision], organizations=[org])

    # Simulate prior caller read transaction open before entering customer provisioning
    await caller_db.execute(
        __import__("sqlalchemy").select(Organization).where(Organization.id == org_id)
    )
    assert caller_db.in_transaction() is True

    customer_id = await get_or_create_razorpay_customer(
        organization_id=org.id,
        organization_name=org.name,
        session_factory=service_db.as_session_factory(),
    )
    assert customer_id == "cust_existing_clean_tx"
    # Caller's open read transaction must remain completely untouched
    assert caller_db.in_transaction() is True
    assert caller_db.committed_count == 0
    assert caller_db.rolled_back_count == 0
    assert caller_db.session_scopes_opened == 0
    # Owned service session opened once, committed cleanly, and exited cleanly
    assert service_db.in_transaction() is False
    assert service_db.committed_count == 1
    assert service_db.session_scopes_opened == 1
    assert service_db.session_scopes_closed == 1


@pytest.mark.asyncio
async def test_phase_a_releases_all_transactions_before_http_and_handles_org_disappeared_mid_http():
    """If the organization becomes unavailable while HTTP create_customer runs, Phase A still closes
    cleanly without FK failures and records an unattached PaymentReconciliation carrying provider_customer_id."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Vanishing Org")
    db = InMemoryDbSession(organizations=[org])

    http_tx_states: list[bool] = []

    class OrgRemovingClient:
        async def create_customer(self, **kwargs):
            http_tx_states.append(db.in_transaction())
            assert db.session_scopes_opened == db.session_scopes_closed
            # Simulate org deletion/unavailability between Phase A TX1 commit and TX2 resolution
            db.organizations.pop(org_id, None)
            return {"id": "cust_orphaned_remote_777"}

    set_razorpay_client_for_testing(OrgRemovingClient())

    with pytest.raises(RazorpayCustomerError, match="became unavailable"):
        await get_or_create_razorpay_customer(
            organization_id=org.id,
            organization_name=org.name,
            session_factory=db.as_session_factory(),
        )

    assert http_tx_states == [False]
    assert db.in_transaction() is False
    assert db.session_scopes_opened == db.session_scopes_closed == 2
    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.organization_id is None
    assert recon.reason == "organization_unavailable_after_customer_created"
    assert recon.diagnostic_payload["organization_id"] == str(org_id)
    assert recon.diagnostic_payload["provider_customer_id"] == "cust_orphaned_remote_777"


@pytest.mark.asyncio
async def test_phase_a_concurrent_provisioning_serializes_with_single_remote_call():
    """Two concurrent sessions provisioning for the same org serialize via Organization FOR UPDATE
    and produce at most one remote create_customer call."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Concurrent Cust Org")

    store_lock = asyncio.Lock()
    shared_orgs = {org_id: org}
    shared_provisions: dict[tuple[uuid.UUID, str, str, str], dict[str, Any]] = {}
    held_locks: set[tuple[str, Any]] = set()

    class DualCustomerSession:
        def __init__(self, name: str) -> None:
            self.name = name
            self._in_tx = False
            self._locked = False
            self._identity_provs: dict[tuple[uuid.UUID, str, str, str], BillingCustomerProvision] = {}
            self._dirty_adds: list[BillingCustomerProvision] = []

        def in_transaction(self) -> bool:
            return self._in_tx

        async def execute(self, stmt: Any) -> MockScalarResult:
            self._in_tx = True
            for_update = getattr(stmt, "_for_update_arg", None) is not None
            w_params: dict[str, Any] = {}
            for w in getattr(stmt, "_where_criteria", ()):
                if hasattr(w, "left") and hasattr(w, "right") and hasattr(w.left, "key") and hasattr(w.right, "value"):
                    w_params[w.left.key] = w.right.value
            froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
            from_table = next((getattr(f, "name", "") for f in froms_list if getattr(f, "name", "")), "")

            if from_table == "organizations":
                if for_update and not self._locked:
                    await store_lock.acquire()
                    self._locked = True
                    held_locks.add((self.name, "organizations"))
                return MockScalarResult(shared_orgs.get(w_params.get("id")))

            if from_table == "billing_customer_provisions":
                key = (
                    w_params["organization_id"],
                    w_params["provider"],
                    w_params["merchant_account_id"],
                    w_params["environment"],
                )
                row = shared_provisions.get(key)
                if row is None:
                    return MockScalarResult(None)
                if key not in self._identity_provs:
                    self._identity_provs[key] = BillingCustomerProvision(**copy.deepcopy(row))
                else:
                    for k, v in copy.deepcopy(row).items():
                        setattr(self._identity_provs[key], k, v)
                return MockScalarResult(self._identity_provs[key])
            return MockScalarResult(None)

        def add(self, obj: Any) -> None:
            self._in_tx = True
            if isinstance(obj, BillingCustomerProvision):
                self._dirty_adds.append(obj)

        async def commit(self) -> None:
            for obj in self._dirty_adds:
                key = (obj.organization_id, obj.provider, obj.merchant_account_id, obj.environment)
                if key in shared_provisions:
                    raise IntegrityError("duplicate prov", params={}, orig=Exception())
                shared_provisions[key] = {
                    "id": obj.id,
                    "organization_id": obj.organization_id,
                    "provider": obj.provider,
                    "merchant_account_id": obj.merchant_account_id,
                    "environment": obj.environment,
                    "provider_customer_id": obj.provider_customer_id,
                    "status": obj.status,
                    "claim_operation_id": obj.claim_operation_id,
                    "lease_expires_at": obj.lease_expires_at,
                    "last_error_code": obj.last_error_code,
                }
                self._identity_provs[key] = obj
            for key, inst in self._identity_provs.items():
                if key in shared_provisions:
                    shared_provisions[key].update({
                        "provider_customer_id": inst.provider_customer_id,
                        "status": inst.status,
                        "claim_operation_id": inst.claim_operation_id,
                        "lease_expires_at": inst.lease_expires_at,
                        "last_error_code": inst.last_error_code,
                    })
            self._dirty_adds.clear()
            self._in_tx = False
            if self._locked:
                self._locked = False
                held_locks.discard((self.name, "organizations"))
                store_lock.release()

        async def rollback(self) -> None:
            self._dirty_adds.clear()
            self._identity_provs.clear()
            self._in_tx = False
            if self._locked:
                self._locked = False
                held_locks.discard((self.name, "organizations"))
                store_lock.release()

    created_sessions: list[DualCustomerSession] = []

    def make_factory(worker_name: str):
        def _factory():
            sess = DualCustomerSession(worker_name)
            created_sessions.append(sess)

            class _Scope:
                async def __aenter__(self) -> DualCustomerSession:
                    return sess

                async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                    if sess.in_transaction():
                        await sess.rollback()
                    return False

            return _Scope()

        return _factory

    factory_a = make_factory("A")
    factory_b = make_factory("B")

    worker_a_in_http = asyncio.Event()
    allow_worker_a_finish = asyncio.Event()
    create_calls: list[str] = []

    class CoordinatedClient:
        async def create_customer(self, **kwargs):
            assert all(not s.in_transaction() for s in created_sessions)
            assert len(held_locks) == 0
            create_calls.append(kwargs["notes"]["organization_id"])
            worker_a_in_http.set()
            await asyncio.wait_for(allow_worker_a_finish.wait(), timeout=2.0)
            return {"id": "cust_concurrent_winner_1"}

    set_razorpay_client_for_testing(CoordinatedClient())

    async def _run_a():
        return await get_or_create_razorpay_customer(
            organization_id=org.id,
            organization_name=org.name,
            session_factory=factory_a,
        )

    async def _run_b():
        await asyncio.wait_for(worker_a_in_http.wait(), timeout=2.0)
        try:
            return await get_or_create_razorpay_customer(
                organization_id=org.id,
                organization_name=org.name,
                session_factory=factory_b,
            )
        finally:
            allow_worker_a_finish.set()

    res_a, res_b = await asyncio.gather(_run_a(), _run_b(), return_exceptions=True)

    assert res_a == "cust_concurrent_winner_1"
    assert isinstance(res_b, RazorpayCustomerProvisioningInProgressError)
    assert len(create_calls) == 1
    assert len(shared_provisions) == 1
    row = next(iter(shared_provisions.values()))
    assert row["status"] == CustomerProvisionStatus.resolved
    assert row["provider_customer_id"] == "cust_concurrent_winner_1"
    assert all(not s.in_transaction() for s in created_sessions)
    assert len(held_locks) == 0


# ============================================================================
# Step 5.2D.3.3B.1E-B — D-1 Corrupted Resolved Mapping & D-6 Cross-Org Collision Tests
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corrupted_customer_id", "expected_anomaly_type"),
    [
        (None, "null_customer_id"),
        ("", "empty_customer_id"),
        ("   \t\n  ", "whitespace_customer_id"),
    ],
)
async def test_d1_corrupted_resolved_provision_fails_closed(
    corrupted_customer_id: str | None,
    expected_anomaly_type: str,
):
    """D-1: A resolved provision with null, empty, or whitespace provider_customer_id
    must fail closed without calling Razorpay, transition to ambiguous (ID=None),
    and record a durable PaymentReconciliation anomaly case.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Corrupted Resolved Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=corrupted_customer_id,
        status=CustomerProvisionStatus.resolved,
        claim_operation_id="stale_op_should_clear",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        last_error_code=None,
    )
    db = ConstraintEnforcingDbSession(provisions=[prov], organizations=[org])

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_duplicate_should_never_be_created"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="corrupted 'resolved'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert remote_calls == 0
    assert db.in_transaction() is False
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id is None
    assert prov.lease_expires_at is None
    assert prov.last_error_code == "resolved_missing_customer_id"

    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.case_key == f"customer_provision:anomaly:resolved_missing_id:{org_id}"
    assert recon.organization_id == org_id
    assert recon.reason == "resolved_missing_customer_id"
    assert recon.status == ReconciliationStatus.pending_review
    assert recon.diagnostic_payload["latest"]["anomaly_type"] == expected_anomaly_type
    assert recon.diagnostic_payload["latest"]["provision_id"] == str(prov.id)
    assert len(recon.diagnostic_payload["history"]) == 1


@pytest.mark.asyncio
async def test_d1_repeated_requests_after_corrupted_resolved_fail_closed_without_duplicate_recon():
    """D-1: Subsequent retries against ambiguous or repeatedly corrupted resolved rows fail closed
    without remote calls or duplicate PaymentReconciliation cases.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Repeated D1 Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[prov], organizations=[org])

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    # Request 1: detects corrupted resolved -> marks ambiguous & stages reconciliation
    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="corrupted 'resolved'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    # Request 2: already ambiguous -> fails closed immediately
    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="ambiguous state"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    # Simulate external/script re-corruption back to resolved with whitespace ID
    prov.status = CustomerProvisionStatus.resolved
    prov.provider_customer_id = "   "
    db._save_snapshots()

    # Request 3: fails closed again and appends history to SAME reconciliation case
    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="corrupted 'resolved'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert remote_calls == 0
    assert db.in_transaction() is False
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert len(db.reconciliations) == 1
    assert len(db.reconciliations[0].diagnostic_payload["history"]) == 2
    assert db.reconciliations[0].diagnostic_payload["latest"]["anomaly_type"] == "whitespace_customer_id"


@pytest.mark.asyncio
async def test_d1_valid_resolved_mapping_still_returns_cleanly_with_zero_http():
    """D-1 guard must allow valid non-empty resolved customer IDs without creating anomalies."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Valid Resolved Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="  cust_valid_trimmed_123  ",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[prov], organizations=[org])

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    cid = await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
    assert cid == "cust_valid_trimmed_123"
    assert remote_calls == 0
    assert len(db.reconciliations) == 0
    assert prov.status == CustomerProvisionStatus.resolved
    assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_d1_tx1_integrity_error_reload_corrupted_resolved_fails_closed():
    """D-1: If TX1 claim insert hits IntegrityError and the concurrently visible resolved record
    has a corrupted empty provider_customer_id, worker fails closed and never invokes Razorpay.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="TX1 Concurrent Corrupt Org")
    db = ConstraintEnforcingDbSession(organizations=[org])

    corrupted_concurrent_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="   ",
        status=CustomerProvisionStatus.resolved,
    )

    injected = False

    def inject_tx1_conflict(session, dirty_provs, dirty_recs):
        nonlocal injected
        if not injected and any(p.status == CustomerProvisionStatus.pending for p in dirty_provs):
            injected = True
            session.provisions.append(corrupted_concurrent_prov)
            session._save_snapshots()
            return _make_pg_unique_error("uq_billing_customer_provisions_org_provider")
        return None

    db.fail_commit_predicate = inject_tx1_conflict

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="corrupted 'resolved'"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert remote_calls == 0
    assert db.in_transaction() is False
    assert corrupted_concurrent_prov.status == CustomerProvisionStatus.ambiguous
    assert corrupted_concurrent_prov.provider_customer_id is None
    assert corrupted_concurrent_prov.last_error_code == "resolved_missing_customer_id"
    assert len(db.reconciliations) == 1


@pytest.mark.asyncio
async def test_d1_reconciliation_persist_failure_surfaces_cause_and_rolls_back():
    """D-1 / E-1: If staging/committing reconciliation for a corrupted resolved record fails,
    roll back cleanly, execute an independent fallback TX that persists ambiguous state
    (ID=None, diagnostic code preserved), surface the reconciliation failure, and keep session clean.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="D1 Persist Fail Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=None,
        status=CustomerProvisionStatus.resolved,
        claim_operation_id="stale_claim_to_clear",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    db = ConstraintEnforcingDbSession(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("reconciliations write error", params={}, orig=Exception())],
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="reconciliation persistence failed") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.original_error is err.__cause__
    assert err.recovery_error is None
    assert err.fallback_committed is True
    assert db.in_transaction() is False
    assert len(db.reconciliations) == 0
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id is None
    assert prov.lease_expires_at is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE
    assert [entry["action"] for entry in db.tx_log] == ["commit_failed", "rollback", "commit"]


@pytest.mark.asyncio
async def test_d6_cross_org_collision_recovers_durably_without_repeating_conflicting_insert():
    """D-6 (Constraint-Faithful Mock): Cross-org collision on uq_billing_customer_provisions_provider_customer
    rolls back TX_A2, never attempts to write the conflicting provider_customer_id again,
    preserves the original owner org intact, marks affected org ambiguous (ID=None),
    and persists a durable PaymentReconciliation case.
    """
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Original Owner Org A")
    org_b = Organization(id=org_b_id, name="Colliding Affected Org B")

    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_shared_collision_999",
        status=CustomerProvisionStatus.resolved,
        claim_operation_id=None,
        lease_expires_at=None,
        last_error_code=None,
    )
    db = ConstraintEnforcingDbSession(provisions=[owner_prov], organizations=[org_a, org_b])

    class CollidingRazorpayClient:
        async def create_customer(self, **kwargs):
            assert db.in_transaction() is False
            return {"id": "cust_shared_collision_999"}

    set_razorpay_client_for_testing(CollidingRazorpayClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.customer_id == "cust_shared_collision_999"
    assert isinstance(err.original_error, IntegrityError)
    assert err.__cause__ is err.original_error
    assert err.recovery_error is None
    assert db.in_transaction() is False

    # Proof of transaction trace: TX1 commit -> TX2 commit_failed -> rollback -> Recovery TX commit with None ID!
    assert [entry["action"] for entry in db.tx_log] == [
        "commit",
        "commit_failed",
        "rollback",
        "commit",
    ]
    assert db.tx_log[1]["dirty_customer_ids"] == ["cust_shared_collision_999"]
    assert PROVIDER_CUSTOMER_UNIQUE_INDEX in db.tx_log[1]["error"]
    assert db.tx_log[3]["dirty_customer_ids"] == [None]
    assert db.tx_log[3]["provisions_statuses"] == ["ambiguous"]
    assert db.tx_log[3]["reconciliations"] == [
        f"customer_provision:collision:{org_b_id}:cust_shared_collision_999"
    ]

    # Original owner (Org A) completely unchanged
    assert owner_prov.status == CustomerProvisionStatus.resolved
    assert owner_prov.provider_customer_id == "cust_shared_collision_999"
    assert owner_prov.last_error_code is None

    # Affected Org B provision fails closed without holding the conflicting customer ID
    org_b_prov = next(p for p in db.provisions if p.organization_id == org_b_id)
    assert org_b_prov.status == CustomerProvisionStatus.ambiguous
    assert org_b_prov.provider_customer_id is None
    assert org_b_prov.claim_operation_id is None
    assert org_b_prov.lease_expires_at is None
    assert org_b_prov.last_error_code == "customer_id_org_collision"

    # Durable PaymentReconciliation recorded for Org B
    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.case_key == f"customer_provision:collision:{org_b_id}:cust_shared_collision_999"
    assert recon.organization_id == org_b_id
    assert recon.reason == "customer_id_org_collision"
    assert recon.status == ReconciliationStatus.pending_review
    latest = recon.diagnostic_payload["latest"]
    assert latest["affected_organization_id"] == str(org_b_id)
    assert latest["conflicting_organization_id"] == str(org_a_id)
    assert latest["conflicting_provision_id"] == str(owner_prov.id)
    assert latest["provider_customer_id"] == "cust_shared_collision_999"
    assert latest["original_error_details"]["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX
    assert latest["original_error_details"]["pgcode"] == "23505"


@pytest.mark.asyncio
async def test_d6_collision_without_constraint_name_still_detected_via_owner_lookup():
    """D-6: Even if IntegrityError text lacks the constraint name, cross-org owner lookup
    detects the conflicting organization using organization_id != affected_org_id.
    """
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner A")
    org_b = Organization(id=org_b_id, name="Affected B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_generic_err_888",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(
        provisions=[owner_prov],
        organizations=[org_a, org_b],
        generic_collision_error=True,
    )

    class CollidingClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_generic_err_888"}

    set_razorpay_client_for_testing(CollidingClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    assert exc_info.value.recovery_error is None
    assert owner_prov.status == CustomerProvisionStatus.resolved
    assert owner_prov.provider_customer_id == "cust_generic_err_888"

    org_b_prov = next(p for p in db.provisions if p.organization_id == org_b_id)
    assert org_b_prov.status == CustomerProvisionStatus.ambiguous
    assert org_b_prov.provider_customer_id is None
    assert org_b_prov.last_error_code == "customer_id_org_collision"
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].diagnostic_payload["latest"]["conflicting_organization_id"] == str(org_a_id)
    assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_d6_repeated_collision_updates_same_reconciliation_case_idempotently():
    """D-6: Repeated cross-org collision for the same (org_b, customer_id) updates the existing
    PaymentReconciliation row idempotently and appends to diagnostic history without duplicates.
    """
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner A")
    org_b = Organization(id=org_b_id, name="Repeat Collider B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_repeat_collision_555",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[owner_prov], organizations=[org_a, org_b])

    class CollidingClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_repeat_collision_555"}

    set_razorpay_client_for_testing(CollidingClient())

    # First collision
    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    # Simulate operator resetting org_b status to 'failed' so a retry executes against Razorpay
    org_b_prov = next(p for p in db.provisions if p.organization_id == org_b_id)
    org_b_prov.status = CustomerProvisionStatus.failed
    org_b_prov.last_error_code = None
    db._save_snapshots()

    # Second collision on same customer ID
    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    assert len(db.reconciliations) == 1
    recon = db.reconciliations[0]
    assert recon.case_key == f"customer_provision:collision:{org_b_id}:cust_repeat_collision_555"
    assert len(recon.diagnostic_payload["history"]) == 2
    assert org_b_prov.status == CustomerProvisionStatus.ambiguous
    assert org_b_prov.provider_customer_id is None
    assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_d6_reconciliation_failure_transitions_out_of_pending_and_surfaces_both_errors():
    """D-6: When persisting PaymentReconciliation during collision recovery fails, the failure is
    surfaced clearly alongside original_error, and a fallback state transition ensures the
    affected provision is marked ambiguous (not left pending silently).
    """
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner A")
    org_b = Organization(id=org_b_id, name="Recon Fail B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_shared_fail_recon",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(
        provisions=[owner_prov],
        organizations=[org_a, org_b],
        reconciliation_commit_errors=[OperationalError("payment_reconciliations write error", params={}, orig=Exception())],
    )

    class CollidingClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_shared_fail_recon"}

    set_razorpay_client_for_testing(CollidingClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.customer_id == "cust_shared_fail_recon"
    assert isinstance(err.original_error, IntegrityError)
    assert isinstance(err.recovery_error, OperationalError)
    assert err.__cause__ is err.recovery_error
    assert db.in_transaction() is False

    # Critical: Org B claim was transitioned to ambiguous with no conflicting customer_id, NOT left pending!
    org_b_prov = next(p for p in db.provisions if p.organization_id == org_b_id)
    assert org_b_prov.status == CustomerProvisionStatus.ambiguous
    assert org_b_prov.provider_customer_id is None
    assert org_b_prov.claim_operation_id is None
    assert org_b_prov.last_error_code == "reconciliation_persist_failed"
    assert len(db.reconciliations) == 0

    # Subsequent retry is blocked without calling Razorpay
    remote_calls = 0

    class ForbiddenSpy:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenSpy())
    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="ambiguous state"):
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())
    assert remote_calls == 0


@pytest.mark.asyncio
async def test_d6_total_recovery_outage_surfaces_original_and_recovery_errors_cleanly():
    """D-6: When both reconciliation commit and fallback ambiguous transition fail, neither exception
    is hidden, session is rolled back cleanly, and diagnostic metadata stays intact.
    """
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner A")
    org_b = Organization(id=org_b_id, name="Total Recovery Outage B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_total_outage_777",
        status=CustomerProvisionStatus.resolved,
    )

    fail_recovery_phase = False

    def fail_after_tx2(session, dirty_provs, dirty_recs):
        nonlocal fail_recovery_phase
        if fail_recovery_phase:
            return OperationalError("Total recovery DB outage", params={}, orig=Exception())
        if any(p.organization_id == org_b_id and p.provider_customer_id == "cust_total_outage_777" for p in dirty_provs):
            fail_recovery_phase = True
        return None

    db = ConstraintEnforcingDbSession(
        provisions=[owner_prov],
        organizations=[org_a, org_b],
        fail_commit_predicate=fail_after_tx2,
    )

    class CollidingClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_total_outage_777"}

    set_razorpay_client_for_testing(CollidingClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.customer_id == "cust_total_outage_777"
    assert isinstance(err.original_error, IntegrityError)
    assert isinstance(err.recovery_error, OperationalError)
    assert "fallback ambiguous update also failed" in str(err)
    assert db.in_transaction() is False
    assert owner_prov.status == CustomerProvisionStatus.resolved


@pytest.mark.asyncio
async def test_d6_retry_after_collision_blocks_without_calling_razorpay():
    """D-6: After collision recovery marks Org B ambiguous, subsequent requests fail closed without HTTP calls."""
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner A")
    org_b = Organization(id=org_b_id, name="Blocked Retry B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_shared_retry_block",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[owner_prov], organizations=[org_a, org_b])

    calls = 0

    class CollidingClient:
        async def create_customer(self, **kwargs):
            nonlocal calls
            calls += 1
            return {"id": "cust_shared_retry_block"}

    set_razorpay_client_for_testing(CollidingClient())

    with pytest.raises(RazorpayCustomerPersistenceError):
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())
    assert calls == 1

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="ambiguous state"):
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())
    assert calls == 1
    assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_d6_long_customer_id_collision_builds_bounded_case_key():
    """D-6: Reconciliation case_key must remain strictly <= 255 characters even for long customer IDs."""
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner Long")
    org_b = Organization(id=org_b_id, name="Affected Long")
    long_customer_id = "cust_" + ("x" * 245)

    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=long_customer_id,
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[owner_prov], organizations=[org_a, org_b])

    class LongIdClient:
        async def create_customer(self, **kwargs):
            return {"id": long_customer_id}

    set_razorpay_client_for_testing(LongIdClient())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org_b.id, organization_name=org_b.name, session_factory=db.as_session_factory())

    assert exc_info.value.recovery_error is None
    assert len(db.reconciliations) == 1
    assert len(db.reconciliations[0].case_key) <= 255
    assert db.reconciliations[0].case_key.startswith("customer_provision:collision:")
    assert db.in_transaction() is False


# ============================================================================
# Step 5.2D.3.3B.1E-D — E-1 Durable Ambiguous Fallback Recovery Tests
# (In-memory / constraint-modeled unit tests; real PG16 validation separate)
# ============================================================================


@pytest.mark.asyncio
async def test_e1_successful_reconciliation_preserves_existing_d1_behavior():
    """E-1 Req 1: When reconciliation succeeds, D-1 commits ambiguous + reconciliation in one TX."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Recon Success Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="   ",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[prov], organizations=[org])

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="marked ambiguous with reconciliation case") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.original_error is None
    assert err.recovery_error is None
    assert err.fallback_committed is None
    assert remote_calls == 0
    assert db.in_transaction() is False
    assert [entry["action"] for entry in db.tx_log] == ["commit"]
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE
    assert len(db.reconciliations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupted_customer_id", [None, "", "   \t\n  "])
async def test_e1_reconciliation_failure_triggers_fresh_fallback_tx_and_clears_id(
    corrupted_customer_id: str | None,
):
    """E-1 Req 2-5: Failed reconciliation rolls back completely, opens a fresh locked TX
    using FOR UPDATE + populate_existing=True, commits ambiguous, clears invalid provider_customer_id,
    and preserves the anomaly diagnostic code without calling Razorpay.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Fallback Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=corrupted_customer_id,
        status=CustomerProvisionStatus.resolved,
        claim_operation_id="stale_claim_op",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )

    executed_stmt_meta: list[tuple[str, bool, bool]] = []

    class TrackingConstraintDb(ConstraintEnforcingDbSession):
        async def execute(self, stmt: Any) -> MockScalarResult:
            for_update = getattr(stmt, "_for_update_arg", None) is not None
            exec_opts = stmt.get_execution_options() if hasattr(stmt, "get_execution_options") else {}
            populate_existing = bool(exec_opts.get("populate_existing", False))
            froms_list = stmt.get_final_froms() if hasattr(stmt, "get_final_froms") else getattr(stmt, "froms", ())
            table_name = next((getattr(f, "name", "") for f in froms_list if getattr(f, "name", "")), "")
            executed_stmt_meta.append((table_name, for_update, populate_existing))
            return await super().execute(stmt)

    db = TrackingConstraintDb(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("reconciliations write error", params={}, orig=Exception())],
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.original_error is err.__cause__
    assert err.recovery_error is None
    assert err.fallback_committed is True
    assert CORRUPTED_RESOLVED_ERROR_CODE in str(err)
    assert remote_calls == 0
    assert db.in_transaction() is False
    assert len(db.reconciliations) == 0

    # Primary TX committed failed -> rolled back -> independent fallback TX committed ambiguous with ID=None
    assert [entry["action"] for entry in db.tx_log] == ["commit_failed", "rollback", "commit"]
    assert db.tx_log[2]["provisions_statuses"] == ["ambiguous"]
    assert db.tx_log[2]["dirty_customer_ids"] == [None]
    assert db.tx_log[2]["reconciliations"] == []

    # Fallback re-queried Organization and BillingCustomerProvision with FOR UPDATE + populate_existing=True
    assert executed_stmt_meta[-2:] == [
        ("organizations", True, True),
        ("billing_customer_provisions", True, True),
    ]

    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.claim_operation_id is None
    assert prov.lease_expires_at is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE
    assert await get_razorpay_customer_id(db, org_id) is None


@pytest.mark.asyncio
async def test_e1_reconciliation_staging_flush_failure_still_commits_fallback_ambiguous(monkeypatch):
    """E-1: Even if stage_or_update_payment_reconciliation() fails prior to commit (e.g. PG flush error),
    the failed TX rolls back and fallback commits ambiguous with diagnostic code preserved.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Stage Flush Fail Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )
    db = ConstraintEnforcingDbSession(provisions=[prov], organizations=[org])

    async def failing_stage(*args, **kwargs):
        raise OperationalError("flush failure on payment_reconciliations", params={}, orig=Exception())

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.stage_or_update_payment_reconciliation",
        failing_stage,
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.original_error is err.__cause__
    assert err.fallback_committed is True
    assert db.in_transaction() is False
    assert [entry["action"] for entry in db.tx_log] == ["rollback", "commit"]
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE


@pytest.mark.asyncio
async def test_e1_concurrent_valid_repair_before_fallback_lock_is_not_overwritten():
    """E-1 Req 6: If another worker repairs the mapping (status=resolved, valid provider_customer_id)
    between reconciliation rollback and fallback lock acquisition, fallback must NOT overwrite it.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Concurrent Repair Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )

    class ConcurrentRepairDb(ConstraintEnforcingDbSession):
        async def rollback(self) -> None:
            await super().rollback()
            if len(self.tx_log) >= 2 and self.tx_log[-2]["action"] == "commit_failed":
                # Simulate another session acquiring the row lock after TX1 rollback and repairing it
                prov.status = CustomerProvisionStatus.resolved
                prov.provider_customer_id = "cust_repaired_by_concurrent_worker"
                prov.last_error_code = None
                self._save_snapshots()

    db = ConcurrentRepairDb(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("recon table unavailable", params={}, orig=Exception())],
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="concurrently repaired 'resolved' mapping") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.original_error is err.__cause__
    assert err.recovery_error is None
    assert err.fallback_committed is False
    assert remote_calls == 0
    assert db.in_transaction() is False

    # Concurrent valid repair preserved untouched
    assert prov.status == CustomerProvisionStatus.resolved
    assert prov.provider_customer_id == "cust_repaired_by_concurrent_worker"
    assert prov.last_error_code is None
    assert [entry["action"] for entry in db.tx_log] == ["commit_failed", "rollback", "rollback"]

    # Subsequent request returns the repaired customer cleanly with 0 HTTP calls
    assert await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory()) == "cust_repaired_by_concurrent_worker"
    assert remote_calls == 0


@pytest.mark.asyncio
async def test_e1_concurrent_state_change_before_fallback_lock_is_not_overwritten():
    """E-1 Req 7: If another worker changes provisioning state (e.g. to active pending or replaces row)
    between reconciliation rollback and fallback lock acquisition, fallback skips overwrite cleanly.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Concurrent State Change Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=None,
        status=CustomerProvisionStatus.resolved,
    )

    class ConcurrentStateChangeDb(ConstraintEnforcingDbSession):
        async def rollback(self) -> None:
            await super().rollback()
            if len(self.tx_log) >= 2 and self.tx_log[-2]["action"] == "commit_failed":
                prov.status = CustomerProvisionStatus.pending
                prov.claim_operation_id = "worker_b_active_claim"
                prov.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
                prov.last_error_code = None
                self._save_snapshots()

    db = ConcurrentStateChangeDb(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("recon write timeout", params={}, orig=Exception())],
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="observed concurrent state change") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.fallback_committed is False
    assert remote_calls == 0
    assert db.in_transaction() is False
    assert prov.status == CustomerProvisionStatus.pending
    assert prov.claim_operation_id == "worker_b_active_claim"
    assert [entry["action"] for entry in db.tx_log] == ["commit_failed", "rollback", "rollback"]

    # Subsequent retry sees concurrent pending lease and blocks cleanly without HTTP
    with pytest.raises(RazorpayCustomerProvisioningInProgressError):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
    assert remote_calls == 0


@pytest.mark.asyncio
async def test_e1_org_unavailable_during_fallback_fails_cleanly_without_commit():
    """E-1: If the organization disappears before fallback acquires the lock, fallback aborts cleanly,
    does not mutate provision or claim durable ambiguous commit, and surfaces original error.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Vanishing Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )

    class OrgVanishesBeforeFallbackDb(ConstraintEnforcingDbSession):
        async def rollback(self) -> None:
            await super().rollback()
            if len(self.tx_log) >= 2 and self.tx_log[-2]["action"] == "commit_failed":
                self.organizations.pop(org_id, None)

    db = OrgVanishesBeforeFallbackDb(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("recon write failed", params={}, orig=Exception())],
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="org_unavailable") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert isinstance(err.__cause__, OperationalError)
    assert err.original_error is err.__cause__
    assert err.fallback_committed is False
    assert db.in_transaction() is False
    assert [entry["action"] for entry in db.tx_log] == ["commit_failed", "rollback", "rollback"]


@pytest.mark.asyncio
async def test_e1_fallback_failure_preserves_both_exceptions_and_leaves_session_clean():
    """E-1 Req 8: When both reconciliation commit and fallback ambiguous commit fail,
    both original_error and recovery_error are preserved without suppression and session is rolled back.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Total Outage Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="   ",
        status=CustomerProvisionStatus.resolved,
    )

    recon_err = OperationalError("primary reconciliation persistence failed", params={}, orig=Exception())
    fallback_err = OperationalError("fallback ambiguous persistence failed", params={}, orig=Exception())

    def fail_every_commit(session, dirty_provs, dirty_recs):
        if dirty_recs:
            return recon_err
        return fallback_err

    db = ConstraintEnforcingDbSession(
        provisions=[prov],
        organizations=[org],
        fail_commit_predicate=fail_every_commit,
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.original_error is recon_err
    assert err.recovery_error is fallback_err
    assert err.__cause__ is fallback_err
    assert err.__context__ is recon_err
    assert err.fallback_committed is False
    assert "unconfirmed commit outcome" in str(err)
    assert "fallback committed ambiguous state" not in str(err)
    assert remote_calls == 0
    assert db.in_transaction() is False
    assert [entry["action"] for entry in db.tx_log] == [
        "commit_failed",
        "rollback",
        "commit_failed",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_e1_unknown_commit_outcome_connection_lost_is_not_reported_as_confirmed_success():
    """E-1 Req 10: If database connection is lost during fallback commit(), even if the DB server
    committed the ambiguous row before dropping the socket, commit outcome is unknown and
    must NOT be reported as a confirmed ambiguous transition.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Commit Drop Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )

    recon_err = OperationalError("recon connection refused", params={}, orig=Exception())
    commit_drop_err = OperationalError("server closed the connection unexpectedly during COMMIT", params={}, orig=Exception())

    class DropConnectionOnFallbackCommitDb(ConstraintEnforcingDbSession):
        async def commit(self) -> None:
            dirty_recs = self._dirty_reconciliations()
            if not dirty_recs and self.tx_log and self.tx_log[-1]["action"] == "rollback":
                # Simulate remote server actually committing right before TCP packet drop
                await super().commit()
                self._in_tx = True
                self.tx_log.append({
                    "action": "commit_failed",
                    "dirty_customer_ids": [prov.provider_customer_id],
                    "reconciliations": 0,
                    "error": str(commit_drop_err),
                })
                raise commit_drop_err
            await super().commit()

    db = DropConnectionOnFallbackCommitDb(
        provisions=[prov],
        organizations=[org],
        reconciliation_commit_errors=[recon_err],
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.fallback_committed is False
    assert err.original_error is recon_err
    assert err.recovery_error is commit_drop_err
    assert "fallback committed ambiguous state" not in str(err)
    assert "unconfirmed commit outcome" in str(err)
    assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_e1_repeated_requests_across_all_recovery_outcomes_never_call_razorpay():
    """E-1 Req 9: Repeated requests never invoke client.create_customer() whether reconciliation fails,
    fallback succeeds, fallback also fails repeatedly, or the row recovers on a subsequent retry.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Retry Never Http Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )

    recon_err = OperationalError("recon outage", params={}, orig=Exception())
    fallback_err = OperationalError("fallback outage", params={}, orig=Exception())
    fail_fallback = True
    fail_recon = True

    def gated_failure(session, dirty_provs, dirty_recs):
        if dirty_recs and fail_recon:
            return recon_err
        if not dirty_recs and fail_fallback:
            return fallback_err
        return None

    db = ConstraintEnforcingDbSession(
        provisions=[prov],
        organizations=[org],
        fail_commit_predicate=gated_failure,
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_duplicate_forbidden"}

    set_razorpay_client_for_testing(ForbiddenClient())

    # Attempt 1 & 2: both recon and fallback fail -> still fails closed with 0 HTTP calls
    for _ in range(2):
        with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
            await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
        assert exc_info.value.fallback_committed is False
        assert prov.status == CustomerProvisionStatus.resolved

    # Attempt 3: DB partially recovers (fallback works while recon still fails) -> commits ambiguous
    fail_fallback = False
    with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())
    assert exc_info.value.fallback_committed is True
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE

    # Attempt 4+: already ambiguous -> Case 1.3 fails closed without HTTP or extra commits
    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="ambiguous state"):
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    assert remote_calls == 0
    assert db.in_transaction() is False
    assert await get_razorpay_customer_id(db, org_id) is None


@pytest.mark.asyncio
async def test_e1_provision_replaced_before_fallback_lock_is_not_overwritten():
    """E-1: If the provision row is replaced (different provision.id) between reconciliation
    rollback and fallback lock acquisition, fallback must NOT overwrite the replacement row.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="E1 Replaced Provision Org")
    original_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="",
        status=CustomerProvisionStatus.resolved,
    )
    replacement_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="   ",
        status=CustomerProvisionStatus.resolved,
        last_error_code="other_source",
    )

    class ReplacedProvisionDb(ConstraintEnforcingDbSession):
        async def rollback(self) -> None:
            await super().rollback()
            if len(self.tx_log) >= 2 and self.tx_log[-2]["action"] == "commit_failed":
                self.provisions = [replacement_prov]
                self._save_snapshots()

    db = ReplacedProvisionDb(
        provisions=[original_prov],
        organizations=[org],
        reconciliation_commit_errors=[OperationalError("recon write blocked", params={}, orig=Exception())],
    )

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="provision_replaced") as exc_info:
        await get_or_create_razorpay_customer(organization_id=org.id, organization_name=org.name, session_factory=db.as_session_factory())

    err = exc_info.value
    assert err.fallback_committed is False
    assert isinstance(err.__cause__, OperationalError)
    assert db.in_transaction() is False
    assert replacement_prov.status == CustomerProvisionStatus.resolved
    assert replacement_prov.last_error_code == "other_source"


@pytest.mark.asyncio
async def test_e1_tx1_and_tx2_reload_paths_also_execute_durable_fallback_on_recon_failure():
    """E-1: Verify both TX1 IntegrityError reload and TX2 IntegrityError reload paths execute
    durable E-1 fallback when reconciliation persistence fails.
    """
    # --- Part 1: TX1 IntegrityError reload path ---
    org_id_1 = uuid.uuid4()
    org_1 = Organization(id=org_id_1, name="E1 TX1 Reload Fallback Org")
    corrupted_concurrent_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id_1,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="   ",
        status=CustomerProvisionStatus.resolved,
    )

    tx1_injected = False

    def inject_tx1_conflict(session, dirty_provs, dirty_recs):
        nonlocal tx1_injected
        if not tx1_injected and any(p.status == CustomerProvisionStatus.pending for p in dirty_provs):
            tx1_injected = True
            session.provisions.append(corrupted_concurrent_prov)
            session._save_snapshots()
            return _make_pg_unique_error("uq_billing_customer_provisions_org_provider")
        return None

    db_1 = ConstraintEnforcingDbSession(
        organizations=[org_1],
        fail_commit_predicate=inject_tx1_conflict,
        reconciliation_commit_errors=[OperationalError("tx1 reload recon blocked", params={}, orig=Exception())],
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_1:
        await get_or_create_razorpay_customer(organization_id=org_1.id, organization_name=org_1.name, session_factory=db_1.as_session_factory())

    assert exc_1.value.fallback_committed is True
    assert isinstance(exc_1.value.original_error, OperationalError)
    assert remote_calls == 0
    assert db_1.in_transaction() is False
    assert corrupted_concurrent_prov.status == CustomerProvisionStatus.ambiguous
    assert corrupted_concurrent_prov.provider_customer_id is None
    assert corrupted_concurrent_prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE

    # --- Part 2: TX2 IntegrityError reload path ---
    org_id_2 = uuid.uuid4()
    org_2 = Organization(id=org_id_2, name="E1 TX2 Reload Fallback Org")
    tx2_injected = False

    def inject_tx2_reload_corrupted(session, dirty_provs, dirty_recs):
        nonlocal tx2_injected
        if not tx2_injected and any(p.provider_customer_id == "cust_tx2_race_123" for p in dirty_provs):
            tx2_injected = True
            # Commit concurrent corrupted 'resolved' state before rollback reloads it
            target = dirty_provs[0]
            target.status = CustomerProvisionStatus.resolved
            target.provider_customer_id = ""
            target.claim_operation_id = None
            target.lease_expires_at = None
            session._save_snapshots()
            return _make_pg_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
        return None

    db_2 = ConstraintEnforcingDbSession(
        organizations=[org_2],
        fail_commit_predicate=inject_tx2_reload_corrupted,
        reconciliation_commit_errors=[OperationalError("tx2 reload recon blocked", params={}, orig=Exception())],
    )

    class SingleSuccessClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_tx2_race_123"}

    set_razorpay_client_for_testing(SingleSuccessClient())

    with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_2:
        await get_or_create_razorpay_customer(organization_id=org_2.id, organization_name=org_2.name, session_factory=db_2.as_session_factory())

    assert exc_2.value.fallback_committed is True
    assert isinstance(exc_2.value.original_error, OperationalError)
    assert db_2.in_transaction() is False
    reloaded_tx2_prov = db_2.provisions[0]
    assert reloaded_tx2_prov.status == CustomerProvisionStatus.ambiguous
    assert reloaded_tx2_prov.provider_customer_id is None
    assert reloaded_tx2_prov.last_error_code == CORRUPTED_RESOLVED_ERROR_CODE


# ============================================================================
# Step 5.2D.3.3B.1E-E — Real SQLAlchemy AsyncSession(expire_on_commit=False)
# Post-Rollback ORM Expiration & MissingGreenlet Regression Tests
# ============================================================================


def _make_real_asyncpg_session_harness(
    *,
    organizations: list[dict[str, Any]] | None = None,
    provisions: list[dict[str, Any]] | None = None,
    reconciliations: list[dict[str, Any]] | None = None,
    fail_statement_predicate: Callable[[dict[str, list[dict[str, Any]]], str, tuple[Any, ...]], Exception | None] | None = None,
    fail_commit_predicate: Callable[[dict[str, list[dict[str, Any]]], list[dict[str, Any]]], Exception | None] | None = None,
    on_after_rollback: Callable[[dict[str, list[dict[str, Any]]], Exception | None, list[dict[str, Any]]], None] | None = None,
    on_after_tx_end: Callable[[dict[str, list[dict[str, Any]]], str, Exception | None, list[dict[str, Any]]], None] | None = None,
):
    """Build a real SQLAlchemy AsyncSession(expire_on_commit=False) backed by postgresql+asyncpg.

    NOTE: Uses SQLAlchemy's real PostgreSQL asyncpg dialect, SessionTransaction snapshot
    restoration, InstanceState._expire(), and greenlet_spawn/MissingGreenlet enforcement
    over an in-memory asyncpg connection fake (NO SQLite). Concurrent worker mutations are
    simulated deterministically via hooks; live PostgreSQL 16 execution remains separate.
    """
    import json
    import re
    from types import SimpleNamespace
    import asyncpg
    from sqlalchemy import inspect as sa_inspect, pool
    from sqlalchemy.exc import MissingGreenlet
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    def make_unique_error(constraint_name: str, detail: str = "duplicate key value") -> Exception:
        return asyncpg.exceptions.PostgresError.new({
            "S": "ERROR",
            "C": "23505",
            "M": f'duplicate key value violates unique constraint "{constraint_name}"',
            "D": detail,
            "n": constraint_name,
        })

    def make_fk_error(constraint_name: str) -> Exception:
        return asyncpg.exceptions.PostgresError.new({
            "S": "ERROR",
            "C": "23503",
            "M": f'violates foreign key constraint "{constraint_name}"',
            "n": constraint_name,
        })

    now_default = datetime.now(timezone.utc)
    committed_tables: dict[str, list[dict[str, Any]]] = {
        "organizations": [
            {
                "id": o["id"],
                "name": o.get("name", "Test Org"),
                "created_at": o.get("created_at", now_default),
                "updated_at": o.get("updated_at", now_default),
            }
            for o in (organizations or [])
        ],
        "billing_customer_provisions": [
            {
                "id": p["id"],
                "organization_id": p["organization_id"],
                "provider": p.get("provider", "razorpay"),
                "merchant_account_id": p.get("merchant_account_id", "acc_test_123"),
                "environment": p.get("environment", "test"),
                "provider_customer_id": p.get("provider_customer_id"),
                "status": (
                    p["status"].value
                    if isinstance(p["status"], CustomerProvisionStatus)
                    else p["status"]
                ),
                "claim_operation_id": p.get("claim_operation_id"),
                "lease_expires_at": p.get("lease_expires_at"),
                "last_error_code": p.get("last_error_code"),
                "created_at": p.get("created_at", now_default),
                "updated_at": p.get("updated_at", now_default),
            }
            for p in (provisions or [])
        ],
        "payment_reconciliations": [
            {
                "id": r["id"],
                "provider": r.get("provider", "razorpay"),
                "merchant_account_id": r.get("merchant_account_id", "acc_test_123"),
                "environment": r.get("environment", "test"),
                "case_key": r["case_key"],
                "organization_id": r.get("organization_id"),
                "subscription_id": r.get("subscription_id"),
                "invoice_id": r.get("invoice_id"),
                "payment_id": r.get("payment_id"),
                "raw_event_id": r.get("raw_event_id"),
                "reason": r["reason"],
                "status": (
                    r["status"].value
                    if isinstance(r["status"], ReconciliationStatus)
                    else r.get("status", "pending_review")
                ),
                "diagnostic_payload": copy.deepcopy(r.get("diagnostic_payload", {})),
                "resolved_by": r.get("resolved_by"),
                "resolved_at": r.get("resolved_at"),
                "resolution_notes": r.get("resolution_notes"),
                "created_at": r.get("created_at", now_default),
                "updated_at": r.get("updated_at", now_default),
            }
            for r in (reconciliations or [])
        ],
    }

    tx_log: list[dict[str, Any]] = []
    last_error_box: list[Exception | None] = [None]

    def _split_top_level_csv(expr: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in expr:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
            current.append(ch)
        if current:
            parts.append("".join(current).strip())
        return parts

    def _eval_expr(raw_expr: str, params: tuple[Any, ...], col_name: str) -> Any:
        expr = raw_expr.strip()
        m = re.match(r"^\$(\d+)(?:::(.+))?$", expr)
        if m:
            val = params[int(m.group(1)) - 1]
            cast_type = (m.group(2) or "").lower()
            if "json" in cast_type and isinstance(val, str):
                return json.loads(val)
            return val
        if expr.lower() == "now()":
            return datetime.now(timezone.utc)
        if expr.upper() == "NULL":
            return None
        return expr

    def _row_matches_where(row: dict[str, Any], where_sql: str, params: tuple[Any, ...]) -> bool:
        if not where_sql.strip():
            return True
        clauses = [c.strip() for c in re.split(r"\s+AND\s+", where_sql.strip(), flags=re.IGNORECASE)]
        for clause in clauses:
            clause = clause.strip("() ")
            m_is = re.match(r"^(?:\w+\.)?(\w+)\s+IS\s+(NOT\s+)?NULL$", clause, re.IGNORECASE)
            if m_is:
                col = m_is.group(1)
                is_not = bool(m_is.group(2))
                val = row.get(col)
                if is_not and val is None:
                    return False
                if not is_not and val is not None:
                    return False
                continue
            m_cmp = re.match(
                r"^(?:\w+\.)?(\w+)\s*(=|!=|<>)\s*\$(\d+)(?:::[A-Za-z0-9_ ]+)?$",
                clause,
                re.IGNORECASE,
            )
            if m_cmp:
                col, op_str, p_idx = m_cmp.group(1), m_cmp.group(2), int(m_cmp.group(3))
                actual = row.get(col)
                expected = params[p_idx - 1]
                if hasattr(actual, "value") and isinstance(expected, str):
                    actual = actual.value
                if op_str == "=" and actual != expected:
                    return False
                if op_str in ("!=", "<>") and actual == expected:
                    return False
                continue
        return True

    def _enforce_pg_constraints(tables: dict[str, list[dict[str, Any]]]) -> None:
        seen_org_prov: set[tuple[Any, Any, Any, Any]] = set()
        seen_cust: set[tuple[Any, Any, Any, Any]] = set()
        for p in tables["billing_customer_provisions"]:
            ok = (p["organization_id"], p["provider"], p["merchant_account_id"], p["environment"])
            if ok in seen_org_prov:
                raise make_unique_error(
                    "uq_billing_customer_provisions_org_provider",
                    f"Key (organization_id, provider, merchant_account_id, environment)=({ok}) already exists.",
                )
            seen_org_prov.add(ok)

            if p["provider_customer_id"] is not None:
                ck = (p["provider"], p["merchant_account_id"], p["environment"], p["provider_customer_id"])
                if ck in seen_cust:
                    raise make_unique_error(
                        PROVIDER_CUSTOMER_UNIQUE_INDEX,
                        f"Key (provider, merchant_account_id, environment, provider_customer_id)=({ck}) already exists.",
                    )
                seen_cust.add(ck)

        seen_case_keys: set[tuple[Any, Any, Any, Any]] = set()
        org_ids = {o["id"] for o in tables["organizations"]}
        for r in tables["payment_reconciliations"]:
            if r.get("organization_id") is not None and r["organization_id"] not in org_ids:
                raise make_fk_error("fk_payment_reconciliations_organization_id")
            rk = (r["provider"], r["merchant_account_id"], r["environment"], r["case_key"])
            if rk in seen_case_keys:
                raise make_unique_error("uq_payment_reconciliations_case_key")
            seen_case_keys.add(rk)

    class _FakeAsyncpgTx:
        def __init__(self, conn: "_FakeAsyncpgConn") -> None:
            self.conn = conn
            self.staging: dict[str, list[dict[str, Any]]] | None = None
            self.ops: list[dict[str, Any]] = []

        async def start(self) -> None:
            self.staging = copy.deepcopy(committed_tables)
            self.ops = []
            tx_log.append({"action": "begin"})

        async def commit(self) -> None:
            assert self.staging is not None
            try:
                if self.ops and fail_commit_predicate is not None:
                    pred_err = fail_commit_predicate(self.staging, self.ops)
                    if pred_err is not None:
                        raise pred_err
                if self.ops:
                    _enforce_pg_constraints(self.staging)
                    base = copy.deepcopy(committed_tables)
                    for op in self.ops:
                        kind = op.get("op")
                        tbl = op.get("table")
                        if kind == "insert" and tbl in base:
                            base[tbl].append(copy.deepcopy(op["row"]))
                        elif kind == "update" and tbl in base:
                            target_ids = set(op.get("target_ids", ()))
                            updates = op.get("updates", {})
                            for row in base[tbl]:
                                if row.get("id") in target_ids:
                                    row.update(copy.deepcopy(updates))
                        elif kind == "delete" and tbl in base:
                            target_ids = set(op.get("target_ids", ()))
                            base[tbl] = [r for r in base[tbl] if r.get("id") not in target_ids]
                    _enforce_pg_constraints(base)
                    for tbl, rows in base.items():
                        committed_tables[tbl] = rows
                tx_log.append({"action": "commit", "ops": list(self.ops)})
                if on_after_tx_end is not None:
                    on_after_tx_end(committed_tables, "commit", None, tx_log)
            except Exception as exc:
                last_error_box[0] = exc
                tx_log.append({"action": "commit_failed", "ops": list(self.ops), "error": str(exc)})
                if on_after_tx_end is not None:
                    on_after_tx_end(committed_tables, "commit_failed", exc, tx_log)
                raise
            finally:
                self.staging = None

        async def rollback(self) -> None:
            self.staging = None
            tx_log.append({"action": "rollback", "ops": list(self.ops)})
            if on_after_rollback is not None:
                on_after_rollback(committed_tables, last_error_box[0], tx_log)
            if on_after_tx_end is not None:
                on_after_tx_end(committed_tables, "rollback", last_error_box[0], tx_log)
            last_error_box[0] = None

    class _FakeAsyncpgPreparedStmt:
        def __init__(self, conn: "_FakeAsyncpgConn", sql: str) -> None:
            self.conn = conn
            self.sql = sql
            self._status = "OK"

        def _parse_select_cols(self) -> tuple[str, list[tuple[str, str]]]:
            m = re.search(r"\bSELECT\s+(.*?)\s+FROM\s+(\w+)", self.sql, re.IGNORECASE | re.DOTALL)
            assert m is not None, f"Unsupported SELECT SQL: {self.sql}"
            table_name = m.group(2).lower()
            cols: list[tuple[str, str]] = []
            for raw_item in _split_top_level_csv(m.group(1)):
                m_alias = re.match(r"^(?:\w+\.)?(\w+)\s+AS\s+(\w+)$", raw_item, re.IGNORECASE)
                if m_alias:
                    cols.append((m_alias.group(1), m_alias.group(2)))
                else:
                    src = raw_item.split(".")[-1].strip()
                    cols.append((src, src))
            return table_name, cols

        def get_attributes(self) -> list[Any]:
            s = self.sql.strip().lower()
            if "pg_catalog.version" in s:
                return [SimpleNamespace(name="version", type=SimpleNamespace(oid=25))]
            if "current_schema" in s:
                return [SimpleNamespace(name="current_schema", type=SimpleNamespace(oid=25))]
            if "transaction isolation level" in s:
                return [SimpleNamespace(name="transaction_isolation", type=SimpleNamespace(oid=25))]
            if "standard_conforming_strings" in s:
                return [SimpleNamespace(name="standard_conforming_strings", type=SimpleNamespace(oid=25))]
            if s.startswith("select"):
                _, cols = self._parse_select_cols()
                return [SimpleNamespace(name=alias, type=SimpleNamespace(oid=25)) for _, alias in cols]
            if " returning " in s:
                ret_part = re.split(r"\bRETURNING\b", self.sql, flags=re.IGNORECASE, maxsplit=1)[1]
                ret_cols = [c.strip().split(".")[-1] for c in _split_top_level_csv(ret_part)]
                return [SimpleNamespace(name=c, type=SimpleNamespace(oid=25)) for c in ret_cols]
            return []

        async def fetch(self, *params: Any) -> list[tuple[Any, ...]]:
            s = self.sql.strip()
            s_low = s.lower()
            if "pg_catalog.version" in s_low:
                self._status = "SELECT 1"
                return [("PostgreSQL 16.0",)]
            if "current_schema" in s_low:
                self._status = "SELECT 1"
                return [("public",)]
            if "transaction isolation level" in s_low:
                self._status = "SHOW"
                return [("read committed",)]
            if "standard_conforming_strings" in s_low:
                self._status = "SHOW"
                return [("on",)]

            tx = self.conn._current_tx
            assert tx is not None and tx.staging is not None
            tables = tx.staging

            if fail_statement_predicate is not None:
                stmt_err = fail_statement_predicate(tables, self.sql, params)
                if stmt_err is not None:
                    last_error_box[0] = stmt_err
                    raise stmt_err

            if s_low.startswith("select"):
                table_name, cols = self._parse_select_cols()
                m_where = re.search(
                    r"\bWHERE\b(.*?)(?:\bLIMIT\b|\bFOR UPDATE\b|\bORDER BY\b|$)",
                    s,
                    re.IGNORECASE | re.DOTALL,
                )
                where_sql = m_where.group(1) if m_where else ""
                m_limit = re.search(r"\bLIMIT\s+\$(\d+)", s, re.IGNORECASE)
                limit_val = int(params[int(m_limit.group(1)) - 1]) if m_limit else None

                matched: list[dict[str, Any]] = []
                for row in tables.get(table_name, []):
                    if _row_matches_where(row, where_sql, params):
                        matched.append(row)
                if limit_val is not None:
                    matched = matched[:limit_val]
                tx.ops.append({"op": "select", "table": table_name, "count": len(matched)})
                self._status = f"SELECT {len(matched)}"
                return [tuple(row.get(src_col) for src_col, _ in cols) for row in matched]

            if s_low.startswith("insert into"):
                m_ins = re.match(
                    r"^INSERT\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)(?:\s+RETURNING\s+(.*))?$",
                    s,
                    re.IGNORECASE | re.DOTALL,
                )
                assert m_ins is not None, f"Unsupported INSERT SQL: {s}"
                table_name = m_ins.group(1).lower()
                col_names = [c.strip() for c in _split_top_level_csv(m_ins.group(2))]
                val_exprs = _split_top_level_csv(m_ins.group(3))
                now = datetime.now(timezone.utc)
                new_row: dict[str, Any] = {
                    "created_at": now,
                    "updated_at": now,
                }
                for col, v_expr in zip(col_names, val_exprs):
                    new_row[col] = _eval_expr(v_expr, params, col)
                tables[table_name].append(new_row)
                try:
                    _enforce_pg_constraints(tables)
                except Exception as exc:
                    last_error_box[0] = exc
                    raise
                tx.ops.append({"op": "insert", "table": table_name, "row": copy.deepcopy(new_row)})
                self._status = "INSERT 0 1"
                ret_part = m_ins.group(4)
                if ret_part:
                    ret_cols = [c.strip().split(".")[-1] for c in _split_top_level_csv(ret_part)]
                    return [tuple(new_row.get(rc) for rc in ret_cols)]
                return []

            if s_low.startswith("update "):
                m_upd = re.match(
                    r"^UPDATE\s+(\w+)\s+SET\s+(.*?)(?:\s+WHERE\s+(.*?))?(?:\s+RETURNING\s+(.*))?$",
                    s,
                    re.IGNORECASE | re.DOTALL,
                )
                assert m_upd is not None, f"Unsupported UPDATE SQL: {s}"
                table_name = m_upd.group(1).lower()
                assigns = _split_top_level_csv(m_upd.group(2))
                where_sql = m_upd.group(3) or ""
                ret_part = m_upd.group(4)

                updates: dict[str, Any] = {}
                for assign in assigns:
                    k, v = assign.split("=", 1)
                    k = k.strip()
                    updates[k] = _eval_expr(v, params, k)

                updated_rows: list[dict[str, Any]] = []
                for row in tables.get(table_name, []):
                    if _row_matches_where(row, where_sql, params):
                        row.update(updates)
                        updated_rows.append(row)
                try:
                    _enforce_pg_constraints(tables)
                except Exception as exc:
                    last_error_box[0] = exc
                    raise
                tx.ops.append({
                    "op": "update",
                    "table": table_name,
                    "count": len(updated_rows),
                    "target_ids": [r.get("id") for r in updated_rows],
                    "updates": copy.deepcopy(updates),
                })
                self._status = f"UPDATE {len(updated_rows)}"
                if ret_part:
                    ret_cols = [c.strip().split(".")[-1] for c in _split_top_level_csv(ret_part)]
                    return [tuple(r.get(rc) for rc in ret_cols) for r in updated_rows]
                return []

            if s_low.startswith("delete from "):
                m_del = re.match(
                    r"^DELETE\s+FROM\s+(\w+)(?:\s+WHERE\s+(.*?))?(?:\s+RETURNING\s+(.*))?$",
                    s,
                    re.IGNORECASE | re.DOTALL,
                )
                assert m_del is not None, f"Unsupported DELETE SQL: {s}"
                table_name = m_del.group(1).lower()
                where_sql = m_del.group(2) or ""
                ret_part = m_del.group(3)

                remaining: list[dict[str, Any]] = []
                deleted_rows: list[dict[str, Any]] = []
                for row in tables.get(table_name, []):
                    if _row_matches_where(row, where_sql, params):
                        deleted_rows.append(row)
                    else:
                        remaining.append(row)
                tables[table_name] = remaining
                try:
                    _enforce_pg_constraints(tables)
                except Exception as exc:
                    last_error_box[0] = exc
                    raise
                tx.ops.append({
                    "op": "delete",
                    "table": table_name,
                    "count": len(deleted_rows),
                    "target_ids": [r.get("id") for r in deleted_rows],
                })
                self._status = f"DELETE {len(deleted_rows)}"
                if ret_part:
                    ret_cols = [c.strip().split(".")[-1] for c in _split_top_level_csv(ret_part)]
                    return [tuple(r.get(rc) for rc in ret_cols) for r in deleted_rows]
                return []

            raise AssertionError(f"Unsupported SQL in test harness: {self.sql}")

        def get_statusmsg(self) -> str:
            return self._status

    class _FakeAsyncpgConn:
        def __init__(self) -> None:
            self._closed = False
            self._current_tx: _FakeAsyncpgTx | None = None

        def is_closed(self) -> bool:
            return self._closed

        def transaction(self, **kwargs: Any) -> _FakeAsyncpgTx:
            tx = _FakeAsyncpgTx(self)
            self._current_tx = tx
            return tx

        async def set_type_codec(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def reload_schema_state(self) -> None:
            return None

        async def prepare(self, sql: str, name: str | None = None) -> _FakeAsyncpgPreparedStmt:
            return _FakeAsyncpgPreparedStmt(self, sql)

        async def close(self, timeout: float | None = None) -> None:
            self._closed = True

        def terminate(self) -> None:
            self._closed = True

    async def _create_conn(**kwargs: Any) -> _FakeAsyncpgConn:
        return _FakeAsyncpgConn()

    engine = create_async_engine(
        "postgresql+asyncpg://test:test@localhost:5432/test",
        poolclass=pool.NullPool,
        async_creator=_create_conn,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return SimpleNamespace(
        engine=engine,
        session_factory=session_factory,
        tables=committed_tables,
        tx_log=tx_log,
        asyncpg=asyncpg,
        make_unique_error=make_unique_error,
        sa_inspect=sa_inspect,
        MissingGreenlet=MissingGreenlet,
    )


@pytest.mark.asyncio
async def test_e1_real_async_session_harness_genuinely_expires_orm_on_rollback():
    """Sanity check for E1-F1/E1-F2 tests: real AsyncSession(expire_on_commit=False)
    must expire identity-map ORM attributes on rollback (for both Organization and
    BillingCustomerProvision) so synchronous access raises MissingGreenlet.
    """
    org_id = uuid.uuid4()
    prov_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Real Expiration Proof Org"}],
        provisions=[
            {
                "id": prov_id,
                "organization_id": org_id,
                "provider": "razorpay",
                "merchant_account_id": "acc_test_123",
                "environment": "test",
                "provider_customer_id": "cust_ok_1",
                "status": CustomerProvisionStatus.resolved,
            }
        ],
    )
    try:
        async with harness.session_factory() as db:
            org = await db.get(Organization, org_id)
            prov = await db.get(BillingCustomerProvision, prov_id)
            assert org is not None and prov is not None
            assert org.name == "Real Expiration Proof Org"
            assert prov.status == CustomerProvisionStatus.resolved

            await db.rollback()
            assert "id" in harness.sa_inspect(org).expired_attributes
            assert "name" in harness.sa_inspect(org).expired_attributes
            assert "status" in harness.sa_inspect(prov).expired_attributes
            assert "provider_customer_id" in harness.sa_inspect(prov).expired_attributes

            with pytest.raises(harness.MissingGreenlet):
                _ = org.id
            with pytest.raises(harness.MissingGreenlet):
                _ = prov.status
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["provision_replaced", "state_changed", "concurrent_repaired"])
async def test_e1_f1_real_async_session_guard_post_rollback_without_missing_greenlet(
    scenario: str,
):
    """E1-F1 (Real AsyncSession expire_on_commit=False):
    Verify provision_replaced, state_changed, and concurrent_repaired guard paths inside
    _mark_corrupted_resolved_ambiguous_after_reconciliation_failure() execute await _safe_rollback(db)
    (which genuinely expires fresh_provision in SQLAlchemy's identity map) and construct logs/results
    strictly from pre-rollback scalars with zero MissingGreenlet errors, leaving concurrent repairs untouched.
    """
    from app.services.billing.razorpay.customer import (
        _mark_corrupted_resolved_ambiguous_after_reconciliation_failure,
    )

    org_id = uuid.uuid4()
    expected_prov_id = uuid.uuid4()
    replacement_id = uuid.uuid4()
    repaired_cust_id = f"cust_repaired_valid_{scenario}"

    initial_prov = {
        "id": expected_prov_id,
        "organization_id": org_id,
        "provider": "razorpay",
        "merchant_account_id": "acc_test_123",
        "environment": "test",
        "provider_customer_id": "   ",
        "status": CustomerProvisionStatus.resolved,
        "claim_operation_id": None,
        "lease_expires_at": None,
        "last_error_code": None,
    }

    mutated_once = [False]

    def _after_tx_end(tables, action, last_err, log):
        if (
            not mutated_once[0]
            and action == "commit_failed"
            and any(
                o["op"] == "insert" and o["table"] == "payment_reconciliations"
                for o in log[-1].get("ops", [])
            )
        ):
            mutated_once[0] = True
            provs = tables["billing_customer_provisions"]
            if scenario == "provision_replaced":
                provs[0] = {
                    "id": replacement_id,
                    "organization_id": org_id,
                    "provider": "razorpay",
                    "merchant_account_id": "acc_test_123",
                    "environment": "test",
                    "provider_customer_id": None,
                    "status": CustomerProvisionStatus.resolved.value,
                    "claim_operation_id": None,
                    "lease_expires_at": None,
                    "last_error_code": "replaced_marker",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            elif scenario == "state_changed":
                provs[0]["status"] = CustomerProvisionStatus.pending.value
                provs[0]["claim_operation_id"] = "worker_b_active"
                provs[0]["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)
                provs[0]["last_error_code"] = "active_claim"
            elif scenario == "concurrent_repaired":
                provs[0]["status"] = CustomerProvisionStatus.resolved.value
                provs[0]["provider_customer_id"] = repaired_cust_id
                provs[0]["last_error_code"] = None

    recon_fail = [True]

    def _fail_recon_commit(staging, ops):
        if recon_fail[0] and any(
            o["op"] == "insert" and o["table"] == "payment_reconciliations" for o in ops
        ):
            recon_fail[0] = False
            return harness.asyncpg.exceptions.InternalServerError("reconciliations write failed")
        return None

    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": f"Guard {scenario} Org"}],
        provisions=[initial_prov],
        fail_commit_predicate=_fail_recon_commit,
        on_after_tx_end=_after_tx_end,
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_forbidden"}

    set_razorpay_client_for_testing(ForbiddenClient())

    try:
        async with harness.session_factory() as db:
            # Session-bound Organization in caller's SQLAlchemy identity_map
            org = await db.get(Organization, org_id)
            assert org is not None

            with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
                await get_or_create_razorpay_customer(
                    organization_id=org.id,
                    organization_name=org.name,
                    session_factory=harness.session_factory,
                )

            err = exc_info.value
            assert err.fallback_committed is False
            assert err.recovery_error is None
            assert remote_calls == 0
            # Caller session & identity_map remain completely untouched by service TXs/rollbacks
            assert db.in_transaction() is True
            assert "id" not in harness.sa_inspect(org).expired_attributes
            assert org.name == f"Guard {scenario} Org"

            if scenario == "provision_replaced":
                assert "provision_replaced" in str(err)
                assert harness.tables["billing_customer_provisions"][0]["id"] == replacement_id
                assert harness.tables["billing_customer_provisions"][0]["last_error_code"] == "replaced_marker"
            elif scenario == "state_changed":
                assert "state_changed" in str(err)
                assert harness.tables["billing_customer_provisions"][0]["status"] == "pending"
                assert harness.tables["billing_customer_provisions"][0]["claim_operation_id"] == "worker_b_active"
            elif scenario == "concurrent_repaired":
                assert "concurrently repaired" in str(err)
                assert harness.tables["billing_customer_provisions"][0]["status"] == "resolved"
                assert harness.tables["billing_customer_provisions"][0]["provider_customer_id"] == repaired_cust_id
                assert harness.tables["billing_customer_provisions"][0]["last_error_code"] is None

        # Verify subsequent get_or_create_razorpay_customer() preserves concurrent repair & returns customer ID cleanly,
        # and verify _mark_corrupted_resolved_ambiguous_after_reconciliation_failure() expires fresh_provision on rollback
        # while constructing logs & _CorruptedResolvedFallbackResult strictly from pre-rollback scalars.
        async with harness.session_factory() as db2:
            if scenario == "concurrent_repaired":
                org_recheck = await db2.get(Organization, org_id)
                assert org_recheck is not None
                assert (
                    await get_or_create_razorpay_customer(
                        organization_id=org_recheck.id,
                        organization_name=org_recheck.name,
                        session_factory=harness.session_factory,
                    )
                    == repaired_cust_id
                )
                assert remote_calls == 0

            current_row_id = harness.tables["billing_customer_provisions"][0]["id"]
            # Hold identity-map reference to the exact ORM instance returned by _find_provision()
            prov_identity_ref = await db2.get(BillingCustomerProvision, current_row_id)
            assert prov_identity_ref is not None

            res = await _mark_corrupted_resolved_ambiguous_after_reconciliation_failure(
                db=db2,
                organization_id=org_id,
                expected_provision_id=expected_prov_id,
                diagnostic_code=CORRUPTED_RESOLVED_ERROR_CODE,
            )
            assert res.outcome == scenario
            if scenario == "state_changed":
                assert res.observed_status == CustomerProvisionStatus.pending
            else:
                assert res.observed_status == CustomerProvisionStatus.resolved
            assert db2.in_transaction() is False
            # Confirm fresh_provision was genuinely expired on rollback inside the guard!
            assert "status" in harness.sa_inspect(prov_identity_ref).expired_attributes
            with pytest.raises(harness.MissingGreenlet):
                _ = prov_identity_ref.status
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_e1_f1_f2_real_async_session_fallback_failure_preserves_original_and_recovery_errors():
    """E1-F1 + E1-F2 (Real AsyncSession expire_on_commit=False):
    With a session-bound Organization loaded in AsyncSession, when both reconciliation persistence
    and fallback ambiguous transition fail, rollbacks genuinely expire ORM instances, no MissingGreenlet
    occurs, and both original_error and recovery_error remain preserved with fallback_committed=False.
    """
    org_id = uuid.uuid4()
    prov_id = uuid.uuid4()

    def _fail_all_commits(staging, ops):
        import asyncpg

        if any(o["op"] == "insert" and o["table"] == "payment_reconciliations" for o in ops):
            return asyncpg.exceptions.InternalServerError("primary recon PG commit error")
        if any(o["op"] == "update" and o["table"] == "billing_customer_provisions" for o in ops):
            return asyncpg.exceptions.InternalServerError("fallback ambiguous PG commit error")
        return None

    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Real Fallback Failure Org"}],
        provisions=[
            {
                "id": prov_id,
                "organization_id": org_id,
                "provider": "razorpay",
                "merchant_account_id": "acc_test_123",
                "environment": "test",
                "provider_customer_id": "",
                "status": CustomerProvisionStatus.resolved,
            }
        ],
        fail_commit_predicate=_fail_all_commits,
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    try:
        async with harness.session_factory() as db:
            org = await db.get(Organization, org_id)
            assert org is not None

            with pytest.raises(RazorpayCustomerReconciliationRequiredError) as exc_info:
                await get_or_create_razorpay_customer(
                    organization_id=org.id,
                    organization_name=org.name,
                    session_factory=harness.session_factory,
                )

            err = exc_info.value
            assert err.fallback_committed is False
            assert err.original_error is not None
            assert err.recovery_error is not None
            assert "primary recon PG commit error" in str(err.original_error)
            assert "fallback ambiguous PG commit error" in str(err.recovery_error)
            assert err.__cause__ is err.recovery_error
            assert err.__context__ is err.original_error
            assert "unconfirmed commit outcome" in str(err)
            assert remote_calls == 0
            assert db.in_transaction() is True
            assert "id" not in harness.sa_inspect(org).expired_attributes
            assert org.name == "Real Fallback Failure Org"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_e1_f2_real_async_session_tx1_integrity_error_reload_with_session_bound_org():
    """E1-F2 (Real AsyncSession expire_on_commit=False):
    TX1 claim INSERT hits IntegrityError -> rollback expires session-bound Organization ->
    reload path reloads concurrent provision cleanly using scalar organization_id without MissingGreenlet.
    Covers both concurrent corrupted resolved -> E-1 fallback ambiguous AND concurrent valid repair preserved.
    """
    # --- Case A: TX1 IntegrityError reloads concurrent corrupted resolved -> recon fails -> fallback commits ambiguous ---
    org_id_1 = uuid.uuid4()
    concurrent_corrupt_id = uuid.uuid4()
    injected_tx1 = [False]
    org_holder_1: list[Organization] = []
    org_expired_at_tx1_reload: list[bool] = []

    def _inject_tx1_corrupted_on_tx_end(tables, action, last_err, log):
        if not injected_tx1[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(o["op"] == "insert" and o["table"] == "billing_customer_provisions" for o in last_ops):
                injected_tx1[0] = True
                tables["billing_customer_provisions"].append(
                    {
                        "id": concurrent_corrupt_id,
                        "organization_id": org_id_1,
                        "provider": "razorpay",
                        "merchant_account_id": "acc_test_123",
                        "environment": "test",
                        "provider_customer_id": "   ",
                        "status": CustomerProvisionStatus.resolved.value,
                        "claim_operation_id": None,
                        "lease_expires_at": None,
                        "last_error_code": None,
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                )

    def _fail_tx1_and_recon(staging, ops):
        if any(o["op"] == "insert" and o["table"] == "billing_customer_provisions" for o in ops):
            return harness_1.make_unique_error("uq_billing_customer_provisions_org_provider")
        if any(o["op"] == "insert" and o["table"] == "payment_reconciliations" for o in ops):
            return harness_1.asyncpg.exceptions.InternalServerError("tx1 reload recon PG error")
        return None

    def _observe_tx1_reload_org_state(tables, sql, params):
        if injected_tx1[0] and org_holder_1 and not org_expired_at_tx1_reload and "FROM organizations" in sql:
            org_expired_at_tx1_reload.append("id" in harness_1.sa_inspect(org_holder_1[0]).expired_attributes)
        return None

    harness_1 = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_1, "name": "Real TX1 Bound Org"}],
        fail_statement_predicate=_observe_tx1_reload_org_state,
        fail_commit_predicate=_fail_tx1_and_recon,
        on_after_tx_end=_inject_tx1_corrupted_on_tx_end,
    )

    remote_calls = 0

    class ForbiddenClient:
        async def create_customer(self, **kwargs):
            nonlocal remote_calls
            remote_calls += 1
            return {"id": "cust_never"}

    set_razorpay_client_for_testing(ForbiddenClient())

    try:
        async with harness_1.session_factory() as db:
            org = await db.get(Organization, org_id_1)
            assert org is not None
            org_holder_1.append(org)

            with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_1:
                await get_or_create_razorpay_customer(
                    organization_id=org.id,
                    organization_name=org.name,
                    session_factory=harness_1.session_factory,
                )

            assert exc_1.value.fallback_committed is True
            assert remote_calls == 0
            assert db.in_transaction() is True
            stored = harness_1.tables["billing_customer_provisions"][0]
            assert stored["status"] == "ambiguous"
            assert stored["provider_customer_id"] is None
            assert stored["last_error_code"] == CORRUPTED_RESOLVED_ERROR_CODE
            # Caller identity-map org stays un-expired while internal TX1 reload runs in its own session!
            assert org_expired_at_tx1_reload == [False]
            assert "id" not in harness_1.sa_inspect(org).expired_attributes
            assert org.name == "Real TX1 Bound Org"
            assert [e["action"] for e in harness_1.tx_log] == [
                "begin", "rollback",  # asyncpg engine dialect handshake
                "begin",  # caller_db (still open)
                "begin", "commit_failed",  # tx1_db (SQLAlchemy asyncpg ends wire TX on failed COMMIT)
                "begin", "commit_failed",  # reload_db (corrupted recon fail)
                "begin", "commit",  # fallback_db
            ]
    finally:
        await harness_1.engine.dispose()

    # --- Case B: TX1 IntegrityError reloads concurrent valid resolved mapping -> returns customer cleanly without MissingGreenlet ---
    org_id_2 = uuid.uuid4()
    valid_concurrent_id = uuid.uuid4()
    injected_tx1_valid = [False]

    def _inject_tx1_valid_on_tx_end(tables, action, last_err, log):
        if not injected_tx1_valid[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(o["op"] == "insert" and o["table"] == "billing_customer_provisions" for o in last_ops):
                injected_tx1_valid[0] = True
                tables["billing_customer_provisions"].append(
                    {
                        "id": valid_concurrent_id,
                        "organization_id": org_id_2,
                        "provider": "razorpay",
                        "merchant_account_id": "acc_test_123",
                        "environment": "test",
                        "provider_customer_id": "cust_tx1_concurrent_valid_111",
                        "status": CustomerProvisionStatus.resolved.value,
                        "claim_operation_id": None,
                        "lease_expires_at": None,
                        "last_error_code": None,
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                )

    def _fail_tx1_only(staging, ops):
        if any(o["op"] == "insert" and o["table"] == "billing_customer_provisions" for o in ops):
            return harness_2.make_unique_error("uq_billing_customer_provisions_org_provider")
        return None

    harness_2 = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_2, "name": "Real TX1 Valid Bound Org"}],
        fail_commit_predicate=_fail_tx1_only,
        on_after_tx_end=_inject_tx1_valid_on_tx_end,
    )

    try:
        async with harness_2.session_factory() as db2:
            org2 = await db2.get(Organization, org_id_2)
            assert org2 is not None

            resolved_cust = await get_or_create_razorpay_customer(
                organization_id=org2.id,
                organization_name=org2.name,
                session_factory=harness_2.session_factory,
            )
            assert resolved_cust == "cust_tx1_concurrent_valid_111"
            assert remote_calls == 0
            assert db2.in_transaction() is True
            assert "id" not in harness_2.sa_inspect(org2).expired_attributes
            stored2 = harness_2.tables["billing_customer_provisions"][0]
            assert stored2["status"] == "resolved"
            assert stored2["provider_customer_id"] == "cust_tx1_concurrent_valid_111"
    finally:
        await harness_2.engine.dispose()


@pytest.mark.asyncio
async def test_e1_f2_real_async_session_tx2_integrity_error_reload_with_session_bound_org():
    """E1-F2 (Real AsyncSession expire_on_commit=False):
    TX2 resolution UPDATE hits IntegrityError -> rollback expires session-bound Organization ->
    reload path reloads state cleanly without MissingGreenlet across:
    (A) concurrent corrupted resolved -> E-1 fallback ambiguous commit,
    (B) concurrent valid resolved -> returns repaired ID cleanly and preserves valid repair untouched,
    (C) reload SELECT failure after TX2 IntegrityError -> marks claim ambiguous and preserves original + recovery errors,
    (D) cross-org customer ID collision on TX2 IntegrityError with session-bound Organization -> preserves owner & records reconciliation.
    """
    # --- Case A: TX2 IntegrityError -> concurrent corrupted resolved -> recon fails -> fallback commits ambiguous ---
    org_id_a = uuid.uuid4()
    injected_tx2_a = [False]
    org_holder_a: list[Organization] = []
    org_expired_at_tx2_reload_a: list[bool] = []

    def _fail_tx2_a(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("provider_customer_id") == "cust_tx2_bound_org_1" and not injected_tx2_a[0]:
                    return harness_a.make_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
        if any(o["op"] == "insert" and o["table"] == "payment_reconciliations" for o in ops):
            return harness_a.asyncpg.exceptions.InternalServerError("tx2 reload recon PG error")
        return None

    def _after_tx2_end_a(tables, action, last_err, log):
        if not injected_tx2_a[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(
                o["op"] == "update"
                and o["table"] == "billing_customer_provisions"
                and o["updates"].get("provider_customer_id") == "cust_tx2_bound_org_1"
                for o in last_ops
            ):
                injected_tx2_a[0] = True
                row = tables["billing_customer_provisions"][0]
                row["status"] = CustomerProvisionStatus.resolved.value
                row["provider_customer_id"] = ""
                row["claim_operation_id"] = None
                row["lease_expires_at"] = None

    def _observe_tx2_reload_org_state(tables, sql, params):
        if injected_tx2_a[0] and org_holder_a and not org_expired_at_tx2_reload_a and "FROM organizations" in sql:
            org_expired_at_tx2_reload_a.append("id" in harness_a.sa_inspect(org_holder_a[0]).expired_attributes)
        return None

    harness_a = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_a, "name": "Real TX2 Corrupted Reload Org"}],
        fail_statement_predicate=_observe_tx2_reload_org_state,
        fail_commit_predicate=_fail_tx2_a,
        on_after_tx_end=_after_tx2_end_a,
    )

    class SingleClientA:
        async def create_customer(self, **kwargs):
            return {"id": "cust_tx2_bound_org_1"}

    set_razorpay_client_for_testing(SingleClientA())

    try:
        async with harness_a.session_factory() as db_a:
            org_a = await db_a.get(Organization, org_id_a)
            assert org_a is not None
            org_holder_a.append(org_a)

            with pytest.raises(RazorpayCustomerReconciliationRequiredError, match="fallback committed ambiguous state") as exc_a:
                await get_or_create_razorpay_customer(
                    organization_id=org_a.id,
                    organization_name=org_a.name,
                    session_factory=harness_a.session_factory,
                )

            assert exc_a.value.fallback_committed is True
            assert db_a.in_transaction() is True
            assert org_expired_at_tx2_reload_a == [False]
            assert "id" not in harness_a.sa_inspect(org_a).expired_attributes
            assert org_a.name == "Real TX2 Corrupted Reload Org"
            stored_a = harness_a.tables["billing_customer_provisions"][0]
            assert stored_a["status"] == "ambiguous"
            assert stored_a["provider_customer_id"] is None
            assert stored_a["last_error_code"] == CORRUPTED_RESOLVED_ERROR_CODE
            assert [e["action"] for e in harness_a.tx_log] == [
                "begin", "rollback",  # asyncpg engine dialect handshake
                "begin",  # caller db_a (still open)
                "begin", "commit",  # tx1_db
                "begin", "commit_failed",  # tx2_db (SQLAlchemy asyncpg ends wire TX on failed COMMIT)
                "begin", "commit_failed",  # reload_db
                "begin", "commit",  # fallback_db
            ]
    finally:
        await harness_a.engine.dispose()

    # --- Case B: TX2 IntegrityError -> concurrent valid resolved repair -> preserved untouched and returned ---
    org_id_b = uuid.uuid4()
    injected_tx2_b = [False]

    def _fail_tx2_b(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("provider_customer_id") == "cust_tx2_loser_2" and not injected_tx2_b[0]:
                    return harness_b.make_unique_error("uq_billing_customer_provisions_org_provider")
        return None

    def _after_tx2_end_b(tables, action, last_err, log):
        if not injected_tx2_b[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(
                o["op"] == "update"
                and o["table"] == "billing_customer_provisions"
                and o["updates"].get("provider_customer_id") == "cust_tx2_loser_2"
                for o in last_ops
            ):
                injected_tx2_b[0] = True
                row = tables["billing_customer_provisions"][0]
                row["status"] = CustomerProvisionStatus.resolved.value
                row["provider_customer_id"] = "cust_tx2_concurrent_repair_valid_2"
                row["claim_operation_id"] = None
                row["lease_expires_at"] = None

    harness_b = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_b, "name": "Real TX2 Valid Repair Org"}],
        fail_commit_predicate=_fail_tx2_b,
        on_after_tx_end=_after_tx2_end_b,
    )

    class SingleClientB:
        async def create_customer(self, **kwargs):
            return {"id": "cust_tx2_loser_2"}

    set_razorpay_client_for_testing(SingleClientB())

    try:
        async with harness_b.session_factory() as db_b:
            org_b = await db_b.get(Organization, org_id_b)
            assert org_b is not None

            cust_b = await get_or_create_razorpay_customer(
                organization_id=org_b.id,
                organization_name=org_b.name,
                session_factory=harness_b.session_factory,
            )
            assert cust_b == "cust_tx2_concurrent_repair_valid_2"
            assert db_b.in_transaction() is True
            assert "id" not in harness_b.sa_inspect(org_b).expired_attributes
            stored_b = harness_b.tables["billing_customer_provisions"][0]
            assert stored_b["status"] == "resolved"
            assert stored_b["provider_customer_id"] == "cust_tx2_concurrent_repair_valid_2"
    finally:
        await harness_b.engine.dispose()

    # --- Case C: TX2 IntegrityError -> reload SELECT fails -> marks claim ambiguous and preserves original + recovery errors ---
    org_id_c = uuid.uuid4()
    fail_reload_once = [False]
    arm_reload_fail = [False]
    org_holder_c: list[Organization] = []
    org_expired_at_reload_c: list[bool] = []

    def _fail_tx2_c(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("provider_customer_id") == "cust_tx2_reload_err_3":
                    arm_reload_fail[0] = True
                    return harness_c.make_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
        return None

    def _fail_stmt_reload_c(tables, sql, params):
        if arm_reload_fail[0] and org_holder_c and not org_expired_at_reload_c and "FROM organizations" in sql:
            org_expired_at_reload_c.append("id" in harness_c.sa_inspect(org_holder_c[0]).expired_attributes)
        if arm_reload_fail[0] and not fail_reload_once[0] and "FROM billing_customer_provisions" in sql:
            fail_reload_once[0] = True
            arm_reload_fail[0] = False
            return harness_c.asyncpg.exceptions.InternalServerError("reload select PG failure")
        return None

    harness_c = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_c, "name": "Real TX2 Reload Fail Org"}],
        fail_commit_predicate=_fail_tx2_c,
        fail_statement_predicate=_fail_stmt_reload_c,
    )

    class SingleClientC:
        async def create_customer(self, **kwargs):
            return {"id": "cust_tx2_reload_err_3"}

    set_razorpay_client_for_testing(SingleClientC())

    try:
        async with harness_c.session_factory() as db_c:
            org_c = await db_c.get(Organization, org_id_c)
            assert org_c is not None
            org_holder_c.append(org_c)

            with pytest.raises(RazorpayCustomerPersistenceError) as exc_c:
                await get_or_create_razorpay_customer(
                    organization_id=org_c.id,
                    organization_name=org_c.name,
                    session_factory=harness_c.session_factory,
                )

            err_c = exc_c.value
            assert err_c.customer_id == "cust_tx2_reload_err_3"
            assert isinstance(err_c.original_error, IntegrityError)
            assert err_c.recovery_error is not None
            assert "reload select PG failure" in str(err_c.recovery_error)
            assert db_c.in_transaction() is True
            assert "id" not in harness_c.sa_inspect(org_c).expired_attributes
            assert org_expired_at_reload_c == [False]
            stored_c = harness_c.tables["billing_customer_provisions"][0]
            assert stored_c["status"] == "ambiguous"
            assert stored_c["provider_customer_id"] is None
            assert stored_c["last_error_code"] == "reconciliation_persist_failed"
    finally:
        await harness_c.engine.dispose()

    # --- Case D: TX2 natural partial-unique-index cross-org collision with session-bound Organization ---
    org_owner_id = uuid.uuid4()
    org_affected_id = uuid.uuid4()
    owner_prov_id = uuid.uuid4()
    org_holder_d: list[Organization] = []
    saw_collision_tx_fail_d = [False]
    org_expired_at_reload_d: list[bool] = []

    def _after_tx_end_d(tables, action, last_err, log):
        if last_err is not None and action in ("commit_failed", "rollback"):
            saw_collision_tx_fail_d[0] = True

    def _observe_reload_d(tables, sql, params):
        if saw_collision_tx_fail_d[0] and org_holder_d and not org_expired_at_reload_d and "FROM organizations" in sql:
            org_expired_at_reload_d.append("id" in harness_d.sa_inspect(org_holder_d[0]).expired_attributes)
        return None

    harness_d = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_owner_id, "name": "Owner Org D"},
            {"id": org_affected_id, "name": "Affected Org D"},
        ],
        provisions=[
            {
                "id": owner_prov_id,
                "organization_id": org_owner_id,
                "provider": "razorpay",
                "merchant_account_id": "acc_test_123",
                "environment": "test",
                "provider_customer_id": "cust_shared_real_pg_4",
                "status": CustomerProvisionStatus.resolved,
            }
        ],
        fail_statement_predicate=_observe_reload_d,
        on_after_tx_end=_after_tx_end_d,
    )

    class CollidingClientD:
        async def create_customer(self, **kwargs):
            return {"id": "cust_shared_real_pg_4"}

    set_razorpay_client_for_testing(CollidingClientD())

    try:
        async with harness_d.session_factory() as db_d:
            org_d = await db_d.get(Organization, org_affected_id)
            assert org_d is not None
            org_holder_d.append(org_d)

            with pytest.raises(RazorpayCustomerPersistenceError) as exc_d:
                await get_or_create_razorpay_customer(
                    organization_id=org_d.id,
                    organization_name=org_d.name,
                    session_factory=harness_d.session_factory,
                )

            err_d = exc_d.value
            assert err_d.customer_id == "cust_shared_real_pg_4"
            assert isinstance(err_d.original_error, IntegrityError)
            assert err_d.recovery_error is None
            assert db_d.in_transaction() is True
            assert "id" not in harness_d.sa_inspect(org_d).expired_attributes
            assert org_d.name == "Affected Org D"
            assert org_expired_at_reload_d == [False]

            owner_row = next(
                r for r in harness_d.tables["billing_customer_provisions"]
                if r["organization_id"] == org_owner_id
            )
            assert owner_row["status"] == "resolved"
            assert owner_row["provider_customer_id"] == "cust_shared_real_pg_4"

            affected_row = next(
                r for r in harness_d.tables["billing_customer_provisions"]
                if r["organization_id"] == org_affected_id
            )
            assert affected_row["status"] == "ambiguous"
            assert affected_row["provider_customer_id"] is None
            assert affected_row["last_error_code"] == "customer_id_org_collision"
            assert len(harness_d.tables["payment_reconciliations"]) == 1
    finally:
        await harness_d.engine.dispose()


# ============================================================================
# Step 5.2D.3.3B.1E-H — D-2 Stage 1 Caller Session Non-Interference Tests
# (Real SQLAlchemy AsyncSession(expire_on_commit=False) over fake asyncpg harness;
# live PG16 execution remains separate.)
# ============================================================================


@pytest.mark.asyncio
async def test_d2_caller_session_unflushed_new_orm_objects_untouched(monkeypatch):
    """D-2 Stage 1 (Real AsyncSession): Caller holding unflushed session.add(obj) keeps it in
    caller_db.new untouched and uncommitted while provisioning completes via injected factory.
    """
    org_id = uuid.uuid4()
    extra_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Caller Primary Org"}],
    )

    def _forbidden_default_factory():
        raise AssertionError("AsyncSessionLocal must not be called when session_factory is injected")

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.AsyncSessionLocal",
        _forbidden_default_factory,
    )

    http_observations: list[dict[str, Any]] = []

    class ObservingClient:
        async def create_customer(self, **kwargs):
            http_observations.append({
                "org_count": len(harness.tables["organizations"]),
                "extra_present": any(o["id"] == extra_org_id for o in harness.tables["organizations"]),
                "last_tx_action": harness.tx_log[-1]["action"],
                "open_tx_balance": sum(
                    1 if e["action"] == "begin" else (-1 if e["action"] in ("commit", "rollback") else 0)
                    for e in harness.tx_log
                ),
            })
            return {"id": "cust_d2_new_orm_1"}

    set_razorpay_client_for_testing(ObservingClient())

    try:
        async with harness.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            assert caller_org is not None
            pending_org = Organization(id=extra_org_id, name="Pending Unflushed Org")
            caller_db.add(pending_org)
            assert pending_org in caller_db.new
            assert caller_db.in_transaction() is True

            cust_id = await get_or_create_razorpay_customer(
                organization_id=caller_org.id,
                organization_name=caller_org.name,
                session_factory=harness.session_factory,
            )

            assert cust_id == "cust_d2_new_orm_1"
            assert http_observations == [
                {
                    "org_count": 1,
                    "extra_present": False,
                    "last_tx_action": "commit",
                    "open_tx_balance": 1,  # Only caller_db's TX is open; tx1_db already committed & exited
                }
            ]
            # Caller's unflushed new ORM object and transaction remain completely untouched
            assert caller_db.in_transaction() is True
            assert pending_org in caller_db.new
            assert all(o["id"] != extra_org_id for o in harness.tables["organizations"])
            assert len(harness.tables["billing_customer_provisions"]) == 1
            assert harness.tables["billing_customer_provisions"][0]["provider_customer_id"] == "cust_d2_new_orm_1"

            # Caller retains sole authority to commit its pending object cleanly
            await caller_db.commit()
            assert pending_org not in caller_db.new
            assert any(o["id"] == extra_org_id for o in harness.tables["organizations"])
            assert len(harness.tables["billing_customer_provisions"]) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_dirty_orm_objects_untouched():
    """D-2 Stage 1 (Real AsyncSession): Caller holding dirty mutated ORM objects keeps them
    dirty/uncommitted while provisioning reads committed org state in its own TX.
    """
    org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Original Committed Org"}],
    )

    seen_remote_names: list[str] = []

    class ObservingClient:
        async def create_customer(self, **kwargs):
            seen_remote_names.append(kwargs["name"])
            return {"id": "cust_d2_dirty_orm_2"}

    set_razorpay_client_for_testing(ObservingClient())

    try:
        async with harness.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            assert caller_org is not None
            caller_org.name = "Uncommitted Dirty Rename"
            assert caller_org in caller_db.dirty

            cust_id = await get_or_create_razorpay_customer(
                organization_id=caller_org.id,
                organization_name=caller_org.name,
                session_factory=harness.session_factory,
            )

            assert cust_id == "cust_d2_dirty_orm_2"
            # Service TX read committed org name, never caller's uncommitted dirty mutation
            assert seen_remote_names == ["Original Committed Org"]
            # Caller's dirty mutation stays uncommitted and intact on caller_db
            assert caller_db.in_transaction() is True
            assert caller_org in caller_db.dirty
            assert caller_org.name == "Uncommitted Dirty Rename"
            assert harness.tables["organizations"][0]["name"] == "Original Committed Org"
            assert harness.tables["billing_customer_provisions"][0]["provider_customer_id"] == "cust_d2_dirty_orm_2"

            # Caller rolls back its dirty rename; provision stays durably committed
            await caller_db.rollback()
            assert harness.tables["organizations"][0]["name"] == "Original Committed Org"
            assert len(harness.tables["billing_customer_provisions"]) == 1
            assert harness.tables["billing_customer_provisions"][0]["status"] == "resolved"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_deleted_orm_objects_untouched():
    """D-2 Stage 1 (Real AsyncSession): Caller holding pending session.delete(obj) keeps it
    in caller_db.deleted without premature commit or loss, even across service errors/rollbacks.
    """
    org_id = uuid.uuid4()
    victim_org_id = uuid.uuid4()
    owner_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_id, "name": "Affected Caller Org"},
            {"id": victim_org_id, "name": "Pending Delete Victim Org"},
            {"id": owner_org_id, "name": "Collision Owner Org"},
        ],
        provisions=[
            {
                "id": uuid.uuid4(),
                "organization_id": owner_org_id,
                "provider": "razorpay",
                "merchant_account_id": "acc_test_123",
                "environment": "test",
                "provider_customer_id": "cust_shared_deleted_test",
                "status": CustomerProvisionStatus.resolved,
            }
        ],
    )

    class CollidingClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_shared_deleted_test"}

    set_razorpay_client_for_testing(CollidingClient())

    try:
        async with harness.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            victim_org = await caller_db.get(Organization, victim_org_id)
            assert caller_org is not None and victim_org is not None

            await caller_db.delete(victim_org)
            assert victim_org in caller_db.deleted

            # Service hits TX_A2 IntegrityError (D-6 collision) and rolls back its internal TX_A2 + commits recon
            with pytest.raises(RazorpayCustomerPersistenceError):
                await get_or_create_razorpay_customer(
                    organization_id=caller_org.id,
                    organization_name=caller_org.name,
                    session_factory=harness.session_factory,
                )

            # Caller's pending delete & open transaction survived untouched!
            assert caller_db.in_transaction() is True
            assert victim_org in caller_db.deleted
            assert any(o["id"] == victim_org_id for o in harness.tables["organizations"])

            # Now caller commits its own delete cleanly
            await caller_db.commit()
            assert all(o["id"] != victim_org_id for o in harness.tables["organizations"])
            assert len(harness.tables["payment_reconciliations"]) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_previously_flushed_writes_untouched():
    """D-2 Stage 1 (Real AsyncSession): Caller holding previously flushed writes
    (where new/dirty/deleted are all empty) never gets prematurely committed by provisioning
    and can later roll back its flushed write while provisioning remains committed.
    """
    org_id = uuid.uuid4()
    flushed_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Caller Primary Org"}],
    )

    class SuccessClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_d2_flushed_4"}

    set_razorpay_client_for_testing(SuccessClient())

    try:
        async with harness.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            assert caller_org is not None
            flushed_org = Organization(id=flushed_org_id, name="Flushed Pending Org")
            caller_db.add(flushed_org)
            await caller_db.flush()

            # Flushed writes leave ORM collections empty while DB TX remains open!
            assert (len(caller_db.new), len(caller_db.dirty), len(caller_db.deleted)) == (0, 0, 0)
            assert caller_db.in_transaction() is True
            assert all(o["id"] != flushed_org_id for o in harness.tables["organizations"])

            cust_id = await get_or_create_razorpay_customer(
                organization_id=caller_org.id,
                organization_name=caller_org.name,
                session_factory=harness.session_factory,
            )

            assert cust_id == "cust_d2_flushed_4"
            assert caller_db.in_transaction() is True
            assert "id" not in harness.sa_inspect(flushed_org).expired_attributes
            # Caller's flushed org was NOT committed by provisioning
            assert all(o["id"] != flushed_org_id for o in harness.tables["organizations"])
            assert len(harness.tables["billing_customer_provisions"]) == 1
            assert harness.tables["billing_customer_provisions"][0]["provider_customer_id"] == "cust_d2_flushed_4"

            # Caller rolls back its flushed write; provisioning stays committed!
            await caller_db.rollback()
            assert all(o["id"] != flushed_org_id for o in harness.tables["organizations"])
            assert len(harness.tables["billing_customer_provisions"]) == 1
            assert harness.tables["billing_customer_provisions"][0]["status"] == "resolved"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_core_sql_dml_untouched():
    """D-2 Stage 1 (Real AsyncSession): Caller executing Core SQL DML (update/insert/delete)
    with empty ORM new/dirty/deleted retains full commit/rollback control independently.
    """
    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    temp_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_id, "name": "Primary Org"},
            {"id": other_org_id, "name": "Initial Other Org"},
            {"id": temp_org_id, "name": "Temp Delete Org"},
        ],
    )

    class SuccessClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_d2_core_dml_5"}

    set_razorpay_client_for_testing(SuccessClient())

    try:
        # Sub-case A: Core UPDATE in caller TX -> caller rolls back -> update discarded, provision preserved
        async with harness.session_factory() as caller_db:
            await caller_db.execute(
                update(Organization)
                .where(Organization.id == other_org_id)
                .values(name="Core DML Rolled Back")
            )
            assert (len(caller_db.new), len(caller_db.dirty), len(caller_db.deleted)) == (0, 0, 0)
            assert caller_db.in_transaction() is True

            cust_id = await get_or_create_razorpay_customer(
                organization_id=org_id,
                organization_name="Primary Org",
                session_factory=harness.session_factory,
            )
            assert cust_id == "cust_d2_core_dml_5"
            assert caller_db.in_transaction() is True
            other_row = next(o for o in harness.tables["organizations"] if o["id"] == other_org_id)
            assert other_row["name"] == "Initial Other Org"

            await caller_db.rollback()
            other_row_after = next(o for o in harness.tables["organizations"] if o["id"] == other_org_id)
            assert other_row_after["name"] == "Initial Other Org"
            assert len(harness.tables["billing_customer_provisions"]) == 1

        # Sub-case B: Core INSERT + DELETE in caller TX -> reuse customer -> caller commits -> DML persists
        inserted_org_id = uuid.uuid4()
        async with harness.session_factory() as caller_db_2:
            await caller_db_2.execute(
                insert(Organization).values(id=inserted_org_id, name="Core Inserted Org")
            )
            await caller_db_2.execute(
                delete(Organization).where(Organization.id == temp_org_id)
            )
            cust_id_2 = await get_or_create_razorpay_customer(
                organization_id=org_id,
                organization_name="Primary Org",
                session_factory=harness.session_factory,
            )
            assert cust_id_2 == "cust_d2_core_dml_5"
            assert caller_db_2.in_transaction() is True
            assert all(o["id"] != inserted_org_id for o in harness.tables["organizations"])
            assert any(o["id"] == temp_org_id for o in harness.tables["organizations"])

            await caller_db_2.commit()
            assert any(o["id"] == inserted_org_id for o in harness.tables["organizations"])
            assert all(o["id"] != temp_org_id for o in harness.tables["organizations"])
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_active_begin_block_and_rollback_safety():
    """D-2 Stage 1 (Real AsyncSession): Calling get_or_create_razorpay_customer inside an
    explicit `async with caller_db.begin():` block causes no InvalidRequestError on exit,
    preserves commit on clean exit, and preserves caller rollback on block exception.
    """
    org_id = uuid.uuid4()
    rolled_back_org_id = uuid.uuid4()
    committed_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "Begin Block Org"}],
    )

    class SuccessClient:
        async def create_customer(self, **kwargs):
            return {"id": "cust_d2_begin_6"}

    set_razorpay_client_for_testing(SuccessClient())

    try:
        # Sub-case A: Exception inside caller `async with caller_db.begin():` rolls back caller write only
        async with harness.session_factory() as caller_db_a:
            with pytest.raises(RuntimeError, match="Simulated caller failure"):
                async with caller_db_a.begin():
                    caller_db_a.add(Organization(id=rolled_back_org_id, name="Rolled Back Begin Org"))
                    cust_a = await get_or_create_razorpay_customer(
                        organization_id=org_id,
                        organization_name="Begin Block Org",
                        session_factory=harness.session_factory,
                    )
                    assert cust_a == "cust_d2_begin_6"
                    assert caller_db_a.in_transaction() is True
                    raise RuntimeError("Simulated caller failure")

            assert all(o["id"] != rolled_back_org_id for o in harness.tables["organizations"])
            assert harness.tables["billing_customer_provisions"][0]["provider_customer_id"] == "cust_d2_begin_6"

        # Sub-case B: Normal completion of `async with caller_db.begin():` commits caller writes cleanly
        async with harness.session_factory() as caller_db_b:
            async with caller_db_b.begin():
                caller_db_b.add(Organization(id=committed_org_id, name="Committed Begin Org"))
                cust_b = await get_or_create_razorpay_customer(
                    organization_id=org_id,
                    organization_name="Begin Block Org",
                    session_factory=harness.session_factory,
                )
                assert cust_b == "cust_d2_begin_6"
                assert caller_db_b.in_transaction() is True

            assert any(o["id"] == committed_org_id for o in harness.tables["organizations"])
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_d2_caller_session_identity_map_intact_across_internal_rollbacks_and_default_factory(
    monkeypatch,
):
    """D-2 Stage 1 (Real AsyncSession): Pre-loaded caller identity_map objects remain unexpired
    (zero MissingGreenlet) even across internal provisioning rollbacks; default AsyncSessionLocal
    is used only when session_factory is None and never used when session_factory is injected.
    """
    org_id = uuid.uuid4()
    extra_org_id = uuid.uuid4()
    harness = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_id, "name": "Identity Map Org"},
            {"id": extra_org_id, "name": "Unrelated Cached Org"},
        ],
    )

    default_factory_calls = 0

    def _counting_default_factory():
        nonlocal default_factory_calls
        default_factory_calls += 1
        return harness.session_factory()

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.AsyncSessionLocal",
        _counting_default_factory,
    )

    fail_tx2_once = [True]

    # Induce a real internal TX_A2 failure via mock commit failure on provision resolution
    orig_client_calls = 0

    class FailingPersistenceClient:
        async def create_customer(self, **kwargs):
            nonlocal orig_client_calls
            orig_client_calls += 1
            return {"id": "cust_d2_identity_7"}

    set_razorpay_client_for_testing(FailingPersistenceClient())

    # Inject a single-shot TX_A2 failure via fail_commit_predicate? Build dedicated harness for failure sub-case
    harness_fail = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_id, "name": "Identity Map Fail Org"},
            {"id": extra_org_id, "name": "Unrelated Cached Fail Org"},
        ],
        fail_commit_predicate=lambda staging, ops: (
            harness_fail.asyncpg.exceptions.InternalServerError("simulated TX_A2 outage")
            if fail_tx2_once[0]
            and any(
                o["op"] == "update"
                and o["table"] == "billing_customer_provisions"
                and o["updates"].get("status") == "resolved"
                for o in ops
            )
            else None
        ),
    )

    try:
        # Part 1: Injected factory during internal TX_A2 rollback -> caller identity_map intact, AsyncSessionLocal uncalled
        async with harness_fail.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            unrelated_org = await caller_db.get(Organization, extra_org_id)
            assert caller_org is not None and unrelated_org is not None
            initial_keys = set(caller_db.identity_map.keys())

            with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
                await get_or_create_razorpay_customer(
                    organization_id=caller_org.id,
                    organization_name=caller_org.name,
                    session_factory=harness_fail.session_factory,
                )

            assert exc_info.value.customer_id == "cust_d2_identity_7"
            assert default_factory_calls == 0
            assert caller_db.in_transaction() is True
            assert set(caller_db.identity_map.keys()) == initial_keys
            assert caller_db.identity_map[(Organization, (org_id,), None)] is caller_org
            assert caller_db.identity_map[(Organization, (extra_org_id,), None)] is unrelated_org
            assert "id" not in harness_fail.sa_inspect(caller_org).expired_attributes
            assert "name" not in harness_fail.sa_inspect(caller_org).expired_attributes
            assert "name" not in harness_fail.sa_inspect(unrelated_org).expired_attributes
            assert caller_org.name == "Identity Map Fail Org"
            assert unrelated_org.name == "Unrelated Cached Fail Org"

        # Part 2: Omitting session_factory falls back cleanly to AsyncSessionLocal
        cust_default = await get_or_create_razorpay_customer(
            organization_id=org_id,
            organization_name="Identity Map Org",
        )
        assert cust_default == "cust_d2_identity_7"
        assert default_factory_calls >= 2  # TX_A1 + TX_A2
    finally:
        await harness_fail.engine.dispose()
        await harness.engine.dispose()


# ============================================================================
# Step 5.2D.3.3B.1E-J — S1-F1 Secondary Integrity Recovery Error Handling Tests
# (Real SQLAlchemy AsyncSession(expire_on_commit=False) over fake asyncpg harness;
# live PostgreSQL 16 execution remains separate.)
# ============================================================================


def _wrap_tracked_session_factory(
    base_factory: Any,
    *,
    on_call: Callable[[int], None] | None = None,
    fail_on_call_factory: dict[int, Exception] | None = None,
    fail_on_call_aenter: dict[int, BaseException] | None = None,
    fail_on_call_aexit: dict[int, BaseException] | None = None,
):
    """Wrap an AsyncSessionFactory to observe every session open/close and inject phase faults."""
    records: list[dict[str, Any]] = []
    call_counter = 0

    def _factory():
        nonlocal call_counter
        call_counter += 1
        call_num = call_counter
        if on_call is not None:
            on_call(call_num)

        if fail_on_call_factory and call_num in fail_on_call_factory:
            err = fail_on_call_factory[call_num]
            records.append({
                "call_num": call_num,
                "stage": "factory_raised",
                "entered": False,
                "exited": False,
                "session": None,
                "in_tx_after_exit": None,
                "exc_type_on_exit": type(err).__name__,
            })
            raise err

        inner_cm = base_factory()
        rec: dict[str, Any] = {
            "call_num": call_num,
            "stage": "created",
            "entered": False,
            "exited": False,
            "session": None,
            "in_tx_after_exit": None,
            "exc_type_on_exit": None,
        }
        records.append(rec)

        class _TrackedScope:
            async def __aenter__(self):
                if fail_on_call_aenter and call_num in fail_on_call_aenter:
                    err = fail_on_call_aenter[call_num]
                    rec["stage"] = "aenter_failed"
                    rec["exc_type_on_exit"] = type(err).__name__
                    raise err
                sess = await inner_cm.__aenter__()
                rec["session"] = sess
                rec["entered"] = True
                rec["stage"] = "active"
                return sess

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
                rec["exc_type_on_exit"] = exc_type.__name__ if exc_type is not None else None
                try:
                    res = await inner_cm.__aexit__(exc_type, exc, tb)
                    return res
                finally:
                    rec["exited"] = True
                    rec["stage"] = "closed"
                    if rec["session"] is not None:
                        rec["in_tx_after_exit"] = rec["session"].in_transaction()
                    if fail_on_call_aexit and call_num in fail_on_call_aexit:
                        raise fail_on_call_aexit[call_num]

        return _TrackedScope()

    return _factory, records


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["factory_call", "aenter"])
async def test_s1_f1_secondary_integrity_session_factory_creation_failure(failure_point: str):
    """S1-F1 Req 1,4,5,6,7 (Real AsyncSession):
    If creating or entering `secondary_db` during `action == 'secondary_integrity'` raises an
    unexpected DB/factory exception, convert it into RazorpayCustomerPersistenceError (never leak raw
    exception), preserve both original persistence error and secondary error + secondary_integrity,
    include org/customer context, never claim recovery succeeded, and leave prior & caller sessions clean.
    """
    from sqlalchemy.exc import DBAPIError

    org_id = uuid.uuid4()
    cust_id = f"cust_s1f1_factory_{failure_point}"

    def _fail_commits(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness.asyncpg.exceptions.InternalServerError("primary TX_A2 write failure")
                if o["updates"].get("status") == "ambiguous":
                    return harness.make_unique_error("ck_secondary_integrity_non_collision")
        return None

    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "S1F1 Factory Fail Org"}],
        fail_commit_predicate=_fail_commits,
    )

    class SingleClient:
        async def create_customer(self, **kwargs):
            return {"id": cust_id}

    set_razorpay_client_for_testing(SingleClient())

    factory_err = OperationalError("secondary session_factory pool failure", params={}, orig=Exception("pool down"))
    aenter_err = OperationalError("secondary session __aenter__ refused", params={}, orig=Exception("conn refused"))

    # Call #1: tx1_db, Call #2: tx2_db, Call #3: recovery_db, Call #4: secondary_db
    tracked_factory, session_records = _wrap_tracked_session_factory(
        harness.session_factory,
        fail_on_call_factory={4: factory_err} if failure_point == "factory_call" else None,
        fail_on_call_aenter={4: aenter_err} if failure_point == "aenter" else None,
    )

    try:
        async with harness.session_factory() as caller_db:
            caller_org = await caller_db.get(Organization, org_id)
            assert caller_org is not None

            with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
                await get_or_create_razorpay_customer(
                    organization_id=caller_org.id,
                    organization_name=caller_org.name,
                    session_factory=tracked_factory,
                )

            err = exc_info.value
            expected_secondary = factory_err if failure_point == "factory_call" else aenter_err
            assert type(err) is RazorpayCustomerPersistenceError
            assert err.customer_id == cust_id
            assert str(org_id) in str(err)
            assert cust_id in str(err)
            assert "secondary integrity recovery failed" in str(err)
            assert isinstance(err.original_error, DBAPIError)
            assert "primary TX_A2 write failure" in str(err.original_error)
            assert err.recovery_error is expected_secondary
            assert err.__cause__ is expected_secondary
            assert isinstance(err.__context__, IntegrityError)
            assert "ck_secondary_integrity_non_collision" in str(err.__context__)

            # All 3 opened real AsyncSessions (tx1, tx2, recovery) entered & closed cleanly with no open TX
            assert len(session_records) == 4
            for rec in session_records[:3]:
                assert rec["entered"] is True
                assert rec["exited"] is True
                assert rec["in_tx_after_exit"] is False
            assert session_records[3]["entered"] is False

            # Caller session & identity map completely untouched
            assert caller_db.in_transaction() is True
            assert caller_org.name == "S1F1 Factory Fail Org"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_query", ["lock_org", "find_provision", "find_conflicting_owner"])
async def test_s1_f1_secondary_integrity_query_failure(failing_query: str):
    """S1-F1 Req 2,4,5,6,7 (Real AsyncSession):
    Query failures inside secondary_db (_lock_organization, _find_provision, or
    _find_conflicting_customer_owner) must roll back and close secondary_db, never leak raw DB errors,
    preserve original_error and secondary recovery error, include org/customer context, and never
    claim recovery succeeded.
    """
    from sqlalchemy.exc import DBAPIError

    org_id = uuid.uuid4()
    cust_id = f"cust_s1f1_query_{failing_query}"
    in_secondary_phase = [False]
    prov_selects_in_secondary = [0]

    def _fail_commits(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness.asyncpg.exceptions.InternalServerError("tx2 primary outage")
                if o["updates"].get("status") == "ambiguous":
                    in_secondary_phase[0] = True
                    return harness.make_unique_error("ck_secondary_query_test")
        return None

    def _fail_stmt(tables, sql, params):
        if not in_secondary_phase[0]:
            return None
        if failing_query == "lock_org" and "FROM organizations" in sql:
            return harness.asyncpg.exceptions.InternalServerError("secondary _lock_organization failed")
        if "FROM billing_customer_provisions" in sql:
            prov_selects_in_secondary[0] += 1
            if failing_query == "find_provision" and prov_selects_in_secondary[0] == 1:
                return harness.asyncpg.exceptions.InternalServerError("secondary _find_provision failed")
            if failing_query == "find_conflicting_owner" and prov_selects_in_secondary[0] == 2:
                return harness.asyncpg.exceptions.InternalServerError("secondary _find_conflicting_customer_owner failed")
        return None

    harness = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id, "name": "S1F1 Query Fail Org"}],
        fail_commit_predicate=_fail_commits,
        fail_statement_predicate=_fail_stmt,
    )

    class SingleClient:
        async def create_customer(self, **kwargs):
            return {"id": cust_id}

    set_razorpay_client_for_testing(SingleClient())
    tracked_factory, session_records = _wrap_tracked_session_factory(harness.session_factory)

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
            await get_or_create_razorpay_customer(
                organization_id=org_id,
                organization_name="S1F1 Query Fail Org",
                session_factory=tracked_factory,
            )

        err = exc_info.value
        assert isinstance(err, RazorpayCustomerPersistenceError)
        assert err.customer_id == cust_id
        assert str(org_id) in str(err)
        assert isinstance(err.original_error, DBAPIError)
        assert "tx2 primary outage" in str(err.original_error)
        assert isinstance(err.recovery_error, DBAPIError)
        assert f"secondary _{'lock_organization' if failing_query == 'lock_org' else ('find_provision' if failing_query == 'find_provision' else 'find_conflicting_customer_owner')} failed" in str(err.recovery_error)
        assert err.__cause__ is err.recovery_error
        assert isinstance(err.__context__, IntegrityError)
        assert "ck_secondary_query_test" in str(err.__context__)
        assert "secondary integrity recovery failed" in str(err)

        # Every session (tx1, tx2, recovery, secondary) entered and exited cleanly
        assert len(session_records) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in session_records)
        # Secondary session transaction rolled back cleanly
        assert harness.tx_log[-1]["action"] == "rollback"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_s1_f1_secondary_integrity_transaction_and_commit_failure():
    """S1-F1 Req 3,4,5,6,7 (Real AsyncSession):
    Verify transaction/commit failures during secondary_integrity recovery across:
    (A) TX_A2 non-IntegrityError -> recovery_db secondary_integrity -> secondary_db TX/exit failure,
    (B) TX_A2 non-collision IntegrityError -> reload_db secondary_integrity -> secondary_db query/TX failure,
    (C) Cross-org collision detected in secondary_db whose reconciliation commit fails -> fallback marks claim ambiguous.
    """
    from sqlalchemy.exc import DBAPIError

    # --- Sub-case A: secondary_db TX close / __aexit__ failure after fallback IntegrityError ---
    org_id_a = uuid.uuid4()
    cust_a = "cust_s1f1_tx_aexit_fail"
    tx_exit_err = OperationalError("secondary TX commit/close failure", params={}, orig=Exception("tx close lost"))

    def _fail_commits_a(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_a.asyncpg.exceptions.InternalServerError("primary tx2 failure A")
                if o["updates"].get("status") == "ambiguous":
                    return harness_a.make_unique_error("ck_secondary_tx_fail_a")
        return None

    harness_a = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_a, "name": "S1F1 TX Fail A"}],
        fail_commit_predicate=_fail_commits_a,
    )

    class ClientA:
        async def create_customer(self, **kwargs):
            return {"id": cust_a}

    set_razorpay_client_for_testing(ClientA())
    tracked_a, records_a = _wrap_tracked_session_factory(
        harness_a.session_factory,
        fail_on_call_aexit={4: tx_exit_err},
    )

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_a:
            await get_or_create_razorpay_customer(
                organization_id=org_id_a,
                organization_name="S1F1 TX Fail A",
                session_factory=tracked_a,
            )

        err_a = exc_a.value
        assert err_a.customer_id == cust_a
        assert str(org_id_a) in str(err_a)
        assert isinstance(err_a.original_error, DBAPIError)
        assert "primary tx2 failure A" in str(err_a.original_error)
        assert err_a.recovery_error is tx_exit_err
        assert err_a.__cause__ is tx_exit_err
        assert isinstance(err_a.__context__, IntegrityError)
        assert "secondary integrity recovery failed" in str(err_a)
        assert len(records_a) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_a)
    finally:
        await harness_a.engine.dispose()

    # --- Sub-case B: _handle_tx2_integrity_error_with_factory -> reload_db secondary_integrity -> secondary_db failure ---
    org_id_b = uuid.uuid4()
    cust_b = "cust_s1f1_reload_sec_fail"
    armed_secondary_b = [False]

    def _fail_commits_b(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_b.make_unique_error("ck_tx2_non_collision_b")
                if o["updates"].get("status") == "ambiguous":
                    armed_secondary_b[0] = True
                    return harness_b.make_unique_error("ck_reload_secondary_b")
        return None

    def _fail_stmt_b(tables, sql, params):
        if armed_secondary_b[0] and "FROM organizations" in sql:
            return harness_b.asyncpg.exceptions.InternalServerError("secondary tx begin/select failure B")
        return None

    harness_b = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_b, "name": "S1F1 TX2 Reload Sec Fail Org"}],
        fail_commit_predicate=_fail_commits_b,
        fail_statement_predicate=_fail_stmt_b,
    )

    class ClientB:
        async def create_customer(self, **kwargs):
            return {"id": cust_b}

    set_razorpay_client_for_testing(ClientB())
    tracked_b, records_b = _wrap_tracked_session_factory(harness_b.session_factory)

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_b:
            await get_or_create_razorpay_customer(
                organization_id=org_id_b,
                organization_name="S1F1 TX2 Reload Sec Fail Org",
                session_factory=tracked_b,
            )

        err_b = exc_b.value
        assert err_b.customer_id == cust_b
        assert str(org_id_b) in str(err_b)
        assert isinstance(err_b.original_error, IntegrityError)
        assert "ck_tx2_non_collision_b" in str(err_b.original_error)
        assert isinstance(err_b.recovery_error, DBAPIError)
        assert "secondary tx begin/select failure B" in str(err_b.recovery_error)
        assert err_b.__cause__ is err_b.recovery_error
        assert isinstance(err_b.__context__, IntegrityError)
        assert "ck_reload_secondary_b" in str(err_b.__context__)
        assert len(records_b) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_b)
    finally:
        await harness_b.engine.dispose()

    # --- Sub-case C: Collision detected in secondary_db, whose recon commit fails -> 5th fallback session runs ---
    org_owner_id = uuid.uuid4()
    org_c_id = uuid.uuid4()
    cust_c = "cust_s1f1_collision_commit_fail"
    owner_injected = [False]

    def _inject_owner_after_tx2(tables, action, last_err, log):
        if not owner_injected[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(
                o["op"] == "update"
                and o["table"] == "billing_customer_provisions"
                and o["updates"].get("status") == "resolved"
                for o in last_ops
            ):
                owner_injected[0] = True
                tables["billing_customer_provisions"].append({
                    "id": uuid.uuid4(),
                    "organization_id": org_owner_id,
                    "provider": "razorpay",
                    "merchant_account_id": "acc_test_123",
                    "environment": "test",
                    "provider_customer_id": cust_c,
                    "status": CustomerProvisionStatus.resolved.value,
                    "claim_operation_id": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                })

    def _fail_commits_c(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_c.asyncpg.exceptions.InternalServerError("primary TX2 timeout C")
        if any(o["op"] == "insert" and o["table"] == "payment_reconciliations" for o in ops):
            return harness_c.asyncpg.exceptions.InternalServerError("secondary recon commit failed C")
        return None

    harness_c = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_owner_id, "name": "Owner Org C"},
            {"id": org_c_id, "name": "Affected Org C"},
        ],
        fail_commit_predicate=_fail_commits_c,
        on_after_tx_end=_inject_owner_after_tx2,
    )

    class ClientC:
        async def create_customer(self, **kwargs):
            return {"id": cust_c}

    set_razorpay_client_for_testing(ClientC())
    tracked_c, records_c = _wrap_tracked_session_factory(harness_c.session_factory)

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_c:
            await get_or_create_razorpay_customer(
                organization_id=org_c_id,
                organization_name="Affected Org C",
                session_factory=tracked_c,
            )

        err_c = exc_c.value
        assert err_c.customer_id == cust_c
        assert str(org_c_id) in str(err_c)
        assert isinstance(err_c.original_error, IntegrityError)
        assert isinstance(err_c.recovery_error, DBAPIError)
        assert "secondary recon commit failed C" in str(err_c.recovery_error)
        assert err_c.__cause__ is err_c.recovery_error
        assert "reconciliation persistence failed" in str(err_c)
        assert "claim transitioned to ambiguous without conflicting customer_id" in str(err_c)
        assert len(harness_c.tables["payment_reconciliations"]) == 0

        owner_row = next(
            p for p in harness_c.tables["billing_customer_provisions"] if p["organization_id"] == org_owner_id
        )
        affected_row = next(
            p for p in harness_c.tables["billing_customer_provisions"] if p["organization_id"] == org_c_id
        )
        assert owner_row["status"] == "resolved"
        assert owner_row["provider_customer_id"] == cust_c
        assert affected_row["status"] == "ambiguous"
        assert affected_row["provider_customer_id"] is None
        assert affected_row["last_error_code"] == "reconciliation_persist_failed"

        # 5 independent sessions: tx1, tx2, recovery, secondary (recon fail), fallback (ambiguous)
        assert len(records_c) == 5
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_c)
    finally:
        await harness_c.engine.dispose()


@pytest.mark.asyncio
async def test_s1_f1_secondary_integrity_preserves_exact_error_objects_and_cleanup_on_direct_handoff(
    monkeypatch,
):
    """S1-F1 Req 4,5,6,7: Directly verify exact identity preservation of original_error,
    secondary_integrity (__context__), secondary recovery error (recovery_error & __cause__),
    RazorpayCustomerPersistenceError classification, org/customer context, and rollback + close cleanup
    even when collision recon failed AND __aexit__ subsequently fails.
    """
    from app.services.billing.razorpay.customer import (
        _PersistenceRecoveryHandoff,
        _finalize_persistence_recovery_handoff_and_raise,
    )

    org_id = uuid.uuid4()
    cust_id = "cust_s1f1_exact_identity"
    primary_err = OperationalError("exact primary persistence error", params={}, orig=Exception("p"))
    sec_integrity = _make_pg_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
    recon_commit_err = OperationalError("exact recon commit err", params={}, orig=Exception("r"))
    aexit_close_err = OperationalError("exact __aexit__ close err", params={}, orig=Exception("x"))

    # Case 1: Pre-commit query error inside secondary_db -> exact objects preserved + rolled back + closed
    db1 = InMemoryDbSession(organizations=[Organization(id=org_id, name="Identity S1F1")])
    query_err = OperationalError("exact query failure", params={}, orig=Exception("q"))

    async def _fail_lock_org(db: Any, oid: uuid.UUID):
        await db.execute(select(Organization).where(Organization.id == oid))
        raise query_err

    handoff1 = _PersistenceRecoveryHandoff(action="secondary_integrity", secondary_integrity=sec_integrity)

    with monkeypatch.context() as scoped_mp:
        scoped_mp.setattr("app.services.billing.razorpay.customer._lock_organization", _fail_lock_org)
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_1:
            await _finalize_persistence_recovery_handoff_and_raise(
                session_factory=db1.as_session_factory(),
                organization_id=org_id,
                customer_id=cust_id,
                claim_op_id="claim_exact_1",
                original_error=primary_err,
                handoff=handoff1,
                merchant_account_id="acc_test_123",
                environment="test",
            )

    err_1 = exc_1.value
    assert err_1.customer_id == cust_id
    assert str(org_id) in str(err_1)
    assert err_1.original_error is primary_err
    assert err_1.recovery_error is query_err
    assert err_1.__cause__ is query_err
    assert err_1.__context__ is sec_integrity
    assert db1.session_scopes_opened == 1
    assert db1.session_scopes_closed == 1
    assert db1.rolled_back_count >= 1
    assert db1.in_transaction() is False

    # Case 2: Collision recon commit failed AND secondary_db __aexit__ ALSO raised -> claim fallback still runs!
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id=None,
        status=CustomerProvisionStatus.pending,
        claim_operation_id="claim_exact_2",
    )
    db2 = ConstraintEnforcingDbSession(
        organizations=[Organization(id=org_id, name="Claim Fallback After Aexit")],
        provisions=[prov],
        reconciliation_commit_errors=[recon_commit_err],
    )
    tracked_2, records_2 = _wrap_tracked_session_factory(
        db2.as_session_factory(),
        fail_on_call_aexit={1: aexit_close_err},
    )

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_2:
        await _finalize_persistence_recovery_handoff_and_raise(
            session_factory=tracked_2,
            organization_id=org_id,
            customer_id=cust_id,
            claim_op_id="claim_exact_2",
            original_error=primary_err,
            handoff=handoff1,
            merchant_account_id="acc_test_123",
            environment="test",
        )

    err_2 = exc_2.value
    assert err_2.customer_id == cust_id
    assert err_2.original_error is sec_integrity
    assert err_2.recovery_error is recon_commit_err
    assert err_2.__cause__ is recon_commit_err
    assert prov.status == CustomerProvisionStatus.ambiguous
    assert prov.provider_customer_id is None
    assert prov.last_error_code == "reconciliation_persist_failed"
    assert len(records_2) == 2
    assert all(r["entered"] and r["exited"] for r in records_2)
    assert db2.in_transaction() is False


@pytest.mark.asyncio
async def test_s1_f1_secondary_integrity_cancellation_propagates_and_closes_real_session(monkeypatch):
    """S1-F1 Req 7,8 (Real AsyncSession + asyncio.Task.cancel()):
    asyncio.CancelledError arising during secondary_db creation, active query (via real Task.cancel()),
    or collision commit must propagate cleanly without being wrapped into RazorpayCustomerPersistenceError,
    while every created AsyncSession is properly closed on cancellation and caller session remains untouched.
    """
    from app.services.billing.razorpay import customer as customer_mod

    # --- Sub-case A: Real task.cancel() while secondary_db is suspended inside a query ---
    org_id_a = uuid.uuid4()
    cust_a = "cust_s1f1_cancel_task"

    def _fail_commits_a(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_a.asyncpg.exceptions.InternalServerError("tx2 error before cancel")
                if o["updates"].get("status") == "ambiguous":
                    return harness_a.make_unique_error("ck_secondary_cancel_task")
        return None

    harness_a = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_a, "name": "S1F1 Cancel Task Org"}],
        fail_commit_predicate=_fail_commits_a,
    )
    tracked_a, records_a = _wrap_tracked_session_factory(harness_a.session_factory)
    orig_find_conflict = customer_mod._find_conflicting_customer_owner
    entered_secondary_query = asyncio.Event()
    never_resolved = asyncio.Event()

    async def _cancellable_conflict_lookup(db, organization_id, customer_id):
        entered_secondary_query.set()
        await never_resolved.wait()
        return await orig_find_conflict(db, organization_id, customer_id)

    class ClientA:
        async def create_customer(self, **kwargs):
            return {"id": cust_a}

    set_razorpay_client_for_testing(ClientA())

    try:
        with monkeypatch.context() as scoped_mp:
            scoped_mp.setattr(customer_mod, "_find_conflicting_customer_owner", _cancellable_conflict_lookup)
            async with harness_a.session_factory() as caller_db:
                caller_org = await caller_db.get(Organization, org_id_a)
                assert caller_org is not None

                task = asyncio.create_task(
                    get_or_create_razorpay_customer(
                        organization_id=caller_org.id,
                        organization_name=caller_org.name,
                        session_factory=tracked_a,
                    )
                )
                await asyncio.wait_for(entered_secondary_query.wait(), timeout=2.0)
                task.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await task

                # Real AsyncSession.__aexit__ ran and closed secondary_db despite CancelledError!
                assert len(records_a) == 4
                assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_a)
                assert caller_db.in_transaction() is True
                assert caller_org.name == "S1F1 Cancel Task Org"
    finally:
        await harness_a.engine.dispose()

    # --- Sub-case B: CancelledError during secondary_db __aenter__ ---
    org_id_b = uuid.uuid4()
    harness_b = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_b, "name": "S1F1 Cancel Aenter Org"}],
        fail_commit_predicate=lambda s, ops: (
            harness_b.asyncpg.exceptions.InternalServerError("tx2 err B")
            if any(o["op"] == "update" and o["updates"].get("status") == "resolved" for o in ops)
            else (
                harness_b.make_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
                if any(o["op"] == "update" and o["updates"].get("status") == "ambiguous" for o in ops)
                else None
            )
        ),
    )

    class ClientB:
        async def create_customer(self, **kwargs):
            return {"id": "cust_s1f1_cancel_b"}

    set_razorpay_client_for_testing(ClientB())
    tracked_b1, records_b1 = _wrap_tracked_session_factory(
        harness_b.session_factory,
        fail_on_call_aenter={4: asyncio.CancelledError()},
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await get_or_create_razorpay_customer(
                organization_id=org_id_b,
                organization_name="S1F1 Cancel Aenter Org",
                session_factory=tracked_b1,
            )
        assert len(records_b1) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_b1[:3])
        assert records_b1[3]["entered"] is False
    finally:
        await harness_b.engine.dispose()

    # --- Sub-case C: Real task.cancel() inside collision commit during secondary_integrity ---
    org_id_c = uuid.uuid4()
    harness_c = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_c, "name": "S1F1 Cancel Collision Recon Org"}],
        fail_commit_predicate=lambda s, ops: (
            harness_c.asyncpg.exceptions.InternalServerError("tx2 err C")
            if any(o["op"] == "update" and o["updates"].get("status") == "resolved" for o in ops)
            else (
                harness_c.make_unique_error(PROVIDER_CUSTOMER_UNIQUE_INDEX)
                if any(o["op"] == "update" and o["updates"].get("status") == "ambiguous" for o in ops)
                else None
            )
        ),
    )

    class ClientC:
        async def create_customer(self, **kwargs):
            return {"id": "cust_s1f1_cancel_c"}

    set_razorpay_client_for_testing(ClientC())
    tracked_c, records_c = _wrap_tracked_session_factory(harness_c.session_factory)
    orig_stage_recon = customer_mod.stage_or_update_payment_reconciliation
    entered_collision_stage = asyncio.Event()
    never_finish_stage = asyncio.Event()

    async def _cancellable_stage(*args, **kwargs):
        entered_collision_stage.set()
        await never_finish_stage.wait()
        return await orig_stage_recon(*args, **kwargs)

    try:
        with monkeypatch.context() as scoped_mp_c:
            scoped_mp_c.setattr(customer_mod, "stage_or_update_payment_reconciliation", _cancellable_stage)
            task_c = asyncio.create_task(
                get_or_create_razorpay_customer(
                    organization_id=org_id_c,
                    organization_name="S1F1 Cancel Collision Recon Org",
                    session_factory=tracked_c,
                )
            )
            await asyncio.wait_for(entered_collision_stage.wait(), timeout=2.0)
            task_c.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_c

            assert len(records_c) == 4
            assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_c)
    finally:
        await harness_c.engine.dispose()


@pytest.mark.asyncio
async def test_s1_f1_existing_successful_secondary_integrity_recovery_preserved():
    """S1-F1 Req 9 (Real AsyncSession):
    Verify existing non-failing `secondary_integrity` recovery paths remain completely unchanged:
    (A) Non-collision IntegrityError in fallback preserves original_error and secondary_integrity cleanly,
    (B) Cross-org collision discovered on secondary_integrity marks affected org ambiguous (ID=None),
        preserves conflicting owner resolved, and commits PaymentReconciliation cleanly.
    """
    from sqlalchemy.exc import DBAPIError

    # --- Sub-case A: Non-collision secondary_integrity ---
    org_id_a = uuid.uuid4()
    cust_a = "cust_s1f1_existing_non_collision"

    def _fail_commits_a(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_a.asyncpg.exceptions.InternalServerError("primary TX2 write failed")
                if o["updates"].get("status") == "ambiguous":
                    return harness_a.make_unique_error("ck_existing_non_collision_a")
        return None

    harness_a = _make_real_asyncpg_session_harness(
        organizations=[{"id": org_id_a, "name": "Existing Non-Collision Org"}],
        fail_commit_predicate=_fail_commits_a,
    )

    class ClientA:
        async def create_customer(self, **kwargs):
            return {"id": cust_a}

    set_razorpay_client_for_testing(ClientA())
    tracked_a, records_a = _wrap_tracked_session_factory(harness_a.session_factory)

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_a:
            await get_or_create_razorpay_customer(
                organization_id=org_id_a,
                organization_name="Existing Non-Collision Org",
                session_factory=tracked_a,
            )

        err_a = exc_a.value
        assert err_a.customer_id == cust_a
        assert str(org_id_a) in str(err_a)
        assert isinstance(err_a.original_error, DBAPIError)
        assert "primary TX2 write failed" in str(err_a.original_error)
        assert isinstance(err_a.recovery_error, IntegrityError)
        assert "ck_existing_non_collision_a" in str(err_a.recovery_error)
        assert err_a.__cause__ is err_a.recovery_error
        assert err_a.__context__ is err_a.original_error
        assert "fallback persistence hit IntegrityError" in str(err_a)
        assert len(records_a) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_a)
    finally:
        await harness_a.engine.dispose()

    # --- Sub-case B: Cross-org collision revealed on fallback IntegrityError recovers durably ---
    org_owner_b = uuid.uuid4()
    org_affected_b = uuid.uuid4()
    cust_b = "cust_s1f1_existing_collision_ok"
    owner_added = [False]

    def _inject_owner_after_tx2_fail(tables, action, last_err, log):
        if not owner_added[0] and action == "commit_failed":
            last_ops = log[-1].get("ops", [])
            if any(
                o["op"] == "update"
                and o["table"] == "billing_customer_provisions"
                and o["updates"].get("status") == "resolved"
                for o in last_ops
            ):
                owner_added[0] = True
                tables["billing_customer_provisions"].append({
                    "id": uuid.uuid4(),
                    "organization_id": org_owner_b,
                    "provider": "razorpay",
                    "merchant_account_id": "acc_test_123",
                    "environment": "test",
                    "provider_customer_id": cust_b,
                    "status": CustomerProvisionStatus.resolved.value,
                    "claim_operation_id": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                })

    def _fail_tx2_only_b(staging, ops):
        for o in ops:
            if o["op"] == "update" and o["table"] == "billing_customer_provisions":
                if o["updates"].get("status") == "resolved":
                    return harness_b.asyncpg.exceptions.InternalServerError("primary TX2 timeout B")
        return None

    harness_b = _make_real_asyncpg_session_harness(
        organizations=[
            {"id": org_owner_b, "name": "Existing Owner B"},
            {"id": org_affected_b, "name": "Existing Affected B"},
        ],
        fail_commit_predicate=_fail_tx2_only_b,
        on_after_tx_end=_inject_owner_after_tx2_fail,
    )

    class ClientB:
        async def create_customer(self, **kwargs):
            return {"id": cust_b}

    set_razorpay_client_for_testing(ClientB())
    tracked_b, records_b = _wrap_tracked_session_factory(harness_b.session_factory)

    try:
        with pytest.raises(RazorpayCustomerPersistenceError) as exc_b:
            await get_or_create_razorpay_customer(
                organization_id=org_affected_b,
                organization_name="Existing Affected B",
                session_factory=tracked_b,
            )

        err_b = exc_b.value
        assert err_b.customer_id == cust_b
        assert str(org_affected_b) in str(err_b)
        assert isinstance(err_b.original_error, IntegrityError)
        assert err_b.recovery_error is None
        assert err_b.__cause__ is err_b.original_error
        assert "collides with an existing mapping" in str(err_b)

        owner_row = next(
            p for p in harness_b.tables["billing_customer_provisions"] if p["organization_id"] == org_owner_b
        )
        affected_row = next(
            p for p in harness_b.tables["billing_customer_provisions"] if p["organization_id"] == org_affected_b
        )
        assert owner_row["status"] == "resolved"
        assert owner_row["provider_customer_id"] == cust_b
        assert affected_row["status"] == "ambiguous"
        assert affected_row["provider_customer_id"] is None
        assert affected_row["last_error_code"] == "customer_id_org_collision"
        assert len(harness_b.tables["payment_reconciliations"]) == 1
        recon = harness_b.tables["payment_reconciliations"][0]
        assert recon["case_key"] == f"customer_provision:collision:{org_affected_b}:{cust_b}"
        assert recon["reason"] == "customer_id_org_collision"
        assert len(records_b) == 4
        assert all(r["entered"] and r["exited"] and r["in_tx_after_exit"] is False for r in records_b)
    finally:
        await harness_b.engine.dispose()


# =============================================================================
# Stage A: SQLAlchemy + asyncpg Exception Diagnostics Extraction Tests
# =============================================================================

import types
from app.services.billing.razorpay import customer as customer_module


def _build_real_sqlalchemy_asyncpg_unique_violation(
    *,
    constraint_name: str = PROVIDER_CUSTOMER_UNIQUE_INDEX,
    detail: str = (
        "Key (provider, merchant_account_id, environment, provider_customer_id)="
        "(razorpay, acc_test_123, test, cust_real_asyncpg_001) already exists."
    ),
    sqlstate: str = "23505",
) -> tuple[IntegrityError, asyncpg.exceptions.UniqueViolationError]:
    """Build an authentic SQLAlchemy 2.0 + asyncpg 0.30 wrapped UniqueViolationError."""
    from sqlalchemy.dialects.postgresql.asyncpg import (
        AsyncAdapt_asyncpg_connection,
        AsyncAdapt_asyncpg_dbapi,
    )

    pg_exc = asyncpg.exceptions.PostgresError.new(
        {
            "S": "ERROR",
            "C": sqlstate,
            "M": f'duplicate key value violates unique constraint "{constraint_name}"',
            "D": detail,
            "n": constraint_name,
            "t": "billing_customer_provisions",
            "s": "public",
        }
    )
    assert isinstance(pg_exc, asyncpg.exceptions.UniqueViolationError)

    dummy_adapter = object.__new__(AsyncAdapt_asyncpg_connection)
    dummy_adapter.dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    dummy_adapter._connection = types.SimpleNamespace(is_closed=lambda: False)
    try:
        dummy_adapter._handle_exception(pg_exc)
    except Exception as wrapped_dbapi_exc:
        sa_exc = IntegrityError(
            statement="UPDATE billing_customer_provisions SET status=$1::customer_provision_status",
            params={},
            orig=wrapped_dbapi_exc,
        )
        return sa_exc, pg_exc
    raise AssertionError("Expected AsyncAdapt_asyncpg_connection._handle_exception to raise")


def test_stage_a_extracts_real_sqlalchemy_asyncpg_wrapped_unique_violation() -> None:
    """Stage A: Actual SQLAlchemy + asyncpg exception wrapping recovers SQLSTATE, constraint_name, and detail."""
    detail_text = (
        "Key (provider, merchant_account_id, environment, provider_customer_id)="
        "(razorpay, acc_test_123, test, cust_asyncpg_wrap_1) already exists."
    )
    sa_exc, pg_exc = _build_real_sqlalchemy_asyncpg_unique_violation(detail=detail_text)

    # Confirm the real driver quirk: sa_exc.orig lacks .diag and .constraint_name; pg_exc lives on __cause__.
    assert getattr(sa_exc.orig, "diag", None) is None
    assert getattr(sa_exc.orig, "constraint_name", None) is None
    assert sa_exc.orig.__cause__ is pg_exc

    details = customer_module._extract_db_error_details(sa_exc)
    assert details["error_type"] == "IntegrityError"
    assert details["pgcode"] == "23505"
    assert details["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX
    assert details["detail"] == detail_text
    assert PROVIDER_CUSTOMER_UNIQUE_INDEX in (details["orig_error"] or "")
    assert customer_module._is_provider_customer_collision_error(sa_exc) is True
    # Original exception identity and chaining remain completely untouched.
    assert sa_exc.orig.__cause__ is pg_exc


@pytest.mark.asyncio
async def test_stage_a_d6_reconciliation_persists_real_asyncpg_constraint_and_detail() -> None:
    """Stage A: End-to-end D-6 collision recovery persists constraint_name and detail from wrapped asyncpg error."""
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    org_a = Organization(id=org_a_id, name="Owner Org A")
    org_b = Organization(id=org_b_id, name="Affected Org B")
    owner_prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_a_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_asyncpg_e2e_77",
        status=CustomerProvisionStatus.resolved,
    )
    detail_text = (
        "Key (provider, merchant_account_id, environment, provider_customer_id)="
        "(razorpay, acc_test_123, test, cust_asyncpg_e2e_77) already exists."
    )
    real_wrapped_exc, _ = _build_real_sqlalchemy_asyncpg_unique_violation(detail=detail_text)

    class AsyncpgWrappedCollisionSession(ConstraintEnforcingDbSession):
        async def commit(self) -> None:
            for p in self.provisions:
                if (
                    p.organization_id == org_b_id
                    and p.status == CustomerProvisionStatus.resolved
                    and p.provider_customer_id == "cust_asyncpg_e2e_77"
                ):
                    raise real_wrapped_exc
            await super().commit()

    db = AsyncpgWrappedCollisionSession(organizations=[org_a, org_b], provisions=[owner_prov])

    class Client:
        async def create_customer(self, **kwargs):
            return {"id": "cust_asyncpg_e2e_77"}

    set_razorpay_client_for_testing(Client())

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await get_or_create_razorpay_customer(
            organization_id=org_b_id,
            organization_name="Affected Org B",
            session_factory=db.as_session_factory(),
        )

    assert exc_info.value.original_error is real_wrapped_exc
    assert exc_info.value.__cause__ is real_wrapped_exc
    assert len(db.reconciliations) == 1
    latest = db.reconciliations[0].diagnostic_payload["latest"]
    orig_details = latest["original_error_details"]
    assert orig_details["pgcode"] == "23505"
    assert orig_details["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX
    assert orig_details["detail"] == detail_text


def test_stage_a_synthetic_compatibility_cases() -> None:
    """Stage A: Synthetic psycopg-style diag and direct orig attributes remain fully supported."""
    synth = _make_pg_unique_error(
        "uq_billing_customer_provisions_org_provider",
        detail="Key (organization_id, provider)=(1, razorpay) already exists.",
    )
    details = customer_module._extract_db_error_details(synth)
    assert details["pgcode"] == "23505"
    assert details["constraint_name"] == "uq_billing_customer_provisions_org_provider"
    assert details["detail"] == "Key (organization_id, provider)=(1, razorpay) already exists."

    class _DirectOrig(Exception):
        pgcode = "23503"
        constraint_name = "fk_billing_customer_provisions_organization_id"
        message_detail = "Key (organization_id)=(missing) is not present in table organizations."

    direct_sa = IntegrityError("INSERT", {}, _DirectOrig())
    direct_details = customer_module._extract_db_error_details(direct_sa)
    assert direct_details["pgcode"] == "23503"
    assert direct_details["constraint_name"] == "fk_billing_customer_provisions_organization_id"
    assert (
        direct_details["detail"]
        == "Key (organization_id)=(missing) is not present in table organizations."
    )


def test_stage_a_missing_and_broken_diagnostic_attributes() -> None:
    """Stage A: Plain exceptions, None attributes, non-strings, and raising properties never fail."""
    plain = RuntimeError("plain database error without orig")
    plain_details = customer_module._extract_db_error_details(plain)
    assert plain_details == {
        "error_type": "RuntimeError",
        "error_message": "plain database error without orig",
        "orig_error": None,
        "pgcode": None,
        "constraint_name": None,
        "detail": None,
    }

    class _HostileOrig(Exception):
        @property
        def diag(self):
            raise RuntimeError("broken diag property")

        @property
        def constraint_name(self):
            return 12345  # non-string attribute must be ignored safely

        @property
        def sqlstate(self):
            return "   "  # whitespace-only must normalize to None

        def __str__(self) -> str:
            raise ValueError("broken __str__")

    hostile = IntegrityError("COMMIT", {}, _HostileOrig())
    hostile_details = customer_module._extract_db_error_details(hostile)
    assert hostile_details["error_type"] == "IntegrityError"
    assert hostile_details["orig_error"] == "_HostileOrig"
    assert hostile_details["pgcode"] is None
    assert hostile_details["constraint_name"] is None
    assert hostile_details["detail"] is None


def test_stage_a_nested_exception_chains_cause_and_context() -> None:
    """Stage A: Multi-hop __cause__ and __context__ exception chains recover deep PG diagnostics."""
    leaf = asyncpg.exceptions.PostgresError.new(
        {
            "C": "23505",
            "M": "duplicate key",
            "D": "Key (id)=(42) already exists.",
            "n": "uq_nested_constraint",
        }
    )
    mid_context = RuntimeError("intermediate context wrapper")
    mid_context.__context__ = leaf
    mid_cause = ValueError("intermediate cause wrapper")
    mid_cause.__cause__ = mid_context
    outer = IntegrityError("UPDATE", {}, mid_cause)

    details = customer_module._extract_db_error_details(outer)
    assert details["pgcode"] == "23505"
    assert details["constraint_name"] == "uq_nested_constraint"
    assert details["detail"] == "Key (id)=(42) already exists."
    assert mid_cause.__cause__ is mid_context
    assert mid_context.__context__ is leaf


def test_stage_a_cyclic_and_repeated_exception_references_terminate_cleanly() -> None:
    """Stage A: Cyclic __cause__/__context__ and diamond references never hang or recurse infinitely."""
    err_a = RuntimeError("cycle node A")
    err_b = ValueError("cycle node B")
    err_a.__cause__ = err_b
    err_b.__cause__ = err_a
    err_a.__context__ = err_a
    err_b.__context__ = err_a

    sa_cyclic = IntegrityError("COMMIT", {}, err_a)
    details = customer_module._extract_db_error_details(sa_cyclic)
    assert details["error_type"] == "IntegrityError"
    assert details["pgcode"] is None
    assert details["constraint_name"] is None
    assert details["detail"] is None


def test_stage_a_credential_safe_diagnostic_serialization() -> None:
    """Stage A: Database URIs, password= tokens, Basic auth, and Razorpay secrets are redacted."""
    secret_pw = "SuperSecretPgPass987!"
    secret_rzp = "rzp_live_SecretToken12345"
    secret_basic = "Basic cnpwX2xpdmVfc2VjcmV0OnBhc3N3b3Jk"
    leaked_detail = (
        f"DSN postgresql+asyncpg://aptlyst_admin:{secret_pw}@db.internal:5432/aptlyst_prod "
        f"password={secret_pw} auth='{secret_basic}' key={secret_rzp} "
        "Key (provider_customer_id)=(cust_safe_1) already exists."
    )
    sa_exc, _ = _build_real_sqlalchemy_asyncpg_unique_violation(detail=leaked_detail)
    details = customer_module._extract_db_error_details(sa_exc)

    serialized = str(details)
    assert secret_pw not in serialized
    assert secret_rzp not in serialized
    assert "cnpwX2xpdmVfc2VjcmV0OnBhc3N3b3Jk" not in serialized
    assert "[REDACTED]" in serialized
    assert details["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX
    assert "Key (provider_customer_id)=(cust_safe_1) already exists." in (details["detail"] or "")
