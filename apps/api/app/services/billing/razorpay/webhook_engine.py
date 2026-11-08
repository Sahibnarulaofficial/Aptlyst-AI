"""Durable Razorpay webhook processing engine with explicit database-backed lease ownership.

Architecture & Invariants:
1. Two-Phase Leased Claim:
   - Claim Transaction: Selects eligible event via SELECT ... FOR UPDATE SKIP LOCKED, stamps
     a unique lease token, worker identifier, and expiry timestamp, and COMMITS immediately.
   - Row Lock Release: The claim transaction and row lock are closed before processing begins.
   - Database transactions are NEVER held open while executing external network calls.
2. Persisted Lease Ownership:
   - Ownership is defined by persisted database fields: (lease_token, locked_by, lease_expires_at).
   - An unexpired lease (lease_expires_at > now) CANNOT be claimed by any other worker.
   - An expired lease (lease_expires_at <= now) becomes eligible for recovery.
   - Recovery of an expired lease assigns a fresh lease token and does NOT increment attempts.
3. Fencing & Stale Worker Rejection:
   - Before applying an outcome, a short completion transaction locks the row and validates
     that lease_token, locked_by, and unexpired lease_expires_at match exactly.
   - If the lease was replaced or expired, StaleLeaseError is raised.
   - A stale worker is strictly forbidden from mutating event status, incrementing attempts,
     scheduling retries, or quarantining the event.
4. Retry & Quarantine Invariants:
   - Newly ingested events start with attempts = 0.
   - 1st failed attempt increments attempts to 1 and applies 5s backoff delay.
   - 2nd failed attempt increments attempts to 2 and applies 15s backoff delay.
   - 3rd failed attempt increments attempts to 3 and applies 45s backoff delay.
   - 4th failed attempt increments attempts to 4 and applies 120s backoff delay.
   - 5th failed attempt increments attempts to 5 and triggers quarantine.
5. Zero Secret Leakage:
   - Error messages are sanitized to strip secrets, keys, and tokens prior to logging or persistence.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import enum
import logging
import re
from typing import Any
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import BillingWebhookEvent
from app.models.subscription import WebhookProcessingStatus

logger = logging.getLogger(__name__)

# Default lease duration in seconds
DEFAULT_LEASE_DURATION_SECONDS: int = 60

# Maximum allowed processing attempts before quarantine
MAX_WEBHOOK_ATTEMPTS: int = 5

# Exponential retry backoff schedule in seconds
WEBHOOK_RETRY_BACKOFF_SCHEDULE: dict[int, int] = {
    1: 5,
    2: 15,
    3: 45,
    4: 120,
}


def get_retry_backoff_seconds(attempts: int) -> int:
    """Calculate exponential retry backoff delay based on the number of failed attempts.

    Schedule:
    - Attempt 1 failure (attempts == 1): 5 seconds
    - Attempt 2 failure (attempts == 2): 15 seconds
    - Attempt 3 failure (attempts == 3): 45 seconds
    - Attempt 4+ failure (attempts >= 4): 120 seconds
    """
    if attempts <= 1:
        return 5
    elif attempts == 2:
        return 15
    elif attempts == 3:
        return 45
    else:
        return 120


# =============================================================================
# Outcome Categories & Results
# =============================================================================


class ProcessingOutcome(str, enum.Enum):
    """Normalized outcome category for a webhook handler execution."""

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN_EVENT = "unknown_event"


@dataclass(frozen=True)
class ClaimedWebhookEvent:
    """Immutable representation of a durably leased webhook event.

    Passed to worker execution outside of any active database transaction.
    """

    event_id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    provider_event_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int
    lease_token: uuid.UUID
    locked_by: str
    lease_expires_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class ProcessResult:
    """Final outcome of an event execution cycle."""

    success: bool
    status: WebhookProcessingStatus
    event_id: uuid.UUID
    provider_event_id: str
    attempts: int
    message: str | None = None
    error: Exception | None = None


# =============================================================================
# Exceptions
# =============================================================================


class RazorpayWebhookEngineError(Exception):
    """Base exception for all Razorpay webhook engine errors."""


class RazorpayWebhookRetryableError(RazorpayWebhookEngineError):
    """Raised by handlers for transient errors that should be retried."""


class RazorpayWebhookPermanentError(RazorpayWebhookEngineError):
    """Raised by handlers for permanent errors that require immediate quarantine."""


class StaleLeaseError(RazorpayWebhookEngineError):
    """Raised when an operation is attempted with an expired, stolen, or superseded lease."""


# =============================================================================
# Error Sanitization
# =============================================================================

_SECRET_PATTERNS = [
    re.compile(r"rzp_(?:test|live)_[0-9a-zA-Z]+"),
    re.compile(r"whsec_[0-9a-zA-Z]+"),
    re.compile(r"sk_(?:test|live)_[0-9a-zA-Z]+"),
    re.compile(r"Bearer\s+[a-zA-Z0-9\._\-]+", re.IGNORECASE),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),  # Payment card numbers
]


def sanitize_error_message(err: Exception | str | None, max_chars: int = 1024) -> str:
    """Sanitize and truncate error strings to prevent secret leakage in audit logs."""
    if err is None:
        return ""
    msg = str(err).strip()
    for pattern in _SECRET_PATTERNS:
        msg = pattern.sub("[REDACTED]", msg)
    msg = re.sub(r"\s+", " ", msg)
    return msg[:max_chars].strip()


# =============================================================================
# Handler Registry & Dispatcher
# =============================================================================

RazorpayWebhookHandler = Callable[[dict[str, Any], AsyncSession], Awaitable[None]]


class RazorpayWebhookDispatcher:
    """Registry mapping Razorpay event types to async handler functions."""

    def __init__(self) -> None:
        self._handlers: dict[str, RazorpayWebhookHandler] = {}

    def register(self, event_type: str, handler: RazorpayWebhookHandler) -> None:
        """Register a handler for a specific Razorpay event type."""
        if not event_type or not isinstance(event_type, str):
            raise ValueError("Event type must be a non-empty string.")
        self._handlers[event_type.strip()] = handler

    def unregister(self, event_type: str) -> None:
        """Remove a registered handler."""
        self._handlers.pop(event_type.strip(), None)

    def get_handler(self, event_type: str) -> RazorpayWebhookHandler | None:
        """Retrieve registered handler for event type, or None if unknown."""
        return self._handlers.get(event_type.strip())

    def has_handler(self, event_type: str) -> bool:
        """Check if a handler is registered for event type."""
        return event_type.strip() in self._handlers


# Default module dispatcher instance
default_razorpay_dispatcher = RazorpayWebhookDispatcher()


# =============================================================================
# Atomic Claim Operation
# =============================================================================


async def claim_next_razorpay_webhook_event(
    db: AsyncSession,
    worker_id: str,
    lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
    provider: str = "razorpay",
    environment: str | None = None,
    now: datetime | None = None,
) -> ClaimedWebhookEvent | None:
    """Atomically claim the next eligible webhook event and commit lease ownership.

    Guarantees:
    1. Safe Locking: Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent lock contention.
    2. Explicit Lease Stamping: Assigns fresh unpredictable UUID lease_token, locked_by,
       and lease_expires_at timestamp.
    3. Immediate Commit: The claim transaction commits to disk before returning, releasing
       the database row lock. Network calls and handler execution happen outside any row lock.
    4. Unexpired Lease Protection: Events with unexpired leases (lease_expires_at > now)
       are never claimed.
    5. Expired Lease Recovery: Events with expired leases (lease_expires_at <= now) are
       eligible for recovery without incrementing attempts.
    6. Multi-Tenant / Environment Scoping: Enforces provider, environment, and signature_verified.

    Args:
        db: Active SQLAlchemy async session.
        worker_id: Unique identifier for the claiming worker instance.
        lease_duration_seconds: Number of seconds before lease expires.
        max_attempts: Maximum processing attempts threshold.
        provider: Provider identifier (defaults to "razorpay").
        environment: Optional environment filter ("test" or "live").
        now: Optional override for current UTC datetime (for deterministic testing).

    Returns:
        ClaimedWebhookEvent instance if claimed, or None if no eligible event exists.
    """
    now_utc = now or datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(seconds=lease_duration_seconds)
    fresh_lease_token = uuid.uuid4()

    stmt = (
        select(BillingWebhookEvent)
        .where(
            BillingWebhookEvent.provider == provider,
            BillingWebhookEvent.signature_verified.is_(True),
            BillingWebhookEvent.status.in_(
                [WebhookProcessingStatus.pending, WebhookProcessingStatus.failed]
            ),
            BillingWebhookEvent.attempts < max_attempts,
            or_(
                BillingWebhookEvent.next_retry_at.is_(None),
                BillingWebhookEvent.next_retry_at <= now_utc,
            ),
            or_(
                BillingWebhookEvent.lease_expires_at.is_(None),
                BillingWebhookEvent.lease_expires_at <= now_utc,
            ),
        )
    )

    if environment is not None:
        stmt = stmt.where(BillingWebhookEvent.environment == environment)

    # Oldest received event first (FIFO order)
    stmt = (
        stmt.order_by(BillingWebhookEvent.received_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        return None

    # Stamp persisted lease fields
    event.lease_token = fresh_lease_token
    event.locked_by = worker_id
    event.lease_expires_at = expires_at

    # Commit claim transaction immediately so row lock is not held during handler execution
    await db.commit()
    await db.refresh(event)

    logger.info(
        "Worker %s successfully claimed event %s (provider_event_id=%s, type=%s, lease_expires_at=%s)",
        worker_id,
        event.id,
        event.provider_event_id,
        event.event_type,
        expires_at.isoformat(),
    )

    return ClaimedWebhookEvent(
        event_id=event.id,
        provider=event.provider,
        merchant_account_id=event.merchant_account_id,
        environment=event.environment,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        payload=event.payload,
        attempts=event.attempts,
        lease_token=fresh_lease_token,
        locked_by=worker_id,
        lease_expires_at=expires_at,
        received_at=event.received_at,
    )


# =============================================================================
# Ownership Validation & Outcome Recording
# =============================================================================


async def record_razorpay_webhook_outcome(
    db: AsyncSession,
    claimed: ClaimedWebhookEvent,
    outcome: ProcessingOutcome,
    error: Exception | str | None = None,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
    now: datetime | None = None,
) -> BillingWebhookEvent:
    """Durably record processing outcome on the event row after validating lease ownership.

    Guarantees:
    1. Fencing Token Validation: Queries the event under lock (SELECT ... FOR UPDATE) and
       asserts that lease_token and locked_by match the claiming worker, and lease_expires_at >= now.
    2. Stale Worker Rejection: If lease ownership was lost, replaced, or expired, raises
       StaleLeaseError and rolls back immediately without mutating event status or attempts.
    3. Safe State Transitions:
       - SUCCESS: sets status='processed', processed_at=now, clears error and next_retry_at.
       - RETRYABLE_FAILURE: increments attempts += 1, sets processed_at=now.
         If attempts >= max_attempts, sets status='quarantined', quarantined_at=now, and quarantine_reason.
         Otherwise sets status='failed' and calculates next_retry_at using 5s, 15s, 45s, 120s backoff.
       - PERMANENT_FAILURE / UNKNOWN_EVENT: increments attempts += 1, sets status='quarantined'.
    4. Atomic Lease Clearing: Clears lease_token, locked_by, and lease_expires_at upon completion.
    5. Transaction Commit: Commits the outcome atomically to disk.

    Raises:
        StaleLeaseError: If the worker's lease has expired, been stolen, or been superseded.

    Returns:
        BillingWebhookEvent: Committed model instance with updated status.
    """
    now_utc = now or datetime.now(timezone.utc)

    # Lock row to inspect current database ownership
    stmt = (
        select(BillingWebhookEvent)
        .where(BillingWebhookEvent.id == claimed.event_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        await db.rollback()
        raise StaleLeaseError(f"Event {claimed.event_id} was deleted or not found.")

    # Validate lease ownership and expiry
    is_same_token = event.lease_token == claimed.lease_token
    is_same_worker = event.locked_by == claimed.locked_by
    is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now_utc

    if not (is_same_token and is_same_worker and is_unexpired):
        await db.rollback()
        logger.warning(
            "Fencing violation: worker %s lost lease on event %s (db_token=%s, held_token=%s, db_locked_by=%s, db_expires_at=%s, now=%s)",
            claimed.locked_by,
            claimed.event_id,
            event.lease_token,
            claimed.lease_token,
            event.locked_by,
            event.lease_expires_at,
            now_utc,
        )
        raise StaleLeaseError(
            f"Worker '{claimed.locked_by}' lost lease ownership for event {claimed.event_id}. "
            "Lease expired, was replaced, or belongs to another worker."
        )

    sanitized_err = sanitize_error_message(error) if error else None

    # Apply outcome mutations
    if outcome == ProcessingOutcome.SUCCESS:
        event.status = WebhookProcessingStatus.processed
        event.processed_at = now_utc
        event.error_message = None
        event.next_retry_at = None
    elif outcome == ProcessingOutcome.RETRYABLE_FAILURE:
        event.attempts += 1
        event.processed_at = now_utc
        if event.attempts >= max_attempts:
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now_utc
            event.quarantine_reason = (
                f"Quarantined: exceeded max attempts ({event.attempts}/{max_attempts})"
            )
            event.error_message = sanitized_err
            event.next_retry_at = None
        else:
            event.status = WebhookProcessingStatus.failed
            delay_sec = get_retry_backoff_seconds(event.attempts)
            event.next_retry_at = now_utc + timedelta(seconds=delay_sec)
            event.error_message = sanitized_err
    elif outcome in (ProcessingOutcome.PERMANENT_FAILURE, ProcessingOutcome.UNKNOWN_EVENT):
        event.attempts += 1
        event.status = WebhookProcessingStatus.quarantined
        event.quarantined_at = now_utc
        event.quarantine_reason = (
            f"Permanent error: {sanitized_err}"
            if outcome == ProcessingOutcome.PERMANENT_FAILURE
            else f"Unknown event type: '{claimed.event_type}'"
        )
        event.error_message = sanitized_err
        event.next_retry_at = None

    # Atomically clear lease ownership
    event.lease_token = None
    event.locked_by = None
    event.lease_expires_at = None

    await db.commit()
    await db.refresh(event)

    logger.info(
        "Worker %s successfully recorded outcome for event %s (outcome=%s, status=%s, attempts=%d)",
        claimed.locked_by,
        event.id,
        outcome.value,
        event.status.value,
        event.attempts,
    )

    return event
