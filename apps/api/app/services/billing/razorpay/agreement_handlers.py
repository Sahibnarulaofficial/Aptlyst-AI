"""Razorpay Subscription Agreement Lifecycle Event Handlers.

Handles:
1. subscription.authenticated
   - Binds provider subscription to tenant organization and customer.
   - Transitions Subscription.status to incomplete (or updates metadata if active).
   - Updates BillingCheckoutAttempt to payment_pending.
   - NEVER grants Pro entitlement or creates billing periods.
2. subscription.activated
   - Transitions Subscription.status to active.
   - Populates current_period_start and current_period_end timestamps.
   - Updates BillingCheckoutAttempt to complete.
   - Refuses resurrection of terminal subscriptions (canceled).
   - NEVER cures past_due (only full-payment settlement can clear past_due_since).
   - STRICTLY keeps plan_tier = 'free' (Zero Pro token quota granted).
3. subscription.updated
   - Applies supported plan, schedule, or status updates monotonically.
   - Protects against stale out-of-order events.
   - Never infers payment success from subscription status.
4. subscription.completed
   - Records subscription agreement completion.
   - Transitions Subscription.status to canceled, plan_tier = 'free', canceled_at = event_created_at.
   - Does not modify OrganizationBillingPeriod rows or mutate quota counters directly.

Locking & Concurrency Invariants (ADR-005):
- Level 0A: pg_advisory_xact_lock("organization", org_id) (acquired before Level 0B and row locks on activation paths)
- Level 0B: pg_advisory_xact_lock("subscription", sub_id)
- Level 2: Subscription row lock (SELECT ... FOR UPDATE)
- Level 3: BillingCheckoutAttempt row lock (SELECT ... FOR UPDATE)
- OrganizationBillingPeriod (Level 1) is NEVER locked or mutated by agreement handlers.
- Fencing: assert_valid_worker_lease() verifies unexpired worker lease inside domain transaction.
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.advisory_lock import acquire_advisory_lock
from app.services.billing.razorpay.handler_base import (
    ClaimedWebhookEvent,
    RazorpayEventContext,
    RazorpayScopeMismatchError,
    RazorpaySubscriptionEntity,
    RazorpayWebhookReconciliationConflictError,
    acquire_razorpay_advisory_lock,
    assert_valid_worker_lease,
    ensure_utc_datetime,
    extract_subscription_entity,
    extract_subscription_id,
    is_stale_event,
    is_terminal_subscription,
    parse_positive_unix_timestamp,
    resolve_tenant_organization,
    stage_or_update_payment_reconciliation,
    upsert_payment_reconciliation,
    validate_razorpay_event_envelope,
)
from app.services.billing.razorpay.webhook_engine import (
    RazorpayWebhookDispatcher,
    default_razorpay_dispatcher,
)

logger = logging.getLogger(__name__)


def _assert_subscription_environment_immutable(
    sub: Subscription,
    expected_env: str,
) -> None:
    """Refuse any webhook mutation if an existing subscription's environment differs or is missing.

    Guarantees that a webhook can never cross-mutate or overwrite an existing subscription's
    persisted environment provenance.
    """
    persisted_env = (sub.environment or "").strip().lower() if isinstance(sub.environment, str) else ""
    target_env = (expected_env or "").strip().lower()
    if not persisted_env or persisted_env != target_env:
        raise RazorpayScopeMismatchError(
            f"Refusing subscription mutation: persisted subscription environment '{sub.environment}' "
            f"does not match validated event environment '{expected_env}'."
        )


# Terminal unsuccessful checkout attempt statuses (immutable historical records)
_TERMINAL_UNSUCCESSFUL_ATTEMPT_STATUSES: frozenset[CheckoutAttemptStatus] = frozenset({
    CheckoutAttemptStatus.expired,
    CheckoutAttemptStatus.failed,
})

# Nonterminal statuses eligible to transition to payment_pending on subscription.authenticated
_AUTHENTICATABLE_ATTEMPT_STATUSES: frozenset[CheckoutAttemptStatus] = frozenset({
    CheckoutAttemptStatus.creating,
    CheckoutAttemptStatus.recovery_required,
    CheckoutAttemptStatus.open,
})

# Nonterminal statuses eligible to transition to complete on subscription.activated
_ACTIVATABLE_ATTEMPT_STATUSES: frozenset[CheckoutAttemptStatus] = frozenset({
    CheckoutAttemptStatus.creating,
    CheckoutAttemptStatus.recovery_required,
    CheckoutAttemptStatus.open,
    CheckoutAttemptStatus.payment_pending,
})

# Subscription statuses occupying the global uq_org_active_subscription invariant
_ACTIVE_INVARIANT_SUBSCRIPTION_STATUSES: tuple[SubscriptionStatus, ...] = (
    SubscriptionStatus.active,
    SubscriptionStatus.trialing,
)


def _extract_event_audit_identifiers(
    event_or_payload: ClaimedWebhookEvent | BillingWebhookEvent | dict[str, Any],
    ctx: RazorpayEventContext,
) -> tuple[uuid.UUID | None, str | None]:
    """Return authoritative (raw_event_id, provider_event_id) when backed by a real webhook event."""
    if isinstance(event_or_payload, (ClaimedWebhookEvent, BillingWebhookEvent)):
        return ctx.event_id, ctx.provider_event_id
    if isinstance(event_or_payload, dict):
        raw_provider_event_id = event_or_payload.get("event_id") or event_or_payload.get("id")
        if isinstance(raw_provider_event_id, str) and raw_provider_event_id.strip():
            return None, raw_provider_event_id.strip()
    return None, None


def _parse_notes_attempt_id(
    notes: dict[str, Any] | None,
) -> tuple[str | None, uuid.UUID | None]:
    """Extract raw and parsed UUID for provider-supplied notes.attempt_id.

    Returns:
        (raw_notes_attempt_id, parsed_uuid):
        - (None, None) if notes.attempt_id is absent or blank.
        - (canonical_uuid_str, uuid_obj) if notes.attempt_id is a valid UUID.
        - (raw_str, None) if notes.attempt_id is present but malformed/invalid UUID.
    """
    if not isinstance(notes, dict):
        return None, None
    raw_val = notes.get("attempt_id")
    if raw_val is None:
        return None, None
    raw_str = str(raw_val).strip()
    if not raw_str:
        return None, None
    try:
        parsed = uuid.UUID(raw_str)
    except (ValueError, TypeError, AttributeError):
        return raw_str, None
    return str(parsed), parsed


def _build_reconciliation_diagnostics(
    *,
    ctx: RazorpayEventContext,
    org_id: uuid.UUID | None,
    sub_id: str,
    event_type: str,
    reason: str,
    notes_attempt_id: str | None = None,
    real_event_id: uuid.UUID | None = None,
    real_provider_event_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic, secret-safe diagnostic payload for PaymentReconciliation cases."""
    diag: dict[str, Any] = {
        "provider_subscription_id": sub_id,
        "organization_id": str(org_id) if org_id is not None else None,
        "environment": ctx.environment,
        "merchant_account_id": ctx.merchant_account_id,
        "event_type": event_type,
        "reason": reason,
    }
    if notes_attempt_id is not None:
        diag["notes_attempt_id"] = notes_attempt_id
    if real_event_id is not None:
        diag["event_id"] = str(real_event_id)
    if real_provider_event_id is not None:
        diag["provider_event_id"] = real_provider_event_id
    if extra:
        diag.update(extra)
    return diag


def _validate_correlated_attempt_ownership_and_scope(
    *,
    attempt: BillingCheckoutAttempt,
    ctx: RazorpayEventContext,
    org_id: uuid.UUID,
    sub_id: str,
    event_type_label: str,
    notes_attempt_id_raw: str | None,
    real_event_id: uuid.UUID | None,
    real_provider_event_id: str | None,
) -> None:
    """Validate that a correlated BillingCheckoutAttempt matches authoritative tenant and provider scope.

    Raises RazorpayWebhookReconciliationConflictError on any tenant, provider, merchant,
    or environment mismatch so callers fail closed without mutating or silently binding.
    """
    if attempt.organization_id != org_id:
        logger.warning(
            "Checkout attempt tenant mismatch for subscription %s: attempt %s belongs to org %s != authoritative org %s",
            sub_id,
            attempt.id,
            attempt.organization_id,
            org_id,
        )
        reason = "checkout_attempt_tenant_mismatch"
        raise RazorpayWebhookReconciliationConflictError(
            f"Checkout attempt {attempt.id} belongs to organization {attempt.organization_id}, "
            f"which conflicts with authoritative organization {org_id}.",
            case_key=f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_id}",
            reason=reason,
            organization_id=org_id,
            raw_event_id=real_event_id,
            diagnostic_payload=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason=reason,
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "attempt_id": str(attempt.id),
                    "attempt_organization_id": str(attempt.organization_id),
                    "authoritative_organization_id": str(org_id),
                },
            ),
        )

    attempt_provider = (attempt.provider or "").strip().lower()
    attempt_merchant = (attempt.merchant_account_id or "").strip()
    attempt_env = (attempt.environment or "").strip().lower()

    if (
        attempt_provider != "razorpay"
        or attempt_merchant != ctx.merchant_account_id
        or attempt_env != ctx.environment
    ):
        logger.warning(
            "Checkout attempt scope mismatch for subscription %s: attempt %s has "
            "(provider=%s, merchant=%s, env=%s) != expected (razorpay, %s, %s)",
            sub_id,
            attempt.id,
            attempt.provider,
            attempt.merchant_account_id,
            attempt.environment,
            ctx.merchant_account_id,
            ctx.environment,
        )
        reason = "checkout_attempt_scope_mismatch"
        raise RazorpayWebhookReconciliationConflictError(
            f"Checkout attempt {attempt.id} scope ({attempt.provider}, {attempt.merchant_account_id}, "
            f"{attempt.environment}) does not match webhook event scope "
            f"(razorpay, {ctx.merchant_account_id}, {ctx.environment}).",
            case_key=f"checkout_attempt_scope_mismatch:{attempt.id}:{sub_id}",
            reason=reason,
            organization_id=org_id,
            raw_event_id=real_event_id,
            diagnostic_payload=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason=reason,
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "attempt_id": str(attempt.id),
                    "attempt_provider": attempt.provider,
                    "attempt_merchant_account_id": attempt.merchant_account_id,
                    "attempt_environment": attempt.environment,
                    "expected_provider": "razorpay",
                    "expected_merchant_account_id": ctx.merchant_account_id,
                    "expected_environment": ctx.environment,
                },
            ),
        )


async def _correlate_checkout_attempt(
    *,
    db: AsyncSession,
    ctx: RazorpayEventContext,
    event_or_payload: ClaimedWebhookEvent | BillingWebhookEvent | dict[str, Any],
    org_id: uuid.UUID,
    sub_id: str,
    sub_entity: RazorpaySubscriptionEntity,
    sub: Subscription | None,
    event_type_label: str,
) -> BillingCheckoutAttempt | None:
    """Deterministically correlate an incoming Razorpay subscription event to its BillingCheckoutAttempt.

    Correlation precedence:
    1. Prefer provider-supplied `notes.attempt_id` when present and a valid UUID.
       Validates organization, provider, merchant_account_id, and environment strictly.
       Never falls back silently if the referenced attempt fails ownership/scope validation
       or disagrees with a different attempt bound to the subscription.
    2. Fall back to scoped `provider_subscription_id == sub_id` lookup when `notes.attempt_id`
       is absent or malformed (not a valid UUID).
    3. Stages deterministic `missing_checkout_attempt:{sub_id}` reconciliation when no attempt
       can be located safely; fails closed with `RazorpayWebhookReconciliationConflictError`
       on tenant/scope/id mismatch or ambiguity.
    """
    notes_attempt_id_raw, notes_attempt_uuid = _parse_notes_attempt_id(sub_entity.notes)
    real_event_id, real_provider_event_id = _extract_event_audit_identifiers(
        event_or_payload, ctx
    )

    # Path 1: Preferred correlation via validated notes.attempt_id
    if notes_attempt_uuid is not None:
        stmt_by_id = (
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == notes_attempt_uuid)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        res_by_id = await db.execute(stmt_by_id)
        by_id_matches = [
            a for a in res_by_id.scalars().all() if a.id == notes_attempt_uuid
        ]
        if len(by_id_matches) == 1:
            attempt = by_id_matches[0]
            _validate_correlated_attempt_ownership_and_scope(
                attempt=attempt,
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type_label=event_type_label,
                notes_attempt_id_raw=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
            )
            return attempt

        # Valid UUID in notes.attempt_id was not found: check whether a different attempt
        # is already bound to sub_id so we never silently bind to an unrelated attempt.
        stmt_sub_check = (
            select(BillingCheckoutAttempt)
            .where(
                BillingCheckoutAttempt.provider == "razorpay",
                BillingCheckoutAttempt.merchant_account_id == ctx.merchant_account_id,
                BillingCheckoutAttempt.environment == ctx.environment,
                BillingCheckoutAttempt.provider_subscription_id == sub_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        res_sub_check = await db.execute(stmt_sub_check)
        bound_attempts = [
            a
            for a in res_sub_check.scalars().all()
            if a.provider_subscription_id == sub_id
        ]
        if bound_attempts:
            reason = "checkout_attempt_id_mismatch"
            logger.warning(
                "notes.attempt_id %s not found for subscription %s, but %d different attempt(s) "
                "are bound to %s; failing closed without silently binding.",
                notes_attempt_uuid,
                sub_id,
                len(bound_attempts),
                sub_id,
            )
            raise RazorpayWebhookReconciliationConflictError(
                f"Webhook notes.attempt_id {notes_attempt_uuid} not found, while subscription {sub_id} "
                f"is bound to different checkout attempt(s) {[str(a.id) for a in bound_attempts]}.",
                case_key=f"checkout_attempt_id_mismatch:{sub_id}:{notes_attempt_uuid}",
                reason=reason,
                organization_id=org_id,
                raw_event_id=real_event_id,
                diagnostic_payload=_build_reconciliation_diagnostics(
                    ctx=ctx,
                    org_id=org_id,
                    sub_id=sub_id,
                    event_type=event_type_label,
                    reason=reason,
                    notes_attempt_id=notes_attempt_id_raw,
                    real_event_id=real_event_id,
                    real_provider_event_id=real_provider_event_id,
                    extra={
                        "matched_attempt_ids": [str(a.id) for a in bound_attempts],
                    },
                ),
            )

        logger.warning(
            "No BillingCheckoutAttempt found for notes.attempt_id=%s or subscription %s; staging reconciliation.",
            notes_attempt_uuid,
            sub_id,
        )
        await upsert_payment_reconciliation(
            db=db,
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            case_key=f"missing_checkout_attempt:{sub_id}",
            reason="missing_checkout_attempt",
            organization_id=org_id,
            subscription_id=sub.id if sub is not None else None,
            raw_event_id=real_event_id,
            diagnostic_data=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason="missing_checkout_attempt",
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "provider_customer_id": sub_entity.customer_id,
                    "provider_plan_id": sub_entity.plan_id,
                },
            ),
            now=ctx.event_created_at,
        )
        return None

    # Path 2: Fallback correlation via scoped provider_subscription_id
    stmt_attempt = (
        select(BillingCheckoutAttempt)
        .where(
            BillingCheckoutAttempt.provider == "razorpay",
            BillingCheckoutAttempt.merchant_account_id == ctx.merchant_account_id,
            BillingCheckoutAttempt.environment == ctx.environment,
            BillingCheckoutAttempt.provider_subscription_id == sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_attempt = await db.execute(stmt_attempt)
    attempts = list(res_attempt.scalars().all())

    if len(attempts) > 1:
        logger.warning(
            "Ambiguous checkout attempts for subscription %s in %s: found %d matching attempts.",
            sub_id,
            event_type_label,
            len(attempts),
        )
        reason = "ambiguous_checkout_attempt"
        raise RazorpayWebhookReconciliationConflictError(
            f"Multiple checkout attempts found for subscription {sub_id} ({len(attempts)} attempts).",
            case_key=f"ambiguous_checkout_attempt:{sub_id}",
            reason=reason,
            organization_id=org_id,
            raw_event_id=real_event_id,
            diagnostic_payload=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason=reason,
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "attempt_count": len(attempts),
                    "attempt_ids": [str(a.id) for a in attempts],
                },
            ),
        )

    if len(attempts) == 0:
        logger.warning(
            "No BillingCheckoutAttempt found for subscription %s in %s; staging reconciliation case.",
            sub_id,
            event_type_label,
        )
        await upsert_payment_reconciliation(
            db=db,
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            case_key=f"missing_checkout_attempt:{sub_id}",
            reason="missing_checkout_attempt",
            organization_id=org_id,
            subscription_id=sub.id if sub is not None else None,
            raw_event_id=real_event_id,
            diagnostic_data=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason="missing_checkout_attempt",
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "provider_customer_id": sub_entity.customer_id,
                    "provider_plan_id": sub_entity.plan_id,
                },
            ),
            now=ctx.event_created_at,
        )
        return None

    attempt = attempts[0]
    _validate_correlated_attempt_ownership_and_scope(
        attempt=attempt,
        ctx=ctx,
        org_id=org_id,
        sub_id=sub_id,
        event_type_label=event_type_label,
        notes_attempt_id_raw=notes_attempt_id_raw,
        real_event_id=real_event_id,
        real_provider_event_id=real_provider_event_id,
    )
    return attempt


async def _apply_checkout_attempt_transition(
    *,
    db: AsyncSession,
    ctx: RazorpayEventContext,
    event_or_payload: ClaimedWebhookEvent | BillingWebhookEvent | dict[str, Any],
    attempt: BillingCheckoutAttempt,
    org_id: uuid.UUID,
    sub_id: str,
    sub_entity: RazorpaySubscriptionEntity,
    sub: Subscription | None,
    event_type_label: str,
    target_status: CheckoutAttemptStatus,
) -> None:
    """Safely apply checkout-attempt state transition while enforcing terminal-state immutability.

    Guarantees:
    - Terminal `expired` and `failed` attempts are NEVER resurrected or rebound.
    - Nonterminal or `complete` attempts already bound to a different provider_subscription_id
      are NEVER rebound (fail closed with RazorpayWebhookReconciliationConflictError).
    - Valid nonterminal attempts advance monotonically and idempotently.
    """
    notes_attempt_id_raw, _ = _parse_notes_attempt_id(sub_entity.notes)
    real_event_id, real_provider_event_id = _extract_event_audit_identifiers(
        event_or_payload, ctx
    )
    bound_sub_id = (
        attempt.provider_subscription_id.strip()
        if isinstance(attempt.provider_subscription_id, str)
        and attempt.provider_subscription_id.strip()
        else None
    )

    # 1. Terminal-state historical immutability guard (expired / failed)
    if attempt.status in _TERMINAL_UNSUCCESSFUL_ATTEMPT_STATUSES:
        reason = (
            "activation_on_terminal_attempt"
            if target_status == CheckoutAttemptStatus.complete
            else "authentication_on_terminal_attempt"
        )
        case_key = f"{reason}:{attempt.id}:{sub_id}"
        sub_id_mismatch = bound_sub_id is not None and bound_sub_id != sub_id
        logger.warning(
            "Refusing to resurrect or mutate terminal checkout attempt %s (status=%s, bound_sub=%s) "
            "on %s for subscription %s; staging reconciliation %s.",
            attempt.id,
            attempt.status,
            bound_sub_id,
            event_type_label,
            sub_id,
            case_key,
        )
        await upsert_payment_reconciliation(
            db=db,
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            case_key=case_key,
            reason=reason,
            organization_id=org_id,
            subscription_id=sub.id if sub is not None else None,
            raw_event_id=real_event_id,
            diagnostic_data=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason=reason,
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "attempt_id": str(attempt.id),
                    "attempt_status": attempt.status.value
                    if isinstance(attempt.status, CheckoutAttemptStatus)
                    else str(attempt.status),
                    "attempt_provider_subscription_id": bound_sub_id,
                    "provider_subscription_id_mismatch": sub_id_mismatch,
                },
            ),
            now=ctx.event_created_at,
        )
        return

    # 2. Prevent rebinding an attempt already bound to a different provider subscription ID
    if bound_sub_id is not None and bound_sub_id != sub_id:
        reason = "checkout_attempt_subscription_mismatch"
        logger.warning(
            "Refusing to rebind checkout attempt %s from subscription %s to %s on %s.",
            attempt.id,
            bound_sub_id,
            sub_id,
            event_type_label,
        )
        raise RazorpayWebhookReconciliationConflictError(
            f"Checkout attempt {attempt.id} is already bound to subscription {bound_sub_id} "
            f"and cannot be rebound to {sub_id}.",
            case_key=f"checkout_attempt_subscription_mismatch:{attempt.id}:{sub_id}",
            reason=reason,
            organization_id=org_id,
            raw_event_id=real_event_id,
            diagnostic_payload=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type=event_type_label,
                reason=reason,
                notes_attempt_id=notes_attempt_id_raw,
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "attempt_id": str(attempt.id),
                    "attempt_status": attempt.status.value
                    if isinstance(attempt.status, CheckoutAttemptStatus)
                    else str(attempt.status),
                    "attempt_provider_subscription_id": bound_sub_id,
                },
            ),
        )

    # 3. Valid monotonic transition
    if target_status == CheckoutAttemptStatus.payment_pending:
        if attempt.status in _AUTHENTICATABLE_ATTEMPT_STATUSES:
            attempt.status = CheckoutAttemptStatus.payment_pending
            if bound_sub_id is None:
                attempt.provider_subscription_id = sub_id
            if sub_entity.customer_id and not attempt.provider_customer_id:
                attempt.provider_customer_id = sub_entity.customer_id
        elif attempt.status == CheckoutAttemptStatus.payment_pending:
            if bound_sub_id is None:
                attempt.provider_subscription_id = sub_id
            if sub_entity.customer_id and not attempt.provider_customer_id:
                attempt.provider_customer_id = sub_entity.customer_id
        # If attempt.status == CheckoutAttemptStatus.complete: idempotent no-op (no regression)
    elif target_status == CheckoutAttemptStatus.complete:
        if attempt.status in _ACTIVATABLE_ATTEMPT_STATUSES:
            attempt.status = CheckoutAttemptStatus.complete
            if bound_sub_id is None:
                attempt.provider_subscription_id = sub_id
            if sub_entity.customer_id and not attempt.provider_customer_id:
                attempt.provider_customer_id = sub_entity.customer_id
        # If attempt.status == CheckoutAttemptStatus.complete: idempotent no-op


async def _assert_no_conflicting_active_subscription(
    *,
    db: AsyncSession,
    ctx: RazorpayEventContext,
    event_or_payload: ClaimedWebhookEvent | BillingWebhookEvent | dict[str, Any],
    org_id: uuid.UUID,
    sub_id: str,
    sub_entity: RazorpaySubscriptionEntity,
    current_sub: Subscription | None,
    event_type_label: str,
) -> None:
    """Verify under Level 0A organization advisory lock that activating this subscription
    will not conflict with the global uq_org_active_subscription invariant (status IN ('active', 'trialing')).

    Must ONLY be invoked after acquire_advisory_lock(db, "organization", org_id).
    Never performs external network calls and never catches arbitrary IntegrityError.
    """
    conditions = [
        Subscription.organization_id == org_id,
        Subscription.status.in_(_ACTIVE_INVARIANT_SUBSCRIPTION_STATUSES),
    ]
    if current_sub is not None:
        conditions.append(Subscription.id != current_sub.id)

    stmt_conflict = (
        select(Subscription)
        .where(*conditions)
        .order_by(Subscription.id.asc())
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_conflict = await db.execute(stmt_conflict)
    candidates = list(res_conflict.scalars().all())

    conflicting: Subscription | None = None
    for candidate in candidates:
        if candidate.organization_id != org_id:
            continue
        if candidate.status not in _ACTIVE_INVARIANT_SUBSCRIPTION_STATUSES:
            continue
        if current_sub is not None and candidate.id == current_sub.id:
            continue
        conflicting = candidate
        break

    if conflicting is None:
        return

    real_event_id, real_provider_event_id = _extract_event_audit_identifiers(
        event_or_payload, ctx
    )
    notes_attempt_id_raw, _ = _parse_notes_attempt_id(sub_entity.notes)
    conflicting_status = (
        conflicting.status.value
        if isinstance(conflicting.status, SubscriptionStatus)
        else str(conflicting.status)
    )
    reason = "duplicate_active_subscription"
    case_key = f"duplicate_active_subscription:{org_id}:{sub_id}"

    logger.warning(
        "Blocking conflicting subscription activation for organization %s: incoming %s "
        "conflicts with existing %s (provider=%s, provider_sub=%s, status=%s).",
        org_id,
        sub_id,
        conflicting.id,
        conflicting.provider,
        conflicting.provider_subscription_id,
        conflicting_status,
    )
    raise RazorpayWebhookReconciliationConflictError(
        f"Cannot activate subscription {sub_id} for organization {org_id}: "
        f"organization already has active subscription {conflicting.id} "
        f"({conflicting.provider_subscription_id}, status={conflicting_status}).",
        case_key=case_key,
        reason=reason,
        organization_id=org_id,
        raw_event_id=real_event_id,
        diagnostic_payload=_build_reconciliation_diagnostics(
            ctx=ctx,
            org_id=org_id,
            sub_id=sub_id,
            event_type=event_type_label,
            reason=reason,
            notes_attempt_id=notes_attempt_id_raw,
            real_event_id=real_event_id,
            real_provider_event_id=real_provider_event_id,
            extra={
                "existing_active_subscription_id": str(conflicting.id),
                "existing_provider": conflicting.provider,
                "existing_provider_subscription_id": conflicting.provider_subscription_id,
                "existing_merchant_account_id": conflicting.merchant_account_id,
                "existing_environment": conflicting.environment,
                "existing_status": conflicting_status,
                "incoming_subscription_id": str(current_sub.id)
                if current_sub is not None
                else None,
                "incoming_provider": "razorpay",
                "incoming_provider_subscription_id": sub_id,
                "incoming_merchant_account_id": ctx.merchant_account_id,
                "incoming_environment": ctx.environment,
            },
        ),
    )


# =============================================================================
# 1. subscription.authenticated
# =============================================================================


async def handle_subscription_authenticated(
    event_or_payload: ClaimedWebhookEvent | dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle Razorpay subscription.authenticated webhook event.

    Trigger: Mandate authenticated by customer during checkout.
    Effects:
    - Binds subscription to tenant organization.
    - Updates local Subscription record to 'incomplete'.
    - Updates BillingCheckoutAttempt to 'payment_pending'.
    - Invariants: plan_tier remains 'free', zero Pro quota, zero billing periods created.
    """
    ctx = validate_razorpay_event_envelope(event_or_payload)
    sub_id = extract_subscription_id(ctx.raw_payload)
    sub_entity = extract_subscription_entity(ctx.raw_payload)

    # 1. Stale lease guard inside domain transaction
    if ctx.lease_token and ctx.locked_by:
        await assert_valid_worker_lease(
            db=db,
            event_id=ctx.event_id,
            expected_lease_token=ctx.lease_token,
            expected_worker_id=ctx.locked_by,
            now=ctx.worker_now,
        )

    # 2. Level 0 Advisory Lock on provider subscription identity
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 3. Resolve tenant organization from authoritative records
    org_id = await resolve_tenant_organization(
        db=db,
        provider="razorpay",
        merchant_account_id=ctx.merchant_account_id,
        environment=ctx.environment,
        customer_id=sub_entity.customer_id,
        subscription_id=sub_id,
        notes=sub_entity.notes,
    )

    # 4. Level 2 Lock on Subscription record
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == ctx.merchant_account_id,
            Subscription.environment == ctx.environment,
            Subscription.provider_subscription_id == sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    period_start = (
        parse_positive_unix_timestamp(sub_entity.current_start, "current_start")
        if sub_entity.current_start
        else None
    )
    period_end = (
        parse_positive_unix_timestamp(sub_entity.current_end, "current_end")
        if sub_entity.current_end
        else None
    )

    if sub is not None:
        _assert_subscription_environment_immutable(sub, ctx.environment)

        # Check monotonicity
        if is_stale_event(sub.last_webhook_event_at, ctx.event_created_at):
            logger.debug(
                "Ignoring stale subscription.authenticated event for %s (event_at=%s, local_at=%s)",
                sub_id,
                ctx.event_created_at.isoformat(),
                sub.last_webhook_event_at.isoformat() if sub.last_webhook_event_at else None,
            )
            return

        # Refusal of resurrection
        if is_terminal_subscription(sub.status):
            logger.info(
                "Refusing resurrection: subscription.authenticated received for terminal subscription %s (status=%s)",
                sub_id,
                sub.status,
            )
            return

        # Active or past_due subscription receives delayed authentication event
        if sub.status in (SubscriptionStatus.active, SubscriptionStatus.past_due):
            logger.info(
                "Subscription %s is already in state %s; ignoring status regression to incomplete.",
                sub_id,
                sub.status,
            )
            return

        # Update metadata for incomplete subscription
        if sub.status in (SubscriptionStatus.incomplete, SubscriptionStatus.incomplete_expired):
            sub.status = SubscriptionStatus.incomplete
            sub.provider_plan_id = sub_entity.plan_id
            if period_start and not sub.current_period_start:
                sub.current_period_start = period_start
            if period_end and not sub.current_period_end:
                sub.current_period_end = period_end
            sub.last_webhook_event_at = ctx.event_created_at
    else:
        # Create new incomplete subscription (Strictly plan_tier='free')
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            provider_customer_id=sub_entity.customer_id or f"cust_unknown_{sub_id}",
            provider_subscription_id=sub_id,
            provider_plan_id=sub_entity.plan_id,
            status=SubscriptionStatus.incomplete,
            plan_tier="free",
            current_period_start=period_start,
            current_period_end=period_end,
            last_webhook_event_at=ctx.event_created_at,
        )
        db.add(sub)

    # 5. Level 3: Deterministically correlate & transition BillingCheckoutAttempt to payment_pending
    attempt = await _correlate_checkout_attempt(
        db=db,
        ctx=ctx,
        event_or_payload=event_or_payload,
        org_id=org_id,
        sub_id=sub_id,
        sub_entity=sub_entity,
        sub=sub,
        event_type_label="subscription.authenticated",
    )
    if attempt is not None:
        await _apply_checkout_attempt_transition(
            db=db,
            ctx=ctx,
            event_or_payload=event_or_payload,
            attempt=attempt,
            org_id=org_id,
            sub_id=sub_id,
            sub_entity=sub_entity,
            sub=sub,
            event_type_label="subscription.authenticated",
            target_status=CheckoutAttemptStatus.payment_pending,
        )

    await db.flush()
    logger.info("Successfully processed subscription.authenticated for %s", sub_id)


# =============================================================================
# 2. subscription.activated
# =============================================================================


async def handle_subscription_activated(
    event_or_payload: ClaimedWebhookEvent | dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle Razorpay subscription.activated webhook event.

    Trigger: Razorpay confirms recurring mandate and transitions subscription to active.
    Effects:
    - Transitions Subscription.status to 'active'.
    - Populates verified period boundary dates.
    - Transitions BillingCheckoutAttempt to 'complete'.
    - Refuses resurrection of terminal (canceled) subscriptions.
    - CRITICAL INVARIANT: plan_tier remains 'free'. Zero Pro quota granted.
    - CRITICAL INVARIANT: past_due_since is NEVER cleared by activation alone.
    """
    ctx = validate_razorpay_event_envelope(event_or_payload)
    sub_id = extract_subscription_id(ctx.raw_payload)
    sub_entity = extract_subscription_entity(ctx.raw_payload)

    # 1. Stale lease guard inside domain transaction
    if ctx.lease_token and ctx.locked_by:
        await assert_valid_worker_lease(
            db=db,
            event_id=ctx.event_id,
            expected_lease_token=ctx.lease_token,
            expected_worker_id=ctx.locked_by,
            now=ctx.worker_now,
        )

    # 2. Resolve tenant organization (read-only authoritative lookups; zero domain row locks)
    org_id = await resolve_tenant_organization(
        db=db,
        provider="razorpay",
        merchant_account_id=ctx.merchant_account_id,
        environment=ctx.environment,
        customer_id=sub_entity.customer_id,
        subscription_id=sub_id,
        notes=sub_entity.notes,
    )

    # 3. Level 0A Organization Advisory Lock (canonical ADR-005 lock before Level 0B & row locks)
    await acquire_advisory_lock(db, "organization", org_id)

    # 4. Level 0B Entity Advisory Lock on provider subscription identity
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 5. Level 2 Lock on Subscription record
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == ctx.merchant_account_id,
            Subscription.environment == ctx.environment,
            Subscription.provider_subscription_id == sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    period_start = (
        parse_positive_unix_timestamp(sub_entity.current_start, "current_start")
        if sub_entity.current_start
        else None
    )
    period_end = (
        parse_positive_unix_timestamp(sub_entity.current_end, "current_end")
        if sub_entity.current_end
        else None
    )

    if sub is not None:
        _assert_subscription_environment_immutable(sub, ctx.environment)

        # Check monotonicity
        if is_stale_event(sub.last_webhook_event_at, ctx.event_created_at):
            logger.debug(
                "Ignoring stale subscription.activated event for %s (event_at=%s, local_at=%s)",
                sub_id,
                ctx.event_created_at.isoformat(),
                sub.last_webhook_event_at.isoformat() if sub.last_webhook_event_at else None,
            )
            return

        # Refusal of resurrection
        if is_terminal_subscription(sub.status):
            logger.warning(
                "Refusing resurrection: subscription.activated received for terminal subscription %s (status=%s)",
                sub_id,
                sub.status,
            )
            return

        # Past-due handling: activation CANNOT cure past_due!
        if sub.status == SubscriptionStatus.past_due:
            logger.warning(
                "Subscription %s is past_due; subscription.activated cannot cure grace period. "
                "Leaving past_due_since anchor intact.",
                sub_id,
            )
            # Update dates and timestamps without regressing status or clearing past_due_since
            if period_start:
                sub.current_period_start = period_start
            if period_end:
                sub.current_period_end = period_end
            sub.last_webhook_event_at = ctx.event_created_at
            await db.flush()
            return

        # Valid state transition: incomplete/incomplete_expired/paused -> active
        # Pre-activation conflict check under Level 0A org advisory lock BEFORE mutating status
        if sub.status in (
            SubscriptionStatus.incomplete,
            SubscriptionStatus.incomplete_expired,
            SubscriptionStatus.paused,
        ):
            await _assert_no_conflicting_active_subscription(
                db=db,
                ctx=ctx,
                event_or_payload=event_or_payload,
                org_id=org_id,
                sub_id=sub_id,
                sub_entity=sub_entity,
                current_sub=sub,
                event_type_label="subscription.activated",
            )
            sub.status = SubscriptionStatus.active

        # Idempotent replay on active status
        if period_start:
            sub.current_period_start = period_start
        if period_end:
            sub.current_period_end = period_end
        sub.provider_plan_id = sub_entity.plan_id
        sub.last_webhook_event_at = ctx.event_created_at
    else:
        # Pre-activation conflict check under Level 0A org advisory lock BEFORE inserting active sub
        await _assert_no_conflicting_active_subscription(
            db=db,
            ctx=ctx,
            event_or_payload=event_or_payload,
            org_id=org_id,
            sub_id=sub_id,
            sub_entity=sub_entity,
            current_sub=None,
            event_type_label="subscription.activated",
        )
        # Create active subscription record if none exists (Strictly plan_tier='free')
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            provider_customer_id=sub_entity.customer_id or f"cust_unknown_{sub_id}",
            provider_subscription_id=sub_id,
            provider_plan_id=sub_entity.plan_id,
            status=SubscriptionStatus.active,
            plan_tier="free",  # STRICT INVARIANT: ZERO Pro entitlement from activation!
            current_period_start=period_start,
            current_period_end=period_end,
            last_webhook_event_at=ctx.event_created_at,
        )
        db.add(sub)

    # 6. Level 3: Deterministically correlate & transition BillingCheckoutAttempt to complete
    attempt = await _correlate_checkout_attempt(
        db=db,
        ctx=ctx,
        event_or_payload=event_or_payload,
        org_id=org_id,
        sub_id=sub_id,
        sub_entity=sub_entity,
        sub=sub,
        event_type_label="subscription.activated",
    )
    if attempt is not None:
        await _apply_checkout_attempt_transition(
            db=db,
            ctx=ctx,
            event_or_payload=event_or_payload,
            attempt=attempt,
            org_id=org_id,
            sub_id=sub_id,
            sub_entity=sub_entity,
            sub=sub,
            event_type_label="subscription.activated",
            target_status=CheckoutAttemptStatus.complete,
        )

    await db.flush()
    logger.info("Successfully processed subscription.activated for %s", sub_id)


# =============================================================================
# 3. subscription.updated
# =============================================================================


async def handle_subscription_updated(
    event_or_payload: ClaimedWebhookEvent | dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle Razorpay subscription.updated webhook event.

    Trigger: Plan changed, schedule modified, or payment method updated.
    Effects:
    - Applies supported changes monotonically.
    - Protects against stale out-of-order updates.
    - Refuses resurrection of terminal subscriptions.
    - Does NOT infer payment success or clear past_due_since.
    """
    ctx = validate_razorpay_event_envelope(event_or_payload)
    sub_id = extract_subscription_id(ctx.raw_payload)
    sub_entity = extract_subscription_entity(ctx.raw_payload)

    # 1. Stale lease guard
    if ctx.lease_token and ctx.locked_by:
        await assert_valid_worker_lease(
            db=db,
            event_id=ctx.event_id,
            expected_lease_token=ctx.lease_token,
            expected_worker_id=ctx.locked_by,
            now=ctx.worker_now,
        )

    # 2. Resolve tenant organization (read-only authoritative lookups; zero domain row locks)
    org_id = await resolve_tenant_organization(
        db=db,
        provider="razorpay",
        merchant_account_id=ctx.merchant_account_id,
        environment=ctx.environment,
        customer_id=sub_entity.customer_id,
        subscription_id=sub_id,
        notes=sub_entity.notes,
    )

    # 3. Advisory Locks (ADR-005): acquire Level 0A before Level 0B when update may activate
    if sub_entity.status == "active":
        await acquire_advisory_lock(db, "organization", org_id)
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 4. Level 2 Lock on Subscription record
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == ctx.merchant_account_id,
            Subscription.environment == ctx.environment,
            Subscription.provider_subscription_id == sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    if sub is None:
        logger.warning(
            "Subscription %s not found during subscription.updated; staging reconciliation.",
            sub_id,
        )
        real_event_id, real_provider_event_id = _extract_event_audit_identifiers(
            event_or_payload, ctx
        )
        await upsert_payment_reconciliation(
            db=db,
            case_key=f"unknown_sub_updated:{sub_id}",
            reason="unknown_subscription_updated",
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            organization_id=org_id,
            subscription_id=None,
            raw_event_id=real_event_id,
            diagnostic_data=_build_reconciliation_diagnostics(
                ctx=ctx,
                org_id=org_id,
                sub_id=sub_id,
                event_type="subscription.updated",
                reason="unknown_subscription_updated",
                real_event_id=real_event_id,
                real_provider_event_id=real_provider_event_id,
                extra={
                    "provider_plan_id": sub_entity.plan_id,
                    "provider_customer_id": sub_entity.customer_id,
                    "subscription_status": sub_entity.status,
                },
            ),
            now=ctx.event_created_at,
        )
        return

    _assert_subscription_environment_immutable(sub, ctx.environment)

    # Check monotonicity
    if is_stale_event(sub.last_webhook_event_at, ctx.event_created_at):
        logger.debug(
            "Ignoring stale subscription.updated event for %s (event_at=%s, local_at=%s)",
            sub_id,
            ctx.event_created_at.isoformat(),
            sub.last_webhook_event_at.isoformat() if sub.last_webhook_event_at else None,
        )
        return

    # Refusal of resurrection
    if is_terminal_subscription(sub.status):
        logger.warning(
            "Refusing resurrection: subscription.updated received for terminal subscription %s",
            sub_id,
        )
        return

    # Pre-activation conflict check BEFORE mutating subscription if transitioning to active
    will_activate = sub_entity.status == "active" and sub.status in (
        SubscriptionStatus.incomplete,
        SubscriptionStatus.paused,
    )
    if will_activate:
        await _assert_no_conflicting_active_subscription(
            db=db,
            ctx=ctx,
            event_or_payload=event_or_payload,
            org_id=org_id,
            sub_id=sub_id,
            sub_entity=sub_entity,
            current_sub=sub,
            event_type_label="subscription.updated",
        )

    # Apply supported updates
    if sub_entity.plan_id:
        sub.provider_plan_id = sub_entity.plan_id

    if sub_entity.current_start:
        sub.current_period_start = parse_positive_unix_timestamp(
            sub_entity.current_start, "current_start"
        )
    if sub_entity.current_end:
        sub.current_period_end = parse_positive_unix_timestamp(
            sub_entity.current_end, "current_end"
        )

    # Status handling (careful: never infer payment success!)
    if sub_entity.status == "paused":
        sub.status = SubscriptionStatus.paused
    elif will_activate:
        sub.status = SubscriptionStatus.active
        # If local is past_due, do NOT overwrite to active on subscription.updated!

    sub.last_webhook_event_at = ctx.event_created_at
    await db.flush()
    logger.info("Successfully processed subscription.updated for %s", sub_id)


# =============================================================================
# 4. subscription.completed
# =============================================================================


async def handle_subscription_completed(
    event_or_payload: ClaimedWebhookEvent | dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle Razorpay subscription.completed webhook event.

    Trigger: All planned cycles completed (total_count reached).
    Effects:
    - Transitions Subscription.status to 'canceled'.
    - Sets plan_tier = 'free'.
    - Records canceled_at = event_created_at.
    - Does NOT mutate OrganizationBillingPeriod or quota accounting directly.
    """
    ctx = validate_razorpay_event_envelope(event_or_payload)
    sub_id = extract_subscription_id(ctx.raw_payload)

    # 1. Stale lease guard
    if ctx.lease_token and ctx.locked_by:
        await assert_valid_worker_lease(
            db=db,
            event_id=ctx.event_id,
            expected_lease_token=ctx.lease_token,
            expected_worker_id=ctx.locked_by,
            now=ctx.worker_now,
        )

    # 2. Level 0 Advisory Lock
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 3. Resolve tenant organization
    org_id = await resolve_tenant_organization(
        db=db,
        provider="razorpay",
        merchant_account_id=ctx.merchant_account_id,
        environment=ctx.environment,
        subscription_id=sub_id,
        notes=ctx.raw_payload.get("notes"),
    )

    # 4. Level 2 Lock on Subscription record
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == ctx.merchant_account_id,
            Subscription.environment == ctx.environment,
            Subscription.provider_subscription_id == sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    if sub is None:
        logger.warning(
            "Subscription %s not found during subscription.completed; staging reconciliation.",
            sub_id,
        )
        await upsert_payment_reconciliation(
            db=db,
            case_key=f"unknown_sub_completed:{sub_id}",
            reason="unknown_subscription_completed",
            provider="razorpay",
            merchant_account_id=ctx.merchant_account_id,
            environment=ctx.environment,
            organization_id=org_id,
            subscription_id=None,
            raw_event_id=ctx.event_id,
            diagnostic_data={
                "provider_subscription_id": sub_id,
                "organization_id": str(org_id) if org_id else None,
                "event_type": "subscription.completed",
                "provider_event_id": ctx.provider_event_id,
            },
            now=ctx.event_created_at,
        )
        return

    _assert_subscription_environment_immutable(sub, ctx.environment)

    # Check monotonicity
    if is_stale_event(sub.last_webhook_event_at, ctx.event_created_at):
        logger.debug(
            "Ignoring stale subscription.completed event for %s (event_at=%s, local_at=%s)",
            sub_id,
            ctx.event_created_at.isoformat(),
            sub.last_webhook_event_at.isoformat() if sub.last_webhook_event_at else None,
        )
        return

    # Idempotent terminal transition
    if sub.status == SubscriptionStatus.canceled:
        logger.info("Subscription %s is already canceled; completing idempotently.", sub_id)
        return

    # Complete subscription: demote to Free and set status to canceled
    sub.status = SubscriptionStatus.canceled
    sub.plan_tier = "free"
    sub.canceled_at = ctx.event_created_at
    sub.last_webhook_event_at = ctx.event_created_at

    await db.flush()
    logger.info("Successfully processed subscription.completed for %s", sub_id)


# =============================================================================
# Handler Registration Helper
# =============================================================================


def register_razorpay_agreement_handlers(
    dispatcher: RazorpayWebhookDispatcher | None = None,
) -> None:
    """Register all four Razorpay subscription agreement handlers into the dispatcher."""
    target = dispatcher or default_razorpay_dispatcher
    target.register("subscription.authenticated", handle_subscription_authenticated)
    target.register("subscription.activated", handle_subscription_activated)
    target.register("subscription.updated", handle_subscription_updated)
    target.register("subscription.completed", handle_subscription_completed)


# Auto-register into the default module dispatcher upon import
register_razorpay_agreement_handlers()
