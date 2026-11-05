"""Stripe Customer Subscription Created Webhook Handler.

Event: customer.subscription.created

Architectural Guarantees & Lifecycle Invariants:
1. Strict Identity Binding: Authorizes subscription creation strictly against existing
   locally persisted customer records (`stripe_customer_id`). Unknown customers without
   prior local associations fail closed with WebhookPermanentError (quarantine).
   Metadata alone never establishes an organization-to-customer binding.
2. Metadata Integrity: If `metadata.organization_id` is present, requires an exact UUID match
   with the customer's bound organization. Inconsistencies fail closed immediately.
3. Fail-Closed Price Verification: Validates that `items.data` contains the server-configured
   Pro Price ID (`settings.stripe_pro_price_id`). Searches all line items without assuming single-item arrays.
4. Multi-Subscription Conflict Guard: If the organization already possesses an active/trialing
   subscription with a different Stripe subscription ID, raises WebhookReconciliationConflictError
   to stage a BillingReconciliation record and quarantine the event atomically.
5. Monotonic Timestamp Ordering: Evaluates `last_stripe_event_created_at`. If an older or equal
   event arrives after checkout completion or a newer update, backfills missing authoritative
   fields (`stripe_price_id`, period dates) without regressing lifecycle state or advancing timestamps.
6. Absolute Accounting & Period Isolation:
   - Does NOT create or modify OrganizationBillingPeriod rows.
   - Does NOT create or modify StripeInvoice records.
   - Does NOT mutate usage counters, tokens, or spending caps.
   - Does NOT alter in-flight UsageReservations.
   - Does NOT grant Pro plan tier solely because Stripe status is "active" before payment confirmation.
     Paid billing periods remain strictly and exclusively owned by the future `invoice.paid` handler.
7. Atomic Transaction Boundary: Never commits from within the handler. All database operations
   participate in the outer webhook processing engine's transaction and savepoint (`begin_nested()`).
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.subscription import (
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
# Helper Timestamp & Price Validation Functions
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


def _find_configured_pro_price(items_data: Any, configured_pro_price_id: str, event_id: str) -> str:
    """Iterate through subscription items.data to find the configured Pro Price ID."""
    if not isinstance(items_data, list) or not items_data:
        raise WebhookPermanentError(
            f"Event {event_id}: Subscription has missing or empty 'items.data'."
        )

    matched_price_id: str | None = None
    for item in items_data:
        if not isinstance(item, dict):
            raise WebhookPermanentError(
                f"Event {event_id}: Subscription line item is malformed."
            )
        price_field = item.get("price")
        item_price_id: str | None = None
        if isinstance(price_field, dict):
            raw_id = price_field.get("id")
            if isinstance(raw_id, str):
                item_price_id = raw_id.strip()
        elif isinstance(price_field, str):
            item_price_id = price_field.strip()

        if item_price_id and item_price_id == configured_pro_price_id:
            matched_price_id = item_price_id
            break

    if not matched_price_id:
        raise WebhookPermanentError(
            f"Event {event_id}: No subscription line item matched configured Pro price '{configured_pro_price_id}'."
        )

    return matched_price_id


# =============================================================================
# Webhook Handler Implementation
# =============================================================================


async def handle_subscription_created(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle the 'customer.subscription.created' Stripe webhook event.

    Args:
        payload: Full verified Stripe webhook event dictionary.
        db: Active SQLAlchemy async session managed by the webhook processing engine.

    Raises:
        WebhookPermanentError: If the event payload fails cryptographic validation,
            references unknown customers, contains price or period mismatches,
            or fails identity verification.
        WebhookReconciliationConflictError: If the incoming subscription conflicts
            with an existing active subscription for the organization.
    """
    event_id = payload.get("id") or "unknown_event"
    event_created = payload.get("created")
    sub_obj = payload.get("data", {}).get("object")

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
    if not raw_status or not isinstance(raw_status, str):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid subscription status '{raw_status}'."
        )
    try:
        sub_status = SubscriptionStatus(raw_status.strip())
    except ValueError:
        raise WebhookPermanentError(
            f"Event {event_id}: Unrecognized subscription status '{raw_status}'."
        )

    # Validate timestamps and period validity
    period_start_dt = _validate_positive_timestamp(
        sub_obj.get("current_period_start"), "current_period_start", event_id
    )
    period_end_dt = _validate_positive_timestamp(
        sub_obj.get("current_period_end"), "current_period_end", event_id
    )
    if period_end_dt <= period_start_dt:
        raise WebhookPermanentError(
            f"Event {event_id}: current_period_end ({period_end_dt.isoformat()}) must be strictly after "
            f"current_period_start ({period_start_dt.isoformat()})."
        )

    raw_cancel_at_end = sub_obj.get("cancel_at_period_end")
    if raw_cancel_at_end is not None and not isinstance(raw_cancel_at_end, bool):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid cancel_at_period_end value '{raw_cancel_at_end}'. Expected boolean."
        )
    cancel_at_period_end = bool(raw_cancel_at_end) if raw_cancel_at_end is not None else False

    canceled_at_dt = _parse_optional_positive_timestamp(
        sub_obj.get("canceled_at"), "canceled_at", event_id
    )

    # Validate configured Pro Price ID and items
    configured_pro_price_id = (settings.stripe_pro_price_id or "").strip()
    if not configured_pro_price_id:
        raise WebhookPermanentError(
            f"Event {event_id}: STRIPE_PRO_PRICE_ID is not configured in server environment."
        )

    items = sub_obj.get("items")
    if not isinstance(items, dict):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or malformed 'items' object in subscription."
        )
    matched_price_id = _find_configured_pro_price(
        items.get("data"), configured_pro_price_id, event_id
    )

    # =========================================================================
    # 2. Strict Customer & Organization Identity Verification
    # =========================================================================

    stmt_sub = (
        select(Subscription)
        .where(Subscription.stripe_customer_id == customer_id)
        .with_for_update()
    )
    result_sub = await db.execute(stmt_sub)
    target_sub = result_sub.scalar_one_or_none()

    if target_sub is None:
        raise WebhookPermanentError(
            f"Event {event_id}: Unknown Stripe customer '{customer_id}'. No local organization association exists."
        )

    # Organization metadata check (if present, must match local row exactly)
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

    # =========================================================================
    # 3. Multi-Subscription Conflict Guard (Reconciliation Ledger)
    # =========================================================================

    stmt_active = (
        select(Subscription)
        .where(
            Subscription.organization_id == target_sub.organization_id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]),
        )
        .with_for_update()
    )
    result_active = await db.execute(stmt_active)
    active_sub = result_active.scalar_one_or_none()

    if active_sub is not None and active_sub.stripe_subscription_id != subscription_id:
        logger.warning(
            "Event %s: Organization %s already has active subscription '%s'. Conflict with incoming '%s'.",
            event_id,
            target_sub.organization_id,
            active_sub.stripe_subscription_id,
            subscription_id,
        )
        raise WebhookReconciliationConflictError(
            f"Organization {target_sub.organization_id} already has active subscription '{active_sub.stripe_subscription_id}'. "
            f"Incoming subscription '{subscription_id}' quarantined in billing reconciliation ledger.",
            organization_id=target_sub.organization_id,
            event_id=event_id,
            existing_subscription_id=active_sub.stripe_subscription_id or "unknown",
            incoming_subscription_id=subscription_id,
            reason="duplicate_active_subscription",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 4. Monotonic Timestamp Ordering & Authoritative Reconciliation
    # =========================================================================

    now_utc = datetime.now(timezone.utc)
    event_timestamp = _parse_stripe_timestamp(event_created) if event_created else now_utc

    is_stale_event = bool(
        target_sub.last_stripe_event_created_at
        and event_timestamp <= target_sub.last_stripe_event_created_at
    )

    if is_stale_event:
        # Case B/C: Event arrives after checkout completion or a newer update.
        # Do not regress status or plan tier. Do not advance last_stripe_event_created_at.
        # Backfill missing authoritative fields if they are currently None.
        if target_sub.stripe_subscription_id is None:
            target_sub.stripe_subscription_id = subscription_id
        if target_sub.stripe_price_id is None:
            target_sub.stripe_price_id = matched_price_id
        if target_sub.current_period_start is None:
            target_sub.current_period_start = period_start_dt
        if target_sub.current_period_end is None:
            target_sub.current_period_end = period_end_dt
        if target_sub.cancel_at_period_end is None:
            target_sub.cancel_at_period_end = cancel_at_period_end
        if target_sub.canceled_at is None and canceled_at_dt is not None:
            target_sub.canceled_at = canceled_at_dt
        target_sub.updated_at = now_utc

        logger.info(
            "Event %s: Older or equal subscription.created event (timestamp=%s <= last_event=%s). "
            "Backfilled missing authoritative fields without lifecycle regression.",
            event_id,
            event_timestamp,
            target_sub.last_stripe_event_created_at,
        )
        return

    # Fresh event: Authoritatively populate local subscription record
    target_sub.stripe_customer_id = customer_id
    target_sub.stripe_subscription_id = subscription_id
    target_sub.stripe_price_id = matched_price_id
    target_sub.current_period_start = period_start_dt
    target_sub.current_period_end = period_end_dt
    target_sub.cancel_at_period_end = cancel_at_period_end
    target_sub.canceled_at = canceled_at_dt
    target_sub.last_stripe_event_created_at = event_timestamp
    target_sub.updated_at = now_utc

    # Entitlement Decision:
    # If checkout.session.completed already granted Pro (verified paid state), keep Pro.
    # Otherwise, subscription.created does not grant Pro or create paid quota before invoice confirmation.
    if target_sub.plan_tier == "pro" and sub_status in (SubscriptionStatus.active, SubscriptionStatus.trialing):
        target_sub.status = sub_status
    else:
        target_sub.status = sub_status
        target_sub.plan_tier = "free"

    logger.info(
        "Event %s: Processed subscription.created for org %s -> sub=%s, price=%s, status=%s, plan=%s",
        event_id,
        target_sub.organization_id,
        subscription_id,
        matched_price_id,
        target_sub.status.value,
        target_sub.plan_tier,
    )


# Automatically register with default webhook dispatcher
default_webhook_dispatcher.register(
    "customer.subscription.created",
    handle_subscription_created,
)
