"""Stripe webhook event ingestion and durable receipt service.

Guarantees:
1. Strict Payload Bounding: Rejects requests exceeding 64 KB during chunked streaming.
2. Cryptographic Authenticity: Verifies HMAC-SHA256 signatures against exact original raw bytes.
3. Zero Secret Leakage: Never logs webhook secret, signature header, or raw request bytes.
4. Fail-Safe Configuration: Safely fails closed if Stripe billing is disabled or webhook secret is unconfigured.
5. Idempotent Deduplication: Uses Stripe event ID as primary key; duplicate deliveries return safely
   without modifying existing processing state or attempt counts.
6. Concurrency Safety: Resolves simultaneous duplicate delivery races via database uniqueness and rollback.
7. Durable Receipt: Commits event to persistent storage before returning receipt acknowledgement.
"""

import json
import logging
from typing import Any
from fastapi import HTTPException, Request, status
import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.subscription import StripeWebhookEvent, WebhookProcessingStatus
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
)

logger = logging.getLogger(__name__)

# Strict 64 KB streaming payload limit
MAX_WEBHOOK_PAYLOAD_BYTES = 64 * 1024  # 65,536 bytes


class WebhookPayloadTooLargeError(Exception):
    """Raised when the incoming webhook request payload exceeds 64 KB."""


class WebhookSignatureVerificationError(ValueError):
    """Raised when the Stripe webhook signature is missing, malformed, invalid, or expired."""


class WebhookPayloadMalformedError(ValueError):
    """Raised when the webhook payload is not valid JSON or lacks mandatory Stripe event fields."""


class WebhookConfigurationError(StripeConfigurationError):
    """Raised when Stripe billing is enabled but webhook secret configuration is missing."""


class WebhookPersistenceError(RuntimeError):
    """Raised when a verified event cannot be durably committed to the database."""


async def read_bounded_request_body(
    request: Request,
    max_bytes: int = MAX_WEBHOOK_PAYLOAD_BYTES,
) -> bytes:
    """Read the raw request body as bytes with a strict streaming size limit.

    Guarantees:
    - Enforces maximum body size during streaming to prevent memory exhaustion attacks.
    - Does not trust Content-Length header alone; checks size dynamically chunk-by-chunk.
    - Rejects oversized payloads immediately with WebhookPayloadTooLargeError before parsing.
    - Preserves exact raw bytes for cryptographic signature verification.

    Args:
        request: FastAPI/Starlette Request object.
        max_bytes: Maximum allowed payload size in bytes (default 64 KB).

    Raises:
        WebhookPayloadTooLargeError: If payload exceeds max_bytes.

    Returns:
        bytes: Exact raw body bytes.
    """
    # 1. Early check on Content-Length header if present
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
            if content_length > max_bytes:
                raise WebhookPayloadTooLargeError(
                    f"Webhook request payload exceeds maximum permitted size of {max_bytes} bytes"
                )
        except ValueError:
            pass  # Malformed header will be caught during chunked reading

    # 2. Enforce limit dynamically while reading stream
    chunks: list[bytes] = []
    total_bytes = 0

    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise WebhookPayloadTooLargeError(
                f"Webhook request payload exceeds maximum permitted size of {max_bytes} bytes"
            )
        chunks.append(chunk)

    return b"".join(chunks)


def verify_stripe_webhook_signature(
    raw_payload: bytes,
    sig_header: str | None,
) -> dict[str, Any]:
    """Verify Stripe HMAC-SHA256 signature against exact raw request bytes.

    Security criteria:
    - Fails safely if Stripe billing is disabled or webhook secret is missing.
    - Requires non-empty Stripe-Signature header.
    - Verifies cryptographic signature using Stripe SDK Webhook utility.
    - Validates timestamp tolerance to reject expired replay attacks.
    - Never logs webhook secret, signature header, or raw request bytes.
    - Validates that the parsed event contains non-empty 'id' and 'type' fields.

    Args:
        raw_payload: Exact raw request bytes.
        sig_header: Stripe-Signature header value.

    Raises:
        StripeBillingDisabledError: If settings.stripe_enabled is False.
        StripeConfigurationError: If stripe_webhook_secret is unconfigured.
        WebhookSignatureVerificationError: If signature is missing, invalid, or expired.
        WebhookPayloadMalformedError: If payload is not valid JSON or lacks id/type.

    Returns:
        dict[str, Any]: Verified canonical Stripe event dictionary.
    """
    if not settings.stripe_enabled:
        raise StripeBillingDisabledError(
            "Stripe billing is currently disabled. Set STRIPE_ENABLED=true to enable."
        )

    webhook_secret = settings.stripe_webhook_secret.get_secret_value().strip()
    if not webhook_secret:
        raise StripeConfigurationError(
            "Stripe webhook secret is not configured. Set STRIPE_WEBHOOK_SECRET."
        )

    if not sig_header or not isinstance(sig_header, str) or not sig_header.strip():
        logger.warning("Stripe webhook request rejected: missing or empty Stripe-Signature header")
        raise WebhookSignatureVerificationError("Missing or empty Stripe-Signature header")

    try:
        # stripe.Webhook.construct_event verifies HMAC signature and timestamp tolerance
        event = stripe.Webhook.construct_event(
            payload=raw_payload,
            sig_header=sig_header.strip(),
            secret=webhook_secret,
        )
    except stripe.SignatureVerificationError as exc:
        logger.warning("Stripe webhook signature verification failed: invalid or expired signature")
        raise WebhookSignatureVerificationError(
            "Invalid or expired Stripe webhook signature"
        ) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Stripe webhook JSON payload malformed")
        raise WebhookPayloadMalformedError(
            "Malformed Stripe webhook payload"
        ) from exc

    # Parse raw bytes into canonical dictionary
    try:
        if isinstance(raw_payload, bytes):
            payload_dict = json.loads(raw_payload.decode("utf-8"))
        else:
            payload_dict = dict(event)
    except Exception as exc:
        logger.warning("Failed to decode verified Stripe webhook payload as JSON")
        raise WebhookPayloadMalformedError(
            "Malformed Stripe webhook payload"
        ) from exc

    if not isinstance(payload_dict, dict):
        raise WebhookPayloadMalformedError("Stripe webhook payload must be a JSON object")

    event_id = payload_dict.get("id")
    event_type = payload_dict.get("type")

    if not event_id or not isinstance(event_id, str) or not event_id.strip():
        logger.warning("Stripe webhook event rejected: missing or empty 'id'")
        raise WebhookPayloadMalformedError("Stripe event missing or invalid 'id'")

    if not event_type or not isinstance(event_type, str) or not event_type.strip():
        logger.warning("Stripe webhook event rejected: missing or empty 'type'")
        raise WebhookPayloadMalformedError("Stripe event missing or invalid 'type'")

    return payload_dict


async def ingest_stripe_webhook_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> tuple[StripeWebhookEvent, bool]:
    """Durably persist an incoming verified Stripe webhook event.

    Guarantees:
    1. Idempotency: Uses Stripe event ID as primary key in stripe_webhook_events table.
    2. Non-Destructive Deduplication: Existing events are never overwritten;
       their processing status (e.g. pending, processed, failed, quarantined)
       and attempts count are preserved completely unchanged.
    3. Concurrency Safety: If two identical event deliveries race, one commits
       and the other encounters a primary key conflict (IntegrityError), rolls
       back, and reloads the existing record cleanly.
    4. Durable Receipt: The database transaction is committed before this function
       returns, ensuring Stripe only receives HTTP 200 after durable disk persistence.
    5. Safe Processing State: New events default strictly to 'pending'. No business
       processing is marked as completed during ingestion.

    Args:
        db: Active SQLAlchemy async session.
        event_id: Verified Stripe event ID (e.g., 'evt_...').
        event_type: Verified Stripe event type (e.g., 'invoice.paid').
        payload: Full verified event dictionary.

    Raises:
        WebhookPersistenceError: If the event cannot be committed to the database.

    Returns:
        tuple[StripeWebhookEvent, bool]: (event_record, is_newly_created)
    """
    # 1. Fast path: check if event was already received and committed
    existing = await db.get(StripeWebhookEvent, event_id)
    if existing is not None:
        logger.info(
            "Duplicate Stripe webhook event received (id=%s, type=%s, status=%s). Preserving existing state.",
            event_id,
            existing.event_type,
            existing.status.value,
        )
        return (existing, False)

    # 2. Stage new pending event
    new_event = StripeWebhookEvent(
        id=event_id,
        event_type=event_type,
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload=payload,
    )
    db.add(new_event)

    # 3. Commit transaction to guarantee durable persistence
    try:
        await db.commit()
        await db.refresh(new_event)
        logger.info(
            "Successfully ingested new Stripe webhook event (id=%s, type=%s, status=pending)",
            event_id,
            event_type,
        )
        return (new_event, True)
    except IntegrityError:
        # Race condition: concurrent worker committed this exact event_id between step 1 and step 3
        await db.rollback()
        existing = await db.get(StripeWebhookEvent, event_id)
        if existing is not None:
            logger.info(
                "Concurrent duplicate Stripe webhook event resolved (id=%s, type=%s, status=%s)",
                event_id,
                existing.event_type,
                existing.status.value,
            )
            return (existing, False)
        logger.error("IntegrityError during event ingestion but record not found for id=%s", event_id)
        raise WebhookPersistenceError(f"Database constraint error persisting event {event_id}")
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to durably persist Stripe webhook event %s: %s", event_id, exc)
        raise WebhookPersistenceError(f"Database error persisting event {event_id}") from exc
