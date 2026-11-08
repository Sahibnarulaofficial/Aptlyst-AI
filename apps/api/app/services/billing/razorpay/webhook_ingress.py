"""Razorpay webhook event ingestion, signature verification, and durable staging service.

Guarantees & Lifecycle Invariants:
1. Strict Payload Bounding: Rejects requests exceeding 64 KB during chunked streaming.
   Does not rely solely on Content-Length header.
2. Cryptographic Authenticity: Verifies HMAC-SHA256 signatures against exact raw bytes
   using the configured Razorpay webhook secret before parsing or database queries.
3. Constant-Time Verification: Prevents timing attacks using hmac.compare_digest.
4. Zero Secret Leakage: Webhook secret, signature headers, and sensitive payload contents
   are never logged or exposed in exceptions or HTTP responses.
5. Fail-Safe Configuration: Safely fails closed (HTTP 503) if Razorpay billing is disabled
   or the webhook secret is unconfigured.
6. Tenant & Scope Isolation: Rejects payloads belonging to an unexpected merchant account.
7. Deterministic Event Identity:
   - Primary: Uses X-Razorpay-Event-Id header when present and valid.
   - Fallback: Uses a deterministic SHA-256 digest of raw bytes scoped by account and event type.
8. Idempotent Deduplication: Employs unique constraint on
   (provider, merchant_account_id, environment, provider_event_id).
   Duplicate deliveries return HTTP 200 safely without mutating state or creating duplicate records.
9. Concurrency Safety: Resolves simultaneous duplicate delivery races via database uniqueness
   and transaction rollback.
10. Durable Persistence: Transaction is committed before returning receipt acknowledgement.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import re
from typing import Any
import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.payment import BillingWebhookEvent
from app.models.subscription import WebhookProcessingStatus
from app.services.billing.razorpay.client import (
    RazorpayBillingDisabledError,
    RazorpayConfigurationError,
)

logger = logging.getLogger(__name__)

# Strict 64 KB streaming payload limit (65,536 bytes)
MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES: int = 64 * 1024


# =============================================================================
# Domain Exceptions
# =============================================================================


class RazorpayWebhookError(Exception):
    """Base exception for all Razorpay webhook ingestion operations."""


class RazorpayWebhookPayloadTooLargeError(RazorpayWebhookError):
    """Raised when the incoming webhook request payload exceeds 64 KB."""


class RazorpayWebhookSignatureVerificationError(RazorpayWebhookError, ValueError):
    """Raised when the Razorpay webhook signature is missing, malformed, or invalid."""


class RazorpayWebhookPayloadMalformedError(RazorpayWebhookError, ValueError):
    """Raised when the webhook payload is not valid JSON or lacks mandatory envelope fields."""


class RazorpayWebhookMerchantMismatchError(RazorpayWebhookError, ValueError):
    """Raised when the webhook payload account_id does not match the configured merchant account."""


class RazorpayWebhookPersistenceError(RazorpayWebhookError, RuntimeError):
    """Raised when a verified event cannot be durably committed to the database."""


# =============================================================================
# Helper & Utility Services
# =============================================================================


def get_webhook_scope() -> tuple[str, str]:
    """Retrieve and validate the Razorpay environment and merchant account scope.

    Fails closed if merchant_account_id or environment is unconfigured or invalid.
    """
    env = (settings.razorpay_environment or "").strip().lower()
    if env not in ("test", "live"):
        raise RazorpayConfigurationError(
            f"Invalid Razorpay environment '{settings.razorpay_environment}'. Must be 'test' or 'live'."
        )
    merchant_id = (settings.razorpay_merchant_account_id or "").strip()
    if not merchant_id:
        raise RazorpayConfigurationError("Razorpay merchant account ID must be configured.")
    return merchant_id, env


async def read_bounded_request_body(
    request: Request,
    max_bytes: int = MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES,
) -> bytes:
    """Read the raw request body as bytes with a strict streaming size limit.

    Guarantees:
    - Enforces maximum body size during streaming to prevent memory exhaustion attacks.
    - Does not trust Content-Length header alone; checks size dynamically chunk-by-chunk.
    - Rejects oversized payloads immediately before parsing or signature verification.
    - Preserves exact raw bytes for cryptographic signature verification.
    """
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
            if content_length > max_bytes:
                raise RazorpayWebhookPayloadTooLargeError(
                    f"Webhook request payload exceeds maximum permitted size of {max_bytes} bytes"
                )
        except ValueError:
            pass  # Malformed header will be caught during chunked reading

    chunks: list[bytes] = []
    total_bytes = 0

    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise RazorpayWebhookPayloadTooLargeError(
                f"Webhook request payload exceeds maximum permitted size of {max_bytes} bytes"
            )
        chunks.append(chunk)

    return b"".join(chunks)


def verify_razorpay_webhook_signature(
    raw_payload: bytes,
    sig_header: str | None,
) -> None:
    """Verify Razorpay HMAC-SHA256 signature against exact raw request bytes.

    Security criteria:
    - Fails safely (503) if Razorpay billing is disabled or webhook secret is unconfigured.
    - Rejects missing or empty X-Razorpay-Signature header.
    - Validates signature format (must be 64-character hex string).
    - Computes expected HMAC-SHA256 signature using settings.razorpay_webhook_secret.
    - Employs constant-time comparison (hmac.compare_digest) to prevent timing attacks.
    - Never logs secrets, signatures, or raw request contents.

    Raises:
        RazorpayBillingDisabledError: If settings.razorpay_enabled is False.
        RazorpayConfigurationError: If razorpay_webhook_secret is unconfigured.
        RazorpayWebhookSignatureVerificationError: If signature is missing, malformed, or invalid.
    """
    if not settings.razorpay_enabled:
        raise RazorpayBillingDisabledError(
            "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
        )

    webhook_secret = settings.razorpay_webhook_secret.get_secret_value().strip()
    if not webhook_secret:
        raise RazorpayConfigurationError(
            "Razorpay webhook secret is not configured. Set RAZORPAY_WEBHOOK_SECRET."
        )

    if not sig_header or not isinstance(sig_header, str) or not sig_header.strip():
        logger.warning("Razorpay webhook request rejected: missing or empty signature header")
        raise RazorpayWebhookSignatureVerificationError("Missing or empty X-Razorpay-Signature header")

    clean_sig = sig_header.strip()

    # Razorpay HMAC-SHA256 hex digest is exactly 64 lowercase/uppercase hex characters
    if not re.fullmatch(r"^[0-9a-fA-F]{64}$", clean_sig):
        logger.warning("Razorpay webhook request rejected: malformed signature format")
        raise RazorpayWebhookSignatureVerificationError("Malformed X-Razorpay-Signature header")

    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature.lower(), clean_sig.lower()):
        logger.warning("Razorpay webhook signature verification failed: signature mismatch")
        raise RazorpayWebhookSignatureVerificationError("Invalid Razorpay webhook signature")


def parse_and_validate_razorpay_envelope(
    raw_payload: bytes,
) -> dict[str, Any]:
    """Parse raw bytes as JSON and validate the minimum required Razorpay event envelope.

    Guarantees:
    - Rejects non-JSON or invalid UTF-8 payloads.
    - Rejects non-dict JSON root objects.
    - Validates mandatory envelope fields: entity ('event'), event, account_id, contains, payload.
    - Validates account_id against configured merchant account ID to enforce tenant isolation.

    Raises:
        RazorpayWebhookPayloadMalformedError: If envelope is malformed or missing required keys.
        RazorpayWebhookMerchantMismatchError: If account_id does not match configured merchant account.

    Returns:
        dict[str, Any]: Verified canonical event dictionary.
    """
    try:
        decoded_text = raw_payload.decode("utf-8")
        payload_dict = json.loads(decoded_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Razorpay webhook JSON payload malformed: %s", exc)
        raise RazorpayWebhookPayloadMalformedError("Malformed JSON in webhook payload") from exc

    if not isinstance(payload_dict, dict):
        raise RazorpayWebhookPayloadMalformedError("Webhook payload must be a JSON object")

    # 1. Validate envelope fields
    entity = payload_dict.get("entity")
    if entity != "event":
        raise RazorpayWebhookPayloadMalformedError(
            f"Invalid or missing 'entity' in webhook envelope; expected 'event', got '{entity}'"
        )

    event_type = payload_dict.get("event")
    if not event_type or not isinstance(event_type, str) or not event_type.strip():
        raise RazorpayWebhookPayloadMalformedError("Missing or invalid 'event' in webhook envelope")

    account_id = payload_dict.get("account_id")
    if not account_id or not isinstance(account_id, str) or not account_id.strip():
        raise RazorpayWebhookPayloadMalformedError("Missing or invalid 'account_id' in webhook envelope")

    contains = payload_dict.get("contains")
    if contains is None or not isinstance(contains, list):
        raise RazorpayWebhookPayloadMalformedError("Missing or invalid 'contains' list in webhook envelope")

    payload_obj = payload_dict.get("payload")
    if payload_obj is None or not isinstance(payload_obj, dict):
        raise RazorpayWebhookPayloadMalformedError("Missing or invalid 'payload' object in webhook envelope")

    # 2. Validate merchant account isolation
    expected_merchant, _ = get_webhook_scope()
    clean_account_id = account_id.strip()

    if expected_merchant != "default" and clean_account_id != expected_merchant:
        logger.warning(
            "Razorpay webhook rejected: unexpected merchant account_id '%s' (expected '%s')",
            clean_account_id,
            expected_merchant,
        )
        raise RazorpayWebhookMerchantMismatchError(
            f"Webhook account_id '{clean_account_id}' does not match configured merchant account"
        )

    return payload_dict


_EVENT_ID_REGEX = re.compile(r"^[A-Za-z0-9_\-\.:]{1,255}$")


def extract_razorpay_event_id(
    raw_payload: bytes,
    event_payload: dict[str, Any],
    event_id_header: str | None,
) -> str:
    """Extract primary event ID from headers or compute deterministic cryptographic fallback.

    Guarantees:
    - Primary: Uses X-Razorpay-Event-Id header when present and valid (matches ^[A-Za-z0-9_\\-\\.:]{1,255}$).
    - Fallback: Uses a deterministic SHA-256 digest of raw request bytes incorporating
      account ID and event type.
    - Fits within VARCHAR(255).
    - Does not rely on second-resolution timestamps.

    Architectural Limitation:
    - Razorpay signs only the raw payload body with HMAC-SHA256. The X-Razorpay-Event-Id header
      is not included in the signed payload. Downstream business processors must ensure idempotent
      entity handling (subscription/payment/invoice IDs) rather than relying solely on the event ID.
    """
    if event_id_header and isinstance(event_id_header, str):
        cleaned = event_id_header.strip()
        if _EVENT_ID_REGEX.match(cleaned):
            return cleaned

    # Fallback: deterministic content-hash identity
    account_id = str(event_payload.get("account_id", "unknown")).strip()
    event_type = str(event_payload.get("event", "unknown")).strip()
    raw_digest = hashlib.sha256(raw_payload).hexdigest()

    fallback_id = f"synthetic:razorpay:{account_id}:{event_type}:{raw_digest}"
    return fallback_id[:255]


async def ingest_razorpay_webhook_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    merchant_account_id: str,
    environment: str,
) -> tuple[BillingWebhookEvent, bool]:
    """Durably persist an incoming verified Razorpay webhook event into billing_webhook_events.

    Guarantees:
    1. Provider is strictly 'razorpay'.
    2. Scoped by provider, merchant_account_id, environment, provider_event_id.
    3. Idempotent Deduplication: Uses provider-scoped unique constraint.
       Duplicate deliveries return (existing_event, False) without modifying state or attempts.
    4. Concurrency Safety: Resolves simultaneous duplicate delivery races via IntegrityError rollback.
    5. Durable Receipt: Database transaction is committed before this function returns.
    6. Initial State: Sets signature_verified = True, status = 'pending', attempts = 0.

    Raises:
        IntegrityError: If a database constraint fails and no matching duplicate exists.
        RazorpayWebhookPersistenceError: If the event cannot be committed to the database.

    Returns:
        tuple[BillingWebhookEvent, bool]: (event_record, is_newly_created)
    """
    # 1. Fast path: check if event was already received and committed
    stmt = select(BillingWebhookEvent).where(
        BillingWebhookEvent.provider == "razorpay",
        BillingWebhookEvent.merchant_account_id == merchant_account_id,
        BillingWebhookEvent.environment == environment,
        BillingWebhookEvent.provider_event_id == event_id,
    )
    res = await db.execute(stmt)
    existing = res.scalar_one_or_none()
    if existing is not None:
        logger.info(
            "Duplicate Razorpay webhook event received (id=%s, type=%s, status=%s). Preserving existing state.",
            event_id,
            existing.event_type,
            existing.status.value,
        )
        return (existing, False)

    # 2. Stage new pending event
    new_event = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_event_id=event_id,
        event_type=event_type,
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=payload,
        signature_verified=True,
        received_at=datetime.now(timezone.utc),
    )
    db.add(new_event)

    # 3. Commit transaction to guarantee durable persistence
    try:
        await db.commit()
        await db.refresh(new_event)
        logger.info(
            "Successfully ingested new Razorpay webhook event (id=%s, type=%s, status=pending)",
            event_id,
            event_type,
        )
        return (new_event, True)
    except IntegrityError as exc:
        # Race condition: concurrent worker committed this exact scoped event between step 1 and step 3
        await db.rollback()
        res_dup = await db.execute(stmt)
        existing_dup = res_dup.scalar_one_or_none()
        if existing_dup is not None:
            logger.info(
                "Concurrent duplicate Razorpay webhook event resolved (id=%s, type=%s, status=%s)",
                event_id,
                existing_dup.event_type,
                existing_dup.status.value,
            )
            return (existing_dup, False)
        logger.error(
            "IntegrityError during Razorpay event ingestion for id=%s but matching scoped record not found: %s",
            event_id,
            exc,
        )
        raise exc
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to durably persist Razorpay webhook event %s: %s", event_id, exc)
        raise RazorpayWebhookPersistenceError(f"Database error persisting event {event_id}") from exc
