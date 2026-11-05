"""Stripe Invoice Payment Failed Webhook Handler.

Event: invoice.payment_failed

Architectural Guarantees & Lifecycle Invariants:
1. Strict Lifecycle Separation:
   - Does NOT mutate Subscription.status (never sets past_due, unpaid, or canceled).
   - Does NOT set, reset, or advance Subscription.past_due_since.
   - Does NOT downgrade Subscription.plan_tier.
   - Does NOT alter current_period_start, current_period_end, or cancel_at_period_end.
   - Does NOT deactivate, close, block, or create OrganizationBillingPeriod rows.
   - Does NOT alter token quotas or usage counters.
   - Subscription lifecycle authority remains strictly with customer.subscription.* handlers.
2. Authoritative Invoice Ledger Updates:
   - Manages StripeInvoice records with processing_status = InvoiceProcessingStatus.failed.
   - Captures monotonically non-decreasing attempt_count = max(existing, incoming).
   - Preserves complete chronological failure history in diagnostic_payload["attempts"].
3. Monotonic State & Stale Event Protection:
   - If an invoice is already processed (e.g. invoice.paid arrived first), a delayed
     invoice.payment_failed event NEVER regresses processing_status to 'failed' and NEVER
     clears billing_period_id. Records diagnostic under 'stale_failures' and exits cleanly.
   - If an invoice is already marked ignored (e.g. canceled or superseded subscription),
     preserves the terminal ignored status.
4. Tenant Identity & Anti-Tampering:
   - Resolves organization strictly through existing local Subscription.stripe_customer_id.
   - Verifies customer metadata organization_id (if present) matches local binding.
   - Superseded subscriptions matching completed checkout attempts are safely recorded as ignored.
   - Unrecognized subscriptions or unbound customers fail closed with reconciliation conflict errors.
5. Deterministic Advisory Locking:
   - Uses PostgreSQL transaction-scoped advisory lock pg_advisory_xact_lock derived from
     the SHA-256 hash of stripe_invoice:{invoice_id} to serialize concurrent events for the same invoice.
6. JSONB Persistence Safety:
   - Uses dictionary copying and flag_modified() to ensure SQLAlchemy change tracking flushes
     nested attempt history updates to PostgreSQL without schema modifications.
7. Atomic Transaction Boundary:
   - Operates inside the webhook engine's transaction and savepoint (begin_nested()).
   - Never commits or rolls back internally.
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.subscription import (
    CheckoutAttemptStatus,
    InvoiceProcessingStatus,
    StripeCheckoutAttempt,
    StripeInvoice,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.stripe_invoice_paid_handler import (
    _validate_positive_timestamp,
    derive_invoice_advisory_lock_key,
)
from app.services.billing.stripe_webhook_engine import (
    WebhookPermanentError,
    WebhookReconciliationConflictError,
    default_webhook_dispatcher,
)

logger = logging.getLogger(__name__)


def _parse_optional_positive_timestamp(val: Any, field_name: str, event_id: str) -> datetime | None:
    """Parse an optional timestamp field, returning None if absent or UTC datetime if valid."""
    if val is None:
        return None
    return _validate_positive_timestamp(val, field_name, event_id)


async def handle_invoice_payment_failed(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Handle the 'invoice.payment_failed' Stripe webhook event.

    Args:
        payload: Full verified Stripe webhook event dictionary.
        db: Active SQLAlchemy async session managed by the webhook processing engine.

    Raises:
        WebhookPermanentError: If the payload fails structural validation, references
            unknown customers, contains metadata mismatches, or lacks required fields.
        WebhookReconciliationConflictError: If the subscription is unrecognized,
            an unbound customer receives an invoice, or reconciliation audit is required.
    """
    event_id = payload.get("id") or "unknown_event"
    event_type = payload.get("type")
    event_created = payload.get("created")
    invoice_obj = payload.get("data", {}).get("object")

    if event_type != "invoice.payment_failed":
        raise WebhookPermanentError(
            f"Event {event_id}: Unexpected event type '{event_type}'. Expected 'invoice.payment_failed'."
        )

    event_timestamp = _validate_positive_timestamp(event_created, "event_created", event_id)

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
            f"Event {event_id}: Missing or invalid customer ID '{customer_id}' on invoice {invoice_id}."
        )

    subscription_id = invoice_obj.get("subscription")
    if not subscription_id or not isinstance(subscription_id, str) or not subscription_id.startswith("sub_"):
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid subscription ID '{subscription_id}' on invoice {invoice_id}. "
            "Only subscription invoices are supported."
        )

    attempt_count = invoice_obj.get("attempt_count")
    if (
        attempt_count is None
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
    ):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid attempt_count '{attempt_count}' on invoice {invoice_id}. Expected integer >= 1."
        )

    amount_due = invoice_obj.get("amount_due")
    if (
        amount_due is None
        or isinstance(amount_due, bool)
        or not isinstance(amount_due, int)
        or amount_due < 0
    ):
        raise WebhookPermanentError(
            f"Event {event_id}: Invalid amount_due '{amount_due}' on invoice {invoice_id}. Expected non-negative integer."
        )

    currency = invoice_obj.get("currency")
    if not currency or not isinstance(currency, str) or len(currency.strip()) != 3:
        raise WebhookPermanentError(
            f"Event {event_id}: Missing or invalid currency '{currency}' on invoice {invoice_id}."
        )
    currency = currency.strip().lower()

    # Defensive parsing of optional fields
    amount_paid_raw = invoice_obj.get("amount_paid")
    if (
        amount_paid_raw is None
        or isinstance(amount_paid_raw, bool)
        or not isinstance(amount_paid_raw, int)
        or amount_paid_raw < 0
    ):
        amount_paid = 0
    else:
        amount_paid = amount_paid_raw

    raw_status = invoice_obj.get("status")
    if not raw_status or not isinstance(raw_status, str):
        raw_status = "open"

    billing_reason = invoice_obj.get("billing_reason")
    if billing_reason is not None and not isinstance(billing_reason, str):
        billing_reason = str(billing_reason)

    next_attempt_raw = invoice_obj.get("next_payment_attempt")
    next_attempt_dt = _parse_optional_positive_timestamp(next_attempt_raw, "next_payment_attempt", event_id)

    period_start_raw = invoice_obj.get("period_start")
    period_start_dt = _parse_optional_positive_timestamp(period_start_raw, "period_start", event_id)

    period_end_raw = invoice_obj.get("period_end")
    period_end_dt = _parse_optional_positive_timestamp(period_end_raw, "period_end", event_id)

    payment_intent_raw = invoice_obj.get("payment_intent")
    if isinstance(payment_intent_raw, dict):
        payment_intent_id = payment_intent_raw.get("id")
    elif isinstance(payment_intent_raw, str):
        payment_intent_id = payment_intent_raw
    else:
        payment_intent_id = None

    last_finalization_error = invoice_obj.get("last_finalization_error")
    failure_reason: str | None = None
    if isinstance(last_finalization_error, dict):
        failure_reason = (
            last_finalization_error.get("message")
            or last_finalization_error.get("decline_code")
            or last_finalization_error.get("code")
        )
    if not failure_reason or not isinstance(failure_reason, str):
        failure_reason = "Payment failed"

    # =========================================================================
    # 2. Advisory Locking (Invoice Serialization)
    # =========================================================================

    advisory_key = derive_invoice_advisory_lock_key(invoice_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key})

    # =========================================================================
    # 3. Non-Locking Tenant Identity Discovery
    # =========================================================================

    stmt_cust = (
        select(Subscription.organization_id, Subscription.stripe_subscription_id)
        .where(Subscription.stripe_customer_id == customer_id)
    )
    res_cust = await db.execute(stmt_cust)
    cust_row = res_cust.scalar_one_or_none()

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
            "Event %s: Received failed invoice %s for subscription %s under customer %s (org %s), "
            "but local subscription has no active subscription ID.",
            event_id,
            invoice_id,
            subscription_id,
            customer_id,
            org_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received failed invoice '{invoice_id}' for subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {org_id}) but local subscription record has no active subscription ID.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id="none",
            incoming_subscription_id=subscription_id,
            reason="unbound_customer_invoice_payment_failed",
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
                "Event %s: Invoice %s failure received for known superseded subscription '%s' (org %s). "
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
                    period_start=period_start_dt,
                    period_end=period_end_dt,
                    processing_status=InvoiceProcessingStatus.ignored,
                    billing_period_id=None,
                    raw_event_id=event_id,
                    diagnostic_payload=payload,
                )
                db.add(new_inv_hist)
            return

        # Unknown subscription ID -> quarantine
        logger.warning(
            "Event %s: Failed invoice %s received for unknown subscription '%s' under customer '%s' (org %s). Current is '%s'.",
            event_id,
            invoice_id,
            subscription_id,
            customer_id,
            org_id,
            cust_row.stripe_subscription_id,
        )
        raise WebhookReconciliationConflictError(
            f"Event {event_id}: Received failed invoice '{invoice_id}' for unknown subscription '{subscription_id}' "
            f"under customer '{customer_id}' (org {org_id}). Current subscription is '{cust_row.stripe_subscription_id}'.",
            organization_id=org_id,
            event_id=event_id,
            existing_subscription_id=cust_row.stripe_subscription_id,
            incoming_subscription_id=subscription_id,
            reason="unknown_subscription_invoice_payment_failed",
            diagnostic_payload=payload,
        )

    # Canceled Subscription Guard: Ignore failures on canceled subscriptions without mutating periods
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.organization_id == org_id,
            Subscription.stripe_subscription_id == subscription_id,
        )
    )
    res_sub = await db.execute(stmt_sub)
    target_sub = res_sub.scalar_one_or_none()

    if target_sub is not None and target_sub.status == SubscriptionStatus.canceled:
        logger.info(
            "Event %s: Failed invoice %s received for canceled subscription %s (org %s). "
            "Recording invoice as ignored.",
            event_id,
            invoice_id,
            subscription_id,
            org_id,
        )
        stmt_inv_canc = (
            select(StripeInvoice)
            .where(StripeInvoice.stripe_invoice_id == invoice_id)
            .with_for_update()
        )
        res_inv_canc = await db.execute(stmt_inv_canc)
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
                period_start=period_start_dt,
                period_end=period_end_dt,
                processing_status=InvoiceProcessingStatus.ignored,
                billing_period_id=None,
                raw_event_id=event_id,
                diagnostic_payload=payload,
            )
            db.add(new_inv_canc)
        return

    # =========================================================================
    # 4. Invoice Ledger Failure Persistence & Attempt History
    # =========================================================================

    now_utc = datetime.now(timezone.utc)
    stmt_invoice = (
        select(StripeInvoice)
        .where(StripeInvoice.stripe_invoice_id == invoice_id)
        .with_for_update()
    )
    res_invoice = await db.execute(stmt_invoice)
    existing_invoice = res_invoice.scalar_one_or_none()

    attempt_entry: dict[str, Any] = {
        "attempt": attempt_count,
        "event_id": event_id,
        "timestamp": event_timestamp.isoformat(),
        "amount_due": amount_due,
        "amount_paid": amount_paid,
        "currency": currency,
        "error_message": failure_reason,
        "next_payment_attempt": next_attempt_dt.isoformat() if next_attempt_dt else None,
        "payment_intent": payment_intent_id,
    }

    # Case 1: Initial failure record
    if existing_invoice is None:
        new_invoice = StripeInvoice(
            id=uuid.uuid4(),
            stripe_invoice_id=invoice_id,
            organization_id=org_id,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            billing_reason=billing_reason,
            invoice_status=raw_status,
            currency=currency,
            amount_paid_minor_units=amount_paid,
            period_start=period_start_dt,
            period_end=period_end_dt,
            processing_status=InvoiceProcessingStatus.failed,
            billing_period_id=None,
            raw_event_id=event_id,
            attempt_count=attempt_count,
            error_message=failure_reason,
            diagnostic_payload={"attempts": [attempt_entry]},
        )
        db.add(new_invoice)
        logger.info(
            "Event %s: Recorded initial payment failure for invoice %s (org %s, attempt=%d, due=%d %s)",
            event_id,
            invoice_id,
            org_id,
            attempt_count,
            amount_due,
            currency,
        )
        return

    # Case 2: Out-of-order failure arriving after invoice.paid
    if existing_invoice.processing_status == InvoiceProcessingStatus.processed:
        logger.warning(
            "Event %s: Invoice %s is already processed in ledger (billing_period_id=%s). "
            "Preserving processed status; recording stale failure diagnostic.",
            event_id,
            invoice_id,
            existing_invoice.billing_period_id,
        )
        payload_copy = dict(existing_invoice.diagnostic_payload or {})
        stale_list = list(payload_copy.get("stale_failures", []))
        stale_list.append(attempt_entry)
        payload_copy["stale_failures"] = stale_list
        existing_invoice.diagnostic_payload = payload_copy
        existing_invoice.updated_at = now_utc
        flag_modified(existing_invoice, "diagnostic_payload")
        return

    # Case 3: Terminal ignored invoice
    if existing_invoice.processing_status == InvoiceProcessingStatus.ignored:
        logger.info(
            "Event %s: Invoice %s is marked ignored in ledger. Preserving ignored status.",
            event_id,
            invoice_id,
        )
        payload_copy = dict(existing_invoice.diagnostic_payload or {})
        ignored_list = list(payload_copy.get("subsequent_failures", []))
        ignored_list.append(attempt_entry)
        payload_copy["subsequent_failures"] = ignored_list
        existing_invoice.diagnostic_payload = payload_copy
        existing_invoice.updated_at = now_utc
        flag_modified(existing_invoice, "diagnostic_payload")
        return

    # Case 4: Subsequent failure for pending or failed invoice
    payload_copy = dict(existing_invoice.diagnostic_payload or {})
    attempts_list = list(payload_copy.get("attempts", []))
    attempts_list.append(attempt_entry)
    payload_copy["attempts"] = attempts_list

    existing_invoice.attempt_count = max(existing_invoice.attempt_count, attempt_count)
    existing_invoice.error_message = failure_reason
    existing_invoice.raw_event_id = event_id
    existing_invoice.invoice_status = raw_status
    existing_invoice.processing_status = InvoiceProcessingStatus.failed
    existing_invoice.diagnostic_payload = payload_copy
    existing_invoice.updated_at = now_utc
    flag_modified(existing_invoice, "diagnostic_payload")

    logger.info(
        "Event %s: Updated payment failure attempt %d for invoice %s (org %s, total_attempts=%d)",
        event_id,
        attempt_count,
        invoice_id,
        org_id,
        existing_invoice.attempt_count,
    )


# Automatically register with default webhook dispatcher
default_webhook_dispatcher.register(
    "invoice.payment_failed",
    handle_invoice_payment_failed,
)
