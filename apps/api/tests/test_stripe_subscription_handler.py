"""Comprehensive Unit Tests for Stripe Subscription Created Webhook Handler.

Event: customer.subscription.created

Validates:
1. Valid Creation:
   - Updates placeholder subscription row with authoritative IDs and period timestamps.
   - Active subscription does NOT prematurely grant Pro tier or quota before invoice/checkout payment.
   - Trialing subscription follows safe fail-closed trial policy (preserves Free entitlement).
   - Incomplete subscription preserves Free entitlement.
2. Strict Validation:
   - Missing/invalid subscription ID and customer ID fail closed.
   - Unknown local customer fails closed (metadata alone cannot create bindings).
   - Organization metadata parsing and mismatch checks.
   - Configured Pro Price ID enforcement across arbitrary item positions.
   - Positive timestamp validation and period range check (end > start).
   - Unrecognized subscription statuses.
3. Multi-Subscription Conflict Guard & Reconciliation:
   - Active subscription with different ID raises WebhookReconciliationConflictError.
   - Verified engine-level persistence of BillingReconciliation and quarantine outcome.
   - Legitimate resubscriptions after cancellation update in place without duplicate rows.
4. Monotonic Event Ordering:
   - Handles arrival before and after checkout completion cleanly.
   - Delayed event after newer update does not regress lifecycle state or advance timestamps.
   - Safely backfills missing fields without overwriting non-null values.
5. Strict Accounting Isolation:
   - Does NOT create or mutate OrganizationBillingPeriod rows.
   - Does NOT create StripeInvoice rows.
   - Does NOT modify token or cost counters or in-flight UsageReservations.
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
from app.models.billing import OrganizationBillingPeriod, ReservationStatus, UsageReservation
from app.models.organization import Organization
from app.models.subscription import (
    BillingReconciliation,
    CheckoutAttemptStatus,
    ReconciliationStatus,
    StripeCheckoutAttempt,
    StripeInvoice,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.stripe_checkout_handler import handle_checkout_session_completed
from app.services.billing.stripe_subscription_handler import handle_subscription_created
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
    process_webhook_event,
)

TEST_PRO_PRICE_ID = "price_pro_subscription_test_123"


# =============================================================================
# Test Settings Helper
# =============================================================================


def get_test_settings(pro_price_id: str = TEST_PRO_PRICE_ID) -> Settings:
    """Construct mock Settings with valid Stripe configuration."""
    s = Settings(
        stripe_enabled=True,
        stripe_secret_key=SecretStr("sk_test_mock_1234567890abcdef"),
        stripe_webhook_secret=SecretStr("whsec_test_mock_1234567890abcdef"),
        stripe_publishable_key="pk_test_mock_1234567890abcdef",
        stripe_pro_price_id=TEST_PRO_PRICE_ID,
        stripe_billing_return_url="http://localhost:3000/billing",
        billing_grace_period_days=7,
        default_free_monthly_token_quota=0,
    )
    if pro_price_id != TEST_PRO_PRICE_ID:
        object.__setattr__(s, "stripe_pro_price_id", pro_price_id)
    return s


# =============================================================================
# Mock Database Session for Subscription Handler
# =============================================================================


class SubscriptionMockSavepoint:
    """Simulates SQLAlchemy async savepoint (begin_nested)."""

    def __init__(self, session: "SubscriptionMockDbSession") -> None:
        self.session = session
        self.added_len = len(session.added)
        self.rec_len = len(session.reconciliations)
        self.sub_snapshots: dict[uuid.UUID, dict[str, Any]] = {}
        self.attempt_snapshots: dict[uuid.UUID, dict[str, Any]] = {}

    async def __aenter__(self) -> "SubscriptionMockSavepoint":
        self.sub_snapshots = {
            s.id: {
                "status": s.status,
                "plan_tier": s.plan_tier,
                "stripe_subscription_id": s.stripe_subscription_id,
                "stripe_price_id": s.stripe_price_id,
                "current_period_start": s.current_period_start,
                "current_period_end": s.current_period_end,
                "last_stripe_event_created_at": s.last_stripe_event_created_at,
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
            del self.session.added[self.added_len:]
            del self.session.reconciliations[self.rec_len:]
            for sid, snap in self.sub_snapshots.items():
                s = self.session.subscriptions.get(sid)
                if s:
                    s.status = snap["status"]
                    s.plan_tier = snap["plan_tier"]
                    s.stripe_subscription_id = snap["stripe_subscription_id"]
                    s.stripe_price_id = snap["stripe_price_id"]
                    s.current_period_start = snap["current_period_start"]
                    s.current_period_end = snap["current_period_end"]
                    s.last_stripe_event_created_at = snap["last_stripe_event_created_at"]
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


class SubscriptionMockScalarResult:
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


class SubscriptionMockDbSession:
    """In-memory AsyncSession double tailored for subscription lifecycle tests."""

    def __init__(
        self,
        organizations: list[Organization] | None = None,
        attempts: list[StripeCheckoutAttempt] | None = None,
        subscriptions: list[Subscription] | None = None,
        events: list[StripeWebhookEvent] | None = None,
        billing_periods: list[OrganizationBillingPeriod] | None = None,
        reservations: list[UsageReservation] | None = None,
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
        self.billing_periods: dict[uuid.UUID, OrganizationBillingPeriod] = {
            bp.id: bp for bp in (billing_periods or [])
        }
        self.reservations: dict[uuid.UUID, UsageReservation] = {
            r.id: r for r in (reservations or [])
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
        if entity_cls is OrganizationBillingPeriod:
            return self.billing_periods.get(pk)
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
        elif isinstance(obj, OrganizationBillingPeriod):
            self.billing_periods[obj.id] = obj
        elif isinstance(obj, UsageReservation):
            self.reservations[obj.id] = obj

    def begin_nested(self) -> SubscriptionMockSavepoint:
        return SubscriptionMockSavepoint(self)

    async def commit(self) -> None:
        self.committed_count += 1

    async def rollback(self) -> None:
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any) -> SubscriptionMockScalarResult:
        where_clauses = getattr(stmt, "_where_criteria", ())
        params: dict[str, Any] = {}
        for c in where_clauses:
            if hasattr(c, "left") and hasattr(c, "right"):
                col_name = getattr(c.left, "key", str(c.left))
                val = getattr(c.right, "value", None)
                params[col_name] = val

        # 1. StripeWebhookEvent queries
        if any("stripe_webhook_events" in str(c) for c in where_clauses) or getattr(stmt, "is_select", False):
            if "id" in params and params["id"] in self.events:
                return SubscriptionMockScalarResult(self.events[params["id"]])

        # 2. BillingReconciliation lookup
        if any("billing_reconciliations" in str(c) for c in where_clauses) or "billing_reconciliations" in str(stmt):
            ev_id = params.get("event_id")
            inc_id = params.get("incoming_subscription_id")
            match = next(
                (r for r in self.reconciliations if (ev_id is None or r.event_id == ev_id) and (inc_id is None or r.incoming_subscription_id == inc_id)),
                None,
            )
            return SubscriptionMockScalarResult(match)

        # 3. StripeCheckoutAttempt lookups
        if any("stripe_checkout_attempts" in str(c) for c in where_clauses) or "stripe_checkout_attempts" in str(stmt):
            sess_id = params.get("stripe_session_id")
            if sess_id:
                match = next((a for a in self.attempts.values() if a.stripe_session_id == sess_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)
            att_id = params.get("id")
            if att_id and att_id in self.attempts:
                return SubscriptionMockScalarResult(self.attempts[att_id])

        # 4. Subscription queries
        if any("subscriptions" in str(c) for c in where_clauses) or "subscriptions" in str(stmt):
            # Check if selecting specifically stripe_customer_id column (get_stripe_customer_id)
            raw_cols = [str(col) for col in getattr(stmt, "_raw_columns", ())]
            if any("stripe_customer_id" in col for col in raw_cols) and not any(isinstance(col, type) for col in getattr(stmt, "_raw_columns", ())):
                org_id = params.get("organization_id")
                if org_id:
                    for s in self.subscriptions.values():
                        if s.organization_id == org_id and s.stripe_customer_id:
                            return SubscriptionMockScalarResult(s.stripe_customer_id)
                return SubscriptionMockScalarResult(None)

            # Check for active/trialing subscription query
            if "organization_id" in params and any("status" in str(c) for c in where_clauses):
                org_id = params["organization_id"]
                match = next(
                    (s for s in self.subscriptions.values() if s.organization_id == org_id and s.status in (SubscriptionStatus.active, SubscriptionStatus.trialing)),
                    None,
                )
                return SubscriptionMockScalarResult(match)

            # Query by stripe_subscription_id
            sub_id = params.get("stripe_subscription_id")
            if sub_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_subscription_id == sub_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

            # Query by stripe_customer_id
            cust_id = params.get("stripe_customer_id")
            if cust_id:
                match = next((s for s in self.subscriptions.values() if s.stripe_customer_id == cust_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

            # Query by organization_id
            org_id = params.get("organization_id")
            if org_id:
                match = next((s for s in self.subscriptions.values() if s.organization_id == org_id), None)
                if match:
                    return SubscriptionMockScalarResult(match)

        return SubscriptionMockScalarResult(None)


# =============================================================================
# Payload Builder Fixtures
# =============================================================================


def build_subscription_created_event(
    *,
    event_id: str = "evt_sub_created_1",
    subscription_id: str = "sub_test_12345",
    customer_id: str = "cus_test_12345",
    status: str = "active",
    price_id: str = TEST_PRO_PRICE_ID,
    current_period_start: int = 1730000000,
    current_period_end: int = 1732592000,
    cancel_at_period_end: bool = False,
    canceled_at: int | None = None,
    org_id: uuid.UUID | None = None,
    created: int = 1730000000,
    items_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a realistic Stripe customer.subscription.created webhook event payload."""
    if items_data is None:
        items_data = [
            {
                "id": "si_test_item_1",
                "price": {
                    "id": price_id,
                    "product": "prod_test_pro",
                },
            }
        ]

    metadata: dict[str, Any] = {}
    if org_id is not None:
        metadata["organization_id"] = str(org_id)

    return {
        "id": event_id,
        "object": "event",
        "type": "customer.subscription.created",
        "created": created,
        "data": {
            "object": {
                "id": subscription_id,
                "object": "subscription",
                "customer": customer_id,
                "status": status,
                "current_period_start": current_period_start,
                "current_period_end": current_period_end,
                "cancel_at_period_end": cancel_at_period_end,
                "canceled_at": canceled_at,
                "items": {
                    "object": "list",
                    "data": items_data,
                },
                "metadata": metadata,
            }
        },
    }


# =============================================================================
# 1. Valid Creation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_valid_active_creation_associates_correctly_without_premature_pro() -> None:
    """Requirement: Active subscription created updates placeholder row with authoritative fields.

    Does NOT grant Pro plan tier before checkout/invoice confirmation.
    """
    org = Organization(id=uuid.uuid4(), name="Active Corp")
    customer_id = "cus_valid_active"
    sub_id = "sub_valid_active"

    # Placeholder customer row created by get_or_create_stripe_customer
    placeholder_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        stripe_price_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
        cancel_at_period_end=False,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[placeholder_sub])

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="active",
        org_id=org.id,
        current_period_start=1730000000,
        current_period_end=1732592000,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    # Local subscription row updated in place
    assert placeholder_sub.stripe_subscription_id == sub_id
    assert placeholder_sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert placeholder_sub.status == SubscriptionStatus.active
    assert placeholder_sub.plan_tier == "free"  # Not upgraded to pro before invoice/checkout payment
    assert placeholder_sub.current_period_start == datetime.fromtimestamp(1730000000, tz=timezone.utc)
    assert placeholder_sub.current_period_end == datetime.fromtimestamp(1732592000, tz=timezone.utc)
    assert placeholder_sub.cancel_at_period_end is False
    assert placeholder_sub.last_stripe_event_created_at == datetime.fromtimestamp(1730000000, tz=timezone.utc)


@pytest.mark.asyncio
async def test_trialing_subscription_created_preserves_free_entitlement() -> None:
    """Requirement: Trialing subscription records status='trialing' but preserves Free plan tier."""
    org = Organization(id=uuid.uuid4(), name="Trial Corp")
    customer_id = "cus_trial_test"
    sub_id = "sub_trial_test"

    placeholder_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[placeholder_sub])

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="trialing",
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    assert placeholder_sub.status == SubscriptionStatus.trialing
    assert placeholder_sub.plan_tier == "free"
    assert placeholder_sub.stripe_subscription_id == sub_id
    assert placeholder_sub.stripe_price_id == TEST_PRO_PRICE_ID


@pytest.mark.asyncio
async def test_incomplete_subscription_created_remains_free() -> None:
    """Requirement: Incomplete subscription sets status='incomplete' and plan_tier='free'."""
    org = Organization(id=uuid.uuid4(), name="Incomplete Corp")
    customer_id = "cus_inc_test"
    sub_id = "sub_inc_test"

    placeholder_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[placeholder_sub])

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="incomplete",
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    assert placeholder_sub.status == SubscriptionStatus.incomplete
    assert placeholder_sub.plan_tier == "free"


@pytest.mark.asyncio
async def test_multi_item_subscription_matches_configured_pro_price() -> None:
    """Requirement: Finds configured Pro price even if it is not the first item in items.data."""
    org = Organization(id=uuid.uuid4(), name="Multi Item Corp")
    customer_id = "cus_multi_item"
    sub_id = "sub_multi_item"

    placeholder_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[placeholder_sub])

    # Pro price is at index 1, preceded by an add-on item
    items_data = [
        {"id": "si_metered_1", "price": {"id": "price_addon_analytics"}},
        {"id": "si_pro_base", "price": {"id": TEST_PRO_PRICE_ID}},
    ]

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        items_data=items_data,
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    assert placeholder_sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert placeholder_sub.stripe_subscription_id == sub_id


# =============================================================================
# 2. Strict Validation & Anti-Tampering Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sub_id_val",
    [None, "", "cus_wrong_prefix", 12345, True],
)
async def test_missing_or_invalid_subscription_id_fails_closed(sub_id_val: Any) -> None:
    """Requirement: Missing or malformed subscription ID raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Bad Sub Corp")
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id="cus_test_1")
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(customer_id="cus_test_1")
    event_payload["data"]["object"]["id"] = sub_id_val

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Missing or invalid Stripe subscription ID"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cust_id_val",
    [None, "", "sub_wrong_prefix", 999, False],
)
async def test_missing_or_invalid_customer_id_fails_closed(cust_id_val: Any) -> None:
    """Requirement: Missing or malformed customer ID raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Bad Cust Corp")
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id="cus_test_1")
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(customer_id="cus_test_1")
    event_payload["data"]["object"]["customer"] = cust_id_val

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Missing or invalid Stripe customer ID"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
async def test_unknown_local_customer_fails_closed() -> None:
    """Requirement: Unknown customer without prior local database record raises WebhookPermanentError.

    Metadata alone must NEVER create customer bindings.
    """
    org = Organization(id=uuid.uuid4(), name="Orphan Corp")
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[])

    event_payload = build_subscription_created_event(
        customer_id="cus_unknown_999",
        org_id=org.id,  # Metadata claims this org, but customer association does not exist locally
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Unknown Stripe customer 'cus_unknown_999'"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
async def test_organization_metadata_mismatch_fails_closed() -> None:
    """Requirement: If metadata.organization_id disagrees with local row, fails closed."""
    org = Organization(id=uuid.uuid4(), name="True Org")
    fake_org_id = uuid.uuid4()
    customer_id = "cus_tamper_test"

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(
        customer_id=customer_id,
        org_id=fake_org_id,  # Claims different org
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Organization metadata mismatch"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_meta_uuid",
    ["not-a-uuid", "12345", ""],
)
async def test_malformed_organization_metadata_fails_closed(bad_meta_uuid: str) -> None:
    """Requirement: Malformed organization UUID in metadata raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Bad Meta Corp")
    customer_id = "cus_bad_meta"

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(customer_id=customer_id)
    event_payload["data"]["object"]["metadata"]["organization_id"] = bad_meta_uuid

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="organization"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
async def test_missing_metadata_relies_on_established_customer_association() -> None:
    """Requirement: If metadata is absent, relies on already established local customer association."""
    org = Organization(id=uuid.uuid4(), name="No Meta Corp")
    customer_id = "cus_no_meta"
    sub_id = "sub_no_meta"

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        org_id=None,  # No metadata supplied
    )
    event_payload["data"]["object"]["metadata"] = {}

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    assert sub.stripe_subscription_id == sub_id
    assert sub.organization_id == org.id


@pytest.mark.asyncio
async def test_missing_configured_pro_price_id_fails_closed() -> None:
    """Requirement: Unconfigured STRIPE_PRO_PRICE_ID in server environment raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="No Pro Price Corp")
    customer_id = "cus_no_pro_cfg"
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id=customer_id)
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(customer_id=customer_id)

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings(pro_price_id="")):
        with pytest.raises(WebhookPermanentError, match="STRIPE_PRO_PRICE_ID is not configured"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
async def test_missing_pro_price_in_items_data_fails_closed() -> None:
    """Requirement: Subscription lacking configured Pro price ID in line items raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Wrong Price Corp")
    customer_id = "cus_wrong_price"
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id=customer_id)
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(
        customer_id=customer_id,
        price_id="price_unauthorized_enterprise_999",
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="No subscription line item matched configured Pro price"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_items",
    [None, [], {}, "invalid_items"],
)
async def test_missing_or_empty_items_data_fails_closed(bad_items: Any) -> None:
    """Requirement: Missing or empty items.data raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Empty Items Corp")
    customer_id = "cus_empty_items"
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id=customer_id)
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(customer_id=customer_id)
    if isinstance(bad_items, list):
        event_payload["data"]["object"]["items"]["data"] = bad_items
    else:
        event_payload["data"]["object"]["items"] = bad_items

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="items"):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_ts", "end_ts", "expected_err"),
    [
        (0, 1732592000, "current_period_start must be positive"),
        (-100, 1732592000, "current_period_start must be positive"),
        ("1730000000", 1732592000, "Expected positive integer"),
        (True, 1732592000, "Expected positive integer"),
        (1730000000, 1730000000, "must be strictly after"),  # start == end
        (1732592000, 1730000000, "must be strictly after"),  # end < start
    ],
)
async def test_invalid_period_timestamps_fail_closed(
    start_ts: Any,
    end_ts: Any,
    expected_err: str,
) -> None:
    """Requirement: Malformed timestamps or inverted period ranges fail closed."""
    org = Organization(id=uuid.uuid4(), name="Bad Timestamps Corp")
    customer_id = "cus_bad_ts"
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id=customer_id)
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(
        customer_id=customer_id,
        current_period_start=start_ts,
        current_period_end=end_ts,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match=expected_err):
            await handle_subscription_created(event_payload, db)


@pytest.mark.asyncio
async def test_invalid_subscription_status_fails_closed() -> None:
    """Requirement: Unknown Stripe subscription status raises WebhookPermanentError."""
    org = Organization(id=uuid.uuid4(), name="Status Corp")
    customer_id = "cus_status_test"
    sub = Subscription(id=uuid.uuid4(), organization_id=org.id, stripe_customer_id=customer_id)
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    event_payload = build_subscription_created_event(
        customer_id=customer_id,
        status="super_active",
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookPermanentError, match="Unrecognized subscription status 'super_active'"):
            await handle_subscription_created(event_payload, db)


# =============================================================================
# 3. Reconciliation & Multi-Subscription Conflict Tests
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_active_subscription_conflict_raises_reconciliation_error() -> None:
    """Requirement: Incoming subscription with different ID when an active subscription exists raises WebhookReconciliationConflictError."""
    org = Organization(id=uuid.uuid4(), name="Conflict Corp")
    existing_sub_id = "sub_existing_active"
    incoming_sub_id = "sub_incoming_conflict"
    customer_id = "cus_conflict_test"

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=existing_sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[existing_sub])

    event_payload = build_subscription_created_event(
        subscription_id=incoming_sub_id,
        customer_id=customer_id,
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        with pytest.raises(WebhookReconciliationConflictError) as exc_info:
            await handle_subscription_created(event_payload, db)

        err = exc_info.value
        assert err.organization_id == org.id
        assert err.existing_subscription_id == existing_sub_id
        assert err.incoming_subscription_id == incoming_sub_id


@pytest.mark.asyncio
async def test_conflict_quarantines_and_commits_reconciliation_record_via_engine() -> None:
    """Requirement: Webhook engine catches WebconciliationConflictError, rolls back domain changes, and commits BillingReconciliation atomically with quarantine status."""
    org = Organization(id=uuid.uuid4(), name="Engine Conflict Corp")
    existing_sub_id = "sub_eng_active"
    incoming_sub_id = "sub_eng_conflict"
    customer_id = "cus_eng_test"
    event_id = "evt_conflict_engine_1"

    existing_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=existing_sub_id,
        status=SubscriptionStatus.active,
        plan_tier="pro",
    )

    event_payload = build_subscription_created_event(
        event_id=event_id,
        subscription_id=incoming_sub_id,
        customer_id=customer_id,
        org_id=org.id,
    )

    raw_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.created",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=event_payload,
    )

    db = SubscriptionMockDbSession(
        organizations=[org],
        subscriptions=[existing_sub],
        events=[raw_event],
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        result = await process_webhook_event(db, event_id)

    assert result.success is False
    assert result.status == WebhookProcessingStatus.quarantined
    assert raw_event.status == WebhookProcessingStatus.quarantined

    # Existing active subscription was NOT corrupted
    assert existing_sub.stripe_subscription_id == existing_sub_id
    assert existing_sub.status == SubscriptionStatus.active

    # BillingReconciliation record was committed
    assert len(db.reconciliations) == 1
    rec = db.reconciliations[0]
    assert rec.organization_id == org.id
    assert rec.event_id == event_id
    assert rec.existing_subscription_id == existing_sub_id
    assert rec.incoming_subscription_id == incoming_sub_id
    assert rec.status == ReconciliationStatus.pending_review


@pytest.mark.asyncio
async def test_legitimate_resubscription_after_prior_cancellation() -> None:
    """Requirement: Resubscribing after cancellation updates the customer record without duplicate rows."""
    org = Organization(id=uuid.uuid4(), name="Resub Corp")
    old_sub_id = "sub_old_canceled"
    new_sub_id = "sub_new_active"
    customer_id = "cus_resub_test"

    canceled_sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=old_sub_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[canceled_sub])

    event_payload = build_subscription_created_event(
        subscription_id=new_sub_id,
        customer_id=customer_id,
        status="active",
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    # In-place update without creating duplicate rows
    assert len(db.subscriptions) == 1
    assert canceled_sub.stripe_subscription_id == new_sub_id
    assert canceled_sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert canceled_sub.status == SubscriptionStatus.active
    assert canceled_sub.plan_tier == "free"  # Awaits checkout/invoice payment confirmation


# =============================================================================
# 4. Event Ordering & Checkout Race Handling Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_created_before_checkout_completion_converges() -> None:
    """Requirement: Case A: subscription.created arrives before checkout.session.completed.

    Associates subscription ID, sets period dates, preserves free plan tier.
    Subsequent checkout completion upgrades plan tier to pro.
    """
    org = Organization(id=uuid.uuid4(), name="Race Corp")
    customer_id = "cus_race_1"
    sub_id = "sub_race_1"
    session_id = "cs_race_1"

    sub = Subscription(
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
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub], attempts=[attempt])

    # 1. customer.subscription.created arrives at T=100
    sub_event = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="active",
        created=1730000100,
        current_period_start=1730000000,
        current_period_end=1732592000,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(sub_event, db)

    assert sub.stripe_subscription_id == sub_id
    assert sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert sub.plan_tier == "free"  # No premature pro grant
    assert sub.status == SubscriptionStatus.active

    # 2. checkout.session.completed arrives at T=105
    from test_stripe_checkout_handler import build_checkout_completed_event
    checkout_event = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="paid",
        event_created=1730000105,
    )

    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(checkout_event, db)

    # Both events converged smoothly
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert attempt.status == CheckoutAttemptStatus.complete


@pytest.mark.asyncio
async def test_subscription_created_after_checkout_completion_backfills_without_regressing_pro() -> None:
    """Requirement: Case B: checkout.session.completed arrives first, subscription.created arrives second.

    Backfills missing authoritative price and period dates without downgrading plan tier to free.
    """
    org = Organization(id=uuid.uuid4(), name="Checkout First Corp")
    customer_id = "cus_chk_first"
    sub_id = "sub_chk_first"
    session_id = "cs_chk_first"

    sub = Subscription(
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
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub], attempts=[attempt])

    # 1. checkout.session.completed executes at T=100
    from test_stripe_checkout_handler import build_checkout_completed_event
    checkout_event = build_checkout_completed_event(
        session_id=session_id,
        customer_id=customer_id,
        subscription_id=sub_id,
        org_id=org.id,
        attempt_id=attempt.id,
        payment_status="paid",
        event_created=1730000100,
    )
    with patch("app.services.billing.stripe_checkout_handler.settings", get_test_settings()):
        await handle_checkout_session_completed(checkout_event, db)

    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.stripe_price_id is None  # Deliberately left unset by checkout handler
    assert sub.current_period_start is None

    # 2. subscription.created arrives second at T=105
    sub_event = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="active",
        created=1730000105,
        current_period_start=1730000000,
        current_period_end=1732592000,
    )
    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(sub_event, db)

    # Pro plan tier is PRESERVED, authoritative fields are backfilled
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.stripe_price_id == TEST_PRO_PRICE_ID
    assert sub.current_period_start == datetime.fromtimestamp(1730000000, tz=timezone.utc)
    assert sub.current_period_end == datetime.fromtimestamp(1732592000, tz=timezone.utc)


@pytest.mark.asyncio
async def test_delayed_created_event_after_newer_update_does_not_advance_timestamp() -> None:
    """Requirement: Case C: Older created event (T=90) arriving after a newer update (T=100).

    Does NOT advance last_stripe_event_created_at and does not overwrite non-null state.
    """
    org = Organization(id=uuid.uuid4(), name="Stale Corp")
    customer_id = "cus_stale_test"
    sub_id = "sub_stale_test"

    newer_ts = datetime.fromtimestamp(1730000100, tz=timezone.utc)
    original_period_start = datetime.fromtimestamp(1730000050, tz=timezone.utc)
    original_period_end = datetime.fromtimestamp(1732592050, tz=timezone.utc)

    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        stripe_price_id=TEST_PRO_PRICE_ID,
        current_period_start=original_period_start,
        current_period_end=original_period_end,
        status=SubscriptionStatus.active,
        plan_tier="pro",
        last_stripe_event_created_at=newer_ts,
    )
    db = SubscriptionMockDbSession(organizations=[org], subscriptions=[sub])

    # Delayed created event generated at T=90 (older than T=100)
    sub_event = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="incomplete",  # Stale event has older status
        created=1730000090,
        current_period_start=1730000000,
        current_period_end=1732592000,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(sub_event, db)

    # Status, plan_tier, period dates, and timestamp were NOT overwritten
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "pro"
    assert sub.current_period_start == original_period_start
    assert sub.current_period_end == original_period_end
    assert sub.last_stripe_event_created_at == newer_ts  # Not regressed or advanced


# =============================================================================
# 5. Strict Accounting Isolation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_created_does_not_create_or_modify_billing_periods_or_invoices() -> None:
    """Requirement: Section 6: Subscription created handler must NOT create or modify OrganizationBillingPeriod, StripeInvoice, tokens, or reservations."""
    org = Organization(id=uuid.uuid4(), name="Accounting Isolation Corp")
    customer_id = "cus_iso_test"
    sub_id = "sub_iso_test"

    now = datetime.now(timezone.utc)
    initial_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org.id,
        period_start=now,
        period_end=now,
        plan_tier="free",
        monthly_token_quota=1000,
        tokens_used=50,
        tokens_reserved=20,
        cost_micros_used=50000,
        cost_micros_reserved=20000,
        is_active=True,
    )
    reservation = UsageReservation(
        id=uuid.uuid4(),
        generation_task_id=uuid.uuid4(),
        organization_id=org.id,
        billing_period_id=initial_period.id,
        lease_token=uuid.uuid4(),
        tokens_reserved=20,
        cost_micros_reserved=20000,
        status=ReservationStatus.active,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
    )

    db = SubscriptionMockDbSession(
        organizations=[org],
        subscriptions=[sub],
        billing_periods=[initial_period],
        reservations=[reservation],
    )

    event_payload = build_subscription_created_event(
        subscription_id=sub_id,
        customer_id=customer_id,
        status="active",
        org_id=org.id,
    )

    with patch("app.services.billing.stripe_subscription_handler.settings", get_test_settings()):
        await handle_subscription_created(event_payload, db)

    # 1. No new billing periods added
    assert len(db.billing_periods) == 1
    # 2. Existing period was completely untouched
    assert initial_period.tokens_used == 50
    assert initial_period.tokens_reserved == 20
    assert initial_period.cost_micros_used == 50000
    assert initial_period.cost_micros_reserved == 20000
    assert initial_period.plan_tier == "free"
    assert initial_period.is_active is True
    # 3. No StripeInvoice records created
    assert not any(isinstance(a, StripeInvoice) for a in db.added)
    # 4. In-flight reservation completely untouched
    assert reservation.tokens_reserved == 20
    assert reservation.status == ReservationStatus.active
