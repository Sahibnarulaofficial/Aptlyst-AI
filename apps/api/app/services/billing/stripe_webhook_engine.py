"""Durable Stripe Webhook Processing Engine.

Architectural Guarantees:
1. Concurrency & Claiming: Row-level locking via `SELECT ... FOR UPDATE` serializes event
   processing and prevents multiple workers from processing the same event simultaneously.
   `SKIP LOCKED` allows queue-based worker consumption without blocking.
2. Safe State Machine: Explicit transitions between `pending`, `processed`, `failed`,
   and `quarantined`.
   - `pending` -> `processed` (on handler success)
   - `pending` -> `failed` (on retryable failure when attempts < max_attempts)
   - `pending` -> `quarantined` (on permanent failure, unknown event, or max attempts reached)
   - `failed` -> `processed` (on retry success)
   - `failed` -> `failed` (on retry failure when attempts < max_attempts)
   - `failed` -> `quarantined` (when attempts >= max_attempts or permanent error)
   - `processed` -> terminal (never re-executed)
   - `quarantined` -> terminal / manual review (never executed automatically)
3. Transaction Isolation & Crash Recovery:
   - Handler execution runs within a database savepoint (`begin_nested()`).
   - If the handler fails, domain mutations are rolled back, leaving the outer transaction
     active to persist the sanitized failure status and increment attempts.
   - If the worker crashes, the uncommitted transaction is aborted by PostgreSQL, releasing
     the row lock and restoring the row to its last committed state (`pending` or `failed`).
   - No external network calls are held under database row locks.
4. Error Sanitization: All error messages recorded to the database are scrubbed of secrets,
   tokens, keys, and payment credentials.
5. Handler Decoupling: A clean registry dispatch interface maps Stripe event types to
   domain handlers, ensuring unknown events are safely quarantined rather than silently
   marked processed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import enum
import logging
import re
from typing import Any, Awaitable, Callable
import uuid

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import StripeWebhookEvent, WebhookProcessingStatus

logger = logging.getLogger(__name__)

# Default maximum attempts before an unprocessable event is quarantined
MAX_WEBHOOK_ATTEMPTS: int = 5

# Bounded exponential backoff delay schedule in seconds for failed events
WEBHOOK_RETRY_BACKOFF_SCHEDULE: dict[int, int] = {
    1: 5,
    2: 15,
    3: 45,
    4: 120,
}


def get_retry_backoff_seconds(attempts: int) -> int:
    """Return the backoff delay in seconds for a failed webhook event.

    Schedule:
    - Attempt 1: 5 seconds
    - Attempt 2: 15 seconds
    - Attempt 3: 45 seconds
    - Attempt 4: 120 seconds
    - Attempt 5+: 120 seconds (quarantined at max_attempts threshold)
    """
    if attempts <= 1:
        return 5
    elif attempts == 2:
        return 15
    elif attempts == 3:
        return 45
    else:
        return 120


def is_webhook_event_eligible(
    event: StripeWebhookEvent,
    now: datetime | None = None,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> bool:
    """Determine whether a webhook event is currently eligible to be claimed.

    Guarantees:
    1. Pending events are immediately eligible.
    2. Processed and Quarantined events are terminal and never eligible.
    3. Failed events are eligible only when attempts < max_attempts and
       either processed_at is None or now >= processed_at + backoff_delay.
    """
    if event.status == WebhookProcessingStatus.pending:
        return True
    if event.status != WebhookProcessingStatus.failed:
        return False
    if event.attempts >= max_attempts:
        return False
    if event.processed_at is None:
        return True
    current_time = now or datetime.now(timezone.utc)
    delay_seconds = get_retry_backoff_seconds(event.attempts)
    return current_time >= event.processed_at + timedelta(seconds=delay_seconds)


# =============================================================================
# Exceptions
# =============================================================================


class WebhookEngineError(Exception):
    """Base exception for all webhook processing engine errors."""


class WebhookRetryableError(WebhookEngineError):
    """Raised by handlers for transient errors that should be retried."""


class WebhookPermanentError(WebhookEngineError):
    """Raised by handlers for permanent errors that require immediate quarantine."""


class WebhookReconciliationConflictError(WebhookPermanentError):
    """Raised when an incoming event conflicts with existing active records and requires audit recording."""

    def __init__(
        self,
        message: str,
        *,
        organization_id: Any,
        event_id: str,
        existing_subscription_id: str,
        incoming_subscription_id: str,
        reason: str = "duplicate_active_subscription",
        diagnostic_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.organization_id = organization_id
        self.event_id = event_id
        self.existing_subscription_id = existing_subscription_id
        self.incoming_subscription_id = incoming_subscription_id
        self.reason = reason
        self.diagnostic_payload = diagnostic_payload or {}


class WebhookUnknownEventError(WebhookPermanentError):
    """Raised when an ingested event has no registered handler."""


class WebhookEnginePersistenceError(WebhookEngineError):
    """Raised when an outcome cannot be durably committed to the database."""


# =============================================================================
# Claiming & Outcomes
# =============================================================================


class ClaimStatus(str, enum.Enum):
    """Status returned when attempting to claim an event for processing."""

    CLAIMED = "claimed"
    ALREADY_PROCESSED = "already_processed"
    QUARANTINED = "quarantined"
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ClaimResult:
    """Result of attempting to claim an event row under lock."""

    status: ClaimStatus
    event: StripeWebhookEvent | None = None

    @property
    def is_claimed(self) -> bool:
        """Return True if the event was successfully locked and is eligible for processing."""
        return self.status == ClaimStatus.CLAIMED


class ProcessingOutcome(str, enum.Enum):
    """Normalized outcome category for a webhook handler execution."""

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN_EVENT = "unknown_event"


@dataclass(frozen=True)
class ProcessResult:
    """Final outcome of the full webhook processing pipeline."""

    success: bool
    status: WebhookProcessingStatus
    event_id: str
    message: str | None = None
    error: Exception | None = None
    attempts: int = 1


# =============================================================================
# Error Sanitization
# =============================================================================

_SECRET_PATTERNS = [
    re.compile(r"sk_(?:test|live)_[0-9a-zA-Z]+"),
    re.compile(r"whsec_[0-9a-zA-Z]+"),
    re.compile(r"pk_(?:test|live)_[0-9a-zA-Z]+"),
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
    # Compress multiple spaces
    msg = re.sub(r"\s+", " ", msg)
    return msg[:max_chars].strip()


# =============================================================================
# Handler Registry & Dispatcher
# =============================================================================

WebhookHandler = Callable[[dict[str, Any], AsyncSession], Awaitable[None]]


class WebhookDispatcher:
    """Registry mapping Stripe event types to async handler functions."""

    def __init__(self) -> None:
        self._handlers: dict[str, WebhookHandler] = {}

    def register(self, event_type: str, handler: WebhookHandler) -> None:
        """Register a handler for a specific Stripe event type (e.g., 'invoice.paid')."""
        if not event_type or not isinstance(event_type, str):
            raise ValueError("Event type must be a non-empty string.")
        self._handlers[event_type.strip()] = handler

    def unregister(self, event_type: str) -> None:
        """Remove a registered handler (useful for testing)."""
        self._handlers.pop(event_type.strip(), None)

    def get_handler(self, event_type: str) -> WebhookHandler | None:
        """Retrieve registered handler for event type, or None if unknown."""
        return self._handlers.get(event_type.strip())

    def has_handler(self, event_type: str) -> bool:
        """Return True if a handler is registered for event type."""
        return event_type.strip() in self._handlers

    @property
    def registered_types(self) -> list[str]:
        """Return a sorted list of all registered event types."""
        return sorted(self._handlers.keys())


# Default shared dispatcher instance
default_webhook_dispatcher = WebhookDispatcher()


# =============================================================================
# Event Claiming Services
# =============================================================================


async def claim_webhook_event_by_id(
    db: AsyncSession,
    event_id: str,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> ClaimResult:
    """Claim a specific Stripe webhook event row using PostgreSQL SELECT FOR UPDATE.

    Guarantees:
    1. Mutual Exclusion: Acquires an exclusive row lock (`with_for_update()`). Concurrent
       workers attempting to claim the same event will block until the lock holder commits.
    2. Terminal State Guard: Already `processed` events immediately return ALREADY_PROCESSED.
    3. Quarantine Guard: Events marked `quarantined` return QUARANTINED without re-executing.
    4. Bounded Retry: Events marked `failed` are eligible for retry only if `attempts < max_attempts`.
       If `attempts >= max_attempts`, the event is transitioned to `quarantined` and committed.
    5. Clean Crash Recovery: If the worker crashes before committing, PostgreSQL terminates
       the connection and releases the row lock, reverting uncommitted changes.

    Args:
        db: Active SQLAlchemy async session.
        event_id: Stripe event ID to lock and inspect.
        max_attempts: Maximum allowed processing attempts before quarantine.

    Returns:
        ClaimResult indicating claim status and loaded model instance.
    """
    stmt = (
        select(StripeWebhookEvent)
        .where(StripeWebhookEvent.id == event_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        logger.warning("Attempted to claim non-existent webhook event %s", event_id)
        return ClaimResult(status=ClaimStatus.NOT_FOUND)

    if event.status == WebhookProcessingStatus.processed:
        logger.info(
            "Webhook event %s already processed (attempts=%d). Skipping re-execution.",
            event_id,
            event.attempts,
        )
        return ClaimResult(status=ClaimStatus.ALREADY_PROCESSED, event=event)

    if event.status == WebhookProcessingStatus.quarantined:
        logger.info(
            "Webhook event %s is quarantined (error=%s). Skipping automated execution.",
            event_id,
            event.error_message,
        )
        return ClaimResult(status=ClaimStatus.QUARANTINED, event=event)

    if event.status == WebhookProcessingStatus.failed:
        if event.attempts >= max_attempts:
            logger.warning(
                "Webhook event %s exceeded max attempts (%d/%d). Transitioning to quarantined.",
                event_id,
                event.attempts,
                max_attempts,
            )
            event.status = WebhookProcessingStatus.quarantined
            event.error_message = sanitize_error_message(
                f"Quarantined: exceeded maximum retry attempts ({event.attempts}/{max_attempts})."
            )
            try:
                await db.commit()
                await db.refresh(event)
            except Exception as exc:
                await db.rollback()
                logger.error("Failed to commit quarantine transition for event %s: %s", event_id, exc)
                raise WebhookEnginePersistenceError(f"Failed to quarantine event {event_id}") from exc
            return ClaimResult(status=ClaimStatus.MAX_ATTEMPTS_EXCEEDED, event=event)

        logger.info(
            "Claimed failed webhook event %s for retry attempt %d/%d.",
            event_id,
            event.attempts + 1,
            max_attempts,
        )
        return ClaimResult(status=ClaimStatus.CLAIMED, event=event)

    # Status is pending
    logger.info("Claimed pending webhook event %s under lock.", event_id)
    return ClaimResult(status=ClaimStatus.CLAIMED, event=event)


async def claim_next_webhook_event(
    db: AsyncSession,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> ClaimResult:
    """Claim the oldest pending or retryable failed event using SELECT FOR UPDATE SKIP LOCKED.

    Designed for background polling workers to process events from the database queue
    without blocking or competing with other concurrent workers.

    Bounded Exponential Backoff:
    - Pending events are immediately eligible.
    - Failed events are eligible only when attempts < max_attempts and the required
      exponential backoff delay (5s, 15s, 45s, 120s) has elapsed since `processed_at`.

    Args:
        db: Active SQLAlchemy async session.
        max_attempts: Maximum allowed processing attempts.

    Returns:
        ClaimResult indicating claim status and loaded event.
    """
    delay_interval = case(
        (StripeWebhookEvent.attempts <= 1, text("INTERVAL '5 seconds'")),
        (StripeWebhookEvent.attempts == 2, text("INTERVAL '15 seconds'")),
        (StripeWebhookEvent.attempts == 3, text("INTERVAL '45 seconds'")),
        else_=text("INTERVAL '120 seconds'"),
    )

    failed_eligibility = and_(
        StripeWebhookEvent.status == WebhookProcessingStatus.failed,
        StripeWebhookEvent.attempts < max_attempts,
        or_(
            StripeWebhookEvent.processed_at.is_(None),
            (StripeWebhookEvent.processed_at + delay_interval) <= func.now(),
        ),
    )

    stmt = (
        select(StripeWebhookEvent)
        .where(
            or_(
                StripeWebhookEvent.status == WebhookProcessingStatus.pending,
                failed_eligibility,
            )
        )
        .order_by(StripeWebhookEvent.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        return ClaimResult(status=ClaimStatus.NOT_FOUND)

    logger.info("Claimed next queued webhook event %s (status=%s)", event.id, event.status.value)
    return ClaimResult(status=ClaimStatus.CLAIMED, event=event)


# =============================================================================
# Outcome Recording Service
# =============================================================================


async def record_webhook_outcome(
    db: AsyncSession,
    event: StripeWebhookEvent,
    outcome: ProcessingOutcome,
    error: Exception | str | None = None,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
    reconciliation_record: Any | None = None,
) -> StripeWebhookEvent:
    """Durably record the processing outcome on the locked event row and commit.

    Guarantees:
    1. Safe State Updates:
       - SUCCESS: sets `status='processed'`, `processed_at=utcnow()`, clears `error_message`.
       - RETRYABLE_FAILURE: increments `attempts`, sets `processed_at=utcnow()`.
         If `attempts >= max_attempts`, transitions to `quarantined`; otherwise transitions
         to `failed` and stores sanitized error with backoff eligibility timestamp.
       - PERMANENT_FAILURE / UNKNOWN_EVENT: increments `attempts`, sets `processed_at=utcnow()`,
         transitions to `quarantined`, and stores sanitized error.
    2. Atomic Reconciliation Audit: If `reconciliation_record` is provided, stages it
       in the outer transaction so audit records survive handler savepoint rollbacks.
    3. Atomic Commit: Commits the outcome transaction to disk.
    4. Failure Safety: If commit fails, rolls back and raises WebhookEnginePersistenceError,
       guaranteeing that uncommitted outcomes are never acknowledged as processed.

    Args:
        db: Active SQLAlchemy async session holding the event lock.
        event: Locked StripeWebhookEvent model instance.
        outcome: Processing outcome category.
        error: Optional exception or message to sanitize and record.
        max_attempts: Maximum retry threshold.
        reconciliation_record: Optional BillingReconciliation model to commit atomically with quarantine.

    Returns:
        StripeWebhookEvent with committed state updates.
    """
    sanitized_err = sanitize_error_message(error) if error else None

    if outcome == ProcessingOutcome.SUCCESS:
        event.status = WebhookProcessingStatus.processed
        event.processed_at = datetime.now(timezone.utc)
        event.error_message = None
    elif outcome == ProcessingOutcome.RETRYABLE_FAILURE:
        event.attempts += 1
        event.processed_at = datetime.now(timezone.utc)
        if event.attempts >= max_attempts:
            event.status = WebhookProcessingStatus.quarantined
            event.error_message = (
                f"Quarantined: exceeded max attempts ({event.attempts}/{max_attempts}). Error: {sanitized_err}"
            )
        else:
            event.status = WebhookProcessingStatus.failed
            event.error_message = sanitized_err
    elif outcome in (ProcessingOutcome.PERMANENT_FAILURE, ProcessingOutcome.UNKNOWN_EVENT):
        event.attempts += 1
        event.status = WebhookProcessingStatus.quarantined
        event.processed_at = datetime.now(timezone.utc)
        event.error_message = sanitized_err

    if reconciliation_record is not None:
        from app.models.subscription import BillingReconciliation
        stmt_rec = select(BillingReconciliation).where(
            BillingReconciliation.event_id == reconciliation_record.event_id,
            BillingReconciliation.incoming_subscription_id == reconciliation_record.incoming_subscription_id,
        )
        existing_rec = (await db.execute(stmt_rec)).scalar_one_or_none()
        if existing_rec is None:
            db.add(reconciliation_record)

    try:
        await db.commit()
        await db.refresh(event)
        if reconciliation_record is not None:
            try:
                await db.refresh(reconciliation_record)
            except Exception:
                pass
        logger.info(
            "Persisted outcome %s for webhook event %s (new_status=%s, attempts=%d)",
            outcome.value,
            event.id,
            event.status.value,
            event.attempts,
        )
        return event
    except Exception as exc:
        await db.rollback()
        logger.error(
            "Database error while recording webhook outcome for event %s: %s",
            event.id,
            exc,
        )
        raise WebhookEnginePersistenceError(
            f"Failed to durably commit outcome for event {event.id}"
        ) from exc


# =============================================================================
# Execution & Orchestration Service
# =============================================================================


async def execute_claimed_webhook_event(
    db: AsyncSession,
    event: StripeWebhookEvent,
    handler: WebhookHandler | None,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> ProcessResult:
    """Execute domain handler for an already-claimed event under a database savepoint.

    Guarantees:
    1. Unknown Event Quarantine: If handler is None, immediately transitions to quarantined.
    2. Savepoint Isolation: Handler runs inside `async with db.begin_nested():`. If handler
       fails, any domain mutations made by the handler are rolled back, preserving the outer
       transaction to record the failure state.
    3. Exception Classification:
       - WebhookPermanentError: Transitions event directly to quarantined.
       - WebhookRetryableError / Exception: Transitions event to failed (or quarantined if max attempts reached).
    4. Success Commit: Transitions event to processed on clean return.

    Args:
        db: Active SQLAlchemy async session holding the event row lock.
        event: Claimed StripeWebhookEvent instance.
        handler: Registered async handler, or None for unknown event type.
        max_attempts: Maximum retry threshold.

    Returns:
        ProcessResult summarizing the execution outcome.
    """
    event_id = event.id
    event_type = event.event_type

    # 1. Guard against unknown event type
    if handler is None:
        logger.warning(
            "No handler registered for Stripe webhook event %s (type=%s). Quarantining.",
            event_id,
            event_type,
        )
        updated = await record_webhook_outcome(
            db=db,
            event=event,
            outcome=ProcessingOutcome.UNKNOWN_EVENT,
            error=f"No handler registered for event type '{event_type}'.",
            max_attempts=max_attempts,
        )
        return ProcessResult(
            success=False,
            status=WebhookProcessingStatus.quarantined,
            event_id=event_id,
            message=f"Quarantined: unknown event type '{event_type}'.",
            attempts=updated.attempts,
        )

    # 2. Execute handler inside database SAVEPOINT for transactional rollback on error
    try:
        async with db.begin_nested():
            await handler(event.payload, db)
    except WebhookPermanentError as exc:
        logger.warning(
            "Permanent failure processing Stripe webhook event %s (type=%s): %s",
            event_id,
            event_type,
            exc,
        )
        reconciliation_record = None
        if isinstance(exc, WebhookReconciliationConflictError):
            from app.models.subscription import BillingReconciliation, ReconciliationStatus
            reconciliation_record = BillingReconciliation(
                id=uuid.uuid4(),
                organization_id=exc.organization_id,
                event_id=exc.event_id,
                existing_subscription_id=exc.existing_subscription_id,
                incoming_subscription_id=exc.incoming_subscription_id,
                reason=exc.reason,
                status=ReconciliationStatus.pending_review,
                diagnostic_payload=exc.diagnostic_payload,
            )

        updated = await record_webhook_outcome(
            db=db,
            event=event,
            outcome=ProcessingOutcome.PERMANENT_FAILURE,
            error=exc,
            max_attempts=max_attempts,
            reconciliation_record=reconciliation_record,
        )
        return ProcessResult(
            success=False,
            status=WebhookProcessingStatus.quarantined,
            event_id=event_id,
            message=f"Permanent failure: {sanitize_error_message(exc)}",
            error=exc,
            attempts=updated.attempts,
        )
    except WebhookRetryableError as exc:
        logger.warning(
            "Retryable failure processing Stripe webhook event %s (type=%s): %s",
            event_id,
            event_type,
            exc,
        )
        updated = await record_webhook_outcome(
            db=db,
            event=event,
            outcome=ProcessingOutcome.RETRYABLE_FAILURE,
            error=exc,
            max_attempts=max_attempts,
        )
        return ProcessResult(
            success=False,
            status=updated.status,
            event_id=event_id,
            message=f"Retryable failure: {sanitize_error_message(exc)}",
            error=exc,
            attempts=updated.attempts,
        )
    except Exception as exc:
        # Treat unexpected handler exceptions as retryable by default
        logger.error(
            "Unexpected error processing Stripe webhook event %s (type=%s): %s",
            event_id,
            event_type,
            exc,
            exc_info=True,
        )
        updated = await record_webhook_outcome(
            db=db,
            event=event,
            outcome=ProcessingOutcome.RETRYABLE_FAILURE,
            error=exc,
            max_attempts=max_attempts,
        )
        return ProcessResult(
            success=False,
            status=updated.status,
            event_id=event_id,
            message=f"Unexpected failure: {sanitize_error_message(exc)}",
            error=exc,
            attempts=updated.attempts,
        )

    # 3. Handler succeeded: record processed outcome
    updated = await record_webhook_outcome(
        db=db,
        event=event,
        outcome=ProcessingOutcome.SUCCESS,
        max_attempts=max_attempts,
    )
    return ProcessResult(
        success=True,
        status=WebhookProcessingStatus.processed,
        event_id=event_id,
        message="Successfully processed",
        attempts=updated.attempts,
    )


async def process_webhook_event(
    db: AsyncSession,
    event_id: str,
    dispatcher: WebhookDispatcher | None = None,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> ProcessResult:
    """Coordinate the complete claim-execute-record pipeline for a Stripe webhook event.

    Args:
        db: Active SQLAlchemy async session.
        event_id: Stripe event ID to process.
        dispatcher: Optional custom dispatcher instance (defaults to default_webhook_dispatcher).
        max_attempts: Maximum retry threshold.

    Returns:
        ProcessResult indicating terminal processing outcome.
    """
    claim_res = await claim_webhook_event_by_id(db, event_id, max_attempts=max_attempts)

    if claim_res.status == ClaimStatus.NOT_FOUND:
        return ProcessResult(
            success=False,
            status=WebhookProcessingStatus.failed,
            event_id=event_id,
            message=f"Event '{event_id}' not found.",
        )

    if claim_res.status == ClaimStatus.ALREADY_PROCESSED:
        return ProcessResult(
            success=True,
            status=WebhookProcessingStatus.processed,
            event_id=event_id,
            message="Event already processed.",
            attempts=claim_res.event.attempts if claim_res.event else 1,
        )

    if claim_res.status == ClaimStatus.QUARANTINED:
        return ProcessResult(
            success=False,
            status=WebhookProcessingStatus.quarantined,
            event_id=event_id,
            message="Event is quarantined; skipping automatic execution.",
            attempts=claim_res.event.attempts if claim_res.event else 1,
        )

    if claim_res.status == ClaimStatus.MAX_ATTEMPTS_EXCEEDED:
        return ProcessResult(
            success=False,
            status=WebhookProcessingStatus.quarantined,
            event_id=event_id,
            message="Event exceeded maximum retry attempts and was quarantined.",
            attempts=claim_res.event.attempts if claim_res.event else max_attempts,
        )

    # Event successfully claimed
    event = claim_res.event
    assert event is not None  # Guaranteed by CLAIMED status

    current_dispatcher = dispatcher or default_webhook_dispatcher
    handler = current_dispatcher.get_handler(event.event_type)

    return await execute_claimed_webhook_event(
        db=db,
        event=event,
        handler=handler,
        max_attempts=max_attempts,
    )
