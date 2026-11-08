"""Stripe Checkout Session Completion Webhook Handler.

Event: checkout.session.completed

Lifecycle Invariants & Security Guarantees:
1. Pure Local Verification: Validates server-controlled metadata, client references, session
   modes, customer IDs, and Pro price IDs against local configuration without making external
   network calls.
2. Anti-Tampering: Rejects arbitrary or unverified client data; enforces that the checkout attempt
   exists locally, matches the organization, and references the configured Pro price ID.
3. No Unverified Entitlements: Pro tier (`plan_tier="pro"`) is granted only if the session's
   authoritative payment status is "paid" or "no_payment_required" (with trialing status).
   If payment is pending ("unpaid"), the subscription remains "incomplete" with "free" tier.
4. Non-Duplication: Updates the existing local Subscription record bound to the customer/org
   rather than creating duplicate subscription rows.
5. Multi-Subscription Conflict Guard: If the organization already has an active subscription with
   a different Stripe subscription ID, the incoming conflict is recorded in the
   `billing_reconciliations` audit table and quarantined for administrative review without
   violating the partial unique index `uq_org_active_subscription`.
6. Checkout Attempt Synchronization: Updates the matching `StripeCheckoutAttempt` to "complete"
   (if paid) or "payment_pending" (if unpaid), linking `stripe_subscription_id`. Replayed
   deliveries do not corrupt completed attempts.
7. Atomic Transaction Boundary: Does not independently commit. All updates participate in the
   outer webhook processing engine transaction and savepoint (`begin_nested()`).
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.organization import Organization
from app.models.subscription import (
    CheckoutAttemptStatus,
    StripeCheckoutAttempt,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.stripe_checkout import _parse_stripe_timestamp
from app.services.billing.stripe_customer import get_stripe_customer_id
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
)

logger = logging.getLogger(__name__)


async def handle_checkout_session_completed(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle the 'checkout.session.completed' Stripe webhook event.

    Args:
        payload: Full verified Stripe webhook event dictionary.
        db: Active SQLAlchemy async session managed by the webhook processing engine.

    Raises:
        WebhookPermanentError: If the event payload fails cryptographic validation,
            references unknown organizations, mismatches local checkout attempts,
            or contains conflicting data requiring quarantine.
    """
    event_id = payload.get("id") or "unknown_event"
    event_created = payload.get("created")
    session = payload.get("data", {}).get("object")

    if not session or not isinstance(session, dict):
        raise WebhookPermanentError(
            f"Event {event_id} malformed: missing or invalid 'data.object' dictionary."
        )

    # =========================================================================
    # 1. Structural & Field Validations
    # =========================================================================

    session_id = session.get("id")
    if not session_id or not isinstance(session_id, str) or not session_id.startswith("cs_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid or missing checkout session ID '{session_id}'."
        )

    mode = session.get("mode")
    if mode != "subscription":
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid checkout session mode '{mode}'. Expected 'subscription'."
        )

    session_status = session.get("status")
    if session_status != "complete":
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout session status is '{session_status}'. Expected 'complete'."
        )

    customer_id = session.get("customer")
    if not customer_id or not isinstance(customer_id, str) or not customer_id.startswith("cus_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe customer ID '{customer_id}'."
        )

    raw_sub = session.get("subscription")
    if isinstance(raw_sub, str):
        subscription_id = raw_sub.strip()
    elif isinstance(raw_sub, dict):
        subscription_id = str(raw_sub.get("id") or "").strip()
    else:
        subscription_id = ""

    if not subscription_id or not subscription_id.startswith("sub_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe subscription ID in session: '{raw_sub}'."
        )

    # =========================================================================
    # 2. Organization Reference & Existence Validation
    # =========================================================================

    metadata = session.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    org_id_str = metadata.get("organization_id") or session.get("client_reference_id")
    if not org_id_str or not isinstance(org_id_str, str) or not org_id_str.strip():
        raise WebhookPermanentError(
            f"Event {event_id}: Missing organization reference in session metadata or client_reference_id."
        )

    try:
        org_id = uuid.UUID(org_id_str.strip())
    except (ValueError, TypeError) as exc:
        raise WebhookPermanentError(
            f"Event {event_id}: Malformed organization UUID '{org_id_str}': {exc}"
        ) from exc

    org = await db.get(Organization, org_id)
    if org is None:
        raise WebhookPermanentError(
            f"Event {event_id}: Referenced organization '{org_id}' not found in database."
        )

    # =========================================================================
    # 3. Local Checkout Attempt Matching & Anti-Tampering
    # =========================================================================

    attempt_id_str = metadata.get("attempt_id")
    attempt: StripeCheckoutAttempt | None = None

    # Primary lookup: match by stripe_session_id
    stmt_session = (
        select(StripeCheckoutAttempt)
        .where(StripeCheckoutAttempt.stripe_session_id == session_id)
        .with_for_update()
    )
    result_session = await db.execute(stmt_session)
    attempt = result_session.scalar_one_or_none()

    # Secondary fallback lookup: match by attempt_id if session_id was not yet populated
    if attempt is None and attempt_id_str:
        try:
            attempt_uuid = uuid.UUID(str(attempt_id_str).strip())
            stmt_attempt = (
                select(StripeCheckoutAttempt)
                .where(StripeCheckoutAttempt.id == attempt_uuid)
                .with_for_update()
            )
            result_attempt = await db.execute(stmt_attempt)
            attempt = result_attempt.scalar_one_or_none()
        except (ValueError, TypeError):
            pass

    if attempt is None:
        raise WebhookPermanentError(
            f"Event {event_id}: No matching local checkout attempt found for session '{session_id}'."
        )

    if attempt.organization_id != org_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt organization mismatch. "
            f"Attempt belongs to {attempt.organization_id}, session metadata claims {org_id}."
        )

    if attempt.stripe_session_id and attempt.stripe_session_id != session_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt session ID mismatch. "
            f"Attempt bound to '{attempt.stripe_session_id}', event delivered '{session_id}'."
        )

    # =========================================================================
    # 4. Customer Identity & Fail-Closed Price Verification
    # =========================================================================

    if attempt.stripe_customer_id != customer_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Customer ID mismatch. "
            f"Attempt was created for customer '{attempt.stripe_customer_id}', session claims '{customer_id}'."
        )

    existing_org_customer_id = await get_stripe_customer_id(db, org_id)
    if existing_org_customer_id and existing_org_customer_id != customer_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Customer ID mismatch. "
            f"Organization {org_id} is bound to customer '{existing_org_customer_id}', session claims '{customer_id}'."
        )

    configured_pro_price_id = (settings.stripe_pro_price_id or "").strip()
    if not configured_pro_price_id:
        raise WebhookPermanentError(
            f"Event {event_id}: STRIPE_PRO_PRICE_ID is not configured in server environment."
        )

    # Verify line items in server-persisted checkout attempt
    attempt_items = attempt.request_payload.get("line_items")
    if not isinstance(attempt_items, list) or not attempt_items:
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt '{attempt.id}' has missing or empty 'line_items' in request_payload."
        )

    attempt_first_item = attempt_items[0]
    if not isinstance(attempt_first_item, dict):
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt '{attempt.id}' line item is malformed."
        )

    attempt_price = attempt_first_item.get("price")
    if not attempt_price or not isinstance(attempt_price, str):
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt '{attempt.id}' has missing or invalid price ID '{attempt_price}'."
        )

    if attempt_price != configured_pro_price_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Checkout attempt price '{attempt_price}' does not match configured Pro price '{configured_pro_price_id}'."
        )

    # Verify line items in session payload if present (defense-in-depth)
    raw_line_items = session.get("line_items")
    if raw_line_items:
        session_items = raw_line_items.get("data", []) if isinstance(raw_line_items, dict) else raw_line_items
        if isinstance(session_items, list) and session_items:
            for item in session_items:
                price_obj = item.get("price") if isinstance(item, dict) else None
                price_id = price_obj.get("id") if isinstance(price_obj, dict) else str(price_obj or "")
                if price_id != configured_pro_price_id:
                    raise WebhookPermanentError(
                        f"Event {event_id}: Unexpected price ID '{price_id}' in session line items. "
                        f"Expected configured Pro price '{configured_pro_price_id}'."
                    )

    # =========================================================================
    # 5. Idempotent Replay Handling
    # =========================================================================

    # Check if this exact subscription is already reconciled and active
    stmt_existing_sub = (
        select(Subscription)
        .where(
            Subscription.stripe_subscription_id == subscription_id,
            Subscription.provider == "stripe",
        )
        .with_for_update()
    )
    result_existing_sub = await db.execute(stmt_existing_sub)
    existing_sub_by_id = result_existing_sub.scalar_one_or_none()

    if attempt.status == CheckoutAttemptStatus.complete and existing_sub_by_id is not None:
        if existing_sub_by_id.organization_id == org_id:
            logger.info(
                "Event %s: Checkout session %s already reconciled and complete for org %s. Skipping (idempotent replay).",
                event_id,
                session_id,
                org_id,
            )
            return

    # =========================================================================
    # 6. Active Subscription Conflict Guard (Reconciliation Ledger)
    # =========================================================================

    # Check if the organization already possesses an active/trialing subscription with a DIFFERENT ID
    stmt_active_sub = (
        select(Subscription)
        .where(
            Subscription.organization_id == org_id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]),
        )
        .with_for_update()
    )
    result_active_sub = await db.execute(stmt_active_sub)
    active_sub = result_active_sub.scalar_one_or_none()

    if active_sub is not None and active_sub.stripe_subscription_id != subscription_id:
        logger.warning(
            "Event %s: Organization %s already has active subscription '%s'. Conflict with incoming '%s'.",
            event_id,
            org_id,
            active_sub.stripe_subscription_id,
            subscription_id,
        )
        raise WebhookReconciliationConflictError(
            f"Organization {org_id} already has active subscription '{active_sub.stripe_subscription_id}'. "
            f"Incoming subscription '{subscription_id}' quarantined in billing reconciliation ledger.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id=active_sub.stripe_subscription_id or "unknown",
            incoming_subscription_id=subscription_id,
            reason="duplicate_active_subscription",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 7. Entitlement & Payment Status Decision Logic
    # =========================================================================

    payment_status = session.get("payment_status")
    now_utc = datetime.now(timezone.utc)
    event_timestamp = _parse_stripe_timestamp(event_created) if event_created else now_utc

    # Determine status and plan tier based strictly on verified payment state
    if payment_status == "paid":
        target_status = SubscriptionStatus.active
        target_plan = "pro"
        attempt_target_status = CheckoutAttemptStatus.complete
    elif payment_status == "no_payment_required":
        # Only grant Pro if checkout attempt explicitly proves that a valid server-authorized trial was configured
        sub_data = attempt.request_payload.get("subscription_data")
        is_trial_authorized = False

        if isinstance(sub_data, dict):
            trial_days = sub_data.get("trial_period_days")
            trial_end = sub_data.get("trial_end")

            # Validate trial_period_days: must be a positive integer (e.g. 14, 30)
            if trial_days is not None:
                if isinstance(trial_days, int) and not isinstance(trial_days, bool) and trial_days > 0:
                    is_trial_authorized = True
                else:
                    raise WebhookPermanentError(
                        f"Event {event_id}: Invalid trial_period_days '{trial_days}' in checkout attempt. "
                        f"Expected positive integer."
                    )

            # Validate trial_end: must be a future integer timestamp
            if trial_end is not None:
                if (
                    isinstance(trial_end, int)
                    and not isinstance(trial_end, bool)
                    and trial_end > int(now_utc.timestamp())
                ):
                    is_trial_authorized = True
                else:
                    raise WebhookPermanentError(
                        f"Event {event_id}: Invalid trial_end '{trial_end}' in checkout attempt. "
                        f"Expected future Unix timestamp."
                    )

        if not is_trial_authorized:
            raise WebhookPermanentError(
                f"Event {event_id}: Session payment_status is 'no_payment_required' but checkout attempt "
                f"does not authorize a valid trial."
            )
        target_status = SubscriptionStatus.trialing
        target_plan = "pro"
        attempt_target_status = CheckoutAttemptStatus.complete
    elif payment_status == "unpaid":
        logger.info(
            "Event %s: Session payment_status is 'unpaid'. Subscription marked incomplete, Pro tier not granted.",
            event_id,
        )
        target_status = SubscriptionStatus.incomplete
        target_plan = "free"
        attempt_target_status = CheckoutAttemptStatus.payment_pending
    else:
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid payment_status '{payment_status}' in checkout session."
        )

    # =========================================================================
    # 8. Subscription Persistence & Lifecycle Guards
    # =========================================================================

    # Locate existing subscription to update, or create a new one
    target_sub = existing_sub_by_id

    if target_sub is None:
        # Search by customer ID
        stmt_sub_by_cust = (
            select(Subscription)
            .where(
                Subscription.stripe_customer_id == customer_id,
                Subscription.provider == "stripe",
            )
            .with_for_update()
        )
        result_sub_by_cust = await db.execute(stmt_sub_by_cust)
        target_sub = result_sub_by_cust.scalar_one_or_none()

    if target_sub is None:
        # Search by organization ID (strictly Stripe provider, latest row)
        stmt_sub_by_org = (
            select(Subscription)
            .where(
                Subscription.organization_id == org_id,
                Subscription.provider == "stripe",
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        result_sub_by_org = await db.execute(stmt_sub_by_org)
        target_sub = result_sub_by_org.scalar_one_or_none()

    if target_sub is not None:
        # Verify ownership consistency
        if target_sub.organization_id != org_id:
            raise WebhookPermanentError(
                f"Event {event_id}: Existing subscription belongs to organization {target_sub.organization_id}, "
                f"cannot reconcile to organization {org_id}."
            )

        # Stale Event Timestamp Ordering Guard:
        # If subscription was already updated by a newer event, do not overwrite subscription state.
        if target_sub.last_stripe_event_created_at and event_timestamp <= target_sub.last_stripe_event_created_at:
            logger.info(
                "Event %s (timestamp=%s): Stale checkout event for subscription %s (current last_event=%s). Skipping.",
                event_id,
                event_timestamp,
                target_sub.stripe_subscription_id,
                target_sub.last_stripe_event_created_at,
            )
            # Advance attempt if needed, but do not touch subscription lifecycle
            if attempt.status != CheckoutAttemptStatus.complete and attempt_target_status == CheckoutAttemptStatus.complete:
                attempt.status = attempt_target_status
                attempt.stripe_subscription_id = subscription_id
                attempt.updated_at = now_utc
            return

        # Terminal / Delinquent Lifecycle Guard:
        # A checkout event for an already-canceled, past-due, or unpaid subscription must NEVER resurrect it!
        if target_sub.stripe_subscription_id == subscription_id and target_sub.status in (
            SubscriptionStatus.canceled,
            SubscriptionStatus.past_due,
            SubscriptionStatus.unpaid,
        ):
            logger.warning(
                "Event %s: Subscription %s is in state '%s'. Not resurrecting via checkout completion.",
                event_id,
                subscription_id,
                target_sub.status.value,
            )
            if attempt.status != CheckoutAttemptStatus.complete and attempt_target_status == CheckoutAttemptStatus.complete:
                attempt.status = attempt_target_status
                attempt.stripe_subscription_id = subscription_id
                attempt.updated_at = now_utc
            return

        # Legitimate update (new checkout or resubscription)
        target_sub.stripe_customer_id = customer_id
        target_sub.stripe_subscription_id = subscription_id
        # Note: Authoritative price and billing period dates belong to subsequent subscription/invoice handlers.
        # We do not fabricate period dates or represent the attempt payload as confirmed Stripe price.
        target_sub.status = target_status
        target_sub.plan_tier = target_plan
        target_sub.last_stripe_event_created_at = event_timestamp
        target_sub.updated_at = now_utc
    else:
        target_sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            stripe_price_id=None,
            status=target_status,
            plan_tier=target_plan,
            current_period_start=None,
            current_period_end=None,
            cancel_at_period_end=False,
            last_stripe_event_created_at=event_timestamp,
        )
        db.add(target_sub)

    # =========================================================================
    # 9. Checkout Attempt Synchronization
    # =========================================================================

    attempt.status = attempt_target_status
    attempt.stripe_session_id = session_id
    attempt.stripe_subscription_id = subscription_id
    attempt.error_message = None
    attempt.updated_at = now_utc

    logger.info(
        "Event %s: Reconciled checkout session %s for org %s -> sub=%s, status=%s, plan=%s, attempt=%s",
        event_id,
        session_id,
        org_id,
        subscription_id,
        target_status.value,
        target_plan,
        attempt.status.value,
    )


# Automatically register with default webhook dispatcher
default_webhook_dispatcher.register(
    "checkout.session.completed",
    handle_checkout_session_completed,
)
