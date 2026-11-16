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

Locking & Concurrency Invariants:
- Level 0: pg_advisory_xact_lock("subscription", sub_id)
- Level 2: Subscription row lock (SELECT ... FOR UPDATE)
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
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.razorpay.handler_base import (
    ClaimedWebhookEvent,
    RazorpayEventContext,
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

    # 5. Transition BillingCheckoutAttempt to payment_pending
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
            "Ambiguous checkout attempts for subscription %s: found %d matching attempts.",
            sub_id,
            len(attempts),
        )
        raise RazorpayWebhookReconciliationConflictError(
            f"Multiple checkout attempts found for subscription {sub_id} ({len(attempts)} attempts).",
            case_key=f"ambiguous_checkout_attempt:{sub_id}",
            reason="ambiguous_checkout_attempt",
            organization_id=org_id,
            raw_event_id=ctx.event_id,
            diagnostic_payload={
                "provider_subscription_id": sub_id,
                "attempt_count": len(attempts),
                "attempt_ids": [str(a.id) for a in attempts],
                "organization_id": str(org_id),
                "event_id": str(ctx.event_id) if ctx.event_id else None,
            },
        )
    elif len(attempts) == 0:
        logger.warning(
            "No BillingCheckoutAttempt found for subscription %s; staging reconciliation case.",
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
            raw_event_id=ctx.event_id,
            diagnostic_data={
                "provider_subscription_id": sub_id,
                "provider_customer_id": sub_entity.customer_id,
                "provider_plan_id": sub_entity.plan_id,
                "organization_id": str(org_id),
                "event_id": str(ctx.event_id) if ctx.event_id else None,
                "event_type": "subscription.authenticated",
            },
            now=ctx.event_created_at,
        )
    else:
        attempt = attempts[0]
        if attempt.organization_id != org_id:
            logger.warning(
                "Checkout attempt tenant mismatch for subscription %s: attempt org %s != authoritative org %s",
                sub_id,
                attempt.organization_id,
                org_id,
            )
            raise RazorpayWebhookReconciliationConflictError(
                f"Checkout attempt {attempt.id} belongs to organization {attempt.organization_id}, "
                f"which conflicts with authoritative organization {org_id}.",
                case_key=f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_id}",
                reason="checkout_attempt_tenant_mismatch",
                organization_id=org_id,
                raw_event_id=ctx.event_id,
                diagnostic_payload={
                    "attempt_id": str(attempt.id),
                    "attempt_organization_id": str(attempt.organization_id),
                    "authoritative_organization_id": str(org_id),
                    "provider_subscription_id": sub_id,
                },
            )

        if attempt.status in (
            CheckoutAttemptStatus.creating,
            CheckoutAttemptStatus.open,
        ):
            attempt.status = CheckoutAttemptStatus.payment_pending
            if sub_entity.customer_id and not attempt.provider_customer_id:
                attempt.provider_customer_id = sub_entity.customer_id

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

    # 2. Level 0 Advisory Lock
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 3. Resolve tenant organization
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

        # Valid state transition: incomplete -> active
        if sub.status in (
            SubscriptionStatus.incomplete,
            SubscriptionStatus.incomplete_expired,
            SubscriptionStatus.paused,
        ):
            sub.status = SubscriptionStatus.active

        # Idempotent replay on active status
        if period_start:
            sub.current_period_start = period_start
        if period_end:
            sub.current_period_end = period_end
        sub.provider_plan_id = sub_entity.plan_id
        sub.last_webhook_event_at = ctx.event_created_at
    else:
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

    # 5. Transition BillingCheckoutAttempt to complete
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
            "Ambiguous checkout attempts for subscription %s in activation: found %d matching attempts.",
            sub_id,
            len(attempts),
        )
        raise RazorpayWebhookReconciliationConflictError(
            f"Multiple checkout attempts found for subscription {sub_id} ({len(attempts)} attempts).",
            case_key=f"ambiguous_checkout_attempt:{sub_id}",
            reason="ambiguous_checkout_attempt",
            organization_id=org_id,
            raw_event_id=ctx.event_id,
            diagnostic_payload={
                "provider_subscription_id": sub_id,
                "attempt_count": len(attempts),
                "attempt_ids": [str(a.id) for a in attempts],
                "organization_id": str(org_id),
                "event_id": str(ctx.event_id) if ctx.event_id else None,
            },
        )
    elif len(attempts) == 1:
        attempt = attempts[0]
        if attempt.organization_id != org_id:
            logger.warning(
                "Checkout attempt tenant mismatch for subscription %s: attempt org %s != authoritative org %s",
                sub_id,
                attempt.organization_id,
                org_id,
            )
            raise RazorpayWebhookReconciliationConflictError(
                f"Checkout attempt {attempt.id} belongs to organization {attempt.organization_id}, "
                f"which conflicts with authoritative organization {org_id}.",
                case_key=f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_id}",
                reason="checkout_attempt_tenant_mismatch",
                organization_id=org_id,
                raw_event_id=ctx.event_id,
                diagnostic_payload={
                    "attempt_id": str(attempt.id),
                    "attempt_organization_id": str(attempt.organization_id),
                    "authoritative_organization_id": str(org_id),
                    "provider_subscription_id": sub_id,
                },
            )

        if attempt.status != CheckoutAttemptStatus.complete:
            attempt.status = CheckoutAttemptStatus.complete
            if sub_entity.customer_id and not attempt.provider_customer_id:
                attempt.provider_customer_id = sub_entity.customer_id

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

    # 2. Level 0 Advisory Lock
    await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

    # 3. Resolve tenant organization
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

    if sub is None:
        logger.warning(
            "Subscription %s not found during subscription.updated; staging reconciliation.",
            sub_id,
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
            raw_event_id=ctx.event_id,
            diagnostic_data={
                "provider_subscription_id": sub_id,
                "provider_plan_id": sub_entity.plan_id,
                "provider_customer_id": sub_entity.customer_id,
                "subscription_status": sub_entity.status,
                "organization_id": str(org_id) if org_id else None,
                "event_type": "subscription.updated",
                "provider_event_id": ctx.provider_event_id,
            },
            now=ctx.event_created_at,
        )
        return

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
    elif sub_entity.status == "active":
        if sub.status in (SubscriptionStatus.incomplete, SubscriptionStatus.paused):
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
