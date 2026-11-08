"""Stripe Invoice Paid Webhook Handler.

Event: invoice.paid

Architectural Guarantees & Lifecycle Invariants:
1. Sole Authority for Paid Pro Periods: `invoice.paid` is the exclusive mechanism
   for provisioning paid Pro `OrganizationBillingPeriod` records.
2. Lifecycle Separation: Does NOT mutate `Subscription.status`, `Subscription.plan_tier`,
   `Subscription.canceled_at`, or `Subscription.past_due_since`. Subscription lifecycle
   ownership remains strictly with `customer.subscription.*` and `checkout.session.completed`.
3. Refusal of Resurrection: A paid invoice arriving for a canceled subscription must NEVER
   reactivate it or provision an active Pro period. Recorded as `InvoiceProcessingStatus.ignored`.
4. Fail-Closed Zero-Value Invoices: `amount_paid == 0` invoices are quarantined via
   `BillingReconciliation` in `pending_review` pending an explicit trial/discount policy.
5. Strict Pro Quota Configuration: Sourced strictly from `settings.default_pro_monthly_token_quota`.
   If <= 0, fails closed and quarantines the invoice until configured.
6. Authoritative Chronology: Uses Stripe line-item period boundaries (`period.start`, `period.end`)
   for billing-period chronology, never transient event timestamps.
7. Monotonic Subscription Period Dates: Advances `Subscription.current_period_start` and
   `current_period_end` only when `line_period_end > Subscription.current_period_end`.
8. PostgreSQL Advisory Lock Serialization: Uses transaction-scoped `pg_advisory_xact_lock`
   derived from a deterministic 64-bit integer hash of `stripe_invoice_id` to serialize concurrent
   events for the same invoice ID.
9. Deadlock-Free Hierarchical Locking & In-Lock Re-Query:
   - Discovers tenant identity via non-locking MVCC read on `Subscription`.
   - Acquires `pg_advisory_xact_lock(invoice_id)`.
   - Acquires `OrganizationBillingPeriod` lock (active period `FOR UPDATE`, if present).
   - Acquires `Subscription` row lock (`FOR UPDATE`, stable per-organization anchor).
   - Re-queries active `OrganizationBillingPeriod` under the `Subscription` lock to catch
     periods provisioned by concurrent workers before deciding to insert.
10. Atomic Transaction Boundary: Never commits or rolls back internally. Operates inside the
    caller's transaction and savepoint.
"""

from datetime import datetime, timezone
import hashlib
import logging
from typing import Any
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.billing import OrganizationBillingPeriod
from app.models.subscription import (
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    StripeCheckoutAttempt,
    StripeInvoice,
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
# Helper Functions
# =============================================================================


def _derive_advisory_lock_key(invoice_id: str) -> int:
    """Derive a deterministic 64-bit signed integer for PostgreSQL advisory locking."""
    hash_bytes = hashlib.sha256(f"stripe_invoice:{invoice_id}".encode("utf-8")).digest()[:8]
    return int.from_bytes(hash_bytes, byteorder="big", signed=True)


derive_invoice_advisory_lock_key = _derive_advisory_lock_key


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


# =============================================================================
# Webhook Handler Implementation
# =============================================================================


async def handle_invoice_paid(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle the 'invoice.paid' Stripe webhook event.

    Args:
        payload: Full verified Stripe webhook event dictionary.
        db: Active SQLAlchemy async session managed by the webhook processing engine.

    Raises:
        WebhookPermanentError: If the invoice payload fails structural validation,
            references unknown customers, contains metadata mismatches, or invalid statuses.
        WebhookReconciliationConflictError: If the subscription is unrecognized,
            an unbound customer receives an invoice, an ambiguous line item exists,
            an invoice has zero amount, or an overlapping/gapped period is detected.
    """
    event_id = payload.get("id") or "unknown_event"
    event_type = payload.get("type")
    event_created = payload.get("created")
    invoice_obj = payload.get("data", {}).get("object")

    if event_type != "invoice.paid":
        raise WebhookPermanentError(
            f"Event {event_id}: Unexpected event type '{event_type}'. Expected 'invoice.paid'."
        )

    if not invoice_obj or not isinstance(invoice_obj, dict):
        raise WebhookPermanentError(
            f"Event {event_id} malformed: missing or invalid 'data.object' dictionary."
        )

    # =========================================================================
    # 1. Structural & Field Validations
    # =========================================================================

    invoice_id = invoice_obj.get("id")
    if not invoice_id or not isinstance(invoice_id, str) or not invoice_id.startswith("in_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe invoice ID '{invoice_id}'."
        )

    customer_id = invoice_obj.get("customer")
    if not customer_id or not isinstance(customer_id, str) or not customer_id.startswith("cus_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe customer ID '{customer_id}'."
        )

    subscription_id = invoice_obj.get("subscription")
    if not subscription_id or not isinstance(subscription_id, str) or not subscription_id.startswith("sub_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid Stripe subscription ID '{subscription_id}' in invoice. "
            f"Non-subscription invoices are not supported."
        )

    raw_status = invoice_obj.get("status")
    if not raw_status or not isinstance(raw_status, str) or raw_status.strip() != "paid":
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid invoice status '{raw_status}'. Expected 'paid'."
        )

    is_paid = invoice_obj.get("paid")
    if is_paid is not True:
        raise WebhookPermanentError(
            f"Event {event_id}: Invoice attribute 'paid' is '{is_paid}'. Expected True."
        )

    amount_paid = invoice_obj.get("amount_paid")
    if amount_paid is None or isinstance(amount_paid, bool) or not isinstance(amount_paid, int):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid or missing 'amount_paid' '{amount_paid}'. Expected integer."
        )

    if amount_paid < 0:
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid negative amount_paid '{amount_paid}'."
        )

    currency = invoice_obj.get("currency")
    if not currency or not isinstance(currency, str) or len(currency.strip()) != 3:
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid 3-letter currency code '{currency}'."
        )
    currency = currency.strip().lower()

    billing_reason = invoice_obj.get("billing_reason")
    supported_billing_reasons = frozenset({
        "subscription_create",
        "subscription_cycle",
        "subscription_update",
    })
    if not billing_reason or billing_reason not in supported_billing_reasons:
        logger.warning(
            "Event %s: Invoice %s has unsupported billing_reason '%s'. Quarantining.",
            event_id,
            invoice_id,
            billing_reason,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Invoice '{invoice_id}' has unsupported billing_reason '{billing_reason}'.",
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            event_id=event_id,
            existing_subscription_id=subscription_id,
            incoming_subscription_id=subscription_id,
            reason="unsupported_billing_reason",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 2. Line Item Selection & Quota Policy
    # =========================================================================

    lines_obj = invoice_obj.get("lines")
    lines_data = lines_obj.get("data") if isinstance(lines_obj, dict) else None
    if not lines_data or not isinstance(lines_data, list):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or malformed 'lines.data' list in invoice '{invoice_id}'."
        )

    configured_pro_price_id = settings.stripe_pro_price_id.strip() if settings.stripe_pro_price_id else ""
    if not configured_pro_price_id:
        raise WebhookPermanentError(
            f"Event {event_id}: STRIPE_PRO_PRICE_ID is not configured in backend settings."
        )

    # Check for foreign/unrecognized prices across all lines
    for line in lines_data:
        if not isinstance(line, dict):
            raise WebhookPermanentError(f"Event {event_id}: Malformed line item in invoice '{invoice_id}'.")
        price_obj = line.get("price")
        plan_obj = line.get("plan")
        line_price_id = None
        if isinstance(price_obj, dict):
            line_price_id = price_obj.get("id")
        elif isinstance(price_obj, str):
            line_price_id = price_obj
        elif isinstance(plan_obj, dict):
            line_price_id = plan_obj.get("id")
        elif isinstance(plan_obj, str):
            line_price_id = plan_obj

        if line_price_id and line_price_id != configured_pro_price_id:
            logger.warning(
                "Event %s: Invoice %s contains unrecognized price '%s' (configured: '%s'). Quarantining.",
                event_id,
                invoice_id,
                line_price_id,
                configured_pro_price_id,
            )
            raise WebhookReconciliationConflictError(
                f"Event {event_id}: Invoice '{invoice_id}' contains unrecognized price '{line_price_id}'.",
                organization_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                event_id=event_id,
                existing_subscription_id=subscription_id,
                incoming_subscription_id=subscription_id,
                reason="unrecognized_line_item_price",
                diagnostic_payload=payload,
            )

    # Filter for non-prorated recurring Pro candidate line items
    candidate_lines: list[dict[str, Any]] = []
    for line in lines_data:
        price_obj = line.get("price")
        plan_obj = line.get("plan")
        line_price_id = None
        if isinstance(price_obj, dict):
            line_price_id = price_obj.get("id")
        elif isinstance(price_obj, str):
            line_price_id = price_obj
        elif isinstance(plan_obj, dict):
            line_price_id = plan_obj.get("id")
        elif isinstance(plan_obj, str):
            line_price_id = plan_obj

        if line_price_id == configured_pro_price_id and not line.get("proration"):
            candidate_lines.append(line)

    if len(candidate_lines) == 0:
        logger.warning(
            "Event %s: Invoice %s contains no eligible non-prorated Pro recurring line items.",
            event_id,
            invoice_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Invoice '{invoice_id}' contains no eligible non-prorated Pro recurring line items.",
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            event_id=event_id,
            existing_subscription_id=subscription_id,
            incoming_subscription_id=subscription_id,
            reason="no_eligible_pro_line_item",
            diagnostic_payload=payload,
        )

    if len(candidate_lines) > 1:
        logger.warning(
            "Event %s: Invoice %s contains multiple (%d) eligible Pro recurring line items. Ambiguous.",
            event_id,
            invoice_id,
            len(candidate_lines),
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Invoice '{invoice_id}' contains multiple ({len(candidate_lines)}) eligible Pro line items.",
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            event_id=event_id,
            existing_subscription_id=subscription_id,
            incoming_subscription_id=subscription_id,
            reason="multiple_eligible_pro_line_items",
            diagnostic_payload=payload,
        )

    pro_line = candidate_lines[0]
    line_period = pro_line.get("period")
    if not isinstance(line_period, dict):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid 'period' dict in line item of invoice '{invoice_id}'."
        )

    line_period_start = _validate_positive_timestamp(line_period.get("start"), "line.period.start", event_id)
    line_period_end = _validate_positive_timestamp(line_period.get("end"), "line.period.end", event_id)
    if line_period_end <= line_period_start:
        raise WebhookPermanentError(
            f"Event {event_id}: line.period.end ({line_period_end}) must be strictly after line.period.start ({line_period_start})."
        )

    # Zero-value invoice policy: fail-closed quarantine
    if amount_paid == 0:
        logger.warning(
            "Event %s: Invoice %s has amount_paid=0. Quarantining pending trial/promotional policy.",
            event_id,
            invoice_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Zero-value invoice '{invoice_id}' quarantined pending commercial trial/discount approval.",
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            event_id=event_id,
            existing_subscription_id=subscription_id,
            incoming_subscription_id=subscription_id,
            reason="zero_value_invoice_pending_review",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 3. Advisory Locking (Invoice Serialization)
    # =========================================================================

    advisory_key = _derive_advisory_lock_key(invoice_id)
    # Transaction-scoped PostgreSQL advisory lock: automatically released on commit/rollback/disconnect
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key})

    # =========================================================================
    # 4. Non-Locking Tenant Identity Discovery
    # =========================================================================

    stmt_cust = (
        select(Subscription.organization_id, Subscription.stripe_subscription_id)
        .where(
            Subscription.stripe_customer_id == customer_id,
            Subscription.provider == "stripe",
        )
    )
    res_cust = await db.execute(stmt_cust)
    cust_row = res_cust.first()

    if cust_row is None:
        raise WebhookPermanentError(
            f"Event {event_id}: Unknown Stripe customer '{customer_id}'. No local organization association exists."
        )

    org_id = cust_row.organization_id

    # Anti-tampering metadata check (if present, must match local organization)
    metadata = invoice_obj.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise WebhookPermanentError(f"Event {event_id}: Malformed invoice metadata object.")

    if isinstance(metadata, dict):
        meta_org_id = metadata.get("organization_id")
        if meta_org_id is not None:
            if not isinstance(meta_org_id, str) or not meta_org_id.strip():
                raise WebhookPermanentError(f"Event {event_id}: Malformed organization_id in metadata: '{meta_org_id}'.")
            try:
                parsed_org_id = uuid.UUID(meta_org_id.strip())
            except (ValueError, TypeError) as exc:
                raise WebhookPermanentError(
                    f"Event {event_id}: Invalid organization UUID in metadata '{meta_org_id}': {exc}"
                ) from exc

            if parsed_org_id != org_id:
                raise WebhookPermanentError(
                    f"Event {event_id}: Organization metadata mismatch. "
                    f"Customer bound to org {org_id}, metadata claims {parsed_org_id}."
                )

    # Unbound customer check: local stripe_subscription_id is None
    if cust_row.stripe_subscription_id is None:
        logger.warning(
            "Event %s: Received invoice %s for subscription %s under customer %s (org %s), "
            "but local subscription has no active subscription ID.",
            event_id,
            invoice_id,
            subscription_id,
            customer_id,
            org_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received invoice '{invoice_id}' for subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {org_id}) but local subscription record has no active subscription ID.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id="none",
            incoming_subscription_id=subscription_id,
            reason="unbound_customer_invoice",
            diagnostic_payload=payload,
        )

    # Historical Superseded vs Unknown Subscription Check
    if cust_row.stripe_subscription_id != subscription_id:
        stmt_historical = (
            select(StripeCheckoutAttempt.id)
            .where(
                StripeCheckoutAttempt.organization_id == org_id,
                StripeCheckoutAttempt.stripe_subscription_id == subscription_id,
                StripeCheckoutAttempt.status == CheckoutAttemptStatus.complete,
            )
            .limit(1)
        )
        res_hist = await db.execute(stmt_historical)
        historical_attempt = res_hist.scalar_one_or_none()

        if historical_attempt is not None:
            logger.info(
                "Event %s: Invoice %s received for known superseded subscription '%s' (org %s). "
                "Recording invoice as ignored.",
                event_id,
                invoice_id,
                subscription_id,
                org_id,
            )
            # Idempotently record invoice ledger as ignored
            stmt_inv_hist = (
                select(StripeInvoice)
                .where(StripeInvoice.stripe_invoice_id == invoice_id)
                .with_for_update()
            )
            res_inv_hist = await db.execute(stmt_inv_hist)
            existing_inv_hist = res_inv_hist.scalar_one_or_none()
            if existing_inv_hist is None:
                new_inv_hist = StripeInvoice(
                    id=uuid.uuid4(),
                    stripe_invoice_id=invoice_id,
                    organization_id=org_id,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    billing_reason=billing_reason,
                    invoice_status=raw_status,
                    currency=currency,
                    amount_paid_minor_units=amount_paid,
                    period_start=line_period_start,
                    period_end=line_period_end,
                    processing_status=InvoiceProcessingStatus.ignored,
                    billing_period_id=None,
                    raw_event_id=event_id,
                    diagnostic_payload=payload,
                )
                db.add(new_inv_hist)
            return

        # Unknown subscription ID -> quarantine
        logger.warning(
            "Event %s: Invoice %s received for unknown subscription '%s' under customer '%s' (org %s). Current is '%s'.",
            event_id,
            invoice_id,
            subscription_id,
            customer_id,
            org_id,
            cust_row.stripe_subscription_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received invoice '{invoice_id}' for unknown subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {org_id}). Current subscription is '{cust_row.stripe_subscription_id}'.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id=cust_row.stripe_subscription_id,
            incoming_subscription_id=subscription_id,
            reason="unknown_subscription_invoice",
            diagnostic_payload=payload,
        )

    # Pro Quota Configuration Check: fail-closed if <= 0
    pro_quota = settings.default_pro_monthly_token_quota
    if pro_quota <= 0:
        logger.warning(
            "Event %s: DEFAULT_PRO_MONTHLY_TOKEN_QUOTA is %d. Cannot provision Pro billing period with zero quota.",
            event_id,
            pro_quota,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: DEFAULT_PRO_MONTHLY_TOKEN_QUOTA is configured to {pro_quota}. "
            "Pro billing period cannot be provisioned with zero quota. Quarantining invoice.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id=subscription_id,
            incoming_subscription_id=subscription_id,
            reason="missing_pro_quota_configuration",
            diagnostic_payload=payload,
        )

    # =========================================================================
    # 5. Deadlock-Safe Hierarchical Row Locking
    # =========================================================================

    # Lock 1: OrganizationBillingPeriod (active period, if present)
    stmt_active_bp = (
        select(OrganizationBillingPeriod)
        .where(
            OrganizationBillingPeriod.organization_id == org_id,
            OrganizationBillingPeriod.is_active == True,
        )
        .with_for_update()
    )
    res_active_bp = await db.execute(stmt_active_bp)
    initial_active_bp = res_active_bp.scalar_one_or_none()

    # Lock 2: Subscription (Targeted lock using verified Stripe subscription ID)
    stmt_sub_lock = (
        select(Subscription)
        .where(
            Subscription.organization_id == org_id,
            Subscription.stripe_subscription_id == subscription_id,
            Subscription.provider == "stripe",
        )
        .with_for_update()
    )
    res_sub_lock = await db.execute(stmt_sub_lock)
    locked_sub = res_sub_lock.scalar_one_or_none()

    if locked_sub is None:
        raise WebhookPermanentError(
            f"Event {event_id}: Subscription record '{subscription_id}' for organization {org_id} not found under lock."
        )

    # In-Lock Re-Query: Fresh active period query to catch periods created by concurrent workers
    stmt_recheck_bp = (
        select(OrganizationBillingPeriod)
        .where(
            OrganizationBillingPeriod.organization_id == org_id,
            OrganizationBillingPeriod.is_active == True,
        )
        .with_for_update()
    )
    res_recheck_bp = await db.execute(stmt_recheck_bp)
    active_period = res_recheck_bp.scalar_one_or_none()

    # Revalidate Identity Under Lock
    if locked_sub.stripe_customer_id != customer_id or locked_sub.stripe_subscription_id != subscription_id:
        raise WebhookPermanentError(
            f"Event {event_id}: Subscription identity changed concurrently under lock for org {org_id}."
        )

    now_utc = datetime.now(timezone.utc)

    # Refusal of Resurrection Guard: A paid invoice must NEVER resurrect a canceled subscription!
    if locked_sub.status == SubscriptionStatus.canceled:
        logger.warning(
            "Event %s: Invoice %s received for canceled subscription %s (org %s). "
            "Refusing resurrection; no billing period will be provisioned.",
            event_id,
            invoice_id,
            subscription_id,
            org_id,
        )
        stmt_inv_canceled = (
            select(StripeInvoice)
            .where(StripeInvoice.stripe_invoice_id == invoice_id)
            .with_for_update()
        )
        res_inv_canc = await db.execute(stmt_inv_canceled)
        existing_inv_canc = res_inv_canc.scalar_one_or_none()
        if existing_inv_canc is None:
            new_inv_canc = StripeInvoice(
                id=uuid.uuid4(),
                stripe_invoice_id=invoice_id,
                organization_id=org_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                billing_reason=billing_reason,
                invoice_status=raw_status,
                currency=currency,
                amount_paid_minor_units=amount_paid,
                period_start=line_period_start,
                period_end=line_period_end,
                processing_status=InvoiceProcessingStatus.ignored,
                billing_period_id=None,
                raw_event_id=event_id,
                diagnostic_payload=payload,
            )
            db.add(new_inv_canc)
        return

    # =========================================================================
    # 6. Invoice Ledger Idempotency Check
    # =========================================================================

    stmt_invoice = (
        select(StripeInvoice)
        .where(StripeInvoice.stripe_invoice_id == invoice_id)
        .with_for_update()
    )
    res_invoice = await db.execute(stmt_invoice)
    stripe_invoice_record = res_invoice.scalar_one_or_none()

    if stripe_invoice_record is not None and stripe_invoice_record.processing_status == InvoiceProcessingStatus.processed:
        logger.info(
            "Event %s: Invoice %s is already processed in ledger (billing_period_id=%s). Idempotent no-op.",
            event_id,
            invoice_id,
            stripe_invoice_record.billing_period_id,
        )
        return

    # If ledger record does not exist yet, instantiate pending record
    if stripe_invoice_record is None:
        stripe_invoice_record = StripeInvoice(
            id=uuid.uuid4(),
            stripe_invoice_id=invoice_id,
            organization_id=org_id,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            billing_reason=billing_reason,
            invoice_status=raw_status,
            currency=currency,
            amount_paid_minor_units=amount_paid,
            period_start=line_period_start,
            period_end=line_period_end,
            processing_status=InvoiceProcessingStatus.pending,
            billing_period_id=None,
            raw_event_id=event_id,
            diagnostic_payload=payload,
        )
        db.add(stripe_invoice_record)

    # =========================================================================
    # 7. Billing-Period Chronology Evaluation & Provisioning
    # =========================================================================

    target_period_id: uuid.UUID

    if active_period is not None:
        active_start = active_period.period_start
        active_end = active_period.period_end
        active_tier = active_period.plan_tier

        # Case 1: Exact Match with Active Period
        if line_period_start == active_start and line_period_end == active_end:
            if active_tier == "pro":
                # Idempotent attach: attach invoice to existing active Pro period without resetting counters
                logger.info(
                    "Event %s: Invoice %s matches existing active Pro period %s exactly. Attaching without duplicating quota.",
                    event_id,
                    invoice_id,
                    active_period.id,
                )
                target_period_id = active_period.id
            else:
                # Upgrade from Free to Pro for current period!
                logger.info(
                    "Event %s: Invoice %s upgrades active Free period %s to Pro.",
                    event_id,
                    invoice_id,
                    active_period.id,
                )
                active_period.is_active = False
                new_pro_period = OrganizationBillingPeriod(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    period_start=line_period_start,
                    period_end=line_period_end,
                    plan_tier="pro",
                    monthly_token_quota=pro_quota,
                    tokens_used=0,
                    tokens_reserved=0,
                    cost_micros_used=0,
                    cost_micros_reserved=0,
                    hard_spending_cap_micros=None,
                    is_active=True,
                    is_blocked=False,
                )
                db.add(new_pro_period)
                await db.flush()
                target_period_id = new_pro_period.id

        # Case 2: Consecutive Renewal Period
        elif line_period_start == active_end and line_period_end > active_end:
            logger.info(
                "Event %s: Invoice %s is consecutive renewal after period %s. Provisioning new active Pro period.",
                event_id,
                invoice_id,
                active_period.id,
            )
            active_period.is_active = False
            new_pro_period = OrganizationBillingPeriod(
                id=uuid.uuid4(),
                organization_id=org_id,
                period_start=line_period_start,
                period_end=line_period_end,
                plan_tier="pro",
                monthly_token_quota=pro_quota,
                tokens_used=0,
                tokens_reserved=0,
                cost_micros_used=0,
                cost_micros_reserved=0,
                hard_spending_cap_micros=None,
                is_active=True,
                is_blocked=False,
            )
            db.add(new_pro_period)
            await db.flush()
            target_period_id = new_pro_period.id

        # Case 3: Strictly Older Invoice
        elif line_period_end <= active_start or line_period_end < active_end:
            # Older invoice must NEVER replace a newer active period!
            stmt_hist = (
                select(OrganizationBillingPeriod)
                .where(
                    OrganizationBillingPeriod.organization_id == org_id,
                    OrganizationBillingPeriod.period_start == line_period_start,
                    OrganizationBillingPeriod.period_end == line_period_end,
                )
            )
            res_hist = await db.execute(stmt_hist)
            hist_match = res_hist.scalar_one_or_none()
            if hist_match is not None:
                logger.info(
                    "Event %s: Older invoice %s matches historical period %s. Attaching without altering active period.",
                    event_id,
                    invoice_id,
                    hist_match.id,
                )
                target_period_id = hist_match.id
            else:
                logger.warning(
                    "Event %s: Invoice %s period (%s - %s) is strictly older than active period (%s - %s) and no matching historical period exists. Quarantining.",
                    event_id,
                    invoice_id,
                    line_period_start,
                    line_period_end,
                    active_start,
                    active_end,
                )
                raise WebhookReconciliationConflictError(
                    f"Event {event_id}: Stale invoice '{invoice_id}' ({line_period_start} to {line_period_end}) "
                    f"arrived prior to active period ({active_start} to {active_end}) without matching historical period.",
                    organization_id=org_id,
                    event_id=event_id,
                    existing_subscription_id=subscription_id,
                    incoming_subscription_id=subscription_id,
                    reason="stale_invoice_prior_to_active_period",
                    diagnostic_payload=payload,
                )

        # Case 4: Gap in Billing Period
        elif line_period_start > active_end:
            logger.warning(
                "Event %s: Invoice %s period start (%s) creates gap after active period end (%s). Quarantining.",
                event_id,
                invoice_id,
                line_period_start,
                active_end,
            )
            raise WebhookReconciliationConflictError(
                f"Event {event_id}: Invoice '{invoice_id}' period start ({line_period_start}) creates gap after active period end ({active_end}).",
                organization_id=org_id,
                event_id=event_id,
                existing_subscription_id=subscription_id,
                incoming_subscription_id=subscription_id,
                reason="billing_period_gap_conflict",
                diagnostic_payload=payload,
            )

        # Case 5: Overlapping or Nested Period
        else:
            logger.warning(
                "Event %s: Invoice %s period (%s - %s) overlaps or is nested within active period (%s - %s). Quarantining.",
                event_id,
                invoice_id,
                line_period_start,
                line_period_end,
                active_start,
                active_end,
            )
            raise WebhookReconciliationConflictError(
                f"Event {event_id}: Invoice '{invoice_id}' period ({line_period_start} to {line_period_end}) overlaps with active period ({active_start} to {active_end}).",
                organization_id=org_id,
                event_id=event_id,
                existing_subscription_id=subscription_id,
                incoming_subscription_id=subscription_id,
                reason="overlapping_billing_period_conflict",
                diagnostic_payload=payload,
            )

    else:
        # No active billing period currently exists. Inspect historical periods for org.
        stmt_all = (
            select(OrganizationBillingPeriod)
            .where(OrganizationBillingPeriod.organization_id == org_id)
            .order_by(OrganizationBillingPeriod.period_end.desc())
        )
        res_all = await db.execute(stmt_all)
        all_periods = res_all.scalars().all()

        if all_periods:
            # Check for exact match against any historical period
            exact_hist = next(
                (p for p in all_periods if p.period_start == line_period_start and p.period_end == line_period_end),
                None,
            )
            if exact_hist is not None:
                logger.info(
                    "Event %s: Invoice %s matches historical period %s. Attaching.",
                    event_id,
                    invoice_id,
                    exact_hist.id,
                )
                target_period_id = exact_hist.id
            else:
                latest_p = all_periods[0]
                if line_period_end <= latest_p.period_start or line_period_end < latest_p.period_end:
                    raise WebhookReconciliationConflictError(
                        f"Event {event_id}: Stale invoice '{invoice_id}' arrived prior to latest historical period ({latest_p.period_end}).",
                        organization_id=org_id,
                        event_id=event_id,
                        existing_subscription_id=subscription_id,
                        incoming_subscription_id=subscription_id,
                        reason="stale_invoice_prior_to_historical_period",
                        diagnostic_payload=payload,
                    )
                if line_period_start < latest_p.period_end:
                    raise WebhookReconciliationConflictError(
                        f"Event {event_id}: Invoice '{invoice_id}' overlaps with latest historical period ({latest_p.period_end}).",
                        organization_id=org_id,
                        event_id=event_id,
                        existing_subscription_id=subscription_id,
                        incoming_subscription_id=subscription_id,
                        reason="overlapping_historical_period_conflict",
                        diagnostic_payload=payload,
                    )
                if line_period_start > latest_p.period_end:
                    raise WebhookReconciliationConflictError(
                        f"Event {event_id}: Invoice '{invoice_id}' period start ({line_period_start}) creates gap after historical period end ({latest_p.period_end}).",
                        organization_id=org_id,
                        event_id=event_id,
                        existing_subscription_id=subscription_id,
                        incoming_subscription_id=subscription_id,
                        reason="billing_period_gap_conflict",
                        diagnostic_payload=payload,
                    )
                # Consecutive to latest historical period
                logger.info(
                    "Event %s: Provisioning new active Pro period consecutive to historical period %s.",
                    event_id,
                    latest_p.id,
                )
                new_pro_period = OrganizationBillingPeriod(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    period_start=line_period_start,
                    period_end=line_period_end,
                    plan_tier="pro",
                    monthly_token_quota=pro_quota,
                    tokens_used=0,
                    tokens_reserved=0,
                    cost_micros_used=0,
                    cost_micros_reserved=0,
                    hard_spending_cap_micros=None,
                    is_active=True,
                    is_blocked=False,
                )
                db.add(new_pro_period)
                await db.flush()
                target_period_id = new_pro_period.id
        else:
            # Very first billing period for organization
            logger.info(
                "Event %s: Provisioning initial Pro billing period for organization %s.",
                event_id,
                org_id,
            )
            new_pro_period = OrganizationBillingPeriod(
                id=uuid.uuid4(),
                organization_id=org_id,
                period_start=line_period_start,
                period_end=line_period_end,
                plan_tier="pro",
                monthly_token_quota=pro_quota,
                tokens_used=0,
                tokens_reserved=0,
                cost_micros_used=0,
                cost_micros_reserved=0,
                hard_spending_cap_micros=None,
                is_active=True,
                is_blocked=False,
            )
            db.add(new_pro_period)
            await db.flush()
            target_period_id = new_pro_period.id

    # =========================================================================
    # 8. Subscription Monotonic Date Updates (Lifecycle Untouched)
    # =========================================================================

    if locked_sub.current_period_end is None or line_period_end > locked_sub.current_period_end:
        locked_sub.current_period_start = line_period_start
        locked_sub.current_period_end = line_period_end
        locked_sub.updated_at = now_utc
        logger.info(
            "Event %s: Advanced subscription %s period boundaries to (%s - %s).",
            event_id,
            subscription_id,
            line_period_start,
            line_period_end,
        )

    # Associate invoice ledger record and mark processed
    stripe_invoice_record.billing_period_id = target_period_id
    stripe_invoice_record.processing_status = InvoiceProcessingStatus.processed
    stripe_invoice_record.amount_paid_minor_units = amount_paid
    stripe_invoice_record.invoice_status = raw_status
    stripe_invoice_record.raw_event_id = event_id
    stripe_invoice_record.updated_at = now_utc

    logger.info(
        "Event %s: Successfully processed invoice %s -> org=%s, sub=%s, billing_period=%s, amount=%d %s",
        event_id,
        invoice_id,
        org_id,
        subscription_id,
        target_period_id,
        amount_paid,
        currency,
    )


# Automatically register with default webhook dispatcher
default_webhook_dispatcher.register(
    "invoice.paid",
    handle_invoice_paid,
)
