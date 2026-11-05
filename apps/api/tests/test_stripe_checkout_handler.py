"""Comprehensive Unit and Integration Tests for Stripe Checkout Webhook Handler.

Event: checkout.session.completed

Validates:
1. Strict Anti-Tampering: Rejection of mode mismatches, incomplete sessions, missing IDs,
   unknown organizations, missing/mismatched attempts, customer mismatches, and unexpected prices.
2. Verified Entitlement: Grants Pro tier ONLY upon verified 'paid' or authorized 'trialing' state.
   Payment required/unpaid leaves subscription incomplete on free tier. Unauthorized zero-payment sessions are rejected.
3. Realistic Stripe Payloads: String subscription IDs, unexpanded line items, no fabricated period dates.
4. Fail-Closed Validation: Attempt line items and configured Pro price must be valid and matching.
5. Lifecycle & Stale Event Protection: Delayed events cannot resurrect canceled/past-due subscriptions or overwrite newer states.
6. Legitimate Resubscriptions: Correctly handles new subscriptions after prior cancellations.
7. Engine Savepoint & Reconciliation Persistence: Verifies that BillingReconciliation records
   survive engine savepoint rollback and are committed atomically with quarantined events.
"""

from __future__ import annotations

import sys

# Windows asyncio ProactorEventLoop fix
if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self: Any) -> None:
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from datetime import datetime, timezone
from typing import Any
import unittest.mock as mock
from unittest.mock import patch
import uuid

from pydantic import SecretStr
import pytest

import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.subscription  # noqa: F401
from app.config import Settings
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
    ReconciliationStatus,
    StripeCheckoutAttempt,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.stripe_checkout_handler import handle_checkout_session_completed
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
    process_webhook_event,
)

TEST_PRO_PRICE_ID = "price_pro_test_12345"


# =============================================================================
# Mock Database Session for Checkout Handler
# =============================================================================


class HandlerMockSavepoint:
    """Simulates SQLAlchemy async savepoint (begin_nested)."""

    def __init__(self, session: "HandlerMockDbSession") -> None:
        self.session = session
        self.added_len = len(session.added)
        self.rec_len = len(session.reconciliations)
        self.sub_snapshots: dict[uuid.UUID, dict[str, Any]] = {}
        self.attempt_snapshots: dict[uuid.UUID, dict[str, Any]] = {}

    async def __aenter__(self) -> "HandlerMockSavepoint":
        self.sub_snapshots = {
            s.id: {
                "status": s.status,
                "plan_tier": s.plan_tier,
                "stripe_subscription_id": s.stripe_subscription_id,
            }
            for s in self.session.subscriptions.values()
        }
        self.attempt_snapshots = {
            a.id: {
                "status": a.status,
                "stripe_subscription_id": a.stripe_subscription_id,
                "stripe_session_id": a.stripe_session_id,
            }
            for a in self.session.attempts.values()
        }
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            # Revert newly added entities
            del self.session.added[self.added_len:]
            del self.session.reconciliations[self.rec_len:]
            # Revert subscription mutations
            for sid, snap in self.sub_snapshots.items():
                s = self.session.subscriptions.get(sid)
                if s:
                    s.status = snap["status"]
                    s.plan_tier = snap["plan_tier"]
                    s.stripe_subscription_id = snap["stripe_subscription_id"]
            # Revert attempt mutations
            for aid, snap in self.attempt_snapshots.items():
                a = self.session.attempts.get(aid)
                if a:
                    a.status = snap["status"]
                    a.stripe_subscription_id = snap["stripe_subscription_id"]
                    a.stripe_session_id = snap["stripe_session_id"]
            self.session.savepoints_rolled_back += 1
            return False
        self.session.savepoints_committed += 1
        return False


class HandlerMockScalarResult:
    """Mock scalar result wrapper for db.execute()."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def scalar_one_or_none(self) -> Any:
        return self._item

    def scalars(self) -> Any:
        class _Scalars:
            def __init__(self, val: Any) -> None:
                self._val = val
            def all(self) -> list[Any]:
                return [self._val] if self._val is not None else []
        return _Scalars(self._item)


class HandlerMockDbSession:
    """In-memory AsyncSession double tailored for checkout handler reconciliation."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
        events: list[StripeWebhookEvent] | None = None,
    ) -> None:
        self.organizations: dict[uuid.UUID, Organization] = {
            o.id: o for o in (organizations or [])
        }
        self.attempts: dict[uuid.UUID, StripeCheckoutAttempt] = {
            a.id: a for a in (attempts or [])
        }
        self.subscriptions: dict[uuid.UUID, Subscription] = {
            s.id: s for s in (subscriptions or [])
        }
        self.events: dict[str, StripeWebhookEvent] = {
            e.id: e for e in (events or [])
        }
        self.reconciliations: list[BillingReconciliation] = []
        self.added: list[Any] = []
        self.committed_count = 0
        self.rolled_back_count = 0
        self.savepoints_committed = 0
        self.savepoints_rolled_back = 0

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is Organization:
            return self.organizations.get(pk)
        if entity_cls is StripeCheckoutAttempt:
            return self.attempts.get(pk)
        if entity_cls is Subscription:
            return self.subscriptions.get(pk)
        if entity_cls is StripeWebhookEvent:
            return self.events.get(pk)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Subscription):
            self.subscriptions[obj.id] = obj
        elif isinstance(obj, StripeCheckoutAttempt):
            self.attempts[obj.id] = obj
        elif isinstance(obj, StripeWebhookEvent):
            self.events[obj.id] = obj
        elif isinstance(obj, BillingReconciliation):
            self.reconciliations.append(obj)

    def begin_nested(self) -> HandlerMockSavepoint:
        return HandlerMockSavepoint(self)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any) -> HandlerMockScalarResult:
        """Route queries by inspecting target table and where expressions."""
        where_clauses = getattr(stmt, "_where_criteria", ())

        # Extract literal parameters from where clause
        params: dict[str, Any] = {}
        for c in where_clauses:
            if hasattr(c, "left") and hasattr(c, "right"):
                col_name = getattr(c.left, "key", str(c.left))
                val = getattr(c.right, "value", None)
                params[col_name] = val

        # 1. StripeWebhookEvent queries (for engine claim)
        if any("stripe_webhook_events" in str(c) for c in where_clauses) or getattr(stmt, "is_select", False):
            if "id" in params and params["id"] in self.events:
                return HandlerMockScalarResult(self.events[params["id"]])

        # 2. BillingReconciliation lookup
        if any("billing_reconciliations" in str(c) for c in where_clauses) or "billing_reconciliations" in str(stmt):
            ev_id = params.get("event_id")
            inc_id = params.get("incoming_subscription_id")
            match = next(
                (r for r in self.reconciliations if (ev_id is None or r.event_id == ev_id) and (inc_id is None or r.incoming_subscription_id == inc_id)),
                None,
            )
            return HandlerMockScalarResult(match)

        # 3. StripeCheckoutAttempt lookups
        if any("stripe_checkout_attempts" in str(c) for c in where_clauses):
            sess_id = params.get("stripe_session_id")
            att_id = params.get("id")
            if sess_id:
                match = next((a for a in self.attempts.values() if a.stripe_session_id == sess_id), None)
                if match:
                    return HandlerMockScalarResult(match)
            if att_id and att_id in self.attempts:
                return HandlerMockScalarResult(self.attempts[att_id])
            return HandlerMockScalarResult(None)

        # 4. Subscription queries
        if any("subscriptions" in str(c) for c in where_clauses) or "subscriptions" in str(stmt):
            # Check if selecting specifically stripe_customer_id column (get_stripe_customer_id)
            raw_cols = [str(col) for col in getattr(stmt, "_raw_columns", ())]
            if any("stripe_customer_id" in col for col in raw_cols) and not any(isinstance(col, type) for col in getattr(stmt, "_raw_columns", ())):
                org_id = params.get("organization_id")
                if org_id:
                    for s in self.subscriptions.values():
                        if s.organization_id == org_id and s.stripe_customer_id:
                            return HandlerMockScalarResult(s.stripe_customer_id)
                return HandlerMockScalarResult(None)

            # Check for active subscription query
            if "organization_id" in params and any("status" in str(c) for c in where_clauses):
                org_id = params["organization_id"]
                match = next(
                    (s for s in self.subscriptions.values() if s.organization_id == org_id and s.status in (SubscriptionStatus.active, SubscriptionStatus.trialing)),
                    None,
                )
                return HandlerMockScalarResult(match)

            # Query by stripe_subscription_id
            sub_id = params.get("stripe_subscription_id")
            if sub_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
                if match:
                    return HandlerMockScalarResult(match)

            # Query by stripe_customer_id
            cust_id = params.get("stripe_customer_id")
            if cust_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if match:
                    return HandlerMockScalarResult(match)

            # Query by organization_id
            org_id = params.get("organization_id")
            if org_id:
                match = next((s for s in self.subscriptions.values() if s.organization_id == org_id), None)
                if match:
                    return HandlerMockScalarResult(match)

        return HandlerMockScalarResult(None)


def get_test_settings(**kwargs: Any) -> Settings:
    """Generate settings with configured Pro price ID."""
    values: dict[str, Any] = {
        "environment": "development",
        "stripe_enabled": True,
        "stripe_secret_key": SecretStr("sk_test_1234567890"),
        "stripe_webhook_secret": SecretStr("whsec_test_secret_12345"),
        "stripe_publishable_key": "pk_test_1234567890",
        "stripe_pro_price_id": TEST_PRO_PRICE_ID,
    }
    values.update(kwargs)
    return Settings(**values)


def build_checkout_completed_event(
    event_id: str = "evt_checkout_100",
    session_id: str = "cs_test_session_100",
    customer_id: str = "cus_test_100",
    subscription_id: str = "sub_test_100",
    org_id: uuid.UUID | str | None = None,
    attempt_id: uuid.UUID | str | None = None,
    mode: str = "subscription",
    status: str = "complete",
    payment_status: str = "paid",
    include_line_items: bool = False,
    price_id: str = TEST_PRO_PRICE_ID,
    event_created: int = 1730000000,
) -> dict[str, Any]:
    """Helper to construct realistic checkout.session.completed event dictionaries."""
    session_obj: dict[str, Any] = {
        "id": session_id,
        "object": "checkout.session",
        "mode": mode,
        "status": status,
        "payment_status": payment_status,
        "customer": customer_id,
        "subscription": subscription_id,  # Realistic Stripe payload: string subscription ID
        "client_reference_id": str(org_id) if org_id else "",
        "metadata": {
            "organization_id": str(org_id) if org_id else "",
            "attempt_id": str(attempt_id) if attempt_id else "",
        },
    }
    if include_line_items:
        session_obj["line_items"] = {
            "object": "list",
            "data": [
                {
                    "price": {"id": price_id},
                    "quantity": 1,
                }
            ],
        }

    return {
        "id": event_id,
        "object": "event",
        "created": event_created,
        "type": "checkout.session.completed",
        "data": {
            "object": session_obj,
        },
    }


# =============================================================================
# 1. Success & Realistic Entitlement Tests
# =============================================================================


@pytest.mark.asyncio
async def test_valid_checkout_completion_reconciles_correct_organization() -> None:
    """Requirement 1: Valid checkout completion grants Pro tier, links IDs, and marks attempt complete."""
    org = Organization(id=uuid.uuid4(), name="Acme Corp")
    session_id = "cs_test_valid_1"
    customer_id = "cus_valid_1"
    sub_id = "sub_valid_1"

    # Pre-existing customer subscription record with status='incomplete'
    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[existing_sub],
    )

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="paid",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Assert subscription is reconciled to Pro
    assert existing_sub.stripe_subscription_id == sub_id
    assert existing_sub.status == SubscriptionStatus.active
    assert existing_sub.plan_tier == "pro"
    assert existing_sub.last_stripe_event_created_at is not None

    # Assert checkout attempt is completed
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.stripe_subscription_id == sub_id


@pytest.mark.asyncio
async def test_payment_required_session_does_not_incorrectly_grant_pro() -> None:
    """Requirement: An 'unpaid' session leaves subscription as incomplete and plan_tier='free'."""
    org = Organization(id=uuid.uuid4(), name="Beta Corp")
    session_id = "cs_test_unpaid_1"
    customer_id = "cus_unpaid_1"
    sub_id = "sub_unpaid_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="unpaid",  # Delayed / asynchronous payment method
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Locate created subscription
    sub = next(s for s in db.subscriptions.values() if s.organization_id == org.id)
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free"  # Pro tier NOT granted!
    assert attempt.status == CheckoutAttemptStatus.payment_pending


@pytest.mark.asyncio
async def test_no_payment_required_with_authorized_trial_grants_trialing_pro() -> None:
    """Requirement: 'no_payment_required' with server-authorized trial sets status='trialing' and plan_tier='pro'."""
    org = Organization(id=uuid.uuid4(), name="Trial Corp")
    session_id = "cs_test_trial_1"
    customer_id = "cus_trial_1"
    sub_id = "sub_trial_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "subscription_data": {"trial_period_days": 14},
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    sub = next(s for s in db.subscriptions.values() if s.organization_id == org.id)
    assert sub.status == SubscriptionStatus.trialing
    assert sub.plan_tier == "pro"
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_no_payment_required_without_authorized_trial_is_quarantined() -> None:
    """Requirement: 'no_payment_required' without server-authorized trial fails closed with WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Unauthorized Free Corp")
    session_id = "cs_test_unauth_free"
    customer_id = "cus_unauth_1"
    sub_id = "sub_unauth_1"

    # Attempt does NOT have a trial configured
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="does not authorize a valid trial"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_no_payment_required_with_authorized_trial_end_grants_trialing_pro() -> None:
    """Requirement: 'no_payment_required' with valid trial_end grants trialing pro."""
    org = Organization(id=uuid.uuid4(), name="Trial End Corp")
    session_id = "cs_test_trial_end"
    customer_id = "cus_trial_end"
    sub_id = "sub_trial_end"

    future_ts = int(datetime.now(timezone.utc).timestamp()) + 86400 * 14
    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "subscription_data": {"trial_end": future_ts},
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    sub = next(s for s in db.subscriptions.values() if s.organization_id == org.id)
    assert sub.status == SubscriptionStatus.trialing
    assert sub.plan_tier == "pro"
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_no_payment_required_with_empty_discounts_is_quarantined() -> None:
    """Requirement: Empty discounts list in attempt does not authorize no_payment_required."""
    org = Organization(id=uuid.uuid4(), name="Empty Discount Corp")
    session_id = "cs_test_empty_disc"
    customer_id = "cus_empty_disc"
    sub_id = "sub_empty_disc"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "discounts": [],
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="does not authorize a valid trial"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_no_payment_required_with_arbitrary_discounts_without_trial_is_quarantined() -> None:
    """Requirement: Arbitrary non-empty discounts list without authorized trial is rejected."""
    org = Organization(id=uuid.uuid4(), name="Promo Free Corp")
    session_id = "cs_test_promo_free"
    customer_id = "cus_promo_free"
    sub_id = "sub_promo_free"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "discounts": [{"coupon": "promo_100_percent_free"}],
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="does not authorize a valid trial"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_no_payment_required_with_client_metadata_is_quarantined() -> None:
    """Requirement: Client metadata claiming a trial is ignored and rejected."""
    org = Organization(id=uuid.uuid4(), name="Client Metadata Corp")
    session_id = "cs_test_client_meta"
    customer_id = "cus_client_meta"
    sub_id = "sub_client_meta"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "metadata": {"trial_period_days": 14, "is_trial": "true"},
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="does not authorize a valid trial"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trial_val", "expected_err"),
    [
        (0, "Invalid trial_period_days '0' in checkout attempt. Expected positive integer."),
        (-5, "Invalid trial_period_days '-5' in checkout attempt. Expected positive integer."),
        (True, "Invalid trial_period_days 'True' in checkout attempt. Expected positive integer."),
        ("14", "Invalid trial_period_days '14' in checkout attempt. Expected positive integer."),
    ],
)
async def test_no_payment_required_with_invalid_trial_period_days_is_quarantined(
    trial_val: Any,
    expected_err: str,
) -> None:
    """Requirement: Malformed, zero, negative, bool, or non-int trial_period_days are rejected."""
    org = Organization(id=uuid.uuid4(), name="Bad Days Corp")
    session_id = "cs_test_bad_days"
    customer_id = "cus_bad_days"
    sub_id = "sub_bad_days"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "subscription_data": {"trial_period_days": trial_val},
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match=expected_err):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trial_end_val", "expected_err"),
    [
        (1000, "Invalid trial_end '1000' in checkout attempt. Expected future Unix timestamp."),
        (True, "Invalid trial_end 'True' in checkout attempt. Expected future Unix timestamp."),
        ("1893456000", "Invalid trial_end '1893456000' in checkout attempt. Expected future Unix timestamp."),
    ],
)
async def test_no_payment_required_with_invalid_trial_end_is_quarantined(
    trial_end_val: Any,
    expected_err: str,
) -> None:
    """Requirement: Past, boolean, or non-int trial_end values are rejected."""
    org = Organization(id=uuid.uuid4(), name="Bad End Corp")
    session_id = "cs_test_bad_end"
    customer_id = "cus_bad_end"
    sub_id = "sub_bad_end"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={
            "line_items": [{"price": TEST_PRO_PRICE_ID}],
            "subscription_data": {"trial_end": trial_end_val},
        },
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="no_payment_required",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match=expected_err):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_or_invalid_payment_status_is_quarantined() -> None:
    """Requirement: Missing or unexpected payment status fails safely with WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Unknown Status Corp")
    session_id = "cs_test_unknown_status"
    customer_id = "cus_unknown_1"
    sub_id = "sub_unknown_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="unknown_status",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Missing or invalid payment_status"):
            await handle_checkout_session_completed(event_payload, db)


# =============================================================================
# 2. Structural & Field Validation Rejections
# =============================================================================


@pytest.mark.asyncio
async def test_invalid_checkout_mode_is_quarantined() -> None:
    """Requirement: Non-subscription checkout mode raises WebhookPermanentError."""
    db = HandlerMockDbSession()
    event_payload = build_checkout_completed_event(mode="payment")

    with pytest.raises(WebhookPermanentError, match="Invalid checkout session mode 'payment'"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_incomplete_session_is_quarantined() -> None:
    """Requirement: Session status not 'complete' raises WebhookPermanentError."""
    db = HandlerMockDbSession()
    event_payload = build_checkout_completed_event(status="open")

    with pytest.raises(WebhookPermanentError, match="Checkout session status is 'open'"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_customer_id_is_quarantined() -> None:
    """Requirement: Missing customer ID raises WebhookPermanentError."""
    db = HandlerMockDbSession()
    event_payload = build_checkout_completed_event(customer_id="")

    with pytest.raises(WebhookPermanentError, match="Missing or invalid Stripe customer ID"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_subscription_id_is_quarantined() -> None:
    """Requirement: Missing subscription ID raises WebhookPermanentError."""
    db = HandlerMockDbSession()
    payload = build_checkout_completed_event()
    payload["data"]["object"]["subscription"] = None

    with pytest.raises(WebhookPermanentError, match="Missing or invalid Stripe subscription ID"):
        await handle_checkout_session_completed(payload, db)


# =============================================================================
# 3. Organization & Attempt Anti-Tampering & Fail-Closed Price Tests
# =============================================================================


@pytest.mark.asyncio
async def test_unknown_organization_is_quarantined() -> None:
    """Requirement: Referenced organization that does not exist raises WebhookPermanentError."""
    non_existent_org_id = uuid.uuid4()
    db = HandlerMockDbSession(organizations=[])
    event_payload = build_checkout_completed_event(org_id=non_existent_org_id)

    with pytest.raises(WebhookPermanentError, match="Referenced organization .* not found"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_checkout_attempt_is_quarantined() -> None:
    """Requirement: Session with no matching local checkout attempt raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Gamma Corp")
    db = HandlerMockDbSession(organizations=[org], attempts=[])
    event_payload = build_checkout_completed_event(
        session_id="cs_untracked_session",
        org_id=org.id,
    )

    with pytest.raises(WebhookPermanentError, match="No matching local checkout attempt found"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_session_attempt_mismatch_is_quarantined() -> None:
    """Requirement: Attempt belonging to a different organization raises WebhookPermanentError."""
    org_a = Organization(id=uuid.uuid4(), name="Org A")
    org_b = Organization(id=uuid.uuid4(), name="Org B")
    session_id = "cs_mismatch_sess"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_a.id,  # Owned by Org A
        stripe_customer_id="cus_mismatch",
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org_a, org_b], attempts=[attempt])

    # Event falsely claims it belongs to Org B
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id="cus_mismatch",
        org_id=org_b.id,
        attempt_id=attempt.id,
    )

    with pytest.raises(WebhookPermanentError, match="Checkout attempt organization mismatch"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_customer_mismatch_is_quarantined() -> None:
    """Requirement: Customer ID in session differing from attempt customer raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Delta Corp")
    session_id = "cs_cust_mismatch"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id="cus_expected_original",
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id="cus_attacker_forged",
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with pytest.raises(WebhookPermanentError, match="Customer ID mismatch"):
        await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_configured_pro_price_id_is_quarantined() -> None:
    """Requirement: Empty configured Pro price ID fails closed with WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Config Corp")
    session_id = "cs_test_conf"
    customer_id = "cus_conf_1"
    sub_id = "sub_conf_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    mock_settings = get_test_settings()
    object.__setattr__(mock_settings, "stripe_pro_price_id", "")
    with patch("app.services.billing.stripe_checkout_handler.settings", mock_settings):
        with pytest.raises(WebhookPermanentError, match="STRIPE_PRO_PRICE_ID is not configured"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_missing_attempt_line_items_is_quarantined() -> None:
    """Requirement: Attempt missing line_items fails closed with WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="No Items Corp")
    session_id = "cs_test_no_items"
    customer_id = "cus_no_items_1"
    sub_id = "sub_no_items_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": []},  # Empty line items!
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="missing or empty 'line_items'"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_mismatched_attempt_price_is_quarantined() -> None:
    """Requirement: Attempt initialized with unexpected price fails closed with WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Price Mismatch Corp")
    session_id = "cs_test_mismatch_price"
    customer_id = "cus_mismatch_price_1"
    sub_id = "sub_mismatch_price_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": "price_cheap_unauthorized"}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="does not match configured Pro price"):
            await handle_checkout_session_completed(event_payload, db)


@pytest.mark.asyncio
async def test_unexpected_session_line_items_price_is_quarantined() -> None:
    """Requirement: When event payload includes line items, unexpected price raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Epsilon Corp")
    session_id = "cs_price_mismatch"
    customer_id = "cus_price_1"

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(organizations=[org], attempts=[attempt])

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        org_id=org.id,
        attempt_id=attempt.id,
        include_line_items=True,
        price_id="price_unauthorized_9999",  # Injected unexpected price
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Unexpected price ID 'price_unauthorized_9999'"):
            await handle_checkout_session_completed(event_payload, db)


# =============================================================================
# 4. Idempotency, Lifecycle Guards & Resubscription Tests
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent() -> None:
    """Requirement: Replaying completed event returns cleanly without modifying records."""
    org = Organization(id=uuid.uuid4(), name="Zeta Corp")
    session_id = "cs_dup_session"
    customer_id = "cus_dup_1"
    sub_id = "sub_dup_1"

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        stripe_subscription_id=sub_id,
        status=CheckoutAttemptStatus.complete,  # Already complete
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[sub],
    )

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    assert attempt.status == CheckoutAttemptStatus.complete
    assert sub.status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_stale_checkout_event_does_not_overwrite_newer_timestamp() -> None:
    """Requirement: Checkout event older than subscription's last_stripe_event_created_at is safely skipped."""
    org = Organization(id=uuid.uuid4(), name="Stale Corp")
    session_id = "cs_stale_session"
    customer_id = "cus_stale_1"
    sub_id = "sub_stale_1"

    # Subscription already processed a newer event at timestamp 2000000000
    newer_time = datetime.fromtimestamp(2000000000, tz=timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_stripe_event_created_at=newer_time,
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[sub],
    )

    # Event was created at timestamp 1700000000 (much older)
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        event_created=1700000000,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Subscription timestamp and state remain completely untouched
    assert sub.last_stripe_event_created_at == newer_time
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_delayed_checkout_does_not_resurrect_canceled_subscription() -> None:
    """Requirement: Delayed checkout completion event does not resurrect an already-canceled subscription."""
    org = Organization(id=uuid.uuid4(), name="Canceled Corp")
    session_id = "cs_canceled_session"
    customer_id = "cus_canceled_1"
    sub_id = "sub_canceled_1"

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status=SubscriptionStatus.canceled,  # Canceled!
        plan_tier="free",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[sub],
    )

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Subscription stays canceled on free tier!
    assert sub.status == SubscriptionStatus.canceled
    assert sub.plan_tier == "free"
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_delayed_checkout_does_not_resurrect_past_due_subscription() -> None:
    """Requirement: Delayed checkout completion event does not overwrite an existing past_due subscription."""
    org = Organization(id=uuid.uuid4(), name="Past Due Corp")
    session_id = "cs_past_due_session"
    customer_id = "cus_past_due_1"
    sub_id = "sub_past_due_1"

    past_due_time = datetime.now(timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status=SubscriptionStatus.past_due,  # Past due!
        plan_tier="pro",
        past_due_since=past_due_time,
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[sub],
    )

    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Subscription stays past_due!
    assert sub.status == SubscriptionStatus.past_due
    assert sub.past_due_since == past_due_time
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_legitimate_resubscription_after_canceled_subscription() -> None:
    """Requirement: A new checkout attempt after a previously canceled subscription reconciles normally."""
    org = Organization(id=uuid.uuid4(), name="Resubscribe Corp")
    session_id = "cs_resub_session"
    customer_id = "cus_resub_1"
    old_sub_id = "sub_old_canceled_1"
    new_sub_id = "sub_new_active_2"

    # Prior canceled subscription
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=old_sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[sub],
    )

    # Event for the NEW subscription
    event_payload = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=new_sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="paid",
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(event_payload, db)

    # Successfully transitions to new subscription and Pro tier
    assert sub.stripe_subscription_id == new_sub_id
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.stripe_subscription_id == new_sub_id


# =============================================================================
# 5. Engine Integration & Reconciliation Persistence Tests
# =============================================================================


@pytest.mark.asyncio
async def test_reconciliation_conflict_persisted_through_engine() -> None:
    """Requirement: Conflicting active subscription quarantines event AND persists BillingReconciliation record."""
    org = Organization(id=uuid.uuid4(), name="Conflict Corp")
    session_id = "cs_conflict_session"
    customer_id = "cus_conflict_1"
    incoming_sub_id = "sub_incoming_conflict_999"
    existing_active_sub_id = "sub_already_active_111"
    event_id = "evt_engine_conflict_test"

    active_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=existing_active_sub_id,
        status=SubscriptionStatus.active,  # Already active!
        plan_tier="pro",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    event_payload = build_checkout_completed_event(
        event_id=event_id,
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=incoming_sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
    )

    event_record = StripeWebhookEvent(
        id=event_id,
        event_type="checkout.session.completed",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=event_payload,
        created_at=datetime.now(timezone.utc),
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[active_sub],
        events=[event_record],
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        result = await process_webhook_event(
            db=db,
            event_id=event_id,
            dispatcher=default_webhook_dispatcher,
        )

    # 1. Webhook event is quarantined
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert event_record.status == WebhookProcessingStatus.quarantined
    assert event_record.attempts == 1

    # 2. BillingReconciliation survived savepoint rollback and is persisted in outer transaction
    assert len(db.reconciliations) == 1
    rec = db.reconciliations[0]
    assert rec.organization_id == org.id
    assert rec.event_id == event_id
    assert rec.existing_subscription_id == existing_active_sub_id
    assert rec.incoming_subscription_id == incoming_sub_id
    assert rec.reason == "duplicate_active_subscription"
    assert rec.status == ReconciliationStatus.pending_review

    # 3. Existing active subscription is untouched
    assert active_sub.status == SubscriptionStatus.active
    assert active_sub.stripe_subscription_id == existing_active_sub_id


@pytest.mark.asyncio
async def test_failed_reconciliation_rolls_back_all_domain_changes() -> None:
    """Requirement: Processing engine savepoint rolls back domain mutations on failure."""
    org = Organization(id=uuid.uuid4(), name="Rollback Corp")
    session_id = "cs_rollback_session"
    customer_id = "cus_rollback_1"
    sub_id = "sub_rollback_1"

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )

    attempt = StripeCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_session_id=session_id,
        status=CheckoutAttemptStatus.open,
        attempt_lease_expires_at=datetime.now(timezone.utc),
        request_payload={"line_items": [{"price": TEST_PRO_PRICE_ID}]},
    )

    event_record = StripeWebhookEvent(
        id="evt_engine_rollback_test",
        event_type="checkout.session.completed",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=build_checkout_completed_event(
            event_id="evt_engine_rollback_test",
            session_id=session_id,
            customer_id=customer_id,
            subscription_id=sub_id,
            org_id=org.id,
            attempt_id=attempt.id,
            status="open",  # Intentional error: status is 'open', should fail and quarantine
        ),
        created_at=datetime.now(timezone.utc),
    )

    db = HandlerMockDbSession(
        organizations=[org],
        attempts=[attempt],
        subscriptions=[existing_sub],
        events=[event_record],
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        result = await process_webhook_event(
            db=db,
            event_id="evt_engine_rollback_test",
            dispatcher=default_webhook_dispatcher,
        )

    # Engine caught the WebhookPermanentError and transitioned event to quarantined
    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert event_record.status == WebhookProcessingStatus.quarantined
    assert db.savepoints_rolled_back == 1

    # Domain entities were rolled back to their original states
    assert existing_sub.status == SubscriptionStatus.incomplete
    assert existing_sub.plan_tier == "free"
    assert existing_sub.stripe_subscription_id is None
    assert attempt.status == CheckoutAttemptStatus.open
