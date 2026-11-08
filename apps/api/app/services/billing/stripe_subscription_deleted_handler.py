"""Stripe Customer Subscription Deleted Webhook Handler.

Event: customer.subscription.deleted

Architectural Guarantees & Lifecycle Invariants:
1. Strict Identity Binding: Authorizes subscription deletion strictly against existing
   locally persisted customer records (`stripe_customer_id`). Unknown customers without
   prior local associations fail closed with WebhookPermanentError (quarantine).
   Metadata alone never establishes an organization-to-customer binding.
2. Metadata Integrity: If `metadata.organization_id` is present, requires an exact UUID match
   with the customer's bound organization. Inconsistencies fail closed immediately.
3. Unbound Customer Protection: If a local customer-only Subscription row exists but its
   `stripe_subscription_id` is None, raises WebhookReconciliationConflictError
   (reason="unbound_customer_subscription_deleted") to stage a BillingReconciliation record
   and quarantine the event, refusing to bind unverified subscriptions.
4. Historical Superseded Subscription Protection: If the incoming subscription ID differs from
   the current local subscription ID:
   - Queries `StripeCheckoutAttempt` for a completed attempt (`status == complete`) matching
     `organization_id` and `stripe_subscription_id`.
   - If proven historical, safely acknowledges without modifying the current Subscription row.
   - If not found, raises WebhookReconciliationConflictError (reason="unknown_subscription_deleted").
   A deletion event for an old subscription must NEVER cancel a newer subscription.
5. Monotonic Timestamp Ordering & Stale Event Protection:
   - Strictly older event (`event_timestamp < last_stripe_event_created_at`): Complete no-op.
     Does not modify status, plan tier, cancellation timestamps, or updated_at.
   - Equal timestamp with non-terminal status (`active`, `past_due`): Raises
     WebhookReconciliationConflictError (reason="equal_timestamp_deletion_collision") to quarantine
     the ambiguous same-second collision for review.
   - Equal timestamp with terminal status (`canceled`): Idempotent acknowledgment; backfills
     `canceled_at` only if currently None.
6. Authoritative Fresh Cancellation:
   - Transitions `status = SubscriptionStatus.canceled`.
   - Immediately resets `plan_tier = "free"`.
   - Resets `cancel_at_period_end = False`.
   - Sets `canceled_at = final_canceled_at or event_timestamp`.
   - Sets `last_stripe_event_created_at = event_timestamp`.
   - Preserves `past_due_since` for audit trail.
7. Absolute Accounting & Period Isolation:
   - Does NOT create or modify OrganizationBillingPeriod rows.
   - Does NOT create or modify StripeInvoice records.
   - Does NOT mutate usage counters, tokens, or spending caps.
   - Does NOT alter in-flight UsageReservations.
   - Runtime authorization (`reserve_usage()`) immediately fails closed for canceled status.
8. Atomic Transaction Boundary: Never commits from within the handler. All database operations
   participate in the outer webhook processing engine's transaction and savepoint (`begin_nested()`).
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import (
    CheckoutAttemptStatus,
    StripeCheckoutAttempt,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.stripe_checkout import _parse_stripe_timestamp
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Helper Timestamp Validation Functions
# =============================================================================


def _validate_positive_timestamp(val: Any, field_name: str, event_id: str) -> datetime:
    """Validate that val is a genuine positive numeric Unix timestamp and convert to UTC datetime."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid or missing {field_name} timestamp '{val}'. "
            f"Expected positive integer."
        )
    if val <= 0:
        raise WebhookPermanentError(
            f"Event {event_id}: Timestamp {field_name} must be positive, got '{val}'."
        )
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise WebhookPermanentError(
            f"Event {event_id}: Malformed timestamp {field_name} '{val}': {exc}"
        ) from exc


def _parse_optional_positive_timestamp(val: Any, field_name: str, event_id: str) -> datetime | None:
    """Parse an optional timestamp field, returning None if absent or UTC datetime if valid."""
    if val is None:
        return None
    return _validate_positive_timestamp(val, field_name, event_id)


# =============================================================================
# Webhook Handler Implementation
# =============================================================================


async def handle_subscription_deleted(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle the 'customer.subscription.deleted' Stripe webhook event.

    Args:
        payload: Full verified Stripe webhook event dictionary.
        db: Active SQLAlchemy async session managed by the webhook processing engine.

    Raises:
        WebhookPermanentError: If the event payload fails structural validation,
            references unknown customers, contains metadata mismatches, or invalid statuses.
        WebhookReconciliationConflictError: If the subscription is unrecognized,
            an unbound customer receives a deletion, or an equal-timestamp collision occurs.
    """
    event_id = payload.get("id") or "unknown_event"
    event_type = payload.get("type")
    event_created = payload.get("created")
    sub_obj = payload.get("data", {}).get("object")

    if event_type != "customer.subscription.deleted":
        raise WebhookPermanentError(
            f"Event {event_id}: Unexpected event type '{event_type}'. Expected 'customer.subscription.deleted'."
        )

    if not sub_obj or not isinstance(sub_obj, dict):
        raise WebhookPermanentError(
            f"Event {event_id} malformed: missing or invalid 'data.object' dictionary."
        )

    # =========================================================================
    # 1. Structural & Field Validations
    # =========================================================================

    subscription_id = sub_obj.get("id")
    if not subscription_id or not isinstance(subscription_id, str) or not subscription_id.startswith("sub_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe subscription ID '{subscription_id}'."
        )

    customer_id = sub_obj.get("customer")
    if not customer_id or not isinstance(customer_id, str) or not customer_id.startswith("cus_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe customer ID '{customer_id}'."
        )

    raw_status = sub_obj.get("status")
    if not raw_status or not isinstance(raw_status, str) or raw_status.strip() != "canceled":
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid subscription status '{raw_status}'. Expected 'canceled'."
        )

    # Optional timestamps
    period_start_dt = _parse_optional_positive_timestamp(
        sub_obj.get("current_period_start"), "current_period_start", event_id
    )
    period_end_dt = _parse_optional_positive_timestamp(
        sub_obj.get("current_period_end"), "current_period_end", event_id
    )
    if period_start_dt and period_end_dt and period_end_dt < period_start_dt:
        raise WebhookPermanentError(
            f"Event {event_id}: current_period_end must not be before current_period_start."
        )

    canceled_at_dt = _parse_optional_positive_timestamp(
        sub_obj.get("canceled_at"), "canceled_at", event_id
    )
    ended_at_dt = _parse_optional_positive_timestamp(
        sub_obj.get("ended_at"), "ended_at", event_id
    )
    final_canceled_at = canceled_at_dt or ended_at_dt

    now_utc = datetime.now(timezone.utc)
    event_timestamp = _parse_stripe_timestamp(event_created) if event_created else now_utc

    # =========================================================================
    # 2. Strict Customer & Organization Identity Verification
    # =========================================================================

    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.stripe_customer_id == customer_id,
            Subscription.provider == "stripe",
        )
        .with_for_update()
    )
    result_sub = await db.execute(stmt_sub)
    target_sub = result_sub.scalar_one_or_none()

    if target_sub is None:
        raise WebhookPermanentError(
            f"Event {event_id}: Unknown Stripe customer '{customer_id}'. No local organization association exists."
        )

    # Organization metadata verification (if present, must match local row exactly)
    metadata = sub_obj.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise WebhookPermanentError(
            f"Event {event_id}: Malformed subscription metadata object."
        )

    if isinstance(metadata, dict):
        meta_org_id = metadata.get("organization_id")
        if meta_org_id is not None:
            if not isinstance(meta_org_id, str) or not meta_org_id.strip():
                raise WebhookPermanentError(
                    f"Event {event_id}: Malformed organization_id in metadata: '{meta_org_id}'."
                )
            try:
                parsed_org_id = uuid.UUID(meta_org_id.strip())
            except (ValueError, TypeError) as exc:
                raise WebhookPermanentError(
                    f"Event {event_id}: Invalid organization UUID in metadata '{meta_org_id}': {exc}"
                ) from exc

            if parsed_org_id != target_sub.organization_id:
                raise WebhookPermanentError(
                    f"Event {event_id}: Organization metadata mismatch. "
                    f"Customer bound to organization {target_sub.organization_id}, metadata claims {parsed_org_id}."
                )

    # Unbound customer check: local stripe_subscription_id is None
    if target_sub.stripe_subscription_id is None:
        logger.warning(
            "Event %s: Received deletion for subscription '%s' under customer '%s' "
            "(org %s) but local subscription has no active subscription ID.",
            event_id,
            subscription_id,
            customer_id,
            target_sub.organization_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received deletion for subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {target_sub.organization_id}) "
            f"but local subscription record has no active subscription ID.",
            organization_id=target_sub.organization_id,
            event_id=event_id,
            existing_subscription_id="none",
            incoming_subscription_id=subscription_id,
            reason="unbound_customer_subscription_deleted",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 3. Different Subscription ID (Superseded vs Unknown)
    # =========================================================================

    if target_sub.stripe_subscription_id != subscription_id:
        stmt_historical = (
            select(StripeCheckoutAttempt.id)
            .where(
                StripeCheckoutAttempt.organization_id == target_sub.organization_id,
                StripeCheckoutAttempt.stripe_subscription_id == subscription_id,
                StripeCheckoutAttempt.status == CheckoutAttemptStatus.complete,
            )
            .limit(1)
        )
        result_hist = await db.execute(stmt_historical)
        historical_attempt = result_hist.scalar_one_or_none()

        if historical_attempt is not None:
            logger.info(
                "Event %s: Deletion received for known superseded subscription '%s' "
                "(organization %s). Current subscription '%s' preserved.",
                event_id,
                subscription_id,
                target_sub.organization_id,
                target_sub.stripe_subscription_id,
            )
            return

        # Unknown subscription ID for known customer -> quarantine via reconciliation
        logger.warning(
            "Event %s: Deletion received for unknown subscription '%s' under customer '%s' "
            "(organization %s). Current subscription is '%s'.",
            event_id,
            subscription_id,
            customer_id,
            target_sub.organization_id,
            target_sub.stripe_subscription_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received deletion for unknown subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {target_sub.organization_id}). "
            f"Current subscription is '{target_sub.stripe_subscription_id}'.",
            organization_id=target_sub.organization_id,
            event_id=event_id,
            existing_subscription_id=target_sub.stripe_subscription_id,
            incoming_subscription_id=subscription_id,
            reason="unknown_subscription_deleted",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 4. Matching Subscription ID: Event Ordering
    # =========================================================================

    last_event_ts = target_sub.last_stripe_event_created_at

    # Strictly older event: complete no-op
    if last_event_ts and event_timestamp < last_event_ts:
        logger.info(
            "Event %s: Strictly older subscription.deleted event (timestamp=%s < last_event=%s). "
            "Ignoring as a complete no-op to preserve newer lifecycle state.",
            event_id,
            event_timestamp,
            last_event_ts,
        )
        return

    # Equal timestamp handling
    if last_event_ts and event_timestamp == last_event_ts:
        if target_sub.status not in (SubscriptionStatus.canceled, SubscriptionStatus.incomplete_expired):
            logger.warning(
                "Event %s: Deletion event timestamp (%s) matches last event timestamp but local "
                "subscription '%s' is in non-terminal state '%s'. Quarantining for review.",
                event_id,
                event_timestamp,
                subscription_id,
                target_sub.status.value,
            )
            raise WebhookReconciliationConflictError(
                f"Event {event_id}: Deletion event timestamp ({event_timestamp}) matches last event "
                f"timestamp but local subscription '{subscription_id}' is in non-terminal state "
                f"'{target_sub.status.value}'. Quarantining for review.",
                organization_id=target_sub.organization_id,
                event_id=event_id,
                existing_subscription_id=subscription_id,
                incoming_subscription_id=subscription_id,
                reason="equal_timestamp_deletion_collision",
                diagnostic_payload=payload,
            )

        if target_sub.status == SubscriptionStatus.canceled:
            # Idempotent duplicate acknowledgment; backfill canceled_at only if locally None
            if target_sub.canceled_at is None and final_canceled_at is not None:
                target_sub.canceled_at = final_canceled_at
                target_sub.updated_at = now_utc
                logger.info(
                    "Event %s: Idempotent duplicate deletion backfilled missing canceled_at timestamp.",
                    event_id,
                )
            else:
                logger.info(
                    "Event %s: Idempotent duplicate deletion received for already-canceled subscription '%s'.",
                    event_id,
                    subscription_id,
                )
            return

        if target_sub.status == SubscriptionStatus.incomplete_expired:
            logger.info(
                "Event %s: Deletion event received for terminal incomplete_expired subscription '%s'. No-op.",
                event_id,
                subscription_id,
            )
            return

    # =========================================================================
    # 5. Authoritative Fresh Cancellation Transition
    # =========================================================================

    target_sub.status = SubscriptionStatus.canceled
    target_sub.plan_tier = "free"
    target_sub.cancel_at_period_end = False
    target_sub.canceled_at = final_canceled_at or event_timestamp
    target_sub.last_stripe_event_created_at = event_timestamp
    target_sub.updated_at = now_utc
    # past_due_since is preserved for audit trail

    logger.info(
        "Event %s: Processed subscription.deleted for org %s -> sub=%s, status=canceled, plan_tier=free, canceled_at=%s",
        event_id,
        target_sub.organization_id,
        subscription_id,
        target_sub.canceled_at,
    )


# Automatically register with default webhook dispatcher
default_webhook_dispatcher.register(
    "customer.subscription.deleted",
    handle_subscription_deleted,
)
