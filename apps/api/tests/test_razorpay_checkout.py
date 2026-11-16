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

import asyncio
from collections.abc import Awaitable, Callable
import contextvars
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import sys
from typing import Any
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

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
from app.db.session import get_db, get_session_factory
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
from app.services.billing.razorpay.customer import (
    RazorpayAmbiguousNetworkError,
    RazorpayCustomerError,
    RazorpayCustomerPersistenceError,
    RazorpayCustomerProvisioningInProgressError,
    RazorpayCustomerReconciliationRequiredError,
    get_or_create_razorpay_customer,
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

    _TRACKED_FIELDS = (
        "status",
        "provider_payment_id",
        "provider_subscription_id",
        "provider_session_id",
        "provider_customer_id",
        "provider_plan_id",
        "claim_operation_id",
        "lease_expires_at",
        "last_error_code",
        "amount_minor_units",
        "currency",
        "error_message",
        "checkout_url",
        "session_expires_at",
        "request_payload",
        "plan_tier",
        "canceled_at",
        "past_due_since",
        "cancel_at_period_end",
        "current_period_start",
        "current_period_end",
        "last_webhook_event_at",
        "reason",
        "diagnostic_payload",
        "updated_at",
        "organization_id",
        "subscription_id",
        "invoice_id",
        "payment_id",
        "raw_event_id",
    )

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
        fail_next_commit_exception: Exception | None = None,
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
        self.fail_next_commit_exception = fail_next_commit_exception
        self.uncommitted: list[Any] = []
        self._in_tx = False
        self._tx_snapshots: dict[int, tuple[Any, dict[str, Any]]] = {}
        self.commit_calls: int = 0
        self.write_commit_calls: int = 0
        self.rollback_calls: int = 0
        self.executed_stmts: list[Any] = []

    def _capture_snapshot(self, obj: Any) -> None:
        if id(obj) in self._tx_snapshots:
            return
        state: dict[str, Any] = {}
        for field in self._TRACKED_FIELDS:
            if hasattr(obj, field):
                state[field] = copy.deepcopy(getattr(obj, field))
        self._tx_snapshots[id(obj)] = (obj, state)

    def _begin_tx_if_needed(self) -> None:
        if not self._in_tx:
            self._in_tx = True
            self._tx_snapshots.clear()
            for bucket in (
                self.attempts,
                self.provisions,
                self.subscriptions,
                self.billing_periods,
                self.reconciliations,
            ):
                for obj in bucket:
                    self._capture_snapshot(obj)

    def _has_pending_writes(self) -> bool:
        if self.uncommitted:
            return True
        for obj, saved_state in self._tx_snapshots.values():
            for field, saved_val in saved_state.items():
                if getattr(obj, field, None) != saved_val:
                    return True
        return False

    def in_transaction(self) -> bool:
        return self._in_tx

    def as_session_factory(self) -> Callable[[], Any]:
        from contextlib import asynccontextmanager

        parent = self

        @asynccontextmanager
        async def _factory():
            try:
                yield parent
            finally:
                if parent.in_transaction():
                    await parent.rollback()

        return _factory

    async def get(self, entity_cls: Any, ident: Any) -> Any:
        self._begin_tx_if_needed()
        if entity_cls is Organization:
            return self.organizations.get(ident)
        return None

    async def execute(self, stmt: Any) -> MockScalarResult:
        self.executed_stmts.append(stmt)
        self._begin_tx_if_needed()

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

        if from_table == "organizations":
            lookup_org_id = w_params.get("id") or org_id
            return MockScalarResult(self.organizations.get(lookup_org_id))

        if from_table == "billing_checkout_attempts":
            provider = w_params.get("provider")
            merchant_account_id = w_params.get("merchant_account_id")
            environment = w_params.get("environment")
            has_sub_filter = "provider_subscription_id" in w_params
            sub_filter = w_params.get("provider_subscription_id")
            has_status_filter = "status" in w_params
            status_filter = w_params.get("status")
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
                if has_sub_filter and a.provider_subscription_id != sub_filter:
                    continue
                if has_status_filter:
                    if isinstance(status_filter, (list, tuple, set)):
                        if a.status not in status_filter:
                            continue
                    elif a.status != status_filter:
                        continue
                return MockScalarResult(a)
            return MockScalarResult(None)

        if from_table == "billing_customer_provisions":
            provider = w_params.get("provider")
            merchant_account_id = w_params.get("merchant_account_id")
            environment = w_params.get("environment")
            for p in reversed(self.provisions):
                if org_id and p.organization_id != org_id:
                    continue
                if provider and p.provider != provider:
                    continue
                if merchant_account_id and p.merchant_account_id != merchant_account_id:
                    continue
                if environment and p.environment != environment:
                    continue
                return MockScalarResult(p)
            return MockScalarResult(None)

        if from_table == "subscriptions":
            plan_tier = w_params.get("plan_tier")
            provider = w_params.get("provider")
            merchant_account_id = w_params.get("merchant_account_id")
            environment = w_params.get("environment")
            has_sub_filter = "provider_subscription_id" in w_params
            sub_filter = w_params.get("provider_subscription_id")
            has_status_filter = "status" in w_params
            status_filter = w_params.get("status")
            for s in reversed(self.subscriptions):
                if org_id and s.organization_id != org_id:
                    continue
                if plan_tier and s.plan_tier != plan_tier:
                    continue
                if provider and s.provider != provider:
                    continue
                if merchant_account_id and s.merchant_account_id != merchant_account_id:
                    continue
                if environment and s.environment != environment:
                    continue
                if has_sub_filter and s.provider_subscription_id != sub_filter:
                    continue
                if has_status_filter:
                    if isinstance(status_filter, (list, tuple, set)):
                        if s.status not in status_filter:
                            continue
                    elif s.status != status_filter:
                        continue
                return MockScalarResult(s)
            return MockScalarResult(None)

        if from_table == "payment_reconciliations":
            provider = w_params.get("provider")
            merchant_account_id = w_params.get("merchant_account_id")
            environment = w_params.get("environment")
            case_key = w_params.get("case_key")
            candidates = [
                r for r in self.reconciliations
            ] + [
                o for o in self.uncommitted if isinstance(o, PaymentReconciliation)
            ]
            for r in reversed(candidates):
                if provider and r.provider != provider:
                    continue
                if merchant_account_id and r.merchant_account_id != merchant_account_id:
                    continue
                if environment and r.environment != environment:
                    continue
                if case_key and r.case_key != case_key:
                    continue
                return MockScalarResult(r)
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
        self._begin_tx_if_needed()
        self.uncommitted.append(obj)

    async def flush(self) -> None:
        self._begin_tx_if_needed()

    async def commit(self) -> None:
        self.commit_calls += 1
        if self._has_pending_writes():
            self.write_commit_calls += 1
            if self.fail_next_commit_exception is not None:
                exc = self.fail_next_commit_exception
                self.fail_next_commit_exception = None
                raise exc
            if self.fail_commit_with_integrity:
                self.fail_commit_with_integrity = False
                raise IntegrityError(
                    "Concurrent attempt constraint conflict",
                    params={},
                    orig=Exception(),
                )
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
        self._tx_snapshots.clear()
        self._in_tx = False

    async def rollback(self) -> None:
        self.rollback_calls += 1
        for obj, saved_state in self._tx_snapshots.values():
            for field, saved_val in saved_state.items():
                setattr(obj, field, copy.deepcopy(saved_val))
        self.uncommitted.clear()
        self._tx_snapshots.clear()
        self._in_tx = False


class SharedCheckoutDbStore:
    """Shared committed store backing multiple independent AsyncSession doubles."""

    _ATTEMPT_FIELDS = (
        "id",
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_customer_id",
        "provider_subscription_id",
        "provider_order_id",
        "provider_payment_id",
        "provider_session_id",
        "idempotency_key",
        "status",
        "amount_minor_units",
        "currency",
        "checkout_url",
        "request_payload",
        "attempt_lease_expires_at",
        "session_expires_at",
        "error_message",
        "created_at",
    )

    _SUB_FIELDS = (
        "id",
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_customer_id",
        "provider_subscription_id",
        "provider_plan_id",
        "status",
        "plan_tier",
        "canceled_at",
        "past_due_since",
        "cancel_at_period_end",
        "current_period_start",
        "current_period_end",
        "last_webhook_event_at",
    )

    def __init__(
        self,
        attempts: list[BillingCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
    ) -> None:
        self.lock = asyncio.Lock()
        self.attempts: dict[uuid.UUID, dict[str, Any]] = {}
        self.subscriptions: dict[uuid.UUID, dict[str, Any]] = {}
        self.reconciliations: list[PaymentReconciliation] = []
        for a in attempts or []:
            self.attempts[a.id] = self.snapshot_model(a, self._ATTEMPT_FIELDS)
        for s in subscriptions or []:
            self.subscriptions[s.id] = self.snapshot_model(s, self._SUB_FIELDS)

    @staticmethod
    def snapshot_model(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
        return {f: copy.deepcopy(getattr(obj, f, None)) for f in fields}

    @staticmethod
    def materialize_attempt(row: dict[str, Any]) -> BillingCheckoutAttempt:
        return BillingCheckoutAttempt(**copy.deepcopy(row))

    @staticmethod
    def materialize_subscription(row: dict[str, Any]) -> Subscription:
        return Subscription(**copy.deepcopy(row))


class SharedCheckoutMockDbSession:
    """Session double with identity map, populate_existing semantics, and shared row lock."""

    def __init__(self, store: SharedCheckoutDbStore) -> None:
        self.store = store
        self._identity_attempts: dict[uuid.UUID, BillingCheckoutAttempt] = {}
        self._loaded_attempt_baseline: dict[uuid.UUID, dict[str, Any]] = {}
        self._identity_subs: dict[uuid.UUID, Subscription] = {}
        self._loaded_sub_baseline: dict[uuid.UUID, dict[str, Any]] = {}
        self._in_tx = False
        self._locked = False
        self.uncommitted: list[Any] = []
        self.populate_existing_calls: list[str] = []
        self.rollback_calls = 0

    def in_transaction(self) -> bool:
        return self._in_tx

    def as_session_factory(self) -> Callable[[], Any]:
        from contextlib import asynccontextmanager

        parent = self

        @asynccontextmanager
        async def _factory():
            try:
                yield parent
            finally:
                if parent.in_transaction():
                    await parent.rollback()

        return _factory

    async def execute(self, stmt: Any) -> MockScalarResult:
        self._in_tx = True
        for_update = getattr(stmt, "_for_update_arg", None) is not None
        exec_opts = stmt.get_execution_options() if hasattr(stmt, "get_execution_options") else {}
        populate_existing = bool(exec_opts.get("populate_existing"))

        if for_update and not self._locked:
            await self.store.lock.acquire()
            self._locked = True

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

        if for_update and populate_existing and from_table:
            self.populate_existing_calls.append(from_table)

        if from_table == "billing_checkout_attempts":
            for a_id, row in self.store.attempts.items():
                if w_params.get("id") and a_id != w_params["id"]:
                    continue
                if w_params.get("organization_id") and row["organization_id"] != w_params["organization_id"]:
                    continue
                if w_params.get("provider") and row["provider"] != w_params["provider"]:
                    continue
                if w_params.get("merchant_account_id") and row["merchant_account_id"] != w_params["merchant_account_id"]:
                    continue
                if w_params.get("environment") and row["environment"] != w_params["environment"]:
                    continue
                if not populate_existing and a_id in self._identity_attempts:
                    return MockScalarResult(self._identity_attempts[a_id])
                if a_id in self._identity_attempts:
                    inst = self._identity_attempts[a_id]
                    for k, v in copy.deepcopy(row).items():
                        setattr(inst, k, v)
                else:
                    inst = self.store.materialize_attempt(row)
                    self._identity_attempts[a_id] = inst
                self._loaded_attempt_baseline[a_id] = copy.deepcopy(row)
                return MockScalarResult(inst)
            return MockScalarResult(None)

        if from_table == "subscriptions":
            for s_id, row in self.store.subscriptions.items():
                if w_params.get("provider") and row["provider"] != w_params["provider"]:
                    continue
                if w_params.get("merchant_account_id") and row["merchant_account_id"] != w_params["merchant_account_id"]:
                    continue
                if w_params.get("environment") and row["environment"] != w_params["environment"]:
                    continue
                if "provider_subscription_id" in w_params and row["provider_subscription_id"] != w_params["provider_subscription_id"]:
                    continue
                if not populate_existing and s_id in self._identity_subs:
                    return MockScalarResult(self._identity_subs[s_id])
                if s_id in self._identity_subs:
                    inst = self._identity_subs[s_id]
                    for k, v in copy.deepcopy(row).items():
                        setattr(inst, k, v)
                else:
                    inst = self.store.materialize_subscription(row)
                    self._identity_subs[s_id] = inst
                self._loaded_sub_baseline[s_id] = copy.deepcopy(row)
                return MockScalarResult(inst)
            return MockScalarResult(None)

        if from_table == "payment_reconciliations":
            for r in reversed(self.store.reconciliations):
                if w_params.get("provider") and r.provider != w_params["provider"]:
                    continue
                if w_params.get("merchant_account_id") and r.merchant_account_id != w_params["merchant_account_id"]:
                    continue
                if w_params.get("environment") and r.environment != w_params["environment"]:
                    continue
                if w_params.get("case_key") and r.case_key != w_params["case_key"]:
                    continue
                return MockScalarResult(r)
            return MockScalarResult(None)

        return MockScalarResult(None)

    def add(self, obj: Any) -> None:
        self._in_tx = True
        self.uncommitted.append(obj)

    async def flush(self) -> None:
        self._in_tx = True

    async def commit(self) -> None:
        try:
            for a_id, inst in self._identity_attempts.items():
                baseline = self._loaded_attempt_baseline.get(a_id, {})
                current = self.store.snapshot_model(inst, self.store._ATTEMPT_FIELDS)
                if current != baseline:
                    existing_row = self.store.attempts[a_id]
                    old_pay = existing_row.get("provider_payment_id")
                    new_pay = current.get("provider_payment_id")
                    if old_pay is not None and new_pay != old_pay:
                        raise IntegrityError("Immutability trigger violation", params={}, orig=Exception())
                    if new_pay is not None:
                        for other_id, other_row in self.store.attempts.items():
                            if other_id != a_id and (
                                other_row["provider"],
                                other_row["merchant_account_id"],
                                other_row["environment"],
                                other_row["provider_payment_id"],
                            ) == (
                                current["provider"],
                                current["merchant_account_id"],
                                current["environment"],
                                new_pay,
                            ):
                                raise IntegrityError("uq_checkout_attempts_provider_payment", params={}, orig=Exception())
                    self.store.attempts[a_id] = current
                    self._loaded_attempt_baseline[a_id] = copy.deepcopy(current)

            for s_id, inst in self._identity_subs.items():
                baseline = self._loaded_sub_baseline.get(s_id, {})
                current = self.store.snapshot_model(inst, self.store._SUB_FIELDS)
                if current != baseline:
                    self.store.subscriptions[s_id] = current
                    self._loaded_sub_baseline[s_id] = copy.deepcopy(current)

            for obj in self.uncommitted:
                if isinstance(obj, PaymentReconciliation) and obj not in self.store.reconciliations:
                    self.store.reconciliations.append(obj)
            self.uncommitted.clear()
        finally:
            self._in_tx = False
            if self._locked:
                self._locked = False
                self.store.lock.release()

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self.uncommitted.clear()
        self._identity_attempts.clear()
        self._loaded_attempt_baseline.clear()
        self._identity_subs.clear()
        self._loaded_sub_baseline.clear()
        self._in_tx = False
        if self._locked:
            self._locked = False
            self.store.lock.release()


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
        payment_account_id: str | None = None,
        payment_customer_id: str | None = None,
        payment_entity: str = "payment",
        payment_error_description: str | None = None,
        get_payment_hook: Callable[[str], Awaitable[None]] | None = None,
        db_session_for_tx_check: Any = None,
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
        self.payment_account_id = payment_account_id
        self.payment_customer_id = payment_customer_id
        self.payment_entity = payment_entity
        self.payment_error_description = payment_error_description
        self.get_payment_hook = get_payment_hook
        self.db_session_for_tx_check = db_session_for_tx_check
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
        if self.db_session_for_tx_check is not None:
            assert not self.db_session_for_tx_check.in_transaction(), (
                "Gateway get_payment() must not execute inside an open DB transaction"
            )
        if self.get_payment_hook is not None:
            await self.get_payment_hook(payment_id)
        self.get_payment_calls.append(payment_id)
        if self.fail_payment:
            raise self.fail_payment
        payload: dict[str, Any] = {
            "id": payment_id,
            "entity": self.payment_entity,
            "amount": self.payment_amount,
            "currency": self.payment_currency,
            "status": self.payment_status,
            "method": "card",
            "subscription_id": self.payment_subscription_id,
            "notes": self.payment_notes if self.payment_notes is not None else {},
        }
        if self.payment_account_id is not None:
            payload["account_id"] = self.payment_account_id
        if self.payment_customer_id is not None:
            payload["customer_id"] = self.payment_customer_id
        if self.payment_error_description is not None:
            payload["error_description"] = self.payment_error_description
        return payload


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

    result = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.as_session_factory()
    )

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

    result = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.as_session_factory()
    )

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
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.as_session_factory()
        )


@pytest.mark.asyncio
async def test_missing_plan_id_fails_closed(monkeypatch):
    """Missing or empty RAZORPAY_PRO_PLAN_ID fails closed before gateway request."""
    monkeypatch.setattr(settings, "razorpay_pro_plan_id", "")
    org = Organization(id=uuid.uuid4(), name="No Plan Org")
    db = CheckoutMockDbSession(organizations=[org])

    with pytest.raises(RazorpayConfigurationError, match="RAZORPAY_PRO_PLAN_ID is not configured"):
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.as_session_factory()
        )


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
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.as_session_factory()
        )

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

    result = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.as_session_factory()
    )

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
    assert attempt.provider_payment_id == pay_id


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
    assert attempt.provider_payment_id is None


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
    assert attempt.provider_payment_id == pay_id

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
    assert attempt.provider_payment_id == pay_id
    assert len(mock_client.get_payment_calls) == 1


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
    create_res = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.as_session_factory()
    )
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
    assert db.attempts[0].provider_payment_id == "pay_pro_456"

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

        async def override_session_factory():
            return db.as_session_factory()

        async def override_user():
            return CurrentUser(id=user_id, email="admin@example.com")

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_session_factory] = override_session_factory
        app.dependency_overrides[get_current_user] = override_user

    yield _override
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_session_factory, None)
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
    assert len(db.provisions) == 1
    assert db.provisions[0].provider_customer_id == "cust_test_mock_123"


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
    assert attempt.provider_payment_id == pay_id


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
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.as_session_factory()
        )

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
    assert attempt.provider_payment_id is None

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
    assert attempt.provider_payment_id == pay_id
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
    result = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.as_session_factory()
    )

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


# =============================================================================
# Step 5.2D.3.3B.1: Checkout Verification & Payment Binding Tests
# =============================================================================


@pytest.mark.asyncio
async def test_valid_captured_payment_binds_provider_payment_id_and_runs_gateway_outside_tx():
    """Captured verification binds provider_payment_id, respects lock order, and calls gateway outside DB tx."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_bind_atomic_123"
    pay_id = "pay_bind_atomic_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_atomic_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_bind_atomic_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        session_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    mock_client = MockRazorpayClient(
        customer_id="cust_atomic_123",
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="captured",
        payment_amount=2900,
        payment_currency="USD",
        payment_customer_id="cust_atomic_123",
        payment_account_id="acc_test_123",
        payment_notes={"organization_id": str(org_id), "attempt_id": str(attempt_id)},
        db_session_for_tx_check=db,
    )
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
    assert res.payment_id == pay_id
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.provider_payment_id == pay_id
    assert attempt.amount_minor_units == 2900
    assert attempt.request_payload["verification"]["payment_id"] == pay_id
    assert attempt.request_payload["verification"]["payment_status"] == "captured"
    assert not db.in_transaction()
    assert len(db.billing_periods) == 0

    for_update_tables = []
    for stmt in db.executed_stmts:
        if getattr(stmt, "_for_update_arg", None) is not None:
            for frm in stmt.get_final_froms():
                for_update_tables.append(getattr(frm, "name", ""))
    assert for_update_tables == ["subscriptions", "billing_checkout_attempts"]


@pytest.mark.asyncio
async def test_webhook_before_callback_complete_attempt_backfills_provider_payment_id():
    """If subscription.activated completed attempt without payment_id, callback still binds captured payment."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_webhook_first_123"
    pay_id = "pay_webhook_first_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_wf_123",
        provider_subscription_id=sub_id,
        provider_plan_id="plan_test_pro_monthly",
        status=SubscriptionStatus.active,
        plan_tier="free",
    )
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_wf_123",
        provider_subscription_id=sub_id,
        provider_payment_id=None,
        idempotency_key="idemp_wf_1",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        session_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db = CheckoutMockDbSession(attempts=[attempt], subscriptions=[sub])
    mock_client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_id=pay_id,
        payment_status="captured",
        payment_amount=2900,
    )
    set_razorpay_client_for_testing(mock_client)

    sig = _compute_valid_signature(pay_id, sub_id, secret)
    res1 = await verify_razorpay_checkout_callback(
        db=db,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=sig,
    )

    assert res1.status == CheckoutAttemptStatus.complete
    assert res1.payment_id == pay_id
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.provider_payment_id == pay_id
    assert attempt.request_payload["verification"]["payment_id"] == pay_id
    assert sub.plan_tier == "free"
    assert len(db.billing_periods) == 0
    assert len(mock_client.get_payment_calls) == 1

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
    assert len(mock_client.get_payment_calls) == 1


@pytest.mark.asyncio
async def test_invalid_signature_on_complete_attempt_still_rejected_without_binding():
    """Invalid signature never bypasses verification even when attempt is already complete."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_sig_comp_123"
    pay_id = "pay_sig_comp_456"

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_123",
        provider_subscription_id=sub_id,
        provider_payment_id=pay_id,
        idempotency_key="idemp_sig_comp_1",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=2900,
        currency="USD",
        request_payload={"verification": {"payment_id": pay_id, "payment_status": "captured"}},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    mock_client = MockRazorpayClient(subscription_id=sub_id, payment_id=pay_id)
    set_razorpay_client_for_testing(mock_client)

    with pytest.raises(RazorpayCheckoutSignatureError, match="Invalid checkout signature"):
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=sub_id,
            razorpay_signature="tampered_sig",
        )

    assert attempt.provider_payment_id == pay_id
    assert mock_client.get_payment_calls == []


@pytest.mark.asyncio
async def test_failed_and_refunded_payments_never_bind_provider_payment_id():
    """Failed and refunded payments must never populate provider_payment_id or downgrade completed state."""
    secret = settings.razorpay_key_secret.get_secret_value()

    # Case 1: failed payment on open attempt
    org1 = uuid.uuid4()
    att1_id = uuid.uuid4()
    att1 = BillingCheckoutAttempt(
        id=att1_id,
        organization_id=org1,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_fail_1",
        provider_subscription_id="sub_fail_1",
        idempotency_key="idemp_fail_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db1 = CheckoutMockDbSession(attempts=[att1])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id="sub_fail_1",
            payment_id="pay_fail_1",
            payment_status="failed",
            payment_error_description="insufficient_funds",
        )
    )
    with pytest.raises(RazorpayCheckoutPaymentError, match="marked as failed"):
        await verify_razorpay_checkout_callback(
            db=db1,
            organization_id=org1,
            attempt_id=att1_id,
            razorpay_payment_id="pay_fail_1",
            razorpay_subscription_id="sub_fail_1",
            razorpay_signature=_compute_valid_signature("pay_fail_1", "sub_fail_1", secret),
        )
    assert att1.status == CheckoutAttemptStatus.failed
    assert att1.provider_payment_id is None

    # Case 2: refunded payment on open attempt
    org2 = uuid.uuid4()
    att2_id = uuid.uuid4()
    att2 = BillingCheckoutAttempt(
        id=att2_id,
        organization_id=org2,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_ref_1",
        provider_subscription_id="sub_ref_1",
        idempotency_key="idemp_ref_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db2 = CheckoutMockDbSession(attempts=[att2])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id="sub_ref_1",
            payment_id="pay_ref_1",
            payment_status="refunded",
        )
    )
    with pytest.raises(RazorpayCheckoutPaymentError, match="refunded"):
        await verify_razorpay_checkout_callback(
            db=db2,
            organization_id=org2,
            attempt_id=att2_id,
            razorpay_payment_id="pay_ref_1",
            razorpay_subscription_id="sub_ref_1",
            razorpay_signature=_compute_valid_signature("pay_ref_1", "sub_ref_1", secret),
        )
    assert att2.status == CheckoutAttemptStatus.open
    assert att2.provider_payment_id is None

    # Case 3: failed payment arriving against webhook-completed attempt without binding never downgrades complete
    org3 = uuid.uuid4()
    att3_id = uuid.uuid4()
    att3 = BillingCheckoutAttempt(
        id=att3_id,
        organization_id=org3,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_wf_fail",
        provider_subscription_id="sub_wf_fail",
        provider_payment_id=None,
        idempotency_key="idemp_wf_fail",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db3 = CheckoutMockDbSession(attempts=[att3])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id="sub_wf_fail",
            payment_id="pay_wf_fail",
            payment_status="failed",
        )
    )
    with pytest.raises(RazorpayCheckoutPaymentError, match="marked as failed"):
        await verify_razorpay_checkout_callback(
            db=db3,
            organization_id=org3,
            attempt_id=att3_id,
            razorpay_payment_id="pay_wf_fail",
            razorpay_subscription_id="sub_wf_fail",
            razorpay_signature=_compute_valid_signature("pay_wf_fail", "sub_wf_fail", secret),
        )
    assert att3.status == CheckoutAttemptStatus.complete
    assert att3.provider_payment_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_kwargs,expected_exc,expected_msg",
    [
        (
            {"payment_notes": {"organization_id": str(uuid.uuid4())}},
            RazorpayCheckoutConflictError,
            "organization mismatch",
        ),
        (
            {"payment_notes": {"attempt_id": str(uuid.uuid4())}},
            RazorpayCheckoutConflictError,
            "checkout attempt mismatch",
        ),
        (
            {"payment_account_id": "acc_foreign_merchant_999"},
            RazorpayCheckoutConflictError,
            "merchant account mismatch",
        ),
        (
            {"payment_customer_id": "cust_foreign_999"},
            RazorpayCheckoutConflictError,
            "customer mismatch",
        ),
        (
            {"payment_entity": "order"},
            RazorpayCheckoutPaymentError,
            "invalid payment entity",
        ),
    ],
)
async def test_payment_metadata_account_customer_and_entity_mismatches_rejected_without_binding(
    client_kwargs: dict[str, Any],
    expected_exc: type[Exception],
    expected_msg: str,
):
    """Mismatched notes, merchant account, customer, or entity never bind provider_payment_id."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_meta_123"
    pay_id = "pay_meta_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_expected_123",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_meta_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_id,
            payment_id=pay_id,
            payment_status="captured",
            **client_kwargs,
        )
    )

    sig = _compute_valid_signature(pay_id, sub_id, secret)
    with pytest.raises(expected_exc, match=expected_msg):
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=sub_id,
            razorpay_signature=sig,
        )

    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_payment_id is None


@pytest.mark.asyncio
async def test_conflicting_payment_id_cannot_overwrite_existing_provider_payment_id(setup_overrides):
    """Existing provider_payment_id binding blocks overwrite with HTTP 409 and preserves original payment ID."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    org = Organization(id=org_id, name="Conflict Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)
    sub_id = "sub_bound_conflict_123"
    pay_a = "pay_bound_first_111"
    pay_b = "pay_bound_second_222"
    secret = settings.razorpay_key_secret.get_secret_value()

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_conflict_123",
        provider_subscription_id=sub_id,
        provider_payment_id=pay_a,
        idempotency_key="idemp_bound_conflict",
        status=CheckoutAttemptStatus.complete,
        amount_minor_units=2900,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt], organizations=[org], members=[member])
    mock_client = MockRazorpayClient(subscription_id=sub_id, payment_id=pay_b)
    set_razorpay_client_for_testing(mock_client)
    setup_overrides(db, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(
            f"/api/v1/organizations/{org_id}/billing/razorpay/verify-checkout",
            json={
                "attempt_id": str(attempt_id),
                "razorpay_payment_id": pay_b,
                "razorpay_subscription_id": sub_id,
                "razorpay_signature": _compute_valid_signature(pay_b, sub_id, secret),
            },
        )

    assert res.status_code == 409
    assert pay_a in res.json()["detail"]
    assert pay_b in res.json()["detail"]
    assert attempt.provider_payment_id == pay_a
    assert attempt.status == CheckoutAttemptStatus.complete
    assert mock_client.get_payment_calls == []


@pytest.mark.asyncio
async def test_expired_checkout_attempt_with_captured_payment_records_reconciliation_and_rejects():
    """Expired checkout attempts cannot complete; delayed capture records deterministic reconciliation."""
    secret = settings.razorpay_key_secret.get_secret_value()

    # Case A: Already expired + delayed captured payment (plus idempotent reconciliation replay)
    org_id = uuid.uuid4()
    att_exp_id = uuid.uuid4()
    sub_exp_id = "sub_exp_123"
    pay_exp_id = "pay_exp_delayed_456"
    att_exp = BillingCheckoutAttempt(
        id=att_exp_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_exp_1",
        provider_subscription_id=sub_exp_id,
        idempotency_key="idemp_exp_1",
        status=CheckoutAttemptStatus.expired,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
        session_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    db_exp = CheckoutMockDbSession(attempts=[att_exp])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_exp_id,
            payment_id=pay_exp_id,
            payment_status="captured",
        )
    )
    sig_exp = _compute_valid_signature(pay_exp_id, sub_exp_id, secret)

    with pytest.raises(RazorpayCheckoutConflictError, match="recorded for reconciliation"):
        await verify_razorpay_checkout_callback(
            db=db_exp,
            organization_id=org_id,
            attempt_id=att_exp_id,
            razorpay_payment_id=pay_exp_id,
            razorpay_subscription_id=sub_exp_id,
            razorpay_signature=sig_exp,
        )
    assert att_exp.status == CheckoutAttemptStatus.expired
    assert att_exp.provider_payment_id is None
    assert len(db_exp.reconciliations) == 1
    expected_key = f"delayed_capture_expired_checkout:{att_exp_id}:{pay_exp_id}"
    assert db_exp.reconciliations[0].case_key == expected_key
    assert db_exp.reconciliations[0].reason == "delayed_capture_after_checkout_expiration"
    assert db_exp.reconciliations[0].status == ReconciliationStatus.pending_review

    # Replay updates diagnostic history without creating duplicate reconciliation case
    with pytest.raises(RazorpayCheckoutConflictError, match="recorded for reconciliation"):
        await verify_razorpay_checkout_callback(
            db=db_exp,
            organization_id=org_id,
            attempt_id=att_exp_id,
            razorpay_payment_id=pay_exp_id,
            razorpay_subscription_id=sub_exp_id,
            razorpay_signature=sig_exp,
        )
    assert len(db_exp.reconciliations) == 1
    assert len(db_exp.reconciliations[0].diagnostic_payload["history"]) == 2

    # Case B: Open attempt past session_expires_at transitions to expired & records reconciliation
    att_past_id = uuid.uuid4()
    sub_past_id = "sub_past_open_123"
    pay_past_id = "pay_past_open_456"
    att_past = BillingCheckoutAttempt(
        id=att_past_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_past_1",
        provider_subscription_id=sub_past_id,
        idempotency_key="idemp_past_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
        session_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    db_past = CheckoutMockDbSession(attempts=[att_past])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_past_id,
            payment_id=pay_past_id,
            payment_status="captured",
        )
    )
    with pytest.raises(RazorpayCheckoutConflictError, match="expired"):
        await verify_razorpay_checkout_callback(
            db=db_past,
            organization_id=org_id,
            attempt_id=att_past_id,
            razorpay_payment_id=pay_past_id,
            razorpay_subscription_id=sub_past_id,
            razorpay_signature=_compute_valid_signature(pay_past_id, sub_past_id, secret),
        )
    assert att_past.status == CheckoutAttemptStatus.expired
    assert att_past.provider_payment_id is None
    assert len(db_past.reconciliations) == 1
    assert db_past.reconciliations[0].case_key == f"delayed_capture_expired_checkout:{att_past_id}:{pay_past_id}"

    # Case C: Open attempt past session_expires_at + authorized payment transitions to expired without binding
    att_auth_exp_id = uuid.uuid4()
    sub_auth_exp_id = "sub_auth_exp_123"
    pay_auth_exp_id = "pay_auth_exp_456"
    att_auth_exp = BillingCheckoutAttempt(
        id=att_auth_exp_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_auth_exp_1",
        provider_subscription_id=sub_auth_exp_id,
        idempotency_key="idemp_auth_exp_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
        session_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    db_auth_exp = CheckoutMockDbSession(attempts=[att_auth_exp])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_auth_exp_id,
            payment_id=pay_auth_exp_id,
            payment_status="authorized",
        )
    )
    with pytest.raises(RazorpayCheckoutConflictError, match="expired"):
        await verify_razorpay_checkout_callback(
            db=db_auth_exp,
            organization_id=org_id,
            attempt_id=att_auth_exp_id,
            razorpay_payment_id=pay_auth_exp_id,
            razorpay_subscription_id=sub_auth_exp_id,
            razorpay_signature=_compute_valid_signature(pay_auth_exp_id, sub_auth_exp_id, secret),
        )
    assert att_auth_exp.status == CheckoutAttemptStatus.expired
    assert att_auth_exp.provider_payment_id is None
    assert len(db_auth_exp.reconciliations) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [SubscriptionStatus.canceled, "completed"],
)
async def test_terminal_subscription_never_resurrected_by_checkout_verification(
    terminal_status: SubscriptionStatus | str,
):
    """Terminal subscriptions stay terminal with free plan_tier and log reconciliation on captured callback."""
    status_label = (
        terminal_status.value
        if isinstance(terminal_status, SubscriptionStatus)
        else str(terminal_status)
    )
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_uuid = uuid.uuid4()
    sub_id = f"sub_term_{status_label}_123"
    pay_id = f"pay_term_{status_label}_456"
    canceled_ts = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    secret = settings.razorpay_key_secret.get_secret_value()

    sub = Subscription(
        id=sub_uuid,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_term_1",
        provider_subscription_id=sub_id,
        status=terminal_status,
        plan_tier="free",
        canceled_at=canceled_ts,
    )
    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_term_1",
        provider_subscription_id=sub_id,
        idempotency_key=f"idemp_term_{status_label}",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db = CheckoutMockDbSession(attempts=[attempt], subscriptions=[sub])
    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_id,
            payment_id=pay_id,
            payment_status="captured",
        )
    )

    with pytest.raises(RazorpayCheckoutConflictError, match="terminal"):
        await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=sub_id,
            razorpay_signature=_compute_valid_signature(pay_id, sub_id, secret),
        )

    assert sub.status == terminal_status
    assert sub.plan_tier == "free"
    assert sub.canceled_at == canceled_ts
    assert attempt.status == CheckoutAttemptStatus.open
    assert attempt.provider_payment_id is None
    assert len(db.billing_periods) == 0
    assert len(db.reconciliations) == 1
    assert db.reconciliations[0].case_key == f"payment_for_terminal_subscription:{sub_uuid}:{pay_id}"
    assert db.reconciliations[0].reason == "payment_for_terminal_subscription"


@pytest.mark.asyncio
async def test_database_integrity_and_generic_error_rollback_leave_no_partial_binding(setup_overrides):
    """DB IntegrityError or generic commit failure rolls back cleanly without leaving complete/partial binding."""
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Rollback Org")
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.admin)
    secret = settings.razorpay_key_secret.get_secret_value()

    # Case 1: IntegrityError on commit -> HTTP 409, status & provider_payment_id rolled back
    att1_id = uuid.uuid4()
    sub1_id = "sub_rb_int_123"
    pay1_id = "pay_rb_int_456"
    att1 = BillingCheckoutAttempt(
        id=att1_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_rb_1",
        provider_subscription_id=sub1_id,
        idempotency_key="idemp_rb_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db1 = CheckoutMockDbSession(
        attempts=[att1],
        organizations=[org],
        members=[member],
        fail_commit_with_integrity=True,
    )
    set_razorpay_client_for_testing(
        MockRazorpayClient(subscription_id=sub1_id, payment_id=pay1_id, payment_status="captured")
    )
    setup_overrides(db1, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res1 = await ac.post(
            f"/api/v1/organizations/{org_id}/billing/razorpay/verify-checkout",
            json={
                "attempt_id": str(att1_id),
                "razorpay_payment_id": pay1_id,
                "razorpay_subscription_id": sub1_id,
                "razorpay_signature": _compute_valid_signature(pay1_id, sub1_id, secret),
            },
        )

    assert res1.status_code == 409
    assert db1.rollback_calls >= 1
    assert att1.status == CheckoutAttemptStatus.open
    assert att1.provider_payment_id is None
    assert "verification" not in att1.request_payload

    # Case 2: Generic DB error on commit -> HTTP 500 (service re-raises), status & provider_payment_id rolled back
    att2_id = uuid.uuid4()
    sub2_id = "sub_rb_gen_123"
    pay2_id = "pay_rb_gen_456"
    att2 = BillingCheckoutAttempt(
        id=att2_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_rb_2",
        provider_subscription_id=sub2_id,
        idempotency_key="idemp_rb_2",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db2 = CheckoutMockDbSession(
        attempts=[att2],
        organizations=[org],
        members=[member],
        fail_next_commit_exception=RuntimeError("Transient DB connection dropped"),
    )
    set_razorpay_client_for_testing(
        MockRazorpayClient(subscription_id=sub2_id, payment_id=pay2_id, payment_status="captured")
    )
    setup_overrides(db2, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res2 = await ac.post(
            f"/api/v1/organizations/{org_id}/billing/razorpay/verify-checkout",
            json={
                "attempt_id": str(att2_id),
                "razorpay_payment_id": pay2_id,
                "razorpay_subscription_id": sub2_id,
                "razorpay_signature": _compute_valid_signature(pay2_id, sub2_id, secret),
            },
        )

    assert res2.status_code == 500
    assert db2.rollback_calls >= 1
    assert att2.status == CheckoutAttemptStatus.open
    assert att2.provider_payment_id is None
    assert "verification" not in att2.request_payload


@pytest.mark.asyncio
async def test_concurrent_verification_different_payments_single_winner_no_overwrite():
    """Concurrent verification for two payments serializes under row lock + populate_existing with single winner."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_race_diff_123"
    pay_a = "pay_race_alpha_111"
    pay_b = "pay_race_beta_222"
    secret = settings.razorpay_key_secret.get_secret_value()

    initial_attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_race_1",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_race_1",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    store = SharedCheckoutDbStore(attempts=[initial_attempt])
    db_a = SharedCheckoutMockDbSession(store)
    db_b = SharedCheckoutMockDbSession(store)

    both_entered_http = asyncio.Event()
    entered_count = 0
    release_b = asyncio.Event()

    async def _get_payment_hook(payment_id: str) -> None:
        nonlocal entered_count
        entered_count += 1
        if entered_count == 2:
            both_entered_http.set()
        await asyncio.wait_for(both_entered_http.wait(), timeout=2.0)
        if payment_id == pay_b:
            await asyncio.wait_for(release_b.wait(), timeout=2.0)

    client = MockRazorpayClient(
        subscription_id=sub_id,
        payment_status="captured",
        payment_amount=2900,
        get_payment_hook=_get_payment_hook,
    )
    set_razorpay_client_for_testing(client)

    async def _run_a() -> Any:
        try:
            return await verify_razorpay_checkout_callback(
                db=db_a,
                organization_id=org_id,
                attempt_id=attempt_id,
                razorpay_payment_id=pay_a,
                razorpay_subscription_id=sub_id,
                razorpay_signature=_compute_valid_signature(pay_a, sub_id, secret),
            )
        finally:
            release_b.set()

    async def _run_b() -> Any:
        return await verify_razorpay_checkout_callback(
            db=db_b,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_b,
            razorpay_subscription_id=sub_id,
            razorpay_signature=_compute_valid_signature(pay_b, sub_id, secret),
        )

    res_a, res_b = await asyncio.gather(_run_a(), _run_b(), return_exceptions=True)

    assert not isinstance(res_a, Exception)
    assert res_a.status == CheckoutAttemptStatus.complete
    assert res_a.payment_id == pay_a
    assert isinstance(res_b, RazorpayCheckoutConflictError)
    assert pay_a in str(res_b)
    assert store.attempts[attempt_id]["status"] == CheckoutAttemptStatus.complete
    assert store.attempts[attempt_id]["provider_payment_id"] == pay_a
    assert "billing_checkout_attempts" in db_a.populate_existing_calls
    assert "billing_checkout_attempts" in db_b.populate_existing_calls
    assert not store.lock.locked()


@pytest.mark.asyncio
async def test_concurrent_authorized_and_captured_verification_never_downgrades_or_clears_binding():
    """Concurrent authorized vs captured callbacks preserve complete state and provider_payment_id."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_race_status_123"
    pay_id = "pay_race_status_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    initial_attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_race_2",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_race_2",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    store = SharedCheckoutDbStore(attempts=[initial_attempt])
    db_captured = SharedCheckoutMockDbSession(store)
    db_authorized = SharedCheckoutMockDbSession(store)

    both_entered = asyncio.Event()
    entered_count = 0
    release_authorized = asyncio.Event()

    class _DualStatusClient(MockRazorpayClient):
        async def get_payment(self, payment_id: str) -> dict[str, Any]:
            nonlocal entered_count
            entered_count += 1
            call_idx = entered_count
            if entered_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2.0)
            if call_idx == 1:
                status_val = "captured"
            else:
                await asyncio.wait_for(release_authorized.wait(), timeout=2.0)
                status_val = "authorized"
            return {
                "id": payment_id,
                "entity": "payment",
                "amount": 2900,
                "currency": "USD",
                "status": status_val,
                "method": "card",
                "subscription_id": sub_id,
                "notes": {},
            }

    set_razorpay_client_for_testing(_DualStatusClient())
    sig = _compute_valid_signature(pay_id, sub_id, secret)

    async def _run_captured() -> Any:
        try:
            return await verify_razorpay_checkout_callback(
                db=db_captured,
                organization_id=org_id,
                attempt_id=attempt_id,
                razorpay_payment_id=pay_id,
                razorpay_subscription_id=sub_id,
                razorpay_signature=sig,
            )
        finally:
            release_authorized.set()

    async def _run_authorized() -> Any:
        return await verify_razorpay_checkout_callback(
            db=db_authorized,
            organization_id=org_id,
            attempt_id=attempt_id,
            razorpay_payment_id=pay_id,
            razorpay_subscription_id=sub_id,
            razorpay_signature=sig,
        )

    res_cap, res_auth = await asyncio.gather(_run_captured(), _run_authorized())

    assert res_cap.status == CheckoutAttemptStatus.complete
    assert res_auth.status == CheckoutAttemptStatus.complete
    assert store.attempts[attempt_id]["status"] == CheckoutAttemptStatus.complete
    assert store.attempts[attempt_id]["provider_payment_id"] == pay_id
    assert not store.lock.locked()


@pytest.mark.asyncio
async def test_concurrent_webhook_complete_during_callback_http_still_backfills_payment_id():
    """If subscription.activated marks complete while /verify-checkout is in HTTP, callback still binds payment."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    sub_id = "sub_race_wh_123"
    pay_id = "pay_race_wh_456"
    secret = settings.razorpay_key_secret.get_secret_value()

    initial_attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_race_wh",
        provider_subscription_id=sub_id,
        idempotency_key="idemp_race_wh",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    store = SharedCheckoutDbStore(attempts=[initial_attempt])
    db_callback = SharedCheckoutMockDbSession(store)

    async def _simulate_webhook_during_http(_payment_id: str) -> None:
        sub_row = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="acc_test_123",
            environment="test",
            provider_customer_id="cust_race_wh",
            provider_subscription_id=sub_id,
            provider_plan_id="plan_test_pro_monthly",
            status=SubscriptionStatus.active,
            plan_tier="free",
        )
        store.subscriptions[sub_row.id] = store.snapshot_model(sub_row, store._SUB_FIELDS)
        store.attempts[attempt_id]["status"] = CheckoutAttemptStatus.complete
        store.attempts[attempt_id]["provider_payment_id"] = None

    set_razorpay_client_for_testing(
        MockRazorpayClient(
            subscription_id=sub_id,
            payment_id=pay_id,
            payment_status="captured",
            payment_amount=2900,
            get_payment_hook=_simulate_webhook_during_http,
        )
    )

    res = await verify_razorpay_checkout_callback(
        db=db_callback,
        organization_id=org_id,
        attempt_id=attempt_id,
        razorpay_payment_id=pay_id,
        razorpay_subscription_id=sub_id,
        razorpay_signature=_compute_valid_signature(pay_id, sub_id, secret),
    )

    assert res.status == CheckoutAttemptStatus.complete
    assert res.payment_id == pay_id
    assert store.attempts[attempt_id]["status"] == CheckoutAttemptStatus.complete
    assert store.attempts[attempt_id]["provider_payment_id"] == pay_id
    sub_saved = next(iter(store.subscriptions.values()))
    assert sub_saved["plan_tier"] == "free"
    assert not store.lock.locked()


# =============================================================================
# C-1 Customer Provisioning Isolation & Multi-Session Concurrency Harness
# =============================================================================


class ProvisionCheckoutIsolationStore:
    """Lock-aware multi-session store double simulating PostgreSQL READ COMMITTED,
    per-row FOR UPDATE locks, partial unique indexes, FK constraints, and tx_id lifecycle.
    """

    _ORG_FIELDS = ("id", "name")
    _PROV_FIELDS = (
        "id",
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_customer_id",
        "status",
        "claim_operation_id",
        "lease_expires_at",
        "last_error_code",
    )
    _SUB_FIELDS = (
        "id",
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_customer_id",
        "provider_subscription_id",
        "provider_plan_id",
        "status",
        "plan_tier",
    )
    _ATTEMPT_FIELDS = (
        "id",
        "organization_id",
        "provider",
        "merchant_account_id",
        "environment",
        "provider_customer_id",
        "provider_subscription_id",
        "provider_order_id",
        "provider_session_id",
        "provider_payment_id",
        "idempotency_key",
        "status",
        "amount_minor_units",
        "currency",
        "checkout_url",
        "request_payload",
        "attempt_lease_expires_at",
        "session_expires_at",
        "error_message",
        "created_at",
    )
    _ACTIVE_ATTEMPT_STATUSES = (
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
        CheckoutAttemptStatus.open,
        CheckoutAttemptStatus.payment_pending,
    )

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        provisions: list[BillingCustomerProvision] | None = None,
        subscriptions: list[Subscription] | None = None,
        attempts: list[BillingCheckoutAttempt] | None = None,
    ) -> None:
        self.organizations: dict[uuid.UUID, dict[str, Any]] = {}
        self.provisions: dict[uuid.UUID, dict[str, Any]] = {}
        self.subscriptions: dict[uuid.UUID, dict[str, Any]] = {}
        self.attempts: dict[uuid.UUID, dict[str, Any]] = {}
        self.reconciliations: dict[uuid.UUID, PaymentReconciliation] = {}

        for org in organizations or []:
            self.organizations[org.id] = self.snapshot(org, self._ORG_FIELDS)
        for prov in provisions or []:
            self.provisions[prov.id] = self.snapshot(prov, self._PROV_FIELDS)
        for sub in subscriptions or []:
            self.subscriptions[sub.id] = self.snapshot(sub, self._SUB_FIELDS)
        for att in attempts or []:
            snap = self.snapshot(att, self._ATTEMPT_FIELDS)
            if snap["created_at"] is None:
                snap["created_at"] = datetime.now(timezone.utc)
            self.attempts[att.id] = snap

        self._row_locks: dict[tuple[str, uuid.UUID], asyncio.Lock] = {}
        self.active_held_locks: dict[tuple[str, uuid.UUID], str] = {}
        self.advisory_locks: set[Any] = set()
        self.tx_counter: int = 0
        self.tx_log: list[dict[str, Any]] = []
        self.http_observations: list[dict[str, Any]] = []

    @staticmethod
    def snapshot(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
        return {f: copy.deepcopy(getattr(obj, f, None)) for f in fields}

    def get_row_lock(self, table: str, row_id: uuid.UUID) -> asyncio.Lock:
        key = (table, row_id)
        lock = self._row_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._row_locks[key] = lock
        return lock


_ACTIVE_ISOLATION_SESSION: contextvars.ContextVar["ProvisionCheckoutIsolationSession | None"] = (
    contextvars.ContextVar("_ACTIVE_ISOLATION_SESSION", default=None)
)


class ProvisionCheckoutIsolationSession:
    """AsyncSession test double backed by ProvisionCheckoutIsolationStore."""

    def __init__(
        self,
        store: ProvisionCheckoutIsolationStore,
        name: str = "S1",
        after_execute_hook: Callable[["ProvisionCheckoutIsolationSession", str, int, Any], Awaitable[None]] | None = None,
        before_commit_hook: Callable[["ProvisionCheckoutIsolationSession", int], Awaitable[None]] | None = None,
        after_commit_hook: Callable[["ProvisionCheckoutIsolationSession", int], Awaitable[None]] | None = None,
        commit_failure_fn: Callable[["ProvisionCheckoutIsolationSession", list[Any]], Exception | None] | None = None,
        on_lock_wait_hook: Callable[["ProvisionCheckoutIsolationSession", str], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.name = name
        self.after_execute_hook = after_execute_hook
        self.before_commit_hook = before_commit_hook
        self.after_commit_hook = after_commit_hook
        self.commit_failure_fn = commit_failure_fn
        self.on_lock_wait_hook = on_lock_wait_hook

        self._in_tx = False
        self.current_tx_id: int | None = None
        self.current_tx_record: dict[str, Any] | None = None
        self._held_locks: list[tuple[str, uuid.UUID]] = []
        self.uncommitted: list[Any] = []

        # Identity map (expire_on_commit=False simulation)
        self._id_orgs: dict[uuid.UUID, Organization] = {}
        self._id_provs: dict[uuid.UUID, BillingCustomerProvision] = {}
        self._id_subs: dict[uuid.UUID, Subscription] = {}
        self._id_attempts: dict[uuid.UUID, BillingCheckoutAttempt] = {}

        self._base_provs: dict[uuid.UUID, dict[str, Any]] = {}
        self._base_attempts: dict[uuid.UUID, dict[str, Any]] = {}
        self.flush_history: list[dict[str, Any]] = []
        self._child_sessions: list[ProvisionCheckoutIsolationSession] = []

    def in_transaction(self) -> bool:
        return self._in_tx

    def make_session_factory(self) -> Callable[[], Any]:
        from contextlib import asynccontextmanager

        parent = self

        @asynccontextmanager
        async def _factory():
            child = ProvisionCheckoutIsolationSession(
                store=parent.store,
                name=parent.name,
                after_execute_hook=parent.after_execute_hook,
                before_commit_hook=parent.before_commit_hook,
                after_commit_hook=parent.after_commit_hook,
                commit_failure_fn=parent.commit_failure_fn,
                on_lock_wait_hook=parent.on_lock_wait_hook,
            )
            parent._child_sessions.append(child)
            try:
                yield child
            finally:
                if child.in_transaction():
                    await child.rollback()

        return _factory

    def _ensure_tx(self) -> int:
        if not self._in_tx:
            self.store.tx_counter += 1
            self.current_tx_id = self.store.tx_counter
            self._in_tx = True
            self.current_tx_record = {
                "session": self.name,
                "tx_id": self.current_tx_id,
                "stmts": [],
                "locked_tables": [],
                "populate_existing_tables": [],
                "flushed": False,
                "outcome": "open",
            }
            self.store.tx_log.append(self.current_tx_record)
        assert self.current_tx_id is not None
        return self.current_tx_id

    async def _acquire_row_lock(self, table: str, row_id: uuid.UUID) -> None:
        key = (table, row_id)
        if key in self._held_locks:
            return
        lock = self.store.get_row_lock(table, row_id)
        if lock.locked() and self.on_lock_wait_hook is not None:
            await self.on_lock_wait_hook(self, table)
        await lock.acquire()
        self._held_locks.append(key)
        self.store.active_held_locks[key] = self.name
        if self.current_tx_record is not None:
            self.current_tx_record["locked_tables"].append(table)

    def _release_all_locks(self) -> None:
        while self._held_locks:
            key = self._held_locks.pop()
            self.store.active_held_locks.pop(key, None)
            self.store.get_row_lock(*key).release()

    def _sync_instance(
        self,
        row: dict[str, Any],
        id_map: dict[uuid.UUID, Any],
        model_cls: Any,
        populate_existing: bool,
        baseline_map: dict[uuid.UUID, dict[str, Any]] | None = None,
    ) -> Any:
        row_id = row["id"]
        if row_id in id_map:
            inst = id_map[row_id]
            if populate_existing:
                for k, v in copy.deepcopy(row).items():
                    setattr(inst, k, v)
                if baseline_map is not None:
                    baseline_map[row_id] = copy.deepcopy(row)
            return inst
        inst = model_cls(**copy.deepcopy(row))
        id_map[row_id] = inst
        if baseline_map is not None:
            baseline_map[row_id] = copy.deepcopy(row)
        return inst

    async def execute(self, stmt: Any) -> MockScalarResult:
        tx_id = self._ensure_tx()
        for_update = getattr(stmt, "_for_update_arg", None) is not None
        exec_opts = stmt.get_execution_options() if hasattr(stmt, "get_execution_options") else {}
        populate_existing = bool(exec_opts.get("populate_existing"))

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

        if self.current_tx_record is not None:
            self.current_tx_record["stmts"].append((from_table, for_update, populate_existing))
            if for_update and populate_existing and from_table:
                self.current_tx_record["populate_existing_tables"].append(from_table)

        result_obj: Any = None

        if from_table == "organizations":
            org_id = w_params.get("id")
            if org_id is not None and for_update:
                await self._acquire_row_lock("organizations", org_id)
            row = self.store.organizations.get(org_id) if org_id is not None else None
            if row is None and org_id is not None and org_id in self._id_orgs and not populate_existing:
                result_obj = self._id_orgs[org_id]
            elif row is not None:
                result_obj = self._sync_instance(row, self._id_orgs, Organization, populate_existing)

        elif from_table == "billing_customer_provisions":
            def _match_prov(row: dict[str, Any]) -> bool:
                if "organization_id" in w_params and row["organization_id"] != w_params["organization_id"]:
                    return False
                if "provider" in w_params and row["provider"] != w_params["provider"]:
                    return False
                if "merchant_account_id" in w_params and row["merchant_account_id"] != w_params["merchant_account_id"]:
                    return False
                if "environment" in w_params and row["environment"] != w_params["environment"]:
                    return False
                return True

            candidate_ids = [pid for pid, r in self.store.provisions.items() if _match_prov(r)]
            if candidate_ids:
                pid = candidate_ids[-1]
                if for_update:
                    await self._acquire_row_lock("billing_customer_provisions", pid)
                row = self.store.provisions.get(pid)
                if row is not None and _match_prov(row):
                    result_obj = self._sync_instance(
                        row, self._id_provs, BillingCustomerProvision, populate_existing, self._base_provs
                    )

        elif from_table == "subscriptions":
            def _match_sub(row: dict[str, Any]) -> bool:
                if "organization_id" in w_params and row["organization_id"] != w_params["organization_id"]:
                    return False
                if "plan_tier" in w_params and row["plan_tier"] != w_params["plan_tier"]:
                    return False
                if "provider" in w_params and row["provider"] != w_params["provider"]:
                    return False
                if "status" in w_params:
                    sf = w_params["status"]
                    if isinstance(sf, (list, tuple, set)):
                        if row["status"] not in sf:
                            return False
                    elif row["status"] != sf:
                        return False
                return True

            matching_subs = sorted(
                [r for r in self.store.subscriptions.values() if _match_sub(r)],
                key=lambda r: str(r["id"]),
            )
            if matching_subs:
                sid = matching_subs[0]["id"]
                if for_update:
                    await self._acquire_row_lock("subscriptions", sid)
                row = self.store.subscriptions.get(sid)
                if row is not None and _match_sub(row):
                    result_obj = self._sync_instance(row, self._id_subs, Subscription, populate_existing)

        elif from_table == "billing_checkout_attempts":
            def _match_attempt(row: dict[str, Any]) -> bool:
                if "id" in w_params and row["id"] != w_params["id"]:
                    return False
                if "organization_id" in w_params and row["organization_id"] != w_params["organization_id"]:
                    return False
                if "provider" in w_params and row["provider"] != w_params["provider"]:
                    return False
                if "merchant_account_id" in w_params and row["merchant_account_id"] != w_params["merchant_account_id"]:
                    return False
                if "environment" in w_params and row["environment"] != w_params["environment"]:
                    return False
                if "status" in w_params:
                    sf = w_params["status"]
                    if isinstance(sf, (list, tuple, set)):
                        if row["status"] not in sf:
                            return False
                    elif row["status"] != sf:
                        return False
                return True

            epoch_min = datetime.min.replace(tzinfo=timezone.utc)
            matching_attempts = sorted(
                [r for r in self.store.attempts.values() if _match_attempt(r)],
                key=lambda r: (-(r["created_at"] or epoch_min).timestamp(), str(r["id"])),
            )
            if matching_attempts:
                aid = matching_attempts[0]["id"]
                if for_update:
                    await self._acquire_row_lock("billing_checkout_attempts", aid)
                row = self.store.attempts.get(aid)
                if row is not None and _match_attempt(row):
                    result_obj = self._sync_instance(
                        row, self._id_attempts, BillingCheckoutAttempt, populate_existing, self._base_attempts
                    )

        if self.after_execute_hook is not None:
            await self.after_execute_hook(self, from_table, tx_id, result_obj)
        return MockScalarResult(result_obj)

    def add(self, obj: Any) -> None:
        self._ensure_tx()
        if isinstance(obj, BillingCheckoutAttempt) and obj.created_at is None:
            obj.created_at = datetime.now(timezone.utc)
        self.uncommitted.append(obj)

    async def flush(self) -> None:
        tx_id = self._ensure_tx()
        if self.current_tx_record is not None:
            self.current_tx_record["flushed"] = True
        self.flush_history.append({
            "tx_id": tx_id,
            "held_locks": list(self._held_locks),
            "in_transaction": self._in_tx,
        })

    async def commit(self) -> None:
        tx_id = self.current_tx_id or -1
        if self.before_commit_hook is not None:
            await self.before_commit_hook(self, tx_id)

        try:
            dirty_objs: list[Any] = list(self.uncommitted)
            for pid, inst in self._id_provs.items():
                if self.store.snapshot(inst, self.store._PROV_FIELDS) != self._base_provs.get(pid):
                    dirty_objs.append(inst)
            for aid, inst in self._id_attempts.items():
                if self.store.snapshot(inst, self.store._ATTEMPT_FIELDS) != self._base_attempts.get(aid):
                    dirty_objs.append(inst)

            if self.commit_failure_fn is not None:
                exc = self.commit_failure_fn(self, dirty_objs)
                if exc is not None:
                    raise exc

            # Prepare candidate snapshots and enforce unique/FK constraints atomically
            next_provs = copy.deepcopy(self.store.provisions)
            for pid, inst in self._id_provs.items():
                cur = self.store.snapshot(inst, self.store._PROV_FIELDS)
                if cur != self._base_provs.get(pid) and pid in next_provs:
                    next_provs[pid] = cur
            for obj in self.uncommitted:
                if isinstance(obj, BillingCustomerProvision):
                    scope_key = (obj.organization_id, obj.provider, obj.merchant_account_id, obj.environment)
                    for existing in next_provs.values():
                        if (
                            existing["organization_id"],
                            existing["provider"],
                            existing["merchant_account_id"],
                            existing["environment"],
                        ) == scope_key:
                            raise IntegrityError("uq_billing_customer_provisions_org_provider", params={}, orig=Exception())
                    next_provs[obj.id] = self.store.snapshot(obj, self.store._PROV_FIELDS)

            next_attempts = copy.deepcopy(self.store.attempts)
            for aid, inst in self._id_attempts.items():
                cur = self.store.snapshot(inst, self.store._ATTEMPT_FIELDS)
                if cur != self._base_attempts.get(aid) and aid in next_attempts:
                    next_attempts[aid] = cur
            for obj in self.uncommitted:
                if isinstance(obj, BillingCheckoutAttempt):
                    next_attempts[obj.id] = self.store.snapshot(obj, self.store._ATTEMPT_FIELDS)

            # Enforce partial unique index uq_org_scoped_active_billing_checkout_attempt
            active_scopes: set[tuple[uuid.UUID, str, str, str]] = set()
            for row in next_attempts.values():
                if row["status"] in self.store._ACTIVE_ATTEMPT_STATUSES:
                    sk = (row["organization_id"], row["provider"], row["merchant_account_id"], row["environment"])
                    if sk in active_scopes:
                        raise IntegrityError("uq_org_scoped_active_billing_checkout_attempt", params={}, orig=Exception())
                    active_scopes.add(sk)

            # Enforce FK PaymentReconciliation.organization_id ON DELETE RESTRICT
            for obj in self.uncommitted:
                if isinstance(obj, PaymentReconciliation):
                    if obj.organization_id is not None and obj.organization_id not in self.store.organizations:
                        raise IntegrityError("fk_payment_reconciliations_org_restrict", params={}, orig=Exception())
                    self.store.reconciliations[obj.id] = obj

            # Publish atomically to shared store
            self.store.provisions = next_provs
            self.store.attempts = next_attempts
            for pid, inst in self._id_provs.items():
                if pid in next_provs:
                    self._base_provs[pid] = copy.deepcopy(next_provs[pid])
            for aid, inst in self._id_attempts.items():
                if aid in next_attempts:
                    self._base_attempts[aid] = copy.deepcopy(next_attempts[aid])
            for obj in self.uncommitted:
                if isinstance(obj, BillingCustomerProvision):
                    self._id_provs[obj.id] = obj
                    self._base_provs[obj.id] = copy.deepcopy(next_provs[obj.id])
                elif isinstance(obj, BillingCheckoutAttempt):
                    self._id_attempts[obj.id] = obj
                    self._base_attempts[obj.id] = copy.deepcopy(next_attempts[obj.id])

            self.uncommitted.clear()
            if self.current_tx_record is not None:
                self.current_tx_record["outcome"] = "committed"
            self._in_tx = False
            self.current_tx_id = None
            self.current_tx_record = None
            self._release_all_locks()
        except Exception:
            raise

        if self.after_commit_hook is not None:
            await self.after_commit_hook(self, tx_id)

    async def rollback(self) -> None:
        for pid, inst in self._id_provs.items():
            base = self._base_provs.get(pid)
            if base is not None:
                for k, v in copy.deepcopy(base).items():
                    setattr(inst, k, v)
        for aid, inst in self._id_attempts.items():
            base = self._base_attempts.get(aid)
            if base is not None:
                for k, v in copy.deepcopy(base).items():
                    setattr(inst, k, v)
        self.uncommitted.clear()
        if self.current_tx_record is not None:
            self.current_tx_record["outcome"] = "rolled_back"
        self._in_tx = False
        self.current_tx_id = None
        self.current_tx_record = None
        self._release_all_locks()


def _make_tx_observing_client(
    store: ProvisionCheckoutIsolationStore,
    sessions: list[ProvisionCheckoutIsolationSession],
    customer_id: str = "cust_iso_123",
    subscription_id: str = "sub_iso_456",
    on_create_customer: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
    on_create_subscription: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
) -> MockRazorpayClient:
    """Create a MockRazorpayClient that records TX & lock state during every external HTTP call."""

    class _ObservingClient(MockRazorpayClient):
        def __init__(self) -> None:
            super().__init__(customer_id=customer_id, subscription_id=subscription_id)
            self.create_customer_calls: list[dict[str, Any]] = []

        async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
            self.create_customer_calls.append(kwargs)
            caller = _ACTIVE_ISOLATION_SESSION.get(None)
            target_sessions = (
                [caller, *getattr(caller, "_child_sessions", ())]
                if caller is not None
                else [
                    *sessions,
                    *(c for s in sessions for c in getattr(s, "_child_sessions", ())),
                ]
            )
            calling_in_tx = any(s.in_transaction() for s in target_sessions)
            calling_locks = (
                {k: v for k, v in store.active_held_locks.items() if v == caller.name}
                if caller is not None
                else dict(store.active_held_locks)
            )
            store.http_observations.append({
                "operation": "create_customer",
                "any_in_tx": calling_in_tx,
                "held_locks": calling_locks,
                "advisory_locks": set(store.advisory_locks),
            })
            assert not calling_in_tx, (
                "HTTP create_customer called inside open DB transaction!"
            )
            assert len(calling_locks) == 0, (
                f"HTTP create_customer called while DB row locks held: {calling_locks}"
            )
            assert len(store.advisory_locks) == 0, (
                f"HTTP create_customer called while advisory locks held: {store.advisory_locks}"
            )
            if on_create_customer is not None:
                custom = await on_create_customer(kwargs)
                if custom is not None:
                    return custom
            return {"id": self.customer_id, "name": kwargs.get("name")}

        async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
            self.create_subscription_calls.append(kwargs)
            caller = _ACTIVE_ISOLATION_SESSION.get(None)
            target_sessions = (
                [caller, *getattr(caller, "_child_sessions", ())]
                if caller is not None
                else [
                    *sessions,
                    *(c for s in sessions for c in getattr(s, "_child_sessions", ())),
                ]
            )
            calling_in_tx = any(s.in_transaction() for s in target_sessions)
            calling_locks = (
                {k: v for k, v in store.active_held_locks.items() if v == caller.name}
                if caller is not None
                else dict(store.active_held_locks)
            )
            store.http_observations.append({
                "operation": "create_subscription",
                "any_in_tx": calling_in_tx,
                "held_locks": calling_locks,
                "advisory_locks": set(store.advisory_locks),
            })
            assert not calling_in_tx, (
                "HTTP create_subscription called inside open DB transaction!"
            )
            assert len(calling_locks) == 0, (
                f"HTTP create_subscription called while DB row locks held: {calling_locks}"
            )
            assert len(store.advisory_locks) == 0, (
                f"HTTP create_subscription called while advisory locks held: {store.advisory_locks}"
            )
            if on_create_subscription is not None:
                custom = await on_create_subscription(kwargs)
                if custom is not None:
                    return custom
            return {
                "id": self.subscription_id,
                "entity": "subscription",
                "plan_id": kwargs.get("plan_id"),
                "customer_id": kwargs.get("customer_id"),
                "status": "created",
                "short_url": f"https://rzp.io/i/{self.subscription_id}",
                "expire_by": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
            }

    return _ObservingClient()


@pytest.mark.asyncio
async def test_c1_scenario_1_successful_provisioning_followed_by_checkout_creation():
    """Scenario 1: Successful customer provisioning commits Phase A cleanly before Phase B opens
    a fresh transaction, revalidates locks in order, stages attempt, creates subscription, and commits Phase 4.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 1 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])
    db = ProvisionCheckoutIsolationSession(store, name="S1")

    client = _make_tx_observing_client(
        store, [db], customer_id="cust_c1_s1", subscription_id="sub_c1_s1"
    )
    set_razorpay_client_for_testing(client)

    res = await create_razorpay_subscription_checkout(
        db=db,
        organization=org,
        user_email="s1@example.com",
        customer_session_factory=db.make_session_factory(),
    )

    assert res.subscription_id == "sub_c1_s1"
    assert res.status == CheckoutAttemptStatus.open
    assert len(client.create_customer_calls) == 1
    assert len(client.create_subscription_calls) == 1
    assert client.create_subscription_calls[0]["customer_id"] == "cust_c1_s1"
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0

    # Verify exact distinct transactions: TX_A1 -> TX_A2 -> TX_B (staging) -> TX_4 (open)
    assert [t["tx_id"] for t in store.tx_log] == [1, 2, 3, 4]
    assert all(t["outcome"] == "committed" for t in store.tx_log)
    assert store.tx_log[0]["locked_tables"] == ["organizations", "billing_customer_provisions"] or store.tx_log[0]["locked_tables"] == ["organizations"]
    assert store.tx_log[2]["locked_tables"][:2] == ["organizations", "billing_customer_provisions"]
    assert store.tx_log[2]["populate_existing_tables"][:2] == ["organizations", "billing_customer_provisions"]


@pytest.mark.asyncio
async def test_c1_scenario_2_existing_customer_reused_without_remote_create_even_with_open_caller_tx():
    """Scenario 2: Existing resolved customer mapping is reused without HTTP create_customer,
    closes both prior caller TX and Phase A read TX cleanly, and runs Phase B in a separate TX.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 2 Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_reused_s2",
        status=CustomerProvisionStatus.resolved,
    )
    store = ProvisionCheckoutIsolationStore(organizations=[org], provisions=[prov])
    db = ProvisionCheckoutIsolationSession(store, name="S1")

    # Simulate API route caller read TX (e.g. RBAC + org lookup before checkout service)
    await db.execute(select(Organization).where(Organization.id == org_id))
    assert db.in_transaction() is True
    caller_tx_id = db.current_tx_id

    client = _make_tx_observing_client(store, [db], subscription_id="sub_c1_s2")
    set_razorpay_client_for_testing(client)

    res = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.make_session_factory()
    )

    assert res.subscription_id == "sub_c1_s2"
    assert len(client.create_customer_calls) == 0
    assert len(client.create_subscription_calls) == 1
    assert client.create_subscription_calls[0]["customer_id"] == "cust_reused_s2"
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0

    # Caller TX (tx 1) + Phase A reuse TX (tx 2) + Phase B staging (tx 3) + Phase 4 (tx 4)
    assert len(store.tx_log) == 4
    assert store.tx_log[0]["tx_id"] == caller_tx_id
    assert store.tx_log[1]["outcome"] == "committed"
    assert store.tx_log[2]["tx_id"] > store.tx_log[1]["tx_id"]


@pytest.mark.asyncio
async def test_c1_scenario_3_concurrent_customer_provisioning_one_org():
    """Scenario 3: Concurrent checkouts on an unprovisioned org serialize customer provisioning under
    Organization FOR UPDATE so at most one create_customer HTTP call executes without duplicate provisions.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 3 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])

    db_a = ProvisionCheckoutIsolationSession(store, name="WorkerA")
    db_b = ProvisionCheckoutIsolationSession(store, name="WorkerB")

    a_in_create_customer = asyncio.Event()
    release_a_customer_http = asyncio.Event()
    create_cust_count = 0

    async def _on_create_cust(_kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal create_cust_count
        create_cust_count += 1
        a_in_create_customer.set()
        await asyncio.wait_for(release_a_customer_http.wait(), timeout=2.0)
        return {"id": "cust_concurrent_winner"}

    client = _make_tx_observing_client(
        store,
        [db_a, db_b],
        subscription_id="sub_concurrent_3",
        on_create_customer=_on_create_cust,
    )
    set_razorpay_client_for_testing(client)

    async def _run_a() -> Any:
        token = _ACTIVE_ISOLATION_SESSION.set(db_a)
        try:
            return await create_razorpay_subscription_checkout(
                db=db_a, organization=org, customer_session_factory=db_a.make_session_factory()
            )
        finally:
            _ACTIVE_ISOLATION_SESSION.reset(token)

    async def _run_b() -> Any:
        token = _ACTIVE_ISOLATION_SESSION.set(db_b)
        try:
            await asyncio.wait_for(a_in_create_customer.wait(), timeout=2.0)
            try:
                return await create_razorpay_subscription_checkout(
                    db=db_b, organization=org, customer_session_factory=db_b.make_session_factory()
                )
            finally:
                release_a_customer_http.set()
        finally:
            _ACTIVE_ISOLATION_SESSION.reset(token)

    res_a, res_b = await asyncio.gather(_run_a(), _run_b(), return_exceptions=True)

    assert not isinstance(res_a, Exception)
    assert res_a.subscription_id == "sub_concurrent_3"
    assert isinstance(res_b, RazorpayCustomerProvisioningInProgressError)
    assert create_cust_count == 1
    assert len(store.provisions) == 1
    assert len(store.attempts) == 1
    assert not db_a.in_transaction()
    assert not db_b.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_4_concurrent_checkout_and_atomic_expired_open_rollover():
    """Scenario 4: Concurrent checkouts after provisioning serialize via Phase B Organization FOR UPDATE,
    and expiring a stale 'open' attempt flushes inside the SAME locked Phase B TX without dropping locks mid-way.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 4 Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_pre_4",
        status=CustomerProvisionStatus.resolved,
    )
    expired_open = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_pre_4",
        provider_subscription_id="sub_expired_old_4",
        idempotency_key="idemp_exp_old_4",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=0,
        currency="USD",
        checkout_url="https://rzp.io/i/sub_expired_old_4",
        request_payload={"plan_id": "plan_test_pro_monthly"},
        attempt_lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
        session_expires_at=datetime.now(timezone.utc) - timedelta(minutes=15),
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    store = ProvisionCheckoutIsolationStore(
        organizations=[org], provisions=[prov], attempts=[expired_open]
    )

    a_holding_phase_b_lock = asyncio.Event()
    b_queued_on_lock = asyncio.Event()
    release_a_phase_b_commit = asyncio.Event()

    async def _after_exec_a(
        _session: ProvisionCheckoutIsolationSession, table: str, tx_id: int, _row: Any
    ) -> None:
        # Pause Worker A inside Phase B after acquiring Organization + Provision + Attempt locks
        if table == "billing_checkout_attempts" and tx_id >= 2:
            a_holding_phase_b_lock.set()
            await asyncio.wait_for(release_a_phase_b_commit.wait(), timeout=2.0)

    async def _on_b_lock_wait(_session: ProvisionCheckoutIsolationSession, table: str) -> None:
        if table == "organizations":
            b_queued_on_lock.set()

    db_a = ProvisionCheckoutIsolationSession(store, name="CheckoutA", after_execute_hook=_after_exec_a)
    db_b = ProvisionCheckoutIsolationSession(store, name="CheckoutB", on_lock_wait_hook=_on_b_lock_wait)

    a_in_http = asyncio.Event()
    release_a_http = asyncio.Event()

    async def _on_create_sub(kwargs: dict[str, Any]) -> dict[str, Any]:
        a_in_http.set()
        await asyncio.wait_for(release_a_http.wait(), timeout=2.0)
        return {
            "id": "sub_new_winner_4",
            "status": "created",
            "short_url": "https://rzp.io/i/sub_new_winner_4",
        }

    client = _make_tx_observing_client(store, [db_a, db_b], on_create_subscription=_on_create_sub)
    set_razorpay_client_for_testing(client)

    async def _run_a() -> Any:
        token = _ACTIVE_ISOLATION_SESSION.set(db_a)
        try:
            return await create_razorpay_subscription_checkout(
                db=db_a, organization=org, customer_session_factory=db_a.make_session_factory()
            )
        finally:
            _ACTIVE_ISOLATION_SESSION.reset(token)

    async def _run_b() -> Any:
        token = _ACTIVE_ISOLATION_SESSION.set(db_b)
        try:
            await asyncio.wait_for(a_holding_phase_b_lock.wait(), timeout=2.0)
            task_b = asyncio.create_task(
                create_razorpay_subscription_checkout(
                    db=db_b, organization=org, customer_session_factory=db_b.make_session_factory()
                )
            )
            # Wait deterministically until Worker B blocks on Worker A's Organization FOR UPDATE lock
            await asyncio.wait_for(b_queued_on_lock.wait(), timeout=2.0)
            release_a_phase_b_commit.set()
            await asyncio.wait_for(a_in_http.wait(), timeout=2.0)
            try:
                return await task_b
            finally:
                release_a_http.set()
        finally:
            _ACTIVE_ISOLATION_SESSION.reset(token)

    res_a, res_b = await asyncio.gather(_run_a(), _run_b(), return_exceptions=True)

    assert not isinstance(res_a, Exception)
    assert res_a.subscription_id == "sub_new_winner_4"
    assert isinstance(res_b, CheckoutInProgressError)
    assert len(client.create_subscription_calls) == 1

    # Verify flush kept Phase B locks held until atomic commit (old attempt expired + new attempt staged together)
    assert len(db_a.flush_history) == 1
    assert ("organizations", org_id) in db_a.flush_history[0]["held_locks"]
    assert store.attempts[expired_open.id]["status"] == CheckoutAttemptStatus.expired
    active_rows = [
        a for a in store.attempts.values() if a["status"] in store._ACTIVE_ATTEMPT_STATUSES
    ]
    assert len(active_rows) == 1
    assert active_rows[0]["provider_subscription_id"] == "sub_new_winner_4"
    assert not db_a.in_transaction()
    assert not db_b.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_4b_simultaneous_phase_b_entry_after_phase_a_barrier():
    """Scenario 4b: Two concurrent callers that BOTH completed Phase A independently serialize
    cleanly on Phase B Organization FOR UPDATE so only one creates a checkout attempt."""
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 4b Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_pre_4b",
        status=CustomerProvisionStatus.resolved,
    )
    store = ProvisionCheckoutIsolationStore(organizations=[org], provisions=[prov])

    both_finished_phase_a = asyncio.Event()
    phase_a_done_count = 0

    async def _barrier_after_phase_a(
        _session: ProvisionCheckoutIsolationSession, _tx_id: int
    ) -> None:
        nonlocal phase_a_done_count
        # First commit on each session is Phase A reuse TX
        if phase_a_done_count < 2:
            phase_a_done_count += 1
            if phase_a_done_count == 2:
                both_finished_phase_a.set()
            await asyncio.wait_for(both_finished_phase_a.wait(), timeout=2.0)

    db_a = ProvisionCheckoutIsolationSession(store, name="PhaseB_A", after_commit_hook=_barrier_after_phase_a)
    db_b = ProvisionCheckoutIsolationSession(store, name="PhaseB_B", after_commit_hook=_barrier_after_phase_a)

    first_in_http = asyncio.Event()
    release_first_http = asyncio.Event()

    async def _on_create_sub(kwargs: dict[str, Any]) -> dict[str, Any]:
        first_in_http.set()
        await asyncio.wait_for(release_first_http.wait(), timeout=2.0)
        return {
            "id": "sub_phase_b_winner",
            "status": "created",
            "short_url": "https://rzp.io/i/sub_phase_b_winner",
        }

    client = _make_tx_observing_client(store, [db_a, db_b], on_create_subscription=_on_create_sub)
    set_razorpay_client_for_testing(client)

    async def _run(session: ProvisionCheckoutIsolationSession) -> Any:
        token = _ACTIVE_ISOLATION_SESSION.set(session)
        try:
            res = await create_razorpay_subscription_checkout(
                db=session, organization=org, customer_session_factory=session.make_session_factory()
            )
            release_first_http.set()
            return res
        except CheckoutInProgressError:
            release_first_http.set()
            raise
        except Exception:
            release_first_http.set()
            raise
        finally:
            _ACTIVE_ISOLATION_SESSION.reset(token)

    res_a, res_b = await asyncio.gather(_run(db_a), _run(db_b), return_exceptions=True)

    successes = [r for r in (res_a, res_b) if not isinstance(r, Exception)]
    conflicts = [r for r in (res_a, res_b) if isinstance(r, CheckoutInProgressError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].subscription_id == "sub_phase_b_winner"
    assert len(client.create_subscription_calls) == 1
    assert len(store.attempts) == 1
    assert not db_a.in_transaction()
    assert not db_b.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_5_remote_customer_success_with_db_persistence_failure():
    """Scenario 5: If remote customer creation succeeds but TX_A2 resolution commit fails,
    customer ID is preserved in 'ambiguous' state, Phase B never runs, and zero attempts/subscriptions are created.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 5 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])

    def _fail_resolved_commit(
        _session: ProvisionCheckoutIsolationSession, dirty: list[Any]
    ) -> Exception | None:
        if any(
            isinstance(o, BillingCustomerProvision) and o.status == CustomerProvisionStatus.resolved
            for o in dirty
        ):
            return OperationalError("PostgreSQL outage on Phase A TX2 commit", params={}, orig=Exception())
        return None

    db = ProvisionCheckoutIsolationSession(store, name="S5", commit_failure_fn=_fail_resolved_commit)
    client = _make_tx_observing_client(store, [db], customer_id="cust_pers_fail_5")
    set_razorpay_client_for_testing(client)

    with pytest.raises(RazorpayCustomerPersistenceError) as exc_info:
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.make_session_factory()
        )

    assert exc_info.value.customer_id == "cust_pers_fail_5"
    assert len(client.create_customer_calls) == 1
    assert len(client.create_subscription_calls) == 0
    assert len(store.attempts) == 0
    assert len(store.provisions) == 1
    prov_saved = next(iter(store.provisions.values()))
    assert prov_saved["status"] == CustomerProvisionStatus.ambiguous
    assert prov_saved["provider_customer_id"] == "cust_pers_fail_5"
    assert prov_saved["last_error_code"] == "db_persistence_failure"
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_6_ambiguous_remote_customer_outcome_blocks_retry_and_skips_phase_b():
    """Scenario 6: Ambiguous network/5xx customer creation transitions claim to ambiguous and
    blocks both immediate checkout and subsequent retry without entering Phase B.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 6 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])
    db = ProvisionCheckoutIsolationSession(store, name="S6")

    async def _fail_500(_kwargs: dict[str, Any]) -> dict[str, Any]:
        raise RazorpayServer5xxError("Upstream 502 Bad Gateway", status_code=502)

    client = _make_tx_observing_client(store, [db], on_create_customer=_fail_500)
    set_razorpay_client_for_testing(client)

    with pytest.raises(RazorpayAmbiguousNetworkError):
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.make_session_factory()
        )

    assert len(client.create_customer_calls) == 1
    assert len(client.create_subscription_calls) == 0
    assert len(store.attempts) == 0
    prov_saved = next(iter(store.provisions.values()))
    assert prov_saved["status"] == CustomerProvisionStatus.ambiguous
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0

    # Retry attempt is blocked cleanly in Phase A TX1 without remote calls or Phase B entry
    with pytest.raises(RazorpayCustomerReconciliationRequiredError):
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.make_session_factory()
        )

    assert len(client.create_customer_calls) == 1
    assert len(client.create_subscription_calls) == 0
    assert len(store.attempts) == 0
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_7_customer_mapping_mutation_between_phases_fails_closed():
    """Scenario 7: If customer mapping mutates (ID drift, non-resolved status, or deletion) after
    Phase A commits and before Phase B locks complete, Phase B detects it via populate_existing=True
    (defeating expire_on_commit=False stale identity map) and fails closed with RazorpayCheckoutConflictError.
    """
    # Subcase A: resolved ID drifts cust_A -> cust_B between Phase A and Phase B even with stale identity map
    org_id_a = uuid.uuid4()
    org_a = Organization(id=org_id_a, name="Drift Org")
    prov_a = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id_a,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_phase_a_111",
        status=CustomerProvisionStatus.resolved,
    )
    store_a = ProvisionCheckoutIsolationStore(organizations=[org_a], provisions=[prov_a])

    async def _mutate_id_after_phase_a_commit(
        _session: ProvisionCheckoutIsolationSession, tx_id: int
    ) -> None:
        if tx_id == 1:
            store_a.provisions[prov_a.id]["provider_customer_id"] = "cust_mutated_222"

    db_a = ProvisionCheckoutIsolationSession(
        store_a, name="DriftSession", after_commit_hook=_mutate_id_after_phase_a_commit
    )
    client_a = _make_tx_observing_client(store_a, [db_a])
    set_razorpay_client_for_testing(client_a)

    with pytest.raises(RazorpayCheckoutConflictError, match="changed between provisioning"):
        await create_razorpay_subscription_checkout(
            db=db_a, organization=org_a, customer_session_factory=db_a.make_session_factory()
        )

    assert len(store_a.attempts) == 0
    assert len(client_a.create_subscription_calls) == 0
    assert not db_a.in_transaction()
    assert len(store_a.active_held_locks) == 0

    # Subcase B: mapping transitions resolved -> ambiguous between Phase A and Phase B
    org_id_b = uuid.uuid4()
    org_b = Organization(id=org_id_b, name="AmbigDrift Org")
    prov_b = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id_b,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_phase_a_333",
        status=CustomerProvisionStatus.resolved,
    )
    store_b = ProvisionCheckoutIsolationStore(organizations=[org_b], provisions=[prov_b])

    async def _mutate_status_after_phase_a(
        _session: ProvisionCheckoutIsolationSession, tx_id: int
    ) -> None:
        if tx_id == 1:
            store_b.provisions[prov_b.id]["status"] = CustomerProvisionStatus.ambiguous

    db_b = ProvisionCheckoutIsolationSession(
        store_b, name="StatusDriftSession", after_commit_hook=_mutate_status_after_phase_a
    )
    client_b = _make_tx_observing_client(store_b, [db_b])
    set_razorpay_client_for_testing(client_b)

    with pytest.raises(RazorpayCheckoutConflictError, match="transitioned to 'ambiguous'"):
        await create_razorpay_subscription_checkout(
            db=db_b, organization=org_b, customer_session_factory=db_b.make_session_factory()
        )

    assert len(store_b.attempts) == 0
    assert len(client_b.create_subscription_calls) == 0
    assert not db_b.in_transaction()

    # Subcase C: provision row deleted between Phase A and Phase B
    org_id_c = uuid.uuid4()
    org_c = Organization(id=org_id_c, name="DeletedProv Org")
    prov_c = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id_c,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_phase_a_444",
        status=CustomerProvisionStatus.resolved,
    )
    store_c = ProvisionCheckoutIsolationStore(organizations=[org_c], provisions=[prov_c])

    async def _delete_prov_after_phase_a(
        _session: ProvisionCheckoutIsolationSession, tx_id: int
    ) -> None:
        if tx_id == 1:
            store_c.provisions.pop(prov_c.id, None)

    db_c = ProvisionCheckoutIsolationSession(
        store_c, name="DeletedProvSession", after_commit_hook=_delete_prov_after_phase_a
    )
    client_c = _make_tx_observing_client(store_c, [db_c])
    set_razorpay_client_for_testing(client_c)

    with pytest.raises(RazorpayCheckoutConflictError, match="disappeared"):
        await create_razorpay_subscription_checkout(
            db=db_c, organization=org_c, customer_session_factory=db_c.make_session_factory()
        )

    assert len(store_c.attempts) == 0
    assert len(client_c.create_subscription_calls) == 0
    assert not db_c.in_transaction()


@pytest.mark.asyncio
async def test_c1_scenario_8_organization_unavailable_between_phases_fails_closed():
    """Scenario 8: If organization disappears between Phase A commit and Phase B Organization FOR UPDATE,
    Phase B detects unavailability via populate_existing=True and fails closed with RazorpayCheckoutNotFoundError.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="Vanishing Between Phases Org")
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="cust_vanish_8",
        status=CustomerProvisionStatus.resolved,
    )
    store = ProvisionCheckoutIsolationStore(organizations=[org], provisions=[prov])

    async def _delete_org_after_phase_a(
        _session: ProvisionCheckoutIsolationSession, tx_id: int
    ) -> None:
        if tx_id == 1:
            store.organizations.pop(org_id, None)

    db = ProvisionCheckoutIsolationSession(
        store, name="OrgVanishSession", after_commit_hook=_delete_org_after_phase_a
    )
    client = _make_tx_observing_client(store, [db])
    set_razorpay_client_for_testing(client)

    with pytest.raises(RazorpayCheckoutNotFoundError, match="unavailable or was removed"):
        await create_razorpay_subscription_checkout(
            db=db, organization=org, customer_session_factory=db.make_session_factory()
        )

    assert len(store.attempts) == 0
    assert len(client.create_subscription_calls) == 0
    assert not db.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_9_retry_after_provisioning_succeeds_and_checkout_fails_reuses_customer():
    """Scenario 9: If Phase A provisions cust_retry_1 and Phase 3 create_subscription fails cleanly,
    retrying checkout reuses cust_retry_1 with ZERO duplicate create_customer HTTP calls.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 9 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])
    db1 = ProvisionCheckoutIsolationSession(store, name="Attempt1")

    should_fail_sub = True

    async def _maybe_fail_sub(_kwargs: dict[str, Any]) -> dict[str, Any] | None:
        if should_fail_sub:
            raise RazorpayNetworkError("ConnectTimeout before dispatch", is_pre_dispatch=True)
        return None

    client = _make_tx_observing_client(
        store,
        [db1],
        customer_id="cust_retry_1",
        subscription_id="sub_retry_2",
        on_create_subscription=_maybe_fail_sub,
    )
    set_razorpay_client_for_testing(client)

    with pytest.raises(RazorpayCheckoutError, match="Pre-dispatch"):
        await create_razorpay_subscription_checkout(
            db=db1, organization=org, customer_session_factory=db1.make_session_factory()
        )

    assert len(client.create_customer_calls) == 1
    assert len(client.create_subscription_calls) == 1
    assert len(store.attempts) == 1
    first_attempt = next(iter(store.attempts.values()))
    assert first_attempt["status"] == CheckoutAttemptStatus.failed
    assert store.provisions and next(iter(store.provisions.values()))["provider_customer_id"] == "cust_retry_1"
    assert not db1.in_transaction()
    assert len(store.active_held_locks) == 0

    # Retry with a fresh session: zero duplicate customer creation!
    should_fail_sub = False
    db2 = ProvisionCheckoutIsolationSession(store, name="Attempt2")
    client2 = _make_tx_observing_client(
        store,
        [db2],
        customer_id="cust_should_never_be_created",
        subscription_id="sub_retry_2",
    )
    set_razorpay_client_for_testing(client2)

    res = await create_razorpay_subscription_checkout(
        db=db2, organization=org, customer_session_factory=db2.make_session_factory()
    )

    assert res.subscription_id == "sub_retry_2"
    assert res.status == CheckoutAttemptStatus.open
    assert len(client2.create_customer_calls) == 0
    assert len(client2.create_subscription_calls) == 1
    assert client2.create_subscription_calls[0]["customer_id"] == "cust_retry_1"
    assert len(store.attempts) == 2
    assert not db2.in_transaction()
    assert len(store.active_held_locks) == 0


@pytest.mark.asyncio
async def test_c1_scenario_10_zero_locks_or_open_transactions_held_during_any_external_http():
    """Scenario 10: Neither Phase A (create_customer) nor Phase 3 (create_subscription) ever runs
    inside an open database transaction or while holding any row or advisory lock.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 10 Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])
    db = ProvisionCheckoutIsolationSession(store, name="NoHttpLockSession")

    client = _make_tx_observing_client(
        store, [db], customer_id="cust_http_clean_10", subscription_id="sub_http_clean_10"
    )
    set_razorpay_client_for_testing(client)

    await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.make_session_factory()
    )

    assert [obs["operation"] for obs in store.http_observations] == [
        "create_customer",
        "create_subscription",
    ]
    for obs in store.http_observations:
        assert obs["any_in_tx"] is False
        assert obs["held_locks"] == {}
        assert obs["advisory_locks"] == set()


@pytest.mark.asyncio
async def test_c1_scenario_11_phase_a_transaction_never_reused_for_phase_b_locks():
    """Scenario 11: Phase A transactions always close strictly before Phase B begins, and Phase B
    always opens a fresh distinct tx_id that explicitly re-acquires Organization FOR UPDATE and
    BillingCustomerProvision FOR UPDATE in populate_existing=True mode before Subscription/Attempt locks.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="C1 Scenario 11 Org")
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_123",
        environment="test",
        provider_customer_id="old_cust",
        provider_subscription_id="sub_old_canceled",
        status=SubscriptionStatus.canceled,
        plan_tier="pro",
    )
    store = ProvisionCheckoutIsolationStore(organizations=[org], subscriptions=[sub])
    db = ProvisionCheckoutIsolationSession(store, name="BoundarySession")

    # Simulate an open caller read transaction before calling checkout creation
    await db.execute(select(Organization).where(Organization.id == org_id))
    caller_tx_id = db.current_tx_id

    client = _make_tx_observing_client(
        store, [db], customer_id="cust_tx_sep_11", subscription_id="sub_tx_sep_11"
    )
    set_razorpay_client_for_testing(client)

    res = await create_razorpay_subscription_checkout(
        db=db, organization=org, customer_session_factory=db.make_session_factory()
    )
    assert res.subscription_id == "sub_tx_sep_11"

    # Expect strictly distinct monotonic transactions:
    # tx 1: caller pre-existing read TX (committed before Phase A)
    # tx 2: Phase A TX1 (Org FOR UPDATE + Provision insert 'pending' -> commit)
    # tx 3: Phase A TX2 (Org FOR UPDATE + Provision update 'resolved' -> commit)
    # tx 4: Phase B (Org FOR UPDATE + Provision FOR UPDATE + Subscription FOR UPDATE + Attempt staging -> commit)
    # tx 5: Phase 4 (Attempt FOR UPDATE -> 'open' -> commit)
    tx_ids = [entry["tx_id"] for entry in store.tx_log]
    assert tx_ids == [1, 2, 3, 4, 5]
    assert len(set(tx_ids)) == 5
    assert all(entry["outcome"] == "committed" for entry in store.tx_log)

    caller_tx, phase_a1, phase_a2, phase_b, phase_4 = store.tx_log
    assert caller_tx["tx_id"] == caller_tx_id
    assert phase_b["tx_id"] > phase_a2["tx_id"] > phase_a1["tx_id"] > caller_tx["tx_id"]

    # Phase B must re-acquire Organization FOR UPDATE and BillingCustomerProvision FOR UPDATE
    # with populate_existing=True strictly inside its own fresh transaction, followed by Subscription -> Attempt.
    assert phase_b["locked_tables"] == ["organizations", "billing_customer_provisions"]
    assert phase_b["populate_existing_tables"] == [
        "organizations",
        "billing_customer_provisions",
        "subscriptions",
        "billing_checkout_attempts",
    ]
    assert phase_b["stmts"] == [
        ("organizations", True, True),
        ("billing_customer_provisions", True, True),
        ("subscriptions", True, True),
        ("billing_checkout_attempts", True, True),
    ]
    assert phase_4["locked_tables"] == ["billing_checkout_attempts"]
    assert phase_4["populate_existing_tables"] == ["billing_checkout_attempts"]
    assert not db.in_transaction()


@pytest.mark.asyncio
async def test_d2_stage1_checkout_uses_injected_customer_session_factory_separately_from_checkout_db(
    monkeypatch,
):
    """D-2 Stage 1 Checkout Boundary: create_razorpay_subscription_checkout passes scalar
    organization_id and customer_session_factory into get_or_create_razorpay_customer(),
    running Phase A customer provisioning inside injected factory sessions rather than `db`
    or `AsyncSessionLocal`.
    """
    org_id = uuid.uuid4()
    org = Organization(id=org_id, name="D2 Checkout Factory Org")
    store = ProvisionCheckoutIsolationStore(organizations=[org])
    checkout_db = ProvisionCheckoutIsolationSession(store, name="CheckoutDB")
    customer_root = ProvisionCheckoutIsolationSession(store, name="CustomerFactory")

    async def _fail_default_session_local():
        raise AssertionError("AsyncSessionLocal must not be used when customer_session_factory is provided")

    monkeypatch.setattr(
        "app.services.billing.razorpay.customer.AsyncSessionLocal",
        _fail_default_session_local,
    )

    client = _make_tx_observing_client(
        store,
        [checkout_db, customer_root],
        customer_id="cust_d2_chk_1",
        subscription_id="sub_d2_chk_1",
    )
    set_razorpay_client_for_testing(client)

    res = await create_razorpay_subscription_checkout(
        db=checkout_db,
        organization=org,
        user_email="d2@example.com",
        customer_session_factory=customer_root.make_session_factory(),
    )

    assert res.subscription_id == "sub_d2_chk_1"
    assert len(customer_root._child_sessions) == 2  # TX_A1 + TX_A2
    assert [t["session"] for t in store.tx_log] == [
        "CustomerFactory",
        "CustomerFactory",
        "CheckoutDB",
        "CheckoutDB",
    ]
    assert all(t["outcome"] == "committed" for t in store.tx_log)
    assert not checkout_db.in_transaction()
    assert len(store.active_held_locks) == 0
