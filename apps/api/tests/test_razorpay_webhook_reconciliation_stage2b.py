"""Stage 2B Focused Tests: Razorpay Webhook Reconciliation Hardening (BLK-06).

Covers all Stage 2B requirements:
A. subscription.authenticated correlation:
   - Valid notes.attempt_id correlation (including binding missing provider_subscription_id).
   - notes.attempt_id belonging to another organization (fail closed, no mutation/fallback).
   - notes.attempt_id with mismatched environment (fail closed).
   - notes.attempt_id with mismatched provider or merchant account (fail closed).
   - Missing or malformed notes.attempt_id with provider_subscription_id fallback.
   - Missing or ambiguous correlation produces deterministic PaymentReconciliation cases.
   - Repeated delivery does not duplicate reconciliation cases.
   - Diagnostic payloads contain available event, subscription, attempt, and scope identifiers.
B. subscription.activated terminal-state protection:
   - expired attempt is never resurrected.
   - failed attempt is never resurrected.
   - Terminal attempt bound to a different provider_subscription_id is never rebound.
   - Valid nonterminal attempts (creating, recovery_required, open, payment_pending) transition to complete.
   - Duplicate activation is idempotent.
   - Missing or ambiguous attempt stages deterministic reconciliation.
C. Active-subscription conflict protection & ADR-005 lock ordering:
   - Existing active/trialing subscription prevents a second activation.
   - Conflict creates a deterministic reconciliation case (verified both directly and via webhook_engine).
   - Existing active subscription remains unchanged; conflicting subscription remains unactivated.
   - Level 0A organization advisory lock is acquired before Level 0B and all domain row-level locks.
   - Two concurrent activation attempts for the same organization serialize on the Level 0A org lock.
D. Event ordering and regression:
   - Older activation cannot regress lifecycle state.
   - past_due_since is preserved on activation.
   - Cancellation / terminal-subscription guards remain intact.
   - subscription.updated active transition also enforces Level 0A org lock and active conflict protection.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sys
from typing import Any, AsyncIterator
import uuid

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

import pytest

# Register all ORM models before building queries
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.payment  # noqa: F401

from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    ReconciliationStatus,
    Subscription,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from app.services.billing.advisory_lock import derive_advisory_lock_key
from app.services.billing.razorpay.agreement_handlers import (
    handle_subscription_activated,
    handle_subscription_authenticated,
    handle_subscription_completed,
    handle_subscription_updated,
)
from app.services.billing.razorpay.handler_base import (
    RazorpayWebhookReconciliationConflictError,
)
from app.services.billing.razorpay.webhook_engine import (
    ClaimedWebhookEvent,
    RazorpayWebhookDispatcher,
    execute_claimed_razorpay_webhook_event,
)
from tests.test_razorpay_agreement_handlers import (
    MockScalarResult,
    RazorpayAgreementMockDbSession,
    make_test_claimed_event,
)


# =============================================================================
# Helpers
# =============================================================================


def _seed_provision(
    session: RazorpayAgreementMockDbSession,
    org_id: uuid.UUID,
    cust_id: str,
    *,
    merchant_account_id: str = "acc_test_merchant",
    environment: str = "test",
) -> BillingCustomerProvision:
    prov = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    session.customer_provisions[cust_id] = prov
    return prov


def _make_attempt(
    org_id: uuid.UUID,
    *,
    provider: str = "razorpay",
    merchant_account_id: str = "acc_test_merchant",
    environment: str = "test",
    provider_subscription_id: str | None = None,
    status: CheckoutAttemptStatus = CheckoutAttemptStatus.open,
    idempotency_key: str | None = None,
) -> BillingCheckoutAttempt:
    return BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=provider,
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_subscription_id=provider_subscription_id,
        idempotency_key=idempotency_key or f"idemp_{uuid.uuid4().hex[:10]}",
        status=status,
        currency="INR",
        attempt_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _make_subscription(
    org_id: uuid.UUID,
    sub_id: str,
    cust_id: str,
    *,
    provider: str = "razorpay",
    merchant_account_id: str = "acc_test_merchant",
    environment: str = "test",
    status: SubscriptionStatus = SubscriptionStatus.incomplete,
    plan_tier: str = "free",
    provider_plan_id: str = "plan_pro_monthly",
    last_webhook_event_at: datetime | None = None,
    past_due_since: datetime | None = None,
    canceled_at: datetime | None = None,
) -> Subscription:
    return Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider=provider,
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_customer_id=cust_id,
        provider_subscription_id=sub_id,
        provider_plan_id=provider_plan_id,
        status=status,
        plan_tier=plan_tier,
        last_webhook_event_at=last_webhook_event_at,
        past_due_since=past_due_since,
        canceled_at=canceled_at,
    )


# =============================================================================
# A. Authenticated-Event Correlation Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
        CheckoutAttemptStatus.open,
    ],
)
async def test_s2b_auth_01_valid_notes_attempt_id_correlation(
    initial_status: CheckoutAttemptStatus,
) -> None:
    """Valid notes.attempt_id correlates deterministically, binds missing sub_id, and advances status."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = f"sub_auth_notes_{initial_status.value}"
    cust_id = f"cust_auth_notes_{initial_status.value}"
    _seed_provision(session, org_id, cust_id)

    # Attempt created in Phase B without provider_subscription_id bound yet
    attempt = _make_attempt(
        org_id,
        provider_subscription_id=None,
        status=initial_status,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        session=session,
    )

    await handle_subscription_authenticated(claimed, session)

    assert sub_id in session.subscriptions
    sub = session.subscriptions[sub_id]
    assert sub.status == SubscriptionStatus.incomplete
    assert sub.plan_tier == "free"
    assert attempt.status == CheckoutAttemptStatus.payment_pending
    assert attempt.provider_subscription_id == sub_id
    assert attempt.provider_customer_id == cust_id
    assert len(session.reconciliations) == 0


@pytest.mark.asyncio
async def test_s2b_auth_02_notes_attempt_id_belonging_to_another_org_fails_closed() -> None:
    """notes.attempt_id pointing to another organization's attempt fails closed without mutating or falling back."""
    session = RazorpayAgreementMockDbSession()
    org_authoritative = uuid.uuid4()
    org_other = uuid.uuid4()
    sub_id = "sub_auth_wrong_org"
    cust_id = "cust_auth_wrong_org"
    _seed_provision(session, org_authoritative, cust_id)

    other_org_attempt = _make_attempt(
        org_other,
        provider_subscription_id=None,
        status=CheckoutAttemptStatus.open,
    )
    # Even if a same-org attempt exists for sub_id, we MUST NOT silently fall back when notes.attempt_id conflicts
    same_org_attempt = _make_attempt(
        org_authoritative,
        provider_subscription_id=sub_id,
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(other_org_attempt.id)] = other_org_attempt
    session.checkout_attempts[str(same_org_attempt.id)] = same_org_attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": str(other_org_attempt.id)},
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_authenticated(claimed, session)

    err = exc_info.value
    assert err.case_key == f"checkout_attempt_tenant_mismatch:{other_org_attempt.id}:{org_authoritative}"
    assert err.reason == "checkout_attempt_tenant_mismatch"
    assert err.organization_id == org_authoritative
    assert err.raw_event_id == claimed.event_id
    diag = err.diagnostic_payload
    assert diag["provider_subscription_id"] == sub_id
    assert diag["notes_attempt_id"] == str(other_org_attempt.id)
    assert diag["provider_event_id"] == claimed.provider_event_id
    assert diag["organization_id"] == str(org_authoritative)
    assert diag["environment"] == "test"
    assert diag["merchant_account_id"] == "acc_test_merchant"
    assert diag["reason"] == "checkout_attempt_tenant_mismatch"

    # Neither attempt mutated!
    assert other_org_attempt.status == CheckoutAttemptStatus.open
    assert other_org_attempt.provider_subscription_id is None
    assert same_org_attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_s2b_auth_03_notes_attempt_id_mismatched_environment_fails_closed() -> None:
    """notes.attempt_id referencing an attempt in a different environment fails closed."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_wrong_env"
    cust_id = "cust_auth_wrong_env"
    _seed_provision(session, org_id, cust_id, environment="test")

    live_attempt = _make_attempt(
        org_id,
        environment="live",
        provider_subscription_id=None,
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(live_attempt.id)] = live_attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        environment="test",
        notes={"attempt_id": str(live_attempt.id)},
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_authenticated(claimed, session)

    err = exc_info.value
    assert err.case_key == f"checkout_attempt_scope_mismatch:{live_attempt.id}:{sub_id}"
    assert err.reason == "checkout_attempt_scope_mismatch"
    assert err.organization_id == org_id
    assert err.diagnostic_payload["attempt_environment"] == "live"
    assert err.diagnostic_payload["expected_environment"] == "test"
    assert err.diagnostic_payload["notes_attempt_id"] == str(live_attempt.id)
    assert err.diagnostic_payload["provider_event_id"] == claimed.provider_event_id
    assert live_attempt.status == CheckoutAttemptStatus.open
    assert live_attempt.provider_subscription_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_provider,bad_merchant",
    [
        ("stripe", "acc_test_merchant"),
        ("razorpay", "acc_foreign_merchant"),
    ],
)
async def test_s2b_auth_04_notes_attempt_id_mismatched_provider_or_merchant_fails_closed(
    bad_provider: str,
    bad_merchant: str,
) -> None:
    """notes.attempt_id with mismatched provider or merchant account fails closed."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = f"sub_auth_scope_{bad_provider}_{bad_merchant}"
    cust_id = f"cust_auth_scope_{bad_provider}"
    _seed_provision(session, org_id, cust_id, merchant_account_id="acc_test_merchant")

    mismatched_attempt = _make_attempt(
        org_id,
        provider=bad_provider,
        merchant_account_id=bad_merchant,
        environment="test",
        provider_subscription_id=None,
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(mismatched_attempt.id)] = mismatched_attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        merchant_account_id="acc_test_merchant",
        environment="test",
        notes={"attempt_id": str(mismatched_attempt.id)},
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_authenticated(claimed, session)

    err = exc_info.value
    assert err.case_key == f"checkout_attempt_scope_mismatch:{mismatched_attempt.id}:{sub_id}"
    assert err.reason == "checkout_attempt_scope_mismatch"
    assert err.diagnostic_payload["attempt_provider"] == bad_provider
    assert err.diagnostic_payload["attempt_merchant_account_id"] == bad_merchant
    assert err.diagnostic_payload["expected_provider"] == "razorpay"
    assert err.diagnostic_payload["expected_merchant_account_id"] == "acc_test_merchant"
    assert mismatched_attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
@pytest.mark.parametrize("notes_payload", [{}, {"attempt_id": "not-a-valid-uuid"}])
async def test_s2b_auth_05_missing_or_invalid_notes_id_falls_back_to_provider_subscription_id(
    notes_payload: dict[str, Any],
) -> None:
    """Missing or malformed notes.attempt_id falls back cleanly to scoped provider_subscription_id correlation."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_fallback_001"
    cust_id = "cust_auth_fallback_001"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id=sub_id,
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes=notes_payload,
        session=session,
    )

    await handle_subscription_authenticated(claimed, session)

    assert attempt.status == CheckoutAttemptStatus.payment_pending
    assert len(session.reconciliations) == 0


@pytest.mark.asyncio
async def test_s2b_auth_06_unknown_valid_notes_uuid_with_bound_other_attempt_does_not_silently_bind() -> None:
    """Valid UUID in notes.attempt_id not found in DB must not silently bind a different attempt bound to sub_id."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_id_mismatch"
    cust_id = "cust_auth_id_mismatch"
    _seed_provision(session, org_id, cust_id)

    different_attempt = _make_attempt(
        org_id,
        provider_subscription_id=sub_id,
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(different_attempt.id)] = different_attempt
    unknown_notes_uuid = uuid.uuid4()

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": str(unknown_notes_uuid)},
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_authenticated(claimed, session)

    err = exc_info.value
    assert err.case_key == f"checkout_attempt_id_mismatch:{sub_id}:{unknown_notes_uuid}"
    assert err.reason == "checkout_attempt_id_mismatch"
    assert err.diagnostic_payload["notes_attempt_id"] == str(unknown_notes_uuid)
    assert str(different_attempt.id) in err.diagnostic_payload["matched_attempt_ids"]
    assert different_attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_s2b_auth_07_missing_correlation_stages_idempotent_reconciliation_with_full_diagnostics() -> None:
    """Missing attempt preserves early incomplete subscription, stages deterministic case with full diagnostics, and avoids duplicates on replay."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_missing_diag"
    cust_id = "cust_auth_missing_diag"
    missing_notes_attempt_id = str(uuid.uuid4())
    _seed_provision(session, org_id, cust_id)

    t1 = int(datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc).timestamp())
    claimed1 = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": missing_notes_attempt_id},
        created_at_ts=t1,
        session=session,
    )
    await handle_subscription_authenticated(claimed1, session)

    # Preserves early subscription creation (incomplete, plan_tier='free')
    assert sub_id in session.subscriptions
    assert session.subscriptions[sub_id].status == SubscriptionStatus.incomplete
    assert session.subscriptions[sub_id].plan_tier == "free"

    case_key = f"missing_checkout_attempt:{sub_id}"
    assert list(session.reconciliations.keys()) == [case_key]
    rec = session.reconciliations[case_key]
    assert rec.reason == "missing_checkout_attempt"
    latest = rec.diagnostic_payload["latest"]
    assert latest["provider_subscription_id"] == sub_id
    assert latest["notes_attempt_id"] == missing_notes_attempt_id
    assert latest["provider_event_id"] == claimed1.provider_event_id
    assert latest["organization_id"] == str(org_id)
    assert latest["environment"] == "test"
    assert latest["merchant_account_id"] == "acc_test_merchant"
    assert latest["reason"] == "missing_checkout_attempt"
    assert len(rec.diagnostic_payload["history"]) == 1

    # Repeated delivery with later timestamp updates same case without creating duplicates
    t2 = int(datetime(2026, 11, 10, 10, 5, tzinfo=timezone.utc).timestamp())
    claimed2 = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": missing_notes_attempt_id},
        created_at_ts=t2,
        session=session,
    )
    await handle_subscription_authenticated(claimed2, session)

    assert len(session.reconciliations) == 1
    assert len(session.reconciliations[case_key].diagnostic_payload["history"]) == 2
    assert session.reconciliations[case_key].diagnostic_payload["latest"]["provider_event_id"] == claimed2.provider_event_id


# =============================================================================
# B. Activation Terminal-State Protection Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [CheckoutAttemptStatus.expired, CheckoutAttemptStatus.failed],
)
async def test_s2b_act_01_terminal_attempt_is_never_resurrected(
    terminal_status: CheckoutAttemptStatus,
) -> None:
    """expired and failed checkout attempts are never resurrected to complete on subscription.activated."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = f"sub_act_term_{terminal_status.value}"
    cust_id = f"cust_act_term_{terminal_status.value}"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id=sub_id,
        status=terminal_status,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    t1 = int(datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc).timestamp())
    claimed1 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        created_at_ts=t1,
        session=session,
    )
    await handle_subscription_activated(claimed1, session)

    # Historical terminal status MUST remain immutable
    assert attempt.status == terminal_status
    assert attempt.provider_subscription_id == sub_id

    case_key = f"activation_on_terminal_attempt:{attempt.id}:{sub_id}"
    assert case_key in session.reconciliations
    rec = session.reconciliations[case_key]
    assert rec.reason == "activation_on_terminal_attempt"
    assert rec.organization_id == org_id
    latest = rec.diagnostic_payload["latest"]
    assert latest["attempt_id"] == str(attempt.id)
    assert latest["attempt_status"] == terminal_status.value
    assert latest["provider_subscription_id"] == sub_id
    assert latest["provider_event_id"] == claimed1.provider_event_id
    assert latest["provider_subscription_id_mismatch"] is False

    # Replay must not create duplicate reconciliation cases
    t2 = int(datetime(2026, 11, 10, 11, 5, tzinfo=timezone.utc).timestamp())
    claimed2 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        created_at_ts=t2,
        session=session,
    )
    await handle_subscription_activated(claimed2, session)

    assert attempt.status == terminal_status
    assert len(session.reconciliations) == 1
    assert len(session.reconciliations[case_key].diagnostic_payload["history"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [CheckoutAttemptStatus.expired, CheckoutAttemptStatus.failed],
)
async def test_s2b_act_02_terminal_attempt_with_different_sub_id_is_never_rebound(
    terminal_status: CheckoutAttemptStatus,
) -> None:
    """A terminal attempt bound to an older provider_subscription_id must never be rebound or resurrected."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    old_sub_id = f"sub_old_{terminal_status.value}"
    new_sub_id = f"sub_new_{terminal_status.value}"
    cust_id = f"cust_rebind_{terminal_status.value}"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id=old_sub_id,
        status=terminal_status,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=new_sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        session=session,
    )
    await handle_subscription_activated(claimed, session)

    # Strictly untouched!
    assert attempt.status == terminal_status
    assert attempt.provider_subscription_id == old_sub_id

    case_key = f"activation_on_terminal_attempt:{attempt.id}:{new_sub_id}"
    assert case_key in session.reconciliations
    latest = session.reconciliations[case_key].diagnostic_payload["latest"]
    assert latest["attempt_provider_subscription_id"] == old_sub_id
    assert latest["provider_subscription_id"] == new_sub_id
    assert latest["provider_subscription_id_mismatch"] is True


@pytest.mark.asyncio
async def test_s2b_act_03_authenticated_also_protects_terminal_attempts() -> None:
    """subscription.authenticated also refuses to mutate expired/failed attempts and stages reconciliation."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_auth_term_exp"
    cust_id = "cust_auth_term_exp"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id="sub_prior_expired",
        status=CheckoutAttemptStatus.expired,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.authenticated",
        sub_id=sub_id,
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        session=session,
    )
    await handle_subscription_authenticated(claimed, session)

    assert attempt.status == CheckoutAttemptStatus.expired
    assert attempt.provider_subscription_id == "sub_prior_expired"
    case_key = f"authentication_on_terminal_attempt:{attempt.id}:{sub_id}"
    assert case_key in session.reconciliations
    assert session.reconciliations[case_key].reason == "authentication_on_terminal_attempt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
        CheckoutAttemptStatus.open,
        CheckoutAttemptStatus.payment_pending,
    ],
)
async def test_s2b_act_04_valid_nonterminal_attempt_transitions_to_complete(
    initial_status: CheckoutAttemptStatus,
) -> None:
    """Valid nonterminal attempts transition cleanly to complete after ownership/scope validation."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = f"sub_act_valid_{initial_status.value}"
    cust_id = f"cust_act_valid_{initial_status.value}"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id=None,
        status=initial_status,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        session=session,
    )
    await handle_subscription_activated(claimed, session)

    assert session.subscriptions[sub_id].status == SubscriptionStatus.active
    assert session.subscriptions[sub_id].plan_tier == "free"
    assert attempt.status == CheckoutAttemptStatus.complete
    assert attempt.provider_subscription_id == sub_id
    assert attempt.provider_customer_id == cust_id
    assert len(session.reconciliations) == 0


@pytest.mark.asyncio
async def test_s2b_act_05_duplicate_activation_is_idempotent() -> None:
    """Repeated subscription.activated deliveries remain idempotent with no false conflicts or duplicates."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_act_idemp_001"
    cust_id = "cust_act_idemp_001"
    _seed_provision(session, org_id, cust_id)

    sub = _make_subscription(org_id, sub_id, cust_id, status=SubscriptionStatus.incomplete)
    session.subscriptions[sub_id] = sub

    attempt = _make_attempt(
        org_id,
        provider_subscription_id=sub_id,
        status=CheckoutAttemptStatus.payment_pending,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    t1 = int(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    claimed1 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        created_at_ts=t1,
        session=session,
    )
    await handle_subscription_activated(claimed1, session)
    assert sub.status == SubscriptionStatus.active
    assert attempt.status == CheckoutAttemptStatus.complete
    assert len(session.reconciliations) == 0

    # Duplicate delivery with identical timestamp (stale no-op)
    claimed_same = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        created_at_ts=t1,
        session=session,
    )
    await handle_subscription_activated(claimed_same, session)

    # Duplicate delivery with newer timestamp (idempotent replay on active sub + complete attempt)
    t2 = int(datetime(2026, 11, 10, 12, 5, tzinfo=timezone.utc).timestamp())
    claimed_newer = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        created_at_ts=t2,
        session=session,
    )
    await handle_subscription_activated(claimed_newer, session)

    assert len(session.subscriptions) == 1
    assert sub.status == SubscriptionStatus.active
    assert sub.plan_tier == "free"
    assert attempt.status == CheckoutAttemptStatus.complete
    assert len(session.reconciliations) == 0


@pytest.mark.asyncio
async def test_s2b_act_06_missing_attempt_stages_deterministic_reconciliation() -> None:
    """Missing checkout attempt on subscription.activated stages missing_checkout_attempt reconciliation."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_act_missing_att"
    cust_id = "cust_act_missing_att"
    _seed_provision(session, org_id, cust_id)

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        session=session,
    )
    await handle_subscription_activated(claimed, session)

    assert session.subscriptions[sub_id].status == SubscriptionStatus.active
    case_key = f"missing_checkout_attempt:{sub_id}"
    assert case_key in session.reconciliations
    rec = session.reconciliations[case_key]
    assert rec.reason == "missing_checkout_attempt"
    assert rec.diagnostic_payload["latest"]["event_type"] == "subscription.activated"
    assert rec.diagnostic_payload["latest"]["provider_event_id"] == claimed.provider_event_id
    assert rec.diagnostic_payload["latest"]["provider_subscription_id"] == sub_id


# =============================================================================
# C. Active-Subscription Conflict Protection & ADR-005 Lock Ordering Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_provider,existing_status,incoming_exists_as_incomplete",
    [
        ("razorpay", SubscriptionStatus.active, True),
        ("razorpay", SubscriptionStatus.active, False),
        ("razorpay", SubscriptionStatus.trialing, True),
        ("stripe", SubscriptionStatus.active, False),
    ],
)
async def test_s2b_conflict_01_existing_active_subscription_prevents_second_activation(
    existing_provider: str,
    existing_status: SubscriptionStatus,
    incoming_exists_as_incomplete: bool,
) -> None:
    """Existing active/trialing subscription blocks a second activation and leaves both records untouched."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    existing_sub_id = "sub_existing_active_001"
    incoming_sub_id = "sub_incoming_conflict_002"
    cust_id = "cust_conflict_001"
    _seed_provision(session, org_id, cust_id)

    existing_sub = _make_subscription(
        org_id,
        existing_sub_id,
        "cust_existing_001" if existing_provider == "stripe" else cust_id,
        provider=existing_provider,
        merchant_account_id="default" if existing_provider == "stripe" else "acc_test_merchant",
        environment="legacy" if existing_provider == "stripe" else "test",
        status=existing_status,
        plan_tier="pro",
    )
    session.subscriptions[existing_sub_id] = existing_sub

    incoming_sub: Subscription | None = None
    if incoming_exists_as_incomplete:
        incoming_sub = _make_subscription(
            org_id,
            incoming_sub_id,
            cust_id,
            status=SubscriptionStatus.incomplete,
            plan_tier="free",
        )
        session.subscriptions[incoming_sub_id] = incoming_sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=incoming_sub_id,
        status="active",
        customer_id=cust_id,
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_activated(claimed, session)

    err = exc_info.value
    assert err.case_key == f"duplicate_active_subscription:{org_id}:{incoming_sub_id}"
    assert err.reason == "duplicate_active_subscription"
    assert err.organization_id == org_id
    assert err.raw_event_id == claimed.event_id
    diag = err.diagnostic_payload
    assert diag["existing_active_subscription_id"] == str(existing_sub.id)
    assert diag["existing_provider_subscription_id"] == existing_sub_id
    assert diag["incoming_provider_subscription_id"] == incoming_sub_id
    assert diag["provider_event_id"] == claimed.provider_event_id
    assert diag["organization_id"] == str(org_id)

    # Existing active subscription completely unchanged
    assert existing_sub.status == existing_status
    assert existing_sub.plan_tier == "pro"

    # Conflicting incoming subscription remains unactivated
    if incoming_exists_as_incomplete:
        assert incoming_sub is not None
        assert incoming_sub.status == SubscriptionStatus.incomplete
    else:
        assert incoming_sub_id not in session.subscriptions


class _EngineCompatibleAgreementSession(RazorpayAgreementMockDbSession):
    """Extends RazorpayAgreementMockDbSession with savepoint support for execute_claimed_razorpay_webhook_event."""

    def __init__(self, shared_store: dict[str, Any]) -> None:
        super().__init__()
        self.customer_provisions = shared_store["customer_provisions"]
        self.subscriptions = shared_store["subscriptions"]
        self.checkout_attempts = shared_store["checkout_attempts"]
        self.billing_periods = shared_store["billing_periods"]
        self.webhook_events = shared_store["webhook_events"]
        self.reconciliations = shared_store["reconciliations"]
        self.organizations: set[uuid.UUID] = shared_store["organizations"]
        self.shared_store = shared_store

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        # Snapshot subscriptions map keys to roll back uncommitted additions on savepoint failure
        pre_sub_keys = set(self.subscriptions.keys())
        try:
            yield
        except Exception:
            for k in list(self.subscriptions.keys()):
                if k not in pre_sub_keys:
                    del self.subscriptions[k]
            raise

    async def commit(self) -> None:
        await super().commit()
        self.shared_store["commits"] = self.shared_store.get("commits", 0) + 1

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MockScalarResult:
        stmt_str = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "organizations" in stmt_str and "organization_billing_periods" not in stmt_str:
            for org_id in self.organizations:
                return MockScalarResult(org_id)
            return MockScalarResult(None)
        return await super().execute(stmt, params)


@pytest.mark.asyncio
async def test_s2b_conflict_02_webhook_engine_commits_deterministic_reconciliation_and_quarantines() -> None:
    """End-to-end through webhook_engine: conflict commits PaymentReconciliation, quarantines event, and stays idempotent on replay."""
    org_id = uuid.uuid4()
    existing_sub_id = "sub_eng_active_001"
    incoming_sub_id = "sub_eng_conflict_002"
    cust_id = "cust_eng_conflict_001"

    shared_store: dict[str, Any] = {
        "customer_provisions": {
            cust_id: BillingCustomerProvision(
                id=uuid.uuid4(),
                organization_id=org_id,
                provider="razorpay",
                merchant_account_id="acc_test_merchant",
                environment="test",
                provider_customer_id=cust_id,
                status=CustomerProvisionStatus.resolved,
            )
        },
        "subscriptions": {
            existing_sub_id: _make_subscription(
                org_id,
                existing_sub_id,
                cust_id,
                status=SubscriptionStatus.active,
                plan_tier="pro",
            ),
            incoming_sub_id: _make_subscription(
                org_id,
                incoming_sub_id,
                cust_id,
                status=SubscriptionStatus.incomplete,
                plan_tier="free",
            ),
        },
        "checkout_attempts": {},
        "billing_periods": {},
        "webhook_events": {},
        "reconciliations": {},
        "organizations": {org_id},
    }

    dispatcher = RazorpayWebhookDispatcher()
    dispatcher.register("subscription.activated", handle_subscription_activated)

    def _session_factory() -> _EngineCompatibleAgreementSession:
        return _EngineCompatibleAgreementSession(shared_store)

    now = datetime.now(timezone.utc)

    # First conflicting activation delivery
    probe_session = _session_factory()
    claimed1 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=incoming_sub_id,
        status="active",
        customer_id=cust_id,
        session=probe_session,
    )
    res1 = await execute_claimed_razorpay_webhook_event(
        session_factory=_session_factory,
        claimed=claimed1,
        handler=dispatcher.get_handler("subscription.activated"),
        expected_merchant_id="acc_test_merchant",
        expected_environment="test",
        now_fn=lambda: now,
    )

    assert res1.success is False
    assert res1.status == WebhookProcessingStatus.quarantined
    assert res1.failure_category == "reconciliation_conflict"
    assert res1.reconciliation_upserted is True
    assert shared_store.get("commits") == 1

    case_key = f"duplicate_active_subscription:{org_id}:{incoming_sub_id}"
    assert list(shared_store["reconciliations"].keys()) == [case_key]
    rec = shared_store["reconciliations"][case_key]
    assert rec.reason == "duplicate_active_subscription"
    assert rec.organization_id == org_id
    assert rec.raw_event_id == claimed1.event_id
    assert rec.status == ReconciliationStatus.pending_review
    assert rec.diagnostic_payload["latest"]["existing_provider_subscription_id"] == existing_sub_id
    assert rec.diagnostic_payload["latest"]["incoming_provider_subscription_id"] == incoming_sub_id
    assert rec.diagnostic_payload["latest"]["provider_event_id"] == claimed1.provider_event_id

    # Existing active unchanged; incoming still incomplete
    assert shared_store["subscriptions"][existing_sub_id].status == SubscriptionStatus.active
    assert shared_store["subscriptions"][incoming_sub_id].status == SubscriptionStatus.incomplete

    # Second conflicting delivery updates the same reconciliation case without duplicating
    claimed2 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=incoming_sub_id,
        status="active",
        customer_id=cust_id,
        session=probe_session,
    )
    res2 = await execute_claimed_razorpay_webhook_event(
        session_factory=_session_factory,
        claimed=claimed2,
        handler=dispatcher.get_handler("subscription.activated"),
        expected_merchant_id="acc_test_merchant",
        expected_environment="test",
        now_fn=lambda: now + timedelta(seconds=5),
    )

    assert res2.status == WebhookProcessingStatus.quarantined
    assert res2.reconciliation_upserted is True
    assert shared_store.get("commits") == 2
    assert len(shared_store["reconciliations"]) == 1
    assert len(shared_store["reconciliations"][case_key].diagnostic_payload["history"]) == 2


@pytest.mark.asyncio
async def test_s2b_conflict_03_level0a_org_lock_acquired_before_level0b_and_domain_row_locks() -> None:
    """Verifies ADR-005 monotonic lock order: Level 0A org lock -> Level 0B sub lock -> Level 2 Sub FOR UPDATE -> Level 3 Attempt FOR UPDATE."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_lock_order_001"
    cust_id = "cust_lock_order_001"
    _seed_provision(session, org_id, cust_id)

    sub = _make_subscription(org_id, sub_id, cust_id, status=SubscriptionStatus.incomplete)
    session.subscriptions[sub_id] = sub
    attempt = _make_attempt(org_id, provider_subscription_id=sub_id, status=CheckoutAttemptStatus.payment_pending)
    session.checkout_attempts[str(attempt.id)] = attempt

    # Use dict payload (mirroring webhook_engine dispatch) so zero prior lease FOR UPDATE runs
    payload = {
        "entity": "event",
        "account_id": "acc_test_merchant",
        "event": "subscription.activated",
        "created_at": 1700000000,
        "payload": {
            "subscription": {
                "entity": {
                    "id": sub_id,
                    "entity": "subscription",
                    "plan_id": "plan_pro_monthly",
                    "customer_id": cust_id,
                    "status": "active",
                    "current_start": 1700000000,
                    "current_end": 1702592000,
                    "notes": {"attempt_id": str(attempt.id)},
                }
            }
        },
    }

    await handle_subscription_activated(payload, session)

    expected_org_key = derive_advisory_lock_key("organization", org_id)
    expected_sub_key = derive_advisory_lock_key("subscription", sub_id)

    assert session.advisory_locks == [expected_org_key, expected_sub_key]

    # Verify exact sequence in statement_log:
    # 1) Read-only tenant resolution (is_for_update == False)
    # 2) Level 0A org advisory lock
    # 3) Level 0B subscription advisory lock
    # 4) Level 2 Subscription FOR UPDATE (target + conflict check)
    # 5) Level 3 BillingCheckoutAttempt FOR UPDATE
    log = session.statement_log
    idx_org_lock = log.index(("advisory_lock", expected_org_key))
    idx_sub_lock = log.index(("advisory_lock", expected_sub_key))
    for_update_indices = [
        i
        for i, (kind, is_for_update) in enumerate(log)
        if kind in ("select_subscription_for_update", "select_checkout_attempt_for_update")
        and is_for_update is True
    ]
    assert for_update_indices, "Expected domain FOR UPDATE locks to be recorded"
    assert idx_org_lock < idx_sub_lock < min(for_update_indices)

    # Ensure zero FOR UPDATE queries occurred before Level 0A org lock
    for kind, detail in log[:idx_org_lock]:
        assert kind != "select_subscription_for_update"
        assert kind != "select_checkout_attempt_for_update"
        if isinstance(detail, bool):
            assert detail is False

    # Ensure Level 2 Subscription FOR UPDATE preceded Level 3 BillingCheckoutAttempt FOR UPDATE
    sub_fu_indices = [i for i, (k, _) in enumerate(log) if k == "select_subscription_for_update"]
    att_fu_indices = [i for i, (k, _) in enumerate(log) if k == "select_checkout_attempt_for_update"]
    assert max(sub_fu_indices) < min(att_fu_indices)


class _AdvisoryLockCoordinatedStore:
    """Lock-aware in-memory store simulating PostgreSQL pg_advisory_xact_lock serialization across concurrent sessions."""

    def __init__(self) -> None:
        self.customer_provisions: dict[str, BillingCustomerProvision] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.checkout_attempts: dict[str, BillingCheckoutAttempt] = {}
        self.webhook_events: dict[uuid.UUID, BillingWebhookEvent] = {}
        self.reconciliations: dict[str, PaymentReconciliation] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.lock_events: list[tuple[str, str, int]] = []
        self.in_critical_section: int = 0
        self.max_concurrent_in_critical_section: int = 0

    def get_lock(self, key: int) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]


class _CoordinatedConcurrentSession(RazorpayAgreementMockDbSession):
    """Session backed by _AdvisoryLockCoordinatedStore that acquires real asyncio.Locks for pg_advisory_xact_lock."""

    def __init__(
        self,
        store: _AdvisoryLockCoordinatedStore,
        worker_name: str,
        *,
        on_org_lock_acquired: asyncio.Event | None = None,
        pause_before_flush: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.worker_name = worker_name
        self.customer_provisions = store.customer_provisions
        self.subscriptions = store.subscriptions
        self.checkout_attempts = store.checkout_attempts
        self.webhook_events = store.webhook_events
        self.reconciliations = store.reconciliations
        self._held_locks: list[tuple[int, asyncio.Lock]] = []
        self._on_org_lock_acquired = on_org_lock_acquired
        self._pause_before_flush = pause_before_flush

    async def commit(self) -> None:
        await super().commit()
        self.shared_store["commits"] = self.shared_store.get("commits", 0) + 1

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MockScalarResult:
        stmt_str = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "pg_advisory_xact_lock" in stmt_str:
            key = params.get("key") if params else None
            assert key is not None
            lock = self.store.get_lock(key)
            self.store.lock_events.append((self.worker_name, "waiting", key))
            await lock.acquire()
            self._held_locks.append((key, lock))
            self.store.lock_events.append((self.worker_name, "acquired", key))
            self.advisory_locks.append(key)
            if len(self._held_locks) == 1:
                self.store.in_critical_section += 1
                self.store.max_concurrent_in_critical_section = max(
                    self.store.max_concurrent_in_critical_section,
                    self.store.in_critical_section,
                )
                if self._on_org_lock_acquired is not None:
                    self._on_org_lock_acquired.set()
            return MockScalarResult(None)
        return await super().execute(stmt, params)

    async def flush(self) -> None:
        if self._pause_before_flush is not None:
            await self._pause_before_flush.wait()
        await super().flush()

    def release_xact_locks(self) -> None:
        if self._held_locks:
            self.store.in_critical_section -= 1
        while self._held_locks:
            key, lock = self._held_locks.pop()
            self.store.lock_events.append((self.worker_name, "released", key))
            lock.release()


@pytest.mark.asyncio
async def test_s2b_conflict_04_two_concurrent_activations_serialized_by_org_advisory_lock() -> None:
    """Two concurrent activations for the same org serialize on Level 0A org lock; only one activates, the second fails closed."""
    store = _AdvisoryLockCoordinatedStore()
    org_id = uuid.uuid4()
    cust_id = "cust_race_org_001"
    sub_1_id = "sub_race_001"
    sub_2_id = "sub_race_002"

    store.customer_provisions[cust_id] = BillingCustomerProvision(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_merchant",
        environment="test",
        provider_customer_id=cust_id,
        status=CustomerProvisionStatus.resolved,
    )
    store.subscriptions[sub_1_id] = _make_subscription(org_id, sub_1_id, cust_id, status=SubscriptionStatus.incomplete)
    store.subscriptions[sub_2_id] = _make_subscription(org_id, sub_2_id, cust_id, status=SubscriptionStatus.incomplete)

    worker1_acquired_org_lock = asyncio.Event()
    allow_worker1_commit = asyncio.Event()

    session1 = _CoordinatedConcurrentSession(
        store,
        "worker_1",
        on_org_lock_acquired=worker1_acquired_org_lock,
        pause_before_flush=allow_worker1_commit,
    )
    session2 = _CoordinatedConcurrentSession(store, "worker_2")

    claimed1 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_1_id,
        status="active",
        customer_id=cust_id,
        session=session1,
    )
    claimed2 = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_2_id,
        status="active",
        customer_id=cust_id,
        session=session2,
    )

    async def _run_worker1() -> str:
        try:
            await handle_subscription_activated(claimed1, session1)
            return "activated"
        finally:
            session1.release_xact_locks()

    async def _run_worker2() -> str:
        await worker1_acquired_org_lock.wait()
        try:
            await handle_subscription_activated(claimed2, session2)
            return "activated"
        finally:
            session2.release_xact_locks()

    task1 = asyncio.create_task(_run_worker1())
    task2 = asyncio.create_task(_run_worker2())

    # Wait until worker_2 is actively queued on the exact Level 0A org advisory lock
    expected_org_key = derive_advisory_lock_key("organization", org_id)
    for _ in range(50):
        if ("worker_2", "waiting", expected_org_key) in store.lock_events:
            break
        await asyncio.sleep(0.01)
    assert ("worker_2", "waiting", expected_org_key) in store.lock_events
    assert ("worker_2", "acquired", expected_org_key) not in store.lock_events

    # Release worker_1 to finish & release transaction advisory lock
    allow_worker1_commit.set()
    res1, res2 = await asyncio.gather(task1, task2, return_exceptions=True)

    # Strict mutual exclusion verified!
    assert store.max_concurrent_in_critical_section == 1
    assert res1 == "activated"
    assert isinstance(res2, RazorpayWebhookReconciliationConflictError)
    assert res2.reason == "duplicate_active_subscription"
    assert res2.case_key == f"duplicate_active_subscription:{org_id}:{sub_2_id}"

    # Exactly one active subscription; second remains unactivated!
    assert store.subscriptions[sub_1_id].status == SubscriptionStatus.active
    assert store.subscriptions[sub_2_id].status == SubscriptionStatus.incomplete


# =============================================================================
# D. Event Ordering and Regression Tests
# =============================================================================


@pytest.mark.asyncio
async def test_s2b_order_01_older_activation_cannot_regress_lifecycle_state() -> None:
    """Older subscription.activated arriving after a newer event cannot regress status, plan, or dates."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_order_stale_act"
    cust_id = "cust_order_stale_act"
    _seed_provision(session, org_id, cust_id)

    newer_time = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    older_time = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
    sub = _make_subscription(
        org_id,
        sub_id,
        cust_id,
        status=SubscriptionStatus.paused,
        provider_plan_id="plan_latest_v2",
        last_webhook_event_at=newer_time,
    )
    session.subscriptions[sub_id] = sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        plan_id="plan_stale_v1",
        customer_id=cust_id,
        created_at_ts=int(older_time.timestamp()),
        session=session,
    )

    await handle_subscription_activated(claimed, session)

    assert sub.status == SubscriptionStatus.paused
    assert sub.provider_plan_id == "plan_latest_v2"
    assert sub.last_webhook_event_at == newer_time


@pytest.mark.asyncio
async def test_s2b_order_02_past_due_since_preserved_on_activation() -> None:
    """subscription.activated preserves past_due status and past_due_since anchor."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_order_past_due"
    cust_id = "cust_order_past_due"
    _seed_provision(session, org_id, cust_id)

    anchor = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    sub = _make_subscription(
        org_id,
        sub_id,
        cust_id,
        status=SubscriptionStatus.past_due,
        plan_tier="pro",
        past_due_since=anchor,
        last_webhook_event_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
    )
    session.subscriptions[sub_id] = sub

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        created_at_ts=int(datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc).timestamp()),
        session=session,
    )

    await handle_subscription_activated(claimed, session)

    assert sub.status == SubscriptionStatus.past_due
    assert sub.past_due_since == anchor


@pytest.mark.asyncio
async def test_s2b_order_03_cancellation_guards_and_updated_active_conflict_protection() -> None:
    """Canceled subscriptions refuse resurrection across handlers, and subscription.updated blocks conflicting activation."""
    # Part 1: Canceled subscription refuses resurrection across authenticated, activated, updated
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    sub_id = "sub_order_canceled"
    cust_id = "cust_order_canceled"
    _seed_provision(session, org_id, cust_id)

    canceled_at = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    sub = _make_subscription(
        org_id,
        sub_id,
        cust_id,
        status=SubscriptionStatus.canceled,
        plan_tier="free",
        canceled_at=canceled_at,
        last_webhook_event_at=canceled_at,
    )
    session.subscriptions[sub_id] = sub

    later_ts = int(datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp())
    for event_type, handler in [
        ("subscription.authenticated", handle_subscription_authenticated),
        ("subscription.activated", handle_subscription_activated),
        ("subscription.updated", handle_subscription_updated),
    ]:
        claimed = make_test_claimed_event(
            event_type=event_type,
            sub_id=sub_id,
            status="active",
            customer_id=cust_id,
            created_at_ts=later_ts,
            session=session,
        )
        await handler(claimed, session)
        assert sub.status == SubscriptionStatus.canceled
        assert sub.canceled_at == canceled_at

    # Idempotent subscription.completed on already canceled sub
    claimed_comp = make_test_claimed_event(
        event_type="subscription.completed",
        sub_id=sub_id,
        status="completed",
        customer_id=cust_id,
        created_at_ts=later_ts + 60,
        session=session,
    )
    await handle_subscription_completed(claimed_comp, session)
    assert sub.status == SubscriptionStatus.canceled

    # Part 2: subscription.updated transitioning paused -> active also enforces Level 0A org lock & conflict protection
    session2 = RazorpayAgreementMockDbSession()
    org2 = uuid.uuid4()
    cust2 = "cust_upd_conflict"
    _seed_provision(session2, org2, cust2)

    active_sub = _make_subscription(org2, "sub_upd_active_1", cust2, status=SubscriptionStatus.active, plan_tier="pro")
    paused_sub = _make_subscription(org2, "sub_upd_paused_2", cust2, status=SubscriptionStatus.paused, plan_tier="free")
    session2.subscriptions["sub_upd_active_1"] = active_sub
    session2.subscriptions["sub_upd_paused_2"] = paused_sub

    claimed_upd = make_test_claimed_event(
        event_type="subscription.updated",
        sub_id="sub_upd_paused_2",
        status="active",
        customer_id=cust2,
        session=session2,
    )
    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_updated(claimed_upd, session2)

    assert exc_info.value.reason == "duplicate_active_subscription"
    assert active_sub.status == SubscriptionStatus.active
    assert paused_sub.status == SubscriptionStatus.paused
    assert session2.advisory_locks[0] == derive_advisory_lock_key("organization", org2)


@pytest.mark.asyncio
async def test_s2b_act_07_nonterminal_or_complete_attempt_bound_to_other_sub_is_not_rebound() -> None:
    """An attempt already bound to another provider_subscription_id fails closed without rebinding."""
    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    cust_id = "cust_sub_rebind_guard"
    _seed_provision(session, org_id, cust_id)

    attempt = _make_attempt(
        org_id,
        provider_subscription_id="sub_bound_original",
        status=CheckoutAttemptStatus.open,
    )
    session.checkout_attempts[str(attempt.id)] = attempt

    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id="sub_different_incoming",
        status="active",
        customer_id=cust_id,
        notes={"attempt_id": str(attempt.id)},
        session=session,
    )

    with pytest.raises(RazorpayWebhookReconciliationConflictError) as exc_info:
        await handle_subscription_activated(claimed, session)

    assert exc_info.value.reason == "checkout_attempt_subscription_mismatch"
    assert exc_info.value.case_key == f"checkout_attempt_subscription_mismatch:{attempt.id}:sub_different_incoming"
    assert attempt.provider_subscription_id == "sub_bound_original"
    assert attempt.status == CheckoutAttemptStatus.open


@pytest.mark.asyncio
async def test_s2b_conflict_05_unexpected_integrity_error_propagates_unswallowed() -> None:
    """Unexpected database IntegrityError is never swallowed as an ordinary reconciliation outcome."""
    from sqlalchemy.exc import IntegrityError

    session = RazorpayAgreementMockDbSession()
    org_id = uuid.uuid4()
    cust_id = "cust_unexpected_ie"
    sub_id = "sub_unexpected_ie"
    _seed_provision(session, org_id, cust_id)

    orig_flush = session.flush

    async def _boom_flush() -> None:
        raise IntegrityError("INSERT INTO subscriptions ...", {}, Exception("simulated unexpected constraint"))

    session.flush = _boom_flush  # type: ignore[method-assign]
    claimed = make_test_claimed_event(
        event_type="subscription.activated",
        sub_id=sub_id,
        status="active",
        customer_id=cust_id,
        session=session,
    )

    with pytest.raises(IntegrityError):
        await handle_subscription_activated(claimed, session)
    session.flush = orig_flush  # type: ignore[method-assign]

