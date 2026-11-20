"""Stage 2A Regression Tests — Razorpay Checkout Exclusivity, Lock Unification & State Safety.

Covers all Stage 2A Task 4 requirements (pure offline deterministic tests, zero live API calls):
1. Two simultaneous Razorpay checkout requests for the same organization cannot stage two active attempts.
2. Different organizations can stage checkouts concurrently (independent advisory lock keys).
3. Canonical Level 0A organization advisory lock is acquired strictly BEFORE row-level checkout locks,
   with zero DB transactions or locks held across external Razorpay HTTP calls.
4. Conflicting active legacy Stripe attempt blocks Razorpay checkout (HTTP 409 semantics) without
   mutating stripe_checkout_attempts.
5. Conflicting active Razorpay attempt blocks reachable legacy Stripe checkout (HTTP 409 semantics),
   acquiring the same canonical Level 0A organization advisory lock before row locks.
6. Unresolved legacy Stripe environment provenance (stripe_session_id IS NULL or unrecognized prefix)
   fails closed (CheckoutRecoveryRequiredError) rather than assuming 'legacy' means 'live'.
7. _record_attempt_success() cannot regress payment_pending to open (safely binds missing sub ID;
   stages PaymentReconciliation and raises conflict on mismatched sub ID).
8. _record_attempt_success() cannot regress complete to open.
9. _record_attempt_success() cannot resurrect expired or failed attempts (preserves terminal state
   and stages PaymentReconciliation).
10. Valid creating checkout success still transitions to open.
11. Valid recovery_required checkout success still transitions to open when no conflicting state exists.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid

from pydantic import SecretStr
import pytest

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401

from app.config import settings
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    CheckoutAttemptStatus,
    ReconciliationStatus,
    StripeCheckoutAttempt,
    Subscription,
)
from app.services.billing.advisory_lock import derive_advisory_lock_key
from app.services.billing.razorpay.checkout import (
    CheckoutInProgressError,
    CheckoutRecoveryRequiredError,
    RazorpayCheckoutConflictError,
    _record_attempt_success,
    create_razorpay_subscription_checkout,
)
from app.services.billing.stripe_checkout import (
    CheckoutInProgressError as StripeCheckoutInProgressError,
    CheckoutRecoveryRequiredError as StripeCheckoutRecoveryRequiredError,
    create_stripe_checkout_session,
)

PROD_MERCHANT_ID = "acc_LIVE_STAGE2A_001"
TEST_MERCHANT_ID = "acc_TEST_STAGE2A_001"


# =============================================================================
# Lock-Aware Transactional Test Store (Honors pg_advisory_xact_lock per key)
# =============================================================================


class _MockResult:
    def __init__(self, item: Any = None, items: list[Any] | None = None) -> None:
        self._item = item
        self._items = items if items is not None else ([item] if item is not None else [])

    def scalar_one_or_none(self) -> Any:
        return self._item

    def scalar(self) -> Any:
        return self._item

    def scalars(self) -> Any:
        items = list(self._items)

        class _Scalars:
            def all(self) -> list[Any]:
                return items

            def first(self) -> Any:
                return items[0] if items else None

        return _Scalars()


class SharedCheckoutStore:
    """In-memory multi-session store modeling PostgreSQL transaction-scoped advisory locks."""

    def __init__(self) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {}
        self.provisions: list[BillingCustomerProvision] = []
        self.subscriptions: list[Subscription] = []
        self.rzp_attempts: list[BillingCheckoutAttempt] = []
        self.stripe_attempts: list[StripeCheckoutAttempt] = []
        self.reconciliations: list[PaymentReconciliation] = []
        self._advisory_locks: dict[int, asyncio.Lock] = {}

    def get_advisory_lock(self, key: int) -> asyncio.Lock:
        if key not in self._advisory_locks:
            self._advisory_locks[key] = asyncio.Lock()
        return self._advisory_locks[key]


class LockAwareAsyncSession:
    """AsyncSession test double that enforces real asyncio.Lock serialization on pg_advisory_xact_lock."""

    def __init__(
        self,
        store: SharedCheckoutStore,
        *,
        on_after_org_lock: Any = None,
    ) -> None:
        self.store = store
        self._on_after_org_lock = on_after_org_lock
        self._in_tx = False
        self._held_keys: list[int] = []
        self._pending_adds: list[Any] = []
        self.ops: list[tuple[str, Any]] = []
        self.stripe_attempts_mutated = False

    def in_transaction(self) -> bool:
        return self._in_tx

    def _release_locks(self) -> None:
        for key in reversed(self._held_keys):
            lock = self.store.get_advisory_lock(key)
            if lock.locked():
                lock.release()
        self._held_keys.clear()

    def add(self, obj: Any) -> None:
        self._in_tx = True
        self._pending_adds.append(obj)

    async def flush(self) -> None:
        for obj in self._pending_adds:
            if isinstance(obj, BillingCheckoutAttempt) and obj not in self.store.rzp_attempts:
                self.store.rzp_attempts.append(obj)
            elif isinstance(obj, StripeCheckoutAttempt) and obj not in self.store.stripe_attempts:
                self.store.stripe_attempts.append(obj)
            elif isinstance(obj, PaymentReconciliation) and obj not in self.store.reconciliations:
                self.store.reconciliations.append(obj)
        self._pending_adds.clear()

    async def commit(self) -> None:
        await self.flush()
        self._in_tx = False
        self._release_locks()

    async def rollback(self) -> None:
        self._pending_adds.clear()
        self._in_tx = False
        self._release_locks()

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any, params: Any = None) -> _MockResult:
        self._in_tx = True
        stmt_str = str(stmt)

        # 1. Transaction-scoped PostgreSQL advisory lock
        if "pg_advisory_xact_lock" in stmt_str:
            key = int(params["key"])
            self.ops.append(("advisory_lock", key))
            if key not in self._held_keys:
                await self.store.get_advisory_lock(key).acquire()
                self._held_keys.append(key)
            return _MockResult(None)

        # Extract equality / IN where predicates
        w_params: dict[str, Any] = {}
        for clause in getattr(stmt, "_where_criteria", ()):
            left = getattr(clause, "left", None)
            right = getattr(clause, "right", None)
            key_name = getattr(left, "key", None) or getattr(left, "name", None)
            if key_name and hasattr(right, "value"):
                w_params[key_name] = right.value

        from_table = ""
        froms_list = (
            stmt.get_final_froms()
            if hasattr(stmt, "get_final_froms")
            else getattr(stmt, "froms", ())
        )
        for frm in froms_list:
            tname = getattr(frm, "name", None) or getattr(frm, "__tablename__", "")
            if tname:
                from_table = tname
                break

        for_update = getattr(stmt, "_for_update_arg", None) is not None
        self.ops.append((f"select:{from_table}:for_update={for_update}", dict(w_params)))

        if from_table == "organizations":
            org_id = w_params.get("id")
            org = self.store.organizations.get(org_id)
            if self._on_after_org_lock is not None:
                await self._on_after_org_lock(org_id)
            return _MockResult(org)

        if from_table == "billing_customer_provisions":
            for prov in self.store.provisions:
                if w_params.get("organization_id") and prov.organization_id != w_params["organization_id"]:
                    continue
                if w_params.get("provider") and prov.provider != w_params["provider"]:
                    continue
                if w_params.get("merchant_account_id") and prov.merchant_account_id != w_params["merchant_account_id"]:
                    continue
                if w_params.get("environment") and prov.environment != w_params["environment"]:
                    continue
                return _MockResult(prov)
            return _MockResult(None)

        if from_table == "subscriptions":
            matched_subs = []
            statuses = w_params.get("status")
            for sub in self.store.subscriptions:
                if w_params.get("organization_id") and sub.organization_id != w_params["organization_id"]:
                    continue
                if statuses is not None:
                    allowed = statuses if isinstance(statuses, (list, tuple, set)) else [statuses]
                    if sub.status not in allowed:
                        continue
                if w_params.get("plan_tier") and sub.plan_tier != w_params["plan_tier"]:
                    continue
                matched_subs.append(sub)
            return _MockResult(matched_subs[0] if matched_subs else None, items=matched_subs)

        if from_table == "stripe_checkout_attempts":
            matched_sa = []
            statuses = w_params.get("status")
            for att in self.store.stripe_attempts:
                if w_params.get("organization_id") and att.organization_id != w_params["organization_id"]:
                    continue
                if statuses is not None:
                    allowed = statuses if isinstance(statuses, (list, tuple, set)) else [statuses]
                    if att.status not in allowed:
                        continue
                matched_sa.append(att)
            return _MockResult(matched_sa[0] if matched_sa else None, items=matched_sa)

        if from_table == "billing_checkout_attempts":
            matched_ra = []
            statuses = w_params.get("status")
            for att in reversed(self.store.rzp_attempts):
                if w_params.get("id") and att.id != w_params["id"]:
                    continue
                if w_params.get("organization_id") and att.organization_id != w_params["organization_id"]:
                    continue
                if w_params.get("provider") and att.provider != w_params["provider"]:
                    continue
                if w_params.get("merchant_account_id") and att.merchant_account_id != w_params["merchant_account_id"]:
                    continue
                if w_params.get("environment") and att.environment != w_params["environment"]:
                    continue
                if statuses is not None:
                    allowed = statuses if isinstance(statuses, (list, tuple, set)) else [statuses]
                    if att.status not in allowed:
                        continue
                matched_ra.append(att)
            return _MockResult(matched_ra[0] if matched_ra else None, items=matched_ra)

        if from_table == "payment_reconciliations":
            for rec in self.store.reconciliations:
                if w_params.get("case_key") and rec.case_key != w_params["case_key"]:
                    continue
                if w_params.get("environment") and rec.environment != w_params["environment"]:
                    continue
                return _MockResult(rec)
            return _MockResult(None)

        return _MockResult(None)


def _seed_org_and_provision(
    store: SharedCheckoutStore,
    *,
    environment: str = "test",
    merchant_id: str = TEST_MERCHANT_ID,
) -> tuple[Organization, str]:
    org = Organization(id=uuid.uuid4(), name=f"Org-{uuid.uuid4().hex[:6]}")
    cust_id = f"cust_{uuid.uuid4().hex[:10]}"
    store.organizations[org.id] = org
    store.provisions.append(
        BillingCustomerProvision(
            id=uuid.uuid4(),
            organization_id=org.id,
            provider="razorpay",
            merchant_account_id=merchant_id,
            environment=environment,
            provider_customer_id=cust_id,
            status=CustomerProvisionStatus.resolved,
        )
    )
    return org, cust_id


def _make_fake_rzp_client(
    *,
    session_to_assert_unlocked: LockAwareAsyncSession | None = None,
    sub_prefix: str = "sub_rzp",
) -> Any:
    counter = {"n": 0}

    class _FakeClient:
        async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
            if session_to_assert_unlocked is not None:
                assert session_to_assert_unlocked.in_transaction() is False
                assert session_to_assert_unlocked._held_keys == []
            counter["n"] += 1
            await asyncio.sleep(0.02)
            sid = f"{sub_prefix}_{counter['n']}_{uuid.uuid4().hex[:8]}"
            return {
                "id": sid,
                "short_url": f"https://rzp.io/i/{sid}",
                "expire_by": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
            }

    return _FakeClient(), counter


# =============================================================================
# 1 & 2. Simultaneous Same-Org Serialization vs. Different-Org Concurrency
# =============================================================================


@pytest.mark.asyncio
async def test_01_two_simultaneous_razorpay_checkouts_same_org_cannot_stage_two_attempts() -> None:
    """1. Two concurrent Razorpay checkouts for the same org serialize via Level 0A advisory lock."""
    store = SharedCheckoutStore()
    org, cust_id = _seed_org_and_provision(store, environment="test", merchant_id=TEST_MERCHANT_ID)

    # First session pauses briefly inside Phase B (holding Level 0A org advisory lock) so
    # second session genuinely contends on the same advisory lock key.
    async def _slow_phase_b(_org_id: Any) -> None:
        await asyncio.sleep(0.03)

    db1 = LockAwareAsyncSession(store, on_after_org_lock=_slow_phase_b)
    db2 = LockAwareAsyncSession(store)
    fake_client, call_counter = _make_fake_rzp_client()

    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "razorpay_enabled", True),
        patch.object(settings, "razorpay_environment", "test"),
        patch.object(settings, "razorpay_merchant_account_id", TEST_MERCHANT_ID),
        patch.object(settings, "razorpay_key_id", "rzp_test_stage2a_key"),
        patch.object(settings, "razorpay_key_secret", SecretStr("sec_test_stage2a")),
        patch.object(settings, "razorpay_pro_plan_id", "plan_stage2a_001"),
        patch.object(settings, "razorpay_plan_currency", "USD"),
        patch(
            "app.services.billing.razorpay.checkout.get_or_create_razorpay_customer",
            new=AsyncMock(return_value=cust_id),
        ),
        patch(
            "app.services.billing.razorpay.checkout.get_razorpay_client",
            return_value=fake_client,
        ),
    ):
        results = await asyncio.gather(
            create_razorpay_subscription_checkout(db=db1, organization=org),  # type: ignore[arg-type]
            create_razorpay_subscription_checkout(db=db2, organization=org),  # type: ignore[arg-type]
            return_exceptions=True,
        )

    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, CheckoutInProgressError)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(store.rzp_attempts) == 1
    assert store.rzp_attempts[0].status == CheckoutAttemptStatus.open
    assert call_counter["n"] == 1


@pytest.mark.asyncio
async def test_02_different_organizations_stage_checkouts_concurrently() -> None:
    """2. Distinct organizations acquire independent advisory lock keys and proceed concurrently."""
    store = SharedCheckoutStore()
    org_a, cust_a = _seed_org_and_provision(store, environment="test", merchant_id=TEST_MERCHANT_ID)
    org_b, cust_b = _seed_org_and_provision(store, environment="test", merchant_id=TEST_MERCHANT_ID)

    # Barrier proves Org A and Org B overlap inside Phase B without blocking each other
    entered: list[uuid.UUID] = []
    both_entered = asyncio.Event()

    async def _barrier(org_id: uuid.UUID) -> None:
        entered.append(org_id)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2.0)

    db_a = LockAwareAsyncSession(store, on_after_org_lock=_barrier)
    db_b = LockAwareAsyncSession(store, on_after_org_lock=_barrier)
    fake_client, call_counter = _make_fake_rzp_client()

    async def _resolve_cust(**kwargs: Any) -> str:
        return cust_a if kwargs["organization_id"] == org_a.id else cust_b

    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "razorpay_enabled", True),
        patch.object(settings, "razorpay_environment", "test"),
        patch.object(settings, "razorpay_merchant_account_id", TEST_MERCHANT_ID),
        patch.object(settings, "razorpay_key_id", "rzp_test_stage2a_key"),
        patch.object(settings, "razorpay_key_secret", SecretStr("sec_test_stage2a")),
        patch.object(settings, "razorpay_pro_plan_id", "plan_stage2a_001"),
        patch.object(settings, "razorpay_plan_currency", "USD"),
        patch(
            "app.services.billing.razorpay.checkout.get_or_create_razorpay_customer",
            new=AsyncMock(side_effect=_resolve_cust),
        ),
        patch(
            "app.services.billing.razorpay.checkout.get_razorpay_client",
            return_value=fake_client,
        ),
    ):
        res_a, res_b = await asyncio.gather(
            create_razorpay_subscription_checkout(db=db_a, organization=org_a),  # type: ignore[arg-type]
            create_razorpay_subscription_checkout(db=db_b, organization=org_b),  # type: ignore[arg-type]
        )

    assert res_a.organization_id == org_a.id
    assert res_b.organization_id == org_b.id
    assert res_a.status == CheckoutAttemptStatus.open
    assert res_b.status == CheckoutAttemptStatus.open
    assert call_counter["n"] == 2
    assert len(store.rzp_attempts) == 2


# =============================================================================
# 3. Canonical Level 0A Lock Acquired Before Row Locks & Released Before HTTP
# =============================================================================


@pytest.mark.asyncio
async def test_03_org_advisory_lock_acquired_before_row_locks_and_released_before_http() -> None:
    """3. Level 0A canonical organization advisory lock precedes all row locks and is released before HTTP."""
    store = SharedCheckoutStore()
    org, cust_id = _seed_org_and_provision(store, environment="live", merchant_id=PROD_MERCHANT_ID)
    db = LockAwareAsyncSession(store)
    fake_client, _ = _make_fake_rzp_client(session_to_assert_unlocked=db)
    expected_key = derive_advisory_lock_key("organization", org.id)

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_enabled", True),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
        patch.object(settings, "razorpay_key_id", "rzp_live_stage2a_key"),
        patch.object(settings, "razorpay_key_secret", SecretStr("sec_live_stage2a")),
        patch.object(settings, "razorpay_pro_plan_id", "plan_live_stage2a"),
        patch.object(settings, "razorpay_plan_currency", "USD"),
        patch(
            "app.services.billing.razorpay.checkout.get_or_create_razorpay_customer",
            new=AsyncMock(return_value=cust_id),
        ),
        patch(
            "app.services.billing.razorpay.checkout.get_razorpay_client",
            return_value=fake_client,
        ),
    ):
        result = await create_razorpay_subscription_checkout(db=db, organization=org)  # type: ignore[arg-type]

    assert result.status == CheckoutAttemptStatus.open
    assert db.ops[0] == ("advisory_lock", expected_key)
    assert db.ops[1][0] == "select:organizations:for_update=True"
    assert db.ops[2][0] == "select:billing_customer_provisions:for_update=True"
    assert db.ops[3][0] == "select:subscriptions:for_update=True"
    assert db.ops[4][0] == "select:stripe_checkout_attempts:for_update=False"
    assert db.ops[5][0] == "select:billing_checkout_attempts:for_update=True"
    assert db._held_keys == []


# =============================================================================
# 4, 5, 6. Cross-Table Legacy Stripe <-> Razorpay Compatibility & Unresolved Provenance
# =============================================================================


@pytest.mark.asyncio
async def test_04_active_legacy_stripe_attempt_blocks_razorpay_checkout_read_only() -> None:
    """4. Conflicting active legacy Stripe attempt blocks Razorpay checkout without mutating Stripe rows."""
    now = datetime.now(timezone.utc)
    store = SharedCheckoutStore()
    org, cust_id = _seed_org_and_provision(store, environment="live", merchant_id=PROD_MERCHANT_ID)

    legacy_live_attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_live_legacy_001",
        stripe_session_id="cs_live_active_999",
        request_payload={"mode": "subscription"},
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=now + timedelta(minutes=5),
        session_expires_at=now + timedelta(hours=1),
        created_at=now,
    )
    store.stripe_attempts.append(legacy_live_attempt)

    db = LockAwareAsyncSession(store)
    fake_client, call_counter = _make_fake_rzp_client()

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_enabled", True),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
        patch.object(settings, "razorpay_key_id", "rzp_live_stage2a_key"),
        patch.object(settings, "razorpay_key_secret", SecretStr("sec_live_stage2a")),
        patch.object(settings, "razorpay_pro_plan_id", "plan_live_stage2a"),
        patch.object(settings, "razorpay_plan_currency", "USD"),
        patch(
            "app.services.billing.razorpay.checkout.get_or_create_razorpay_customer",
            new=AsyncMock(return_value=cust_id),
        ),
        patch(
            "app.services.billing.razorpay.checkout.get_razorpay_client",
            return_value=fake_client,
        ),
    ):
        with pytest.raises(CheckoutInProgressError, match="legacy Stripe checkout attempt"):
            await create_razorpay_subscription_checkout(db=db, organization=org)  # type: ignore[arg-type]

    assert call_counter["n"] == 0
    assert len(store.rzp_attempts) == 0
    # Legacy Stripe attempt remained untouched (read-only)
    assert legacy_live_attempt.status == CheckoutAttemptStatus.open
    assert legacy_live_attempt.stripe_session_id == "cs_live_active_999"


@pytest.mark.asyncio
async def test_05_active_razorpay_attempt_blocks_reachable_legacy_stripe_checkout() -> None:
    """5. Active Razorpay attempt in matching environment blocks legacy Stripe checkout under Level 0A lock."""
    now = datetime.now(timezone.utc)
    store = SharedCheckoutStore()
    org, cust_id = _seed_org_and_provision(store, environment="test", merchant_id=TEST_MERCHANT_ID)

    active_rzp = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        provider="razorpay",
        merchant_account_id=TEST_MERCHANT_ID,
        environment="test",
        provider_customer_id=cust_id,
        provider_subscription_id="sub_rzp_active_001",
        idempotency_key="idem_rzp_001",
        status=CheckoutAttemptStatus.open,
        amount_minor_units=2900,
        currency="USD",
        attempt_lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    store.rzp_attempts.append(active_rzp)

    db = LockAwareAsyncSession(store)
    expected_key = derive_advisory_lock_key("organization", org.id)

    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "stripe_enabled", True),
        patch.object(settings, "stripe_pro_price_id", "price_test_12345678"),
        patch.object(settings, "stripe_billing_return_url", "http://localhost:3000/billing"),
        patch(
            "app.services.billing.stripe_checkout.is_stripe_enabled",
            return_value=True,
        ),
        patch(
            "app.services.billing.stripe_checkout.get_or_create_stripe_customer",
            new=AsyncMock(return_value="cus_stripe_001"),
        ),
    ):
        with pytest.raises(StripeCheckoutInProgressError, match="Razorpay checkout attempt"):
            await create_stripe_checkout_session(db=db, organization=org)  # type: ignore[arg-type]

    assert db.ops[0] == ("advisory_lock", expected_key)
    assert db.ops[1][0] == "select:organizations:for_update=True"
    assert len(store.stripe_attempts) == 0

    # Recovery-required Razorpay attempt raises StripeCheckoutRecoveryRequiredError (HTTP 409)
    active_rzp.status = CheckoutAttemptStatus.recovery_required
    db2 = LockAwareAsyncSession(store)
    with (
        patch.object(settings, "environment", "development"),
        patch.object(settings, "stripe_enabled", True),
        patch.object(settings, "stripe_pro_price_id", "price_test_12345678"),
        patch.object(settings, "stripe_billing_return_url", "http://localhost:3000/billing"),
        patch(
            "app.services.billing.stripe_checkout.is_stripe_enabled",
            return_value=True,
        ),
        patch(
            "app.services.billing.stripe_checkout.get_or_create_stripe_customer",
            new=AsyncMock(return_value="cus_stripe_001"),
        ),
    ):
        with pytest.raises(StripeCheckoutRecoveryRequiredError, match="Razorpay checkout attempt"):
            await create_stripe_checkout_session(db=db2, organization=org)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_session_id", [None, "", "legacy_unknown_session_123"])
async def test_06_unresolved_legacy_stripe_provenance_fails_closed(
    ambiguous_session_id: str | None,
) -> None:
    """6. Active legacy Stripe attempt with NULL or unrecognized session ID fails closed in production."""
    now = datetime.now(timezone.utc)
    store = SharedCheckoutStore()
    org, cust_id = _seed_org_and_provision(store, environment="live", merchant_id=PROD_MERCHANT_ID)

    store.stripe_attempts.append(
        StripeCheckoutAttempt(
            id=uuid.uuid4(),
            organization_id=org.id,
            stripe_customer_id="cus_legacy_ambig_01",
            stripe_session_id=ambiguous_session_id,
            request_payload={"mode": "subscription"},
            status=CheckoutAttemptStatus.creating,
            attempt_lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
    )

    db = LockAwareAsyncSession(store)
    fake_client, call_counter = _make_fake_rzp_client()

    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "razorpay_enabled", True),
        patch.object(settings, "razorpay_environment", "live"),
        patch.object(settings, "razorpay_merchant_account_id", PROD_MERCHANT_ID),
        patch.object(settings, "razorpay_key_id", "rzp_live_stage2a_key"),
        patch.object(settings, "razorpay_key_secret", SecretStr("sec_live_stage2a")),
        patch.object(settings, "razorpay_pro_plan_id", "plan_live_stage2a"),
        patch.object(settings, "razorpay_plan_currency", "USD"),
        patch(
            "app.services.billing.razorpay.checkout.get_or_create_razorpay_customer",
            new=AsyncMock(return_value=cust_id),
        ),
        patch(
            "app.services.billing.razorpay.checkout.get_razorpay_client",
            return_value=fake_client,
        ),
    ):
        with pytest.raises(CheckoutRecoveryRequiredError, match="unresolved environment provenance"):
            await create_razorpay_subscription_checkout(db=db, organization=org)  # type: ignore[arg-type]

    assert call_counter["n"] == 0
    assert len(store.rzp_attempts) == 0


# =============================================================================
# 7, 8, 9, 10, 11. _record_attempt_success() State-Regression & Terminal Guards
# =============================================================================


def _make_attempt(
    *,
    org_id: uuid.UUID,
    status: CheckoutAttemptStatus,
    provider_subscription_id: str | None = None,
    environment: str = "live",
) -> BillingCheckoutAttempt:
    now = datetime.now(timezone.utc)
    return BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=PROD_MERCHANT_ID if environment == "live" else TEST_MERCHANT_ID,
        environment=environment,
        provider_customer_id="cust_state_guard_01",
        provider_subscription_id=provider_subscription_id,
        idempotency_key=f"idem_{uuid.uuid4().hex[:10]}",
        status=status,
        amount_minor_units=2900,
        currency="USD",
        attempt_lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
    )


@pytest.mark.asyncio
async def test_07_record_attempt_success_cannot_regress_payment_pending() -> None:
    """7. _record_attempt_success() preserves payment_pending (binds missing sub ID; rejects conflict)."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    store = SharedCheckoutStore()
    att = _make_attempt(
        org_id=org_id,
        status=CheckoutAttemptStatus.payment_pending,
        provider_subscription_id=None,
    )
    store.rzp_attempts.append(att)
    db = LockAwareAsyncSession(store)

    persisted = await _record_attempt_success(
        db=db,  # type: ignore[arg-type]
        attempt_id=att.id,
        subscription_id="sub_pending_001",
        checkout_url="https://rzp.io/i/sub_pending_001",
        session_expires_at=now + timedelta(days=1),
    )
    assert persisted == CheckoutAttemptStatus.payment_pending
    assert att.status == CheckoutAttemptStatus.payment_pending
    assert att.provider_subscription_id == "sub_pending_001"
    assert len(store.reconciliations) == 0

    # Conflicting subscription ID on payment_pending must NOT overwrite and must stage reconciliation
    with pytest.raises(RazorpayCheckoutConflictError, match="already bound to a different subscription"):
        await _record_attempt_success(
            db=db,  # type: ignore[arg-type]
            attempt_id=att.id,
            subscription_id="sub_DIFFERENT_999",
            checkout_url="https://rzp.io/i/sub_DIFFERENT_999",
            session_expires_at=now + timedelta(days=1),
        )
    assert att.status == CheckoutAttemptStatus.payment_pending
    assert att.provider_subscription_id == "sub_pending_001"
    assert len(store.reconciliations) == 1
    assert store.reconciliations[0].case_key == f"checkout_success_sub_conflict:{att.id}"
    assert store.reconciliations[0].status == ReconciliationStatus.pending_review


@pytest.mark.asyncio
async def test_08_record_attempt_success_cannot_regress_complete() -> None:
    """8. _record_attempt_success() preserves complete and refuses conflicting subscription overwrite."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    store = SharedCheckoutStore()
    att = _make_attempt(
        org_id=org_id,
        status=CheckoutAttemptStatus.complete,
        provider_subscription_id="sub_complete_001",
    )
    store.rzp_attempts.append(att)
    db = LockAwareAsyncSession(store)

    persisted = await _record_attempt_success(
        db=db,  # type: ignore[arg-type]
        attempt_id=att.id,
        subscription_id="sub_complete_001",
        checkout_url="https://rzp.io/i/sub_complete_001",
        session_expires_at=now + timedelta(days=1),
    )
    assert persisted == CheckoutAttemptStatus.complete
    assert att.status == CheckoutAttemptStatus.complete

    with pytest.raises(RazorpayCheckoutConflictError):
        await _record_attempt_success(
            db=db,  # type: ignore[arg-type]
            attempt_id=att.id,
            subscription_id="sub_complete_OTHER",
            checkout_url="https://rzp.io/i/sub_complete_OTHER",
            session_expires_at=now + timedelta(days=1),
        )
    assert att.status == CheckoutAttemptStatus.complete
    assert att.provider_subscription_id == "sub_complete_001"
    assert len(store.reconciliations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [CheckoutAttemptStatus.expired, CheckoutAttemptStatus.failed],
)
async def test_09_record_attempt_success_cannot_resurrect_expired_or_failed(
    terminal_status: CheckoutAttemptStatus,
) -> None:
    """9. _record_attempt_success() never resurrects expired or failed attempts and stages reconciliation."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    store = SharedCheckoutStore()
    att = _make_attempt(
        org_id=org_id,
        status=terminal_status,
        provider_subscription_id=None,
    )
    store.rzp_attempts.append(att)
    db = LockAwareAsyncSession(store)

    with pytest.raises(RazorpayCheckoutConflictError, match="terminal state"):
        await _record_attempt_success(
            db=db,  # type: ignore[arg-type]
            attempt_id=att.id,
            subscription_id="sub_late_remote_001",
            checkout_url="https://rzp.io/i/sub_late_remote_001",
            session_expires_at=now + timedelta(days=1),
        )

    assert att.status == terminal_status
    assert att.provider_subscription_id is None
    assert len(store.reconciliations) == 1
    assert store.reconciliations[0].case_key == f"checkout_success_on_terminal_attempt:{att.id}"
    assert store.reconciliations[0].reason == "checkout_success_on_terminal_attempt"
    assert store.reconciliations[0].status == ReconciliationStatus.pending_review


@pytest.mark.asyncio
async def test_10_record_attempt_success_transitions_creating_to_open() -> None:
    """10. Valid creating attempt transitions cleanly to open."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    store = SharedCheckoutStore()
    att = _make_attempt(
        org_id=org_id,
        status=CheckoutAttemptStatus.creating,
        provider_subscription_id=None,
    )
    store.rzp_attempts.append(att)
    db = LockAwareAsyncSession(store)

    persisted = await _record_attempt_success(
        db=db,  # type: ignore[arg-type]
        attempt_id=att.id,
        subscription_id="sub_creating_ok_001",
        checkout_url="https://rzp.io/i/sub_creating_ok_001",
        session_expires_at=now + timedelta(days=1),
    )

    assert persisted == CheckoutAttemptStatus.open
    assert att.status == CheckoutAttemptStatus.open
    assert att.provider_subscription_id == "sub_creating_ok_001"
    assert att.checkout_url == "https://rzp.io/i/sub_creating_ok_001"
    assert len(store.reconciliations) == 0


@pytest.mark.asyncio
async def test_11_record_attempt_success_transitions_recovery_required_to_open() -> None:
    """11. Valid recovery_required attempt transitions cleanly to open when no conflict exists."""
    now = datetime.now(timezone.utc)
    org_id = uuid.uuid4()
    store = SharedCheckoutStore()
    att = _make_attempt(
        org_id=org_id,
        status=CheckoutAttemptStatus.recovery_required,
        provider_subscription_id=None,
    )
    att.error_message = "post_dispatch_network_timeout"
    store.rzp_attempts.append(att)
    db = LockAwareAsyncSession(store)

    persisted = await _record_attempt_success(
        db=db,  # type: ignore[arg-type]
        attempt_id=att.id,
        subscription_id="sub_recovered_ok_001",
        checkout_url="https://rzp.io/i/sub_recovered_ok_001",
        session_expires_at=now + timedelta(days=1),
    )

    assert persisted == CheckoutAttemptStatus.open
    assert att.status == CheckoutAttemptStatus.open
    assert att.provider_subscription_id == "sub_recovered_ok_001"
    assert att.error_message is None
    assert len(store.reconciliations) == 0
