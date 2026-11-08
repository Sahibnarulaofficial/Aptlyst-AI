"""Reusable handler foundation, payload validation, identity binding, and advisory locking for Razorpay.

Guarantees & Invariants:
1. Reusable Foundation:
   - Receives already claimed events (ClaimedWebhookEvent, BillingWebhookEvent, or validated payload).
   - Validates provider, merchant account, environment, signature verification, and event identity.
2. Strict Payload Validation:
   - Parses and validates event payloads using strongly typed dataclass schemas.
   - Enforces positive numeric Unix timestamps; rejects non-positive or non-numeric timestamps.
3. Authoritative Identity Binding:
   - Resolves tenant organization from authoritative database records (BillingCustomerProvision, Subscription).
   - Validates client-controlled metadata (notes.organization_id) against authoritative records.
   - Mismatches FAIL CLOSED with RazorpayWebhookReconciliationConflictError.
   - Unbound customers or subscriptions without existing associations are NEVER bound to client notes;
     they stage a reconciliation case and fail closed.
4. Error Categorization:
   - Retryable: Transient network, lock timeout, database serialization errors.
   - Permanent: Malformed payloads, missing mandatory IDs, scope mismatch, unverified signature.
   - Reconciliation Conflict: Unbound customer, metadata tenant mismatch, conflicting active subscriptions.
5. Diagnostic History Preservation:
   - Staging a PaymentReconciliation case never overwrites earlier diagnostic evidence.
   - Maintains an append-only timestamped history list under diagnostic_payload["history"].
6. Entity-Level Idempotency:
   - Supports monotonic timestamp validation independent of webhook event IDs.
   - Prevents terminal subscription resurrection (canceled/completed subscriptions remain terminal).
7. Stale Lease Protection:
   - Asserts active worker lease under lock before committing domain transactions.
   - Prevents stale workers whose lease expired from committing business mutations.
8. Cancellation Finalization Design:
   - Evaluates whether deferred cancellation during past_due should be finalized to 'canceled'
     after grace period expiration (past_due_since + 7d <= now).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.payment import (
    BillingCustomerProvision,
    BillingWebhookEvent,
    PaymentReconciliation,
)
from app.models.subscription import (
    ReconciliationStatus,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.razorpay.lock import (
    acquire_razorpay_advisory_lock,
    derive_razorpay_advisory_lock_key,
    try_acquire_razorpay_advisory_lock,
)
from app.services.billing.razorpay.webhook_engine import (
    ClaimedWebhookEvent,
    StaleLeaseError,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Error Hierarchy
# =============================================================================


class RazorpayWebhookHandlerError(Exception):
    """Base error for all Razorpay business webhook handler processing."""


class RazorpayWebhookRetryableError(RazorpayWebhookHandlerError):
    """Raised for transient errors (lock contention, serialization failure, network glitches)

    Worker will increment attempts and schedule exponential retry backoff.
    """


class RazorpayWebhookPermanentError(RazorpayWebhookHandlerError):
    """Raised for permanent errors that must quarantine immediately without retry.

    Includes malformed payloads, unverified signatures, missing required IDs.
    """


class RazorpayWebhookReconciliationConflictError(RazorpayWebhookPermanentError):
    """Raised when an event conflicts with authoritative state and requires audit reconciliation.

    Stages a PaymentReconciliation case and quarantines the event.
    """

    def __init__(
        self,
        message: str,
        *,
        case_key: str,
        reason: str,
        organization_id: uuid.UUID | None = None,
        subscription_id: uuid.UUID | None = None,
        invoice_id: uuid.UUID | None = None,
        payment_id: uuid.UUID | None = None,
        raw_event_id: uuid.UUID | None = None,
        diagnostic_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.case_key = case_key
        self.reason = reason
        self.organization_id = organization_id
        self.subscription_id = subscription_id
        self.invoice_id = invoice_id
        self.payment_id = payment_id
        self.raw_event_id = raw_event_id
        self.diagnostic_payload = diagnostic_payload or {}


class RazorpayPayloadValidationError(RazorpayWebhookPermanentError):
    """Raised when event payload violates strict schema or validation rules."""


class RazorpayScopeMismatchError(RazorpayWebhookPermanentError):
    """Raised when provider, merchant account, or environment does not match expected scope."""


class RazorpayMissingEntityIdError(RazorpayWebhookPermanentError):
    """Raised when a mandatory provider entity identifier (subscription_id, payment_id, invoice_id) is missing."""


# =============================================================================
# Typed Entity Schemas
# =============================================================================


@dataclass(frozen=True)
class RazorpaySubscriptionEntity:
    """Strongly-typed parsed representation of a Razorpay subscription entity."""

    id: str
    entity: str
    plan_id: str
    customer_id: str | None
    status: str
    current_start: int | None
    current_end: int | None
    ended_at: int | None
    charge_at: int | None
    start_at: int | None
    end_at: int | None
    total_count: int | None
    paid_count: int | None
    remaining_count: int | None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RazorpayPaymentEntity:
    """Strongly-typed parsed representation of a Razorpay payment entity."""

    id: str
    entity: str
    amount: int  # in paise (minor units)
    currency: str
    status: str
    order_id: str | None
    invoice_id: str | None
    customer_id: str | None
    method: str | None
    amount_refunded: int = 0
    refund_status: str | None = None
    description: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_description: str | None = None
    created_at: int | None = None


@dataclass(frozen=True)
class RazorpayInvoiceEntity:
    """Strongly-typed parsed representation of a Razorpay invoice entity."""

    id: str
    entity: str
    subscription_id: str | None
    customer_id: str | None
    order_id: str | None
    payment_id: str | None
    status: str
    amount: int
    amount_paid: int
    amount_due: int
    currency: str
    billing_start: int | None
    billing_end: int | None
    paid_at: int | None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RazorpayEventContext:
    """Consolidated execution context for an ingested, validated Razorpay webhook event."""

    event_id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    provider_event_id: str
    event_type: str
    event_created_at: datetime
    signature_verified: bool
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    locked_by: str | None
    raw_payload: dict[str, Any]
    subscription: RazorpaySubscriptionEntity | None = None
    payment: RazorpayPaymentEntity | None = None
    invoice: RazorpayInvoiceEntity | None = None


# =============================================================================
# Helper & Timestamp Validation Functions
# =============================================================================


def parse_positive_unix_timestamp(val: Any, field_name: str = "timestamp") -> datetime:
    """Validate that val is a genuine positive numeric Unix timestamp and convert to UTC datetime.

    Raises:
        RazorpayPayloadValidationError: If timestamp is non-positive, non-numeric, or malformed.
    """
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        raise RazorpayPayloadValidationError(
            f"Invalid or missing {field_name} timestamp '{val}'. Expected positive numeric integer."
        )
    if val <= 0:
        raise RazorpayPayloadValidationError(
            f"Timestamp {field_name} must be strictly positive, got '{val}'."
        )
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise RazorpayPayloadValidationError(
            f"Malformed timestamp {field_name} '{val}': {exc}"
        ) from exc


# =============================================================================
# Entity Extraction & Parsing
# =============================================================================


def extract_subscription_entity(payload: dict[str, Any]) -> RazorpaySubscriptionEntity:
    """Extract and strictly validate a subscription entity from event payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    sub_dict = inner.get("subscription", {}).get("entity") if "subscription" in inner else None
    if sub_dict is None and inner.get("entity") == "subscription":
        sub_dict = inner
    if sub_dict is None and "subscription" in payload:
        sub_dict = payload.get("subscription", {}).get("entity")
    if sub_dict is None and payload.get("entity") == "subscription":
        sub_dict = payload

    if not isinstance(sub_dict, dict):
        raise RazorpayPayloadValidationError("Missing or invalid subscription entity in payload.")

    sub_id = sub_dict.get("id")
    if not sub_id or not isinstance(sub_id, str) or not sub_id.strip():
        raise RazorpayMissingEntityIdError("Missing mandatory subscription id.")

    plan_id = sub_dict.get("plan_id")
    if not plan_id or not isinstance(plan_id, str) or not plan_id.strip():
        raise RazorpayPayloadValidationError("Missing mandatory subscription plan_id.")

    status = sub_dict.get("status")
    if not status or not isinstance(status, str) or not status.strip():
        raise RazorpayPayloadValidationError("Missing mandatory subscription status.")

    return RazorpaySubscriptionEntity(
        id=sub_id.strip(),
        entity=str(sub_dict.get("entity", "subscription")).strip(),
        plan_id=plan_id.strip(),
        customer_id=str(sub_dict["customer_id"]).strip() if sub_dict.get("customer_id") else None,
        status=status.strip().lower(),
        current_start=sub_dict.get("current_start"),
        current_end=sub_dict.get("current_end"),
        ended_at=sub_dict.get("ended_at"),
        charge_at=sub_dict.get("charge_at"),
        start_at=sub_dict.get("start_at"),
        end_at=sub_dict.get("end_at"),
        total_count=sub_dict.get("total_count"),
        paid_count=sub_dict.get("paid_count"),
        remaining_count=sub_dict.get("remaining_count"),
        notes=sub_dict.get("notes") if isinstance(sub_dict.get("notes"), dict) else {},
    )


def extract_payment_entity(payload: dict[str, Any]) -> RazorpayPaymentEntity:
    """Extract and strictly validate a payment entity from event payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    pay_dict = inner.get("payment", {}).get("entity") if "payment" in inner else None
    if pay_dict is None and inner.get("entity") == "payment":
        pay_dict = inner
    if pay_dict is None and "payment" in payload:
        pay_dict = payload.get("payment", {}).get("entity")
    if pay_dict is None and payload.get("entity") == "payment":
        pay_dict = payload

    if not isinstance(pay_dict, dict):
        raise RazorpayPayloadValidationError("Missing or invalid payment entity in payload.")

    pay_id = pay_dict.get("id")
    if not pay_id or not isinstance(pay_id, str) or not pay_id.strip():
        raise RazorpayMissingEntityIdError("Missing mandatory payment id.")

    amount = pay_dict.get("amount")
    if amount is None or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise RazorpayPayloadValidationError(
            f"Payment amount must be a positive integer in minor units (paise), got '{amount}'."
        )

    currency = pay_dict.get("currency")
    if not currency or not isinstance(currency, str) or len(currency.strip()) != 3:
        raise RazorpayPayloadValidationError(f"Invalid currency '{currency}'; expected 3-letter ISO code.")

    status = pay_dict.get("status")
    if not status or not isinstance(status, str) or not status.strip():
        raise RazorpayPayloadValidationError("Missing mandatory payment status.")

    amount_refunded = pay_dict.get("amount_refunded", 0)
    if isinstance(amount_refunded, bool) or not isinstance(amount_refunded, int) or amount_refunded < 0:
        raise RazorpayPayloadValidationError(f"Invalid amount_refunded: '{amount_refunded}'.")

    return RazorpayPaymentEntity(
        id=pay_id.strip(),
        entity=str(pay_dict.get("entity", "payment")).strip(),
        amount=amount,
        currency=currency.strip().upper(),
        status=status.strip().lower(),
        order_id=str(pay_dict["order_id"]).strip() if pay_dict.get("order_id") else None,
        invoice_id=str(pay_dict["invoice_id"]).strip() if pay_dict.get("invoice_id") else None,
        customer_id=str(pay_dict["customer_id"]).strip() if pay_dict.get("customer_id") else None,
        method=str(pay_dict["method"]).strip() if pay_dict.get("method") else None,
        amount_refunded=amount_refunded,
        refund_status=str(pay_dict["refund_status"]).strip() if pay_dict.get("refund_status") else None,
        description=str(pay_dict["description"]).strip() if pay_dict.get("description") else None,
        notes=pay_dict.get("notes") if isinstance(pay_dict.get("notes"), dict) else {},
        error_code=str(pay_dict["error_code"]).strip() if pay_dict.get("error_code") else None,
        error_description=str(pay_dict["error_description"]).strip() if pay_dict.get("error_description") else None,
        created_at=pay_dict.get("created_at"),
    )


def extract_invoice_entity(payload: dict[str, Any]) -> RazorpayInvoiceEntity:
    """Extract and strictly validate an invoice entity from event payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    inv_dict = inner.get("invoice", {}).get("entity") if "invoice" in inner else None
    if inv_dict is None and inner.get("entity") == "invoice":
        inv_dict = inner
    if inv_dict is None and "invoice" in payload:
        inv_dict = payload.get("invoice", {}).get("entity")
    if inv_dict is None and payload.get("entity") == "invoice":
        inv_dict = payload

    if not isinstance(inv_dict, dict):
        raise RazorpayPayloadValidationError("Missing or invalid invoice entity in payload.")

    inv_id = inv_dict.get("id")
    if not inv_id or not isinstance(inv_id, str) or not inv_id.strip():
        raise RazorpayMissingEntityIdError("Missing mandatory invoice id.")

    amount = inv_dict.get("amount")
    if amount is None or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise RazorpayPayloadValidationError(f"Invalid invoice amount: '{amount}'.")

    amount_paid = inv_dict.get("amount_paid", 0)
    if isinstance(amount_paid, bool) or not isinstance(amount_paid, int) or amount_paid < 0:
        raise RazorpayPayloadValidationError(f"Invalid invoice amount_paid: '{amount_paid}'.")

    amount_due = inv_dict.get("amount_due", 0)
    if isinstance(amount_due, bool) or not isinstance(amount_due, int) or amount_due < 0:
        raise RazorpayPayloadValidationError(f"Invalid invoice amount_due: '{amount_due}'.")

    status = inv_dict.get("status")
    if not status or not isinstance(status, str) or not status.strip():
        raise RazorpayPayloadValidationError("Missing mandatory invoice status.")

    currency = inv_dict.get("currency", "INR")

    return RazorpayInvoiceEntity(
        id=inv_id.strip(),
        entity=str(inv_dict.get("entity", "invoice")).strip(),
        subscription_id=str(inv_dict["subscription_id"]).strip() if inv_dict.get("subscription_id") else None,
        customer_id=str(inv_dict["customer_id"]).strip() if inv_dict.get("customer_id") else None,
        order_id=str(inv_dict["order_id"]).strip() if inv_dict.get("order_id") else None,
        payment_id=str(inv_dict["payment_id"]).strip() if inv_dict.get("payment_id") else None,
        status=status.strip().lower(),
        amount=amount,
        amount_paid=amount_paid,
        amount_due=amount_due,
        currency=str(currency).strip().upper(),
        billing_start=inv_dict.get("billing_start"),
        billing_end=inv_dict.get("billing_end"),
        paid_at=inv_dict.get("paid_at"),
        notes=inv_dict.get("notes") if isinstance(inv_dict.get("notes"), dict) else {},
    )


def extract_subscription_id(payload: dict[str, Any]) -> str:
    """Extract and validate provider subscription ID from payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

    for source in (inner, payload):
        # 1. From subscription.entity.id
        sub_id = source.get("subscription", {}).get("entity", {}).get("id")
        if sub_id and isinstance(sub_id, str) and sub_id.strip():
            return sub_id.strip()

        # 2. From invoice.entity.subscription_id
        sub_id = source.get("invoice", {}).get("entity", {}).get("subscription_id")
        if sub_id and isinstance(sub_id, str) and sub_id.strip():
            return sub_id.strip()

        # 3. Direct subscription_id
        sub_id = source.get("subscription_id")
        if sub_id and isinstance(sub_id, str) and sub_id.strip():
            return sub_id.strip()

        # 4. Direct entity if it's a subscription
        if source.get("entity") == "subscription" and source.get("id"):
            return str(source["id"]).strip()

    raise RazorpayMissingEntityIdError("Missing mandatory provider subscription_id in payload.")


def extract_payment_id(payload: dict[str, Any]) -> str:
    """Extract and validate provider payment ID from payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

    for source in (inner, payload):
        pay_id = source.get("payment", {}).get("entity", {}).get("id")
        if pay_id and isinstance(pay_id, str) and pay_id.strip():
            return pay_id.strip()

        pay_id = source.get("payment_id")
        if pay_id and isinstance(pay_id, str) and pay_id.strip():
            return pay_id.strip()

        if source.get("entity") == "payment" and source.get("id"):
            return str(source["id"]).strip()

    raise RazorpayMissingEntityIdError("Missing mandatory provider payment_id in payload.")


def extract_invoice_id(payload: dict[str, Any]) -> str:
    """Extract and validate provider invoice ID from payload."""
    if not isinstance(payload, dict):
        raise RazorpayPayloadValidationError("Payload must be a dictionary.")

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

    for source in (inner, payload):
        inv_id = source.get("invoice", {}).get("entity", {}).get("id")
        if inv_id and isinstance(inv_id, str) and inv_id.strip():
            return inv_id.strip()

        inv_id = source.get("invoice_id")
        if inv_id and isinstance(inv_id, str) and inv_id.strip():
            return inv_id.strip()

        if source.get("entity") == "invoice" and source.get("id"):
            return str(source["id"]).strip()

    raise RazorpayMissingEntityIdError("Missing mandatory provider invoice_id in payload.")


def extract_customer_id(payload: dict[str, Any]) -> str | None:
    """Safely extract customer_id across subscription, payment, invoice, or root entities."""
    if not isinstance(payload, dict):
        return None

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

    for source in (inner, payload):
        # Check subscription entity
        cid = source.get("subscription", {}).get("entity", {}).get("customer_id")
        if cid and isinstance(cid, str) and cid.strip():
            return cid.strip()

        # Check payment entity
        cid = source.get("payment", {}).get("entity", {}).get("customer_id")
        if cid and isinstance(cid, str) and cid.strip():
            return cid.strip()

        # Check invoice entity
        cid = source.get("invoice", {}).get("entity", {}).get("customer_id")
        if cid and isinstance(cid, str) and cid.strip():
            return cid.strip()

        # Check direct customer_id
        cid = source.get("customer_id")
        if cid and isinstance(cid, str) and cid.strip():
            return cid.strip()

    return None


# =============================================================================
# Envelope & Scope Validation
# =============================================================================


def validate_razorpay_event_envelope(
    event_or_payload: ClaimedWebhookEvent | BillingWebhookEvent | dict[str, Any],
    expected_merchant_id: str | None = None,
    expected_environment: str | None = None,
) -> RazorpayEventContext:
    """Validate provider, merchant account, environment, signature, and envelope payload.

    Args:
        event_or_payload: ClaimedWebhookEvent, BillingWebhookEvent, or payload dictionary.
        expected_merchant_id: Expected merchant account ID (defaults to settings.razorpay_merchant_account_id).
        expected_environment: Expected environment (defaults to settings.razorpay_environment).

    Raises:
        RazorpayScopeMismatchError: If provider, merchant_account_id, or environment mismatches.
        RazorpayWebhookPermanentError: If signature_verified is False or envelope is invalid.
        RazorpayPayloadValidationError: If inner JSON structure is malformed.

    Returns:
        RazorpayEventContext: Validated context object with typed entities.
    """
    configured_merchant = expected_merchant_id or (settings.razorpay_merchant_account_id or "").strip()
    configured_env = expected_environment or (settings.razorpay_environment or "").strip().lower()

    if isinstance(event_or_payload, (ClaimedWebhookEvent, BillingWebhookEvent)):
        event_model = event_or_payload
        # 1. Provider validation
        if event_model.provider.strip().lower() != "razorpay":
            raise RazorpayScopeMismatchError(
                f"Invalid provider '{event_model.provider}'. Expected 'razorpay'."
            )

        # 2. Signature verification guard (Fail closed)
        sig_verified = getattr(event_model, "signature_verified", True)
        if not sig_verified:
            raise RazorpayWebhookPermanentError(
                f"Event {getattr(event_model, 'provider_event_id', 'unknown')}: signature_verified is False. "
                "Unverified webhook events must never be processed."
            )

        # 3. Scope validation
        if configured_merchant and configured_merchant != "default":
            if event_model.merchant_account_id.strip() != configured_merchant:
                raise RazorpayScopeMismatchError(
                    f"Merchant account mismatch: event merchant '{event_model.merchant_account_id}' "
                    f"does not match expected '{configured_merchant}'."
                )

        if configured_env:
            if event_model.environment.strip().lower() != configured_env:
                raise RazorpayScopeMismatchError(
                    f"Environment mismatch: event environment '{event_model.environment}' "
                    f"does not match expected '{configured_env}'."
                )

        raw_payload = event_model.payload
        event_id = getattr(event_model, "event_id", getattr(event_model, "id", uuid.uuid4()))
        provider_event_id = event_model.provider_event_id
        event_type = event_model.event_type
        lease_token = getattr(event_model, "lease_token", None)
        lease_expires_at = getattr(event_model, "lease_expires_at", None)
        locked_by = getattr(event_model, "locked_by", None)
    elif isinstance(event_or_payload, dict):
        raw_payload = event_or_payload
        event_id = uuid.uuid4()
        provider_event_id = str(raw_payload.get("event_id") or raw_payload.get("id") or f"evt_rzp_{uuid.uuid4().hex[:12]}")
        event_type = str(raw_payload.get("event") or "").strip()
        lease_token = None
        lease_expires_at = None
        locked_by = None
    else:
        raise RazorpayPayloadValidationError(
            f"Unsupported event parameter type: {type(event_or_payload)}. Expected event model or dict."
        )

    # 4. Envelope payload validation
    if not isinstance(raw_payload, dict):
        raise RazorpayPayloadValidationError("Webhook payload must be a JSON object dictionary.")

    # Validate mandatory envelope keys if payload is an outer envelope
    if raw_payload.get("entity") == "event":
        payload_account_id = raw_payload.get("account_id")
        if not payload_account_id or not isinstance(payload_account_id, str):
            raise RazorpayPayloadValidationError("Missing or invalid 'account_id' in webhook envelope.")

        if configured_merchant and configured_merchant != "default":
            if payload_account_id.strip() != configured_merchant:
                raise RazorpayScopeMismatchError(
                    f"Payload account_id '{payload_account_id}' does not match expected merchant '{configured_merchant}'."
                )

        inner_payload = raw_payload.get("payload")
        if inner_payload is None or not isinstance(inner_payload, dict):
            raise RazorpayPayloadValidationError("Missing or invalid 'payload' object in event envelope.")
    else:
        inner_payload = raw_payload

    # Event timestamp
    created_at_num = raw_payload.get("created_at")
    if created_at_num is not None:
        try:
            event_created_at = parse_positive_unix_timestamp(created_at_num, "created_at")
        except RazorpayPayloadValidationError:
            event_created_at = datetime.now(timezone.utc)
    else:
        event_created_at = datetime.now(timezone.utc)

    # Parse entities if present in inner payload
    subscription_entity: RazorpaySubscriptionEntity | None = None
    if "subscription" in inner_payload or inner_payload.get("entity") == "subscription":
        try:
            subscription_entity = extract_subscription_entity(inner_payload)
        except (RazorpayPayloadValidationError, RazorpayMissingEntityIdError):
            subscription_entity = None

    payment_entity: RazorpayPaymentEntity | None = None
    if "payment" in inner_payload or inner_payload.get("entity") == "payment":
        try:
            payment_entity = extract_payment_entity(inner_payload)
        except (RazorpayPayloadValidationError, RazorpayMissingEntityIdError):
            payment_entity = None

    invoice_entity: RazorpayInvoiceEntity | None = None
    if "invoice" in inner_payload or inner_payload.get("entity") == "invoice":
        try:
            invoice_entity = extract_invoice_entity(inner_payload)
        except (RazorpayPayloadValidationError, RazorpayMissingEntityIdError):
            invoice_entity = None

    return RazorpayEventContext(
        event_id=event_id,
        provider="razorpay",
        merchant_account_id=configured_merchant or "default",
        environment=configured_env or "live",
        provider_event_id=provider_event_id,
        event_type=event_type,
        event_created_at=event_created_at,
        signature_verified=True,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        locked_by=locked_by,
        raw_payload=raw_payload,
        subscription=subscription_entity,
        payment=payment_entity,
        invoice=invoice_entity,
    )


# =============================================================================
# Authoritative Tenant Organization Resolution
# =============================================================================


async def resolve_tenant_organization(
    db: AsyncSession,
    provider: str = "razorpay",
    merchant_account_id: str = "default",
    environment: str = "live",
    customer_id: str | None = None,
    subscription_id: str | None = None,
    notes: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Resolve the authoritative organization_id for an incoming event.

    Security Criteria:
    1. Authoritative Lookups: Queries BillingCustomerProvision and Subscription records.
    2. Conflict Detection: If customer provision and subscription point to different orgs,
       raises RazorpayWebhookReconciliationConflictError.
    3. Metadata Verification: If notes contains 'organization_id', verifies that it exactly
       matches the authoritative organization_id. Mismatches FAIL CLOSED.
    4. Anti-Spoofing: Unbound customers/subscriptions NEVER bind to client notes alone.
       Fails closed with RazorpayWebhookReconciliationConflictError.

    Raises:
        RazorpayWebhookReconciliationConflictError: On unbound entity, conflicting records, or metadata mismatch.
        RazorpayPayloadValidationError: If notes.organization_id is not a valid UUID string.

    Returns:
        uuid.UUID: The bound authoritative organization_id.
    """
    clean_customer_id = customer_id.strip() if customer_id else None
    clean_subscription_id = subscription_id.strip() if subscription_id else None

    # 1. Authoritative Customer Provision Lookup
    prov_org_id: uuid.UUID | None = None
    if clean_customer_id:
        stmt_prov = select(BillingCustomerProvision.organization_id).where(
            BillingCustomerProvision.provider == provider,
            BillingCustomerProvision.merchant_account_id == merchant_account_id,
            BillingCustomerProvision.environment == environment,
            BillingCustomerProvision.provider_customer_id == clean_customer_id,
        )
        res_prov = await db.execute(stmt_prov)
        prov_org_id = res_prov.scalar_one_or_none()

    # 2. Authoritative Subscription Lookup
    sub_org_id: uuid.UUID | None = None
    if clean_subscription_id:
        stmt_sub = select(Subscription.organization_id).where(
            Subscription.provider == provider,
            Subscription.merchant_account_id == merchant_account_id,
            Subscription.environment == environment,
            Subscription.provider_subscription_id == clean_subscription_id,
        )
        res_sub = await db.execute(stmt_sub)
        sub_org_id = res_sub.scalar_one_or_none()

    # 3. Cross-Validation of Authoritative Records
    if prov_org_id is not None and sub_org_id is not None:
        if prov_org_id != sub_org_id:
            logger.warning(
                "Authoritative tenant conflict: customer %s belongs to org %s but subscription %s belongs to org %s",
                clean_customer_id,
                prov_org_id,
                clean_subscription_id,
                sub_org_id,
            )
            raise RazorpayWebhookReconciliationConflictError(
                f"Conflicting authoritative records for customer '{clean_customer_id}' and subscription '{clean_subscription_id}'.",
                case_key=f"tenant_conflict:cust_{clean_customer_id}:sub_{clean_subscription_id}",
                reason="conflicting_tenant_records",
                organization_id=prov_org_id,
                diagnostic_payload={
                    "customer_id": clean_customer_id,
                    "customer_org_id": str(prov_org_id),
                    "subscription_id": clean_subscription_id,
                    "subscription_org_id": str(sub_org_id),
                },
            )

    authoritative_org_id = prov_org_id or sub_org_id

    # 4. Client Metadata Validation (notes.organization_id)
    notes_org_id: uuid.UUID | None = None
    if notes and isinstance(notes, dict) and "organization_id" in notes:
        raw_notes_org = notes.get("organization_id")
        try:
            notes_org_id = uuid.UUID(str(raw_notes_org).strip())
        except (ValueError, TypeError, AttributeError) as exc:
            raise RazorpayPayloadValidationError(
                f"Malformed 'organization_id' in notes: '{raw_notes_org}'. Expected valid UUID."
            ) from exc

    # Case A: Authoritative record exists
    if authoritative_org_id is not None:
        if notes_org_id is not None and notes_org_id != authoritative_org_id:
            logger.warning(
                "Tenant metadata mismatch: payload notes claim org %s, but gateway entity belongs to org %s",
                notes_org_id,
                authoritative_org_id,
            )
            raise RazorpayWebhookReconciliationConflictError(
                f"Metadata tenant mismatch: notes claim org '{notes_org_id}', but entity is bound to '{authoritative_org_id}'.",
                case_key=f"metadata_mismatch:{authoritative_org_id}:{notes_org_id}",
                reason="metadata_tenant_mismatch",
                organization_id=authoritative_org_id,
                diagnostic_payload={
                    "authoritative_org_id": str(authoritative_org_id),
                    "notes_organization_id": str(notes_org_id),
                    "customer_id": clean_customer_id,
                    "subscription_id": clean_subscription_id,
                },
            )
        return authoritative_org_id

    # Case B: No authoritative record found (Unbound customer/subscription)
    entity_ref = clean_subscription_id or clean_customer_id or "unspecified_entity"
    logger.warning(
        "Unbound Razorpay entity %s: no authoritative customer provision or subscription found.",
        entity_ref,
    )
    raise RazorpayWebhookReconciliationConflictError(
        f"Unbound Razorpay entity '{entity_ref}'. No local customer provision or subscription found.",
        case_key=f"unbound_entity:{entity_ref}",
        reason="unbound_provider_customer",
        organization_id=notes_org_id,
        diagnostic_payload={
            "customer_id": clean_customer_id,
            "subscription_id": clean_subscription_id,
            "notes_claimed_organization_id": str(notes_org_id) if notes_org_id else None,
        },
    )


# =============================================================================
# Diagnostic History Preservation & Reconciliation Staging
# =============================================================================


async def stage_or_update_payment_reconciliation(
    db: AsyncSession,
    case_key: str,
    reason: str,
    provider: str = "razorpay",
    merchant_account_id: str = "default",
    environment: str = "live",
    organization_id: uuid.UUID | None = None,
    subscription_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    raw_event_id: uuid.UUID | None = None,
    diagnostic_data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> PaymentReconciliation:
    """Stage or update a payment reconciliation case preserving all historical evidence.

    Guarantees:
    - Queries existing case under (provider, merchant_account_id, environment, case_key).
    - If case exists: appends timestamped diagnostic entry to diagnostic_payload["history"]
      and updates diagnostic_payload["latest"]. Never overwrites earlier diagnostic evidence.
    - If case does not exist: creates new record with initial history entry.
    """
    now_utc = now or datetime.now(timezone.utc)
    clean_data = diagnostic_data or {}

    stmt = (
        select(PaymentReconciliation)
        .where(
            PaymentReconciliation.provider == provider,
            PaymentReconciliation.merchant_account_id == merchant_account_id,
            PaymentReconciliation.environment == environment,
            PaymentReconciliation.case_key == case_key,
        )
        .with_for_update()
    )
    res = await db.execute(stmt)
    existing = res.scalar_one_or_none()

    new_history_entry = {
        "timestamp": now_utc.isoformat(),
        "reason": reason,
        "details": clean_data,
    }

    if existing is not None:
        # Preserve existing history without overwriting
        existing_history = list(existing.diagnostic_payload.get("history", []))
        existing_history.append(new_history_entry)

        # Update diagnostic payload immutably
        existing.diagnostic_payload = {
            **existing.diagnostic_payload,
            "history": existing_history,
            "latest": clean_data,
        }
        existing.updated_at = now_utc
        if organization_id and not existing.organization_id:
            existing.organization_id = organization_id
        if subscription_id and not existing.subscription_id:
            existing.subscription_id = subscription_id
        if invoice_id and not existing.invoice_id:
            existing.invoice_id = invoice_id
        if payment_id and not existing.payment_id:
            existing.payment_id = payment_id
        if raw_event_id and not existing.raw_event_id:
            existing.raw_event_id = raw_event_id

        await db.flush()
        return existing

    # Create new reconciliation case
    new_case = PaymentReconciliation(
        id=uuid.uuid4(),
        provider=provider,
        merchant_account_id=merchant_account_id,
        environment=environment,
        case_key=case_key,
        organization_id=organization_id,
        subscription_id=subscription_id,
        invoice_id=invoice_id,
        payment_id=payment_id,
        raw_event_id=raw_event_id,
        reason=reason,
        status=ReconciliationStatus.pending_review,
        diagnostic_payload={
            "history": [new_history_entry],
            "latest": clean_data,
        },
        created_at=now_utc,
        updated_at=now_utc,
    )
    db.add(new_case)
    await db.flush()
    return new_case


# =============================================================================
# Entity-Level Idempotency Helpers
# =============================================================================


def ensure_utc_datetime(dt: datetime) -> datetime:
    """Normalize a datetime instance to a timezone-aware UTC datetime.

    If an offset-naive datetime is supplied, attaches timezone.utc defensively.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_stale_event(
    current_entity_timestamp: datetime | None,
    incoming_event_timestamp: datetime,
) -> bool:
    """Determine whether an incoming event is chronologically stale compared to local state.

    Returns True if an earlier or equal event was already processed.
    """
    if current_entity_timestamp is None:
        return False
    curr_utc = ensure_utc_datetime(current_entity_timestamp)
    incoming_utc = ensure_utc_datetime(incoming_event_timestamp)
    return incoming_utc <= curr_utc


def is_terminal_subscription(status: SubscriptionStatus | str) -> bool:
    """Check whether a subscription is in a terminal status ('canceled', 'completed').

    Refusal of resurrection: terminal subscriptions cannot be reactivated by replay events.
    """
    val = status.value if isinstance(status, SubscriptionStatus) else str(status)
    return val.strip().lower() in ("canceled", "completed")


# =============================================================================
# Stale Lease & Fencing Guard
# =============================================================================


async def assert_valid_worker_lease(
    db: AsyncSession,
    event_id: uuid.UUID,
    expected_lease_token: uuid.UUID,
    expected_worker_id: str,
    now: datetime | None = None,
) -> None:
    """Verify under row lock that worker still owns an unexpired lease before committing business mutations.

    If lease ownership was lost, replaced, or expired, rolls back the transaction immediately
    and raises StaleLeaseError.
    """
    now_utc = now or datetime.now(timezone.utc)

    stmt = (
        select(BillingWebhookEvent)
        .where(BillingWebhookEvent.id == event_id)
        .with_for_update()
    )
    res = await db.execute(stmt)
    event = res.scalar_one_or_none()

    if event is None:
        await db.rollback()
        raise StaleLeaseError(f"Event '{event_id}' not found during lease assertion.")

    is_same_token = event.lease_token == expected_lease_token
    is_same_worker = event.locked_by == expected_worker_id
    is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now_utc

    if not (is_same_token and is_same_worker and is_unexpired):
        await db.rollback()
        logger.warning(
            "Worker %s lost lease on event %s before committing domain transaction (token_match=%s, worker_match=%s, unexpired=%s)",
            expected_worker_id,
            event_id,
            is_same_token,
            is_same_worker,
            is_unexpired,
        )
        raise StaleLeaseError(
            f"Worker '{expected_worker_id}' lost lease on event '{event_id}'. "
            "Lease expired, was replaced, or belongs to another worker."
        )


# =============================================================================
# Cancellation Finalization Predicate (Design Note Implementation)
# =============================================================================


def should_finalize_deferred_cancellation(
    status: SubscriptionStatus | str,
    cancel_at_period_end: bool,
    past_due_since: datetime | None,
    grace_period_days: int = 7,
    now: datetime | None = None,
) -> bool:
    """Determine whether a deferred cancellation during past_due should be finalized to canceled.

    Design Rule:
    When canceled during grace, Subscription.status remains 'past_due' with
    cancel_at_period_end = True. Once past_due_since + 7d <= now, access is already
    blocked by accounting.py; this predicate signals that local Subscription.status
    must now transition to terminal 'canceled' and plan_tier to 'free'.
    """
    now_utc = now or datetime.now(timezone.utc)
    val = status.value if isinstance(status, SubscriptionStatus) else str(status)

    if val.strip().lower() != "past_due":
        return False
    if not cancel_at_period_end:
        return False
    if past_due_since is None:
        return False

    past_due_utc = ensure_utc_datetime(past_due_since)
    current_utc = ensure_utc_datetime(now_utc)
    grace_deadline = past_due_utc + timedelta(days=grace_period_days)
    return current_utc >= grace_deadline
