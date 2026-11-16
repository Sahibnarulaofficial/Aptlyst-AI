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

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import enum
import inspect
import logging
import re
from typing import Any
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import BillingWebhookEvent
from app.models.subscription import WebhookProcessingStatus
from app.services.billing.webhook_errors import (
    BillingWebhookConflictError,
    BillingWebhookError,
    BillingWebhookLeaseError,
    BillingWebhookPermanentError,
    BillingWebhookRetryableError,
    BillingWebhookScopeMismatchError,
    BillingWebhookUnknownEventError,
    BillingWebhookValidationError,
    DbErrorClassification,
    DbRetryDisposition,
    classify_db_exception as _classify_db_exception,
    sanitize_billing_error_message,
    sanitize_conflict_diagnostic_payload,
)

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
    signature_verified: bool = True


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
    failure_category: str | None = None
    lease_lost: bool = False
    reconciliation_upserted: bool = False
    error_code: str | None = None


# =============================================================================
# Canonical Razorpay Webhook Exception Hierarchy
# =============================================================================


class RazorpayWebhookEngineError(BillingWebhookError):
    """Base exception for all Razorpay webhook engine and handler errors."""

    default_failure_category: str = "webhook_error"


class StaleLeaseError(RazorpayWebhookEngineError, BillingWebhookLeaseError):
    """Raised when an operation is attempted with an expired, stolen, or superseded lease.

    Strictly disjoint from retryable and permanent failure classifications.
    The worker must abort immediately without mutating event status or incrementing attempts.
    """

    default_failure_category: str = "stale_lease"
    is_retryable: bool = False


class RazorpayWebhookRetryableError(
    RazorpayWebhookEngineError, BillingWebhookRetryableError
):
    """Base exception for transient errors that should be retried with exponential backoff."""

    default_failure_category: str = "retryable_failure"
    is_retryable: bool = True


class RazorpayDatabaseRetryableError(RazorpayWebhookRetryableError):
    """Transient database error (e.g. serialization failure 40001, deadlock 40P01, lock_timeout 55P03, pool drop)."""

    default_failure_category: str = "database_retryable"


class RazorpayProviderTransientError(RazorpayWebhookRetryableError):
    """Transient provider error (e.g. HTTP 429 rate limit, 5xx server error, pre-dispatch network error)."""

    default_failure_category: str = "provider_transient"


class RazorpayTransientRaceRetryError(RazorpayWebhookRetryableError):
    """Bounded race condition retry (e.g. customer provisioning in-flight, short backoff required)."""

    default_failure_category: str = "transient_race"


class RazorpayWebhookPermanentError(
    RazorpayWebhookEngineError, BillingWebhookPermanentError
):
    """Base exception for permanent errors that require immediate quarantine without retry."""

    default_failure_category: str = "permanent_failure"
    is_retryable: bool = False


class RazorpayPayloadValidationError(
    RazorpayWebhookPermanentError, BillingWebhookValidationError
):
    """Payload schema violation, bad timestamp, missing required attribute, or unparseable payload."""

    default_failure_category: str = "validation"


class RazorpayMissingEntityIdError(RazorpayPayloadValidationError):
    """Missing mandatory provider entity identifier (subscription_id, payment_id, invoice_id)."""

    default_failure_category: str = "missing_entity_id"


class RazorpayScopeMismatchError(
    RazorpayWebhookPermanentError, BillingWebhookScopeMismatchError
):
    """Provider, merchant account, or environment mismatch."""

    default_failure_category: str = "scope_mismatch"


class RazorpayUnverifiedEventError(RazorpayWebhookPermanentError):
    """Defense-in-depth: event reached worker with signature_verified == False."""

    default_failure_category: str = "unverified_event"


class RazorpayUnknownEventError(
    RazorpayWebhookPermanentError, BillingWebhookUnknownEventError
):
    """Unregistered or unsupported Razorpay event type."""

    default_failure_category: str = "unknown_event"


class RazorpayBusinessRejectionError(RazorpayWebhookPermanentError):
    """Deterministic business rule rejection (non-conflict, non-retryable)."""

    default_failure_category: str = "business_rejection"


class RazorpayDatabasePermanentError(RazorpayWebhookPermanentError):
    """Deterministic database error (e.g. trigger P0001, NOT NULL 23502, check constraint 23514)."""

    default_failure_category: str = "database_permanent"


class RazorpayProviderPermanentError(RazorpayWebhookPermanentError):
    """Deterministic provider error (e.g. HTTP 400/401/404/422, invalid credentials, billing disabled)."""

    default_failure_category: str = "provider_permanent"


class RazorpayWebhookReconciliationConflictError(
    RazorpayWebhookPermanentError, BillingWebhookConflictError
):
    """Raised when an event conflicts with authoritative state and requires audit reconciliation.

    Stages a PaymentReconciliation case and quarantines the event.
    Accepts both canonical keyword arguments (committed_*) and legacy keyword arguments
    for backwards compatibility.
    """

    default_failure_category: str = "reconciliation_conflict"

    def __init__(
        self,
        message: str = "",
        *,
        case_key: str,
        reason: str,
        organization_id: uuid.UUID | None = None,
        committed_subscription_id: uuid.UUID | None = None,
        subscription_id: uuid.UUID | None = None,
        committed_invoice_id: uuid.UUID | None = None,
        invoice_id: uuid.UUID | None = None,
        committed_payment_id: uuid.UUID | None = None,
        payment_id: uuid.UUID | None = None,
        raw_event_id: uuid.UUID | None = None,
        diagnostic_payload: dict[str, Any] | None = None,
        failure_category: str | None = None,
        error_code: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__(
            message,
            failure_category=failure_category or self.default_failure_category,
            error_code=error_code,
            constraint_name=constraint_name,
        )
        self.case_key: str = case_key
        self.reason: str = reason
        self.organization_id: uuid.UUID | None = organization_id
        # Support both committed_* and legacy kwargs; committed_* takes precedence if both provided
        self.committed_subscription_id: uuid.UUID | None = (
            committed_subscription_id if committed_subscription_id is not None else subscription_id
        )
        self.subscription_id: uuid.UUID | None = self.committed_subscription_id
        self.committed_invoice_id: uuid.UUID | None = (
            committed_invoice_id if committed_invoice_id is not None else invoice_id
        )
        self.invoice_id: uuid.UUID | None = self.committed_invoice_id
        self.committed_payment_id: uuid.UUID | None = (
            committed_payment_id if committed_payment_id is not None else payment_id
        )
        self.payment_id: uuid.UUID | None = self.committed_payment_id
        self.raw_event_id: uuid.UUID | None = raw_event_id
        self.diagnostic_payload: dict[str, Any] = (
            sanitize_conflict_diagnostic_payload(diagnostic_payload)
            if diagnostic_payload is not None
            else {}
        )


# =============================================================================
# Error Sanitization & Classification
# =============================================================================


def sanitize_error_message(err: BaseException | str | None, max_chars: int = 1024) -> str:
    """Sanitize and truncate error strings to prevent secret leakage in audit logs.

    Delegates to the unified provider-neutral sanitizer in webhook_errors.
    """
    return sanitize_billing_error_message(err, max_chars=max_chars)


def classify_client_error(exc: Exception) -> RazorpayWebhookEngineError | None:
    """Classify a Razorpay client/HTTP exception into a canonical Razorpay webhook error.

    Rules:
    - Already a RazorpayWebhookEngineError: returns exc unmodified.
    - RazorpayRateLimitError (HTTP 429): RazorpayProviderTransientError
    - RazorpayServer5xxError (HTTP 5xx): RazorpayProviderTransientError
    - RazorpayNetworkError:
        - If is_pre_dispatch is True: RazorpayProviderTransientError (connect timeout, pre-dispatch failure)
        - If is_pre_dispatch is False: RazorpayProviderPermanentError (ambiguous post-dispatch mutation)
    - asyncio.TimeoutError: RazorpayProviderTransientError
    - RazorpayAuthError (HTTP 401): RazorpayProviderPermanentError
    - RazorpayBillingDisabledError: RazorpayProviderPermanentError
    - RazorpayConfigurationError: RazorpayProviderPermanentError
    - RazorpayAPIError:
        - If status_code == 429: RazorpayProviderTransientError
        - If status_code >= 500: RazorpayProviderTransientError
        - If status_code in (400, 401, 403, 404, 422) or any 4xx: RazorpayProviderPermanentError
        - Other status codes: RazorpayProviderPermanentError
    - Other unmapped exceptions: returns None (caller applies generic classification).
    """
    if isinstance(exc, RazorpayWebhookEngineError):
        return exc

    import asyncio
    if isinstance(exc, asyncio.TimeoutError):
        return RazorpayProviderTransientError(
            message=sanitize_billing_error_message(exc) or "Provider request timed out",
            error_code="TIMEOUT",
        )

    # Lazy import to prevent circular dependency
    from app.services.billing.razorpay.client import (
        RazorpayAPIError,
        RazorpayAuthError,
        RazorpayBillingDisabledError,
        RazorpayClientError,
        RazorpayConfigurationError,
        RazorpayNetworkError,
        RazorpayRateLimitError,
        RazorpayServer5xxError,
    )

    if not isinstance(exc, RazorpayClientError):
        return None

    sanitized = sanitize_billing_error_message(exc)

    if isinstance(exc, RazorpayRateLimitError):
        return RazorpayProviderTransientError(
            message=sanitized,
            error_code="429",
        )

    if isinstance(exc, RazorpayServer5xxError):
        return RazorpayProviderTransientError(
            message=sanitized,
            error_code=str(getattr(exc, "status_code", 500)),
        )

    if isinstance(exc, RazorpayNetworkError):
        if getattr(exc, "is_pre_dispatch", False):
            return RazorpayProviderTransientError(
                message=sanitized,
                error_code="PRE_DISPATCH_NETWORK_ERROR",
            )
        return RazorpayProviderPermanentError(
            message=sanitized,
            error_code="POST_DISPATCH_NETWORK_ERROR",
        )

    if isinstance(exc, RazorpayAuthError):
        return RazorpayProviderPermanentError(
            message=sanitized,
            error_code="401",
        )

    if isinstance(exc, (RazorpayBillingDisabledError, RazorpayConfigurationError)):
        return RazorpayProviderPermanentError(
            message=sanitized,
            error_code=type(exc).__name__,
        )

    if isinstance(exc, RazorpayAPIError):
        status_code = getattr(exc, "status_code", 400)
        code_str = str(status_code)
        if status_code == 429 or status_code >= 500:
            return RazorpayProviderTransientError(
                message=sanitized,
                error_code=code_str,
            )
        return RazorpayProviderPermanentError(
            message=sanitized,
            error_code=code_str,
        )

    # Any other RazorpayClientError
    return RazorpayProviderPermanentError(
        message=sanitized,
        error_code=type(exc).__name__,
    )


def classify_db_exception(
    exc: Exception, *, max_nodes: int = 32
) -> DbErrorClassification:
    """Classify a database exception using the provider-neutral SQLSTATE classifier.

    Delegates to app.services.billing.webhook_errors.classify_db_exception.
    """
    return _classify_db_exception(exc, max_nodes=max_nodes)


def classify_db_error_to_razorpay_exception(
    exc: Exception, *, max_nodes: int = 32
) -> RazorpayDatabasePermanentError | RazorpayDatabaseRetryableError | None:
    """Classify a database exception into a concrete Razorpay database error subclass.

    Maps:
    - DbRetryDisposition.PERMANENT -> RazorpayDatabasePermanentError
    - DbRetryDisposition.RETRYABLE / RETRYABLE_WITH_ALERT -> RazorpayDatabaseRetryableError
    - DbRetryDisposition.UNKNOWN -> None
    """
    res = _classify_db_exception(exc, max_nodes=max_nodes)
    if res.disposition == DbRetryDisposition.PERMANENT:
        return RazorpayDatabasePermanentError(
            message=sanitize_billing_error_message(exc),
            failure_category=res.failure_category,
            error_code=res.details.sqlstate,
            constraint_name=res.details.constraint_name,
        )
    elif res.disposition in (
        DbRetryDisposition.RETRYABLE,
        DbRetryDisposition.RETRYABLE_WITH_ALERT,
    ):
        return RazorpayDatabaseRetryableError(
            message=sanitize_billing_error_message(exc),
            failure_category=res.failure_category,
            error_code=res.details.sqlstate,
            constraint_name=res.details.constraint_name,
        )
    return None


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
        signature_verified=getattr(event, "signature_verified", True),
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
        db_token = event.lease_token
        db_locked_by = event.locked_by
        db_expires_at = event.lease_expires_at
        await db.rollback()
        logger.warning(
            "Fencing violation: worker %s lost lease on event %s (db_token=%s, held_token=%s, db_locked_by=%s, db_expires_at=%s, now=%s)",
            claimed.locked_by,
            claimed.event_id,
            db_token,
            claimed.lease_token,
            db_locked_by,
            db_expires_at,
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


# =============================================================================
# Phase 3 Atomic Transaction Engine
# =============================================================================


async def _check_unbound_race_qualification(
    work_db: AsyncSession,
    claimed: ClaimedWebhookEvent,
    exc: RazorpayWebhookReconciliationConflictError,
    now: datetime,
) -> bool:
    """Check whether an unbound_provider_customer conflict qualifies for a single 2s race retry (ED-02).

    Qualifies only if there is an in-flight or just-committed customer provision or checkout attempt
    for the same provider, merchant account, and customer/subscription ID.
    """
    diag = exc.diagnostic_payload or {}
    clean_cust_id = diag.get("customer_id")
    clean_sub_id = diag.get("subscription_id")

    if not clean_cust_id and not clean_sub_id:
        return False

    try:
        from app.models.payment import (
            BillingCheckoutAttempt,
            BillingCustomerProvision,
            CheckoutAttemptStatus,
            CustomerProvisionStatus,
        )

        # Check 1: BillingCustomerProvision
        if clean_cust_id:
            stmt_prov = select(BillingCustomerProvision).where(
                BillingCustomerProvision.provider == claimed.provider,
                BillingCustomerProvision.merchant_account_id == claimed.merchant_account_id,
                BillingCustomerProvision.environment == claimed.environment,
                BillingCustomerProvision.provider_customer_id == clean_cust_id,
                or_(
                    BillingCustomerProvision.status == CustomerProvisionStatus.resolved,
                    BillingCustomerProvision.status == CustomerProvisionStatus.pending,
                ),
            )
            res_prov = await work_db.execute(stmt_prov)
            prov = res_prov.scalar_one_or_none()
            if prov is not None:
                return True

        # Check 2: BillingCheckoutAttempt
        or_conditions = []
        if clean_sub_id:
            or_conditions.append(BillingCheckoutAttempt.provider_subscription_id == clean_sub_id)
        if clean_cust_id:
            or_conditions.append(BillingCheckoutAttempt.provider_customer_id == clean_cust_id)

        if or_conditions:
            stmt_att = select(BillingCheckoutAttempt).where(
                BillingCheckoutAttempt.provider == claimed.provider,
                BillingCheckoutAttempt.merchant_account_id == claimed.merchant_account_id,
                BillingCheckoutAttempt.environment == claimed.environment,
                or_(*or_conditions),
                BillingCheckoutAttempt.status.in_([
                    CheckoutAttemptStatus.creating,
                    CheckoutAttemptStatus.recovery_required,
                    CheckoutAttemptStatus.open,
                ]),
            )
            res_att = await work_db.execute(stmt_att)
            att = res_att.scalar_one_or_none()
            if att is not None:
                return True
    except Exception as q_exc:
        logger.debug("Failed querying race qualification records: %s", q_exc)

    return False


async def _handle_commit_failure(
    session_factory: Callable[[], AsyncSession],
    claimed: ClaimedWebhookEvent,
    commit_exc: Exception,
    max_attempts: int,
    now: datetime,
) -> ProcessResult:
    """Handle outer transaction commit failure via a fenced fallback transaction."""
    logger.error(
        "Worker %s commit failed for event %s: %s; attempting fenced fallback outcome transaction",
        claimed.locked_by,
        claimed.event_id,
        sanitize_billing_error_message(str(commit_exc)),
        extra={
            "worker_id": claimed.locked_by,
            "event_id": str(claimed.event_id),
            "failure_category": "commit_failure",
        },
    )
    try:
        async with session_factory() as fallback_db:
            stmt = select(BillingWebhookEvent).where(BillingWebhookEvent.id == claimed.event_id).with_for_update()
            res = await fallback_db.execute(stmt)
            fb_event = res.scalar_one_or_none()
            if fb_event and fb_event.lease_token == claimed.lease_token and fb_event.locked_by == claimed.locked_by:
                fb_event.attempts += 1
                fb_event.processed_at = now
                sanitized_err = sanitize_billing_error_message(str(commit_exc))
                fb_event.error_message = sanitized_err
                if fb_event.attempts >= max_attempts:
                    fb_event.status = WebhookProcessingStatus.quarantined
                    fb_event.quarantined_at = now
                    fb_event.quarantine_reason = f"Quarantined: commit failure exceeded max attempts ({fb_event.attempts}/{max_attempts})"
                    fb_event.next_retry_at = None
                else:
                    fb_event.status = WebhookProcessingStatus.failed
                    delay_sec = get_retry_backoff_seconds(fb_event.attempts)
                    fb_event.next_retry_at = now + timedelta(seconds=delay_sec)
                fb_event.lease_token = None
                fb_event.locked_by = None
                fb_event.lease_expires_at = None
                await fallback_db.commit()
                return ProcessResult(
                    success=False,
                    status=fb_event.status,
                    event_id=claimed.event_id,
                    provider_event_id=claimed.provider_event_id,
                    attempts=fb_event.attempts,
                    message=f"Commit failed; fallback recorded {fb_event.status.value}",
                    failure_category="commit_failure",
                    error=commit_exc,
                )
    except Exception as fb_exc:
        logger.error(
            "Fallback outcome recording failed for event %s: %s",
            claimed.event_id,
            sanitize_billing_error_message(str(fb_exc)),
            extra={
                "worker_id": claimed.locked_by,
                "event_id": str(claimed.event_id),
                "failure_category": "fallback_failure",
            },
        )

    return ProcessResult(
        success=False,
        status=WebhookProcessingStatus.failed,
        event_id=claimed.event_id,
        provider_event_id=claimed.provider_event_id,
        attempts=claimed.attempts,
        message="Commit failed and fallback recording failed; lease will recover after expiry",
        failure_category="commit_failure",
        error=commit_exc,
    )


async def execute_claimed_razorpay_webhook_event(
    session_factory: Callable[[], AsyncSession],
    claimed: ClaimedWebhookEvent,
    handler: RazorpayWebhookHandler | None,
    *,
    max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
    now_fn: Callable[[], datetime] | None = None,
    expected_merchant_id: str | None = None,
    expected_environment: str | None = None,
) -> ProcessResult:
    """Execute a claimed Razorpay webhook event in a single atomic database transaction.

    Guarantees & Lifecycle:
    1. Single Atomic Transaction: Handler business writes, reconciliation upserts (if any),
       and final event outcome status are committed together in one outer transaction.
    2. Lease Fencing: Acquires row lock (SELECT ... FOR UPDATE) on the event row and
       asserts unexpired lease ownership before executing or committing mutations.
    3. Savepoint Isolation: Executes the handler inside an inner savepoint (begin_nested()).
       Handler failure rolls back domain changes cleanly without losing the outer transaction.
    4. Anti-Spoofing & FK Safety: On conflict, persists reconciliation using native PostgreSQL
       ON CONFLICT DO UPDATE, ensuring only verified committed foreign keys are referenced.
    5. Clean Cancellation: Propagates asyncio.CancelledError cleanly without writing outcomes.
    6. Sanitized Logging: Zero credentials or raw traceback locals exposed in logs.
    """
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)

    async with session_factory() as work_db:
        # 1. Bounded local lock timeout for PostgreSQL
        try:
            from sqlalchemy import text

            await work_db.execute(text("SET LOCAL lock_timeout = '5000'"))
        except Exception:
            pass

        # 2. Acquire Level -1 row lock on event row
        stmt = (
            select(BillingWebhookEvent)
            .where(BillingWebhookEvent.id == claimed.event_id)
            .with_for_update()
        )
        res = await work_db.execute(stmt)
        event = res.scalar_one_or_none()

        if event is None:
            await work_db.rollback()
            return ProcessResult(
                success=False,
                status=WebhookProcessingStatus.failed,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=claimed.attempts,
                message=f"Event {claimed.event_id} was deleted or not found.",
                lease_lost=True,
                failure_category="lease_lost",
            )

        # 3. Fencing assertion
        is_same_token = event.lease_token == claimed.lease_token
        is_same_worker = event.locked_by == claimed.locked_by
        is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now

        if not (is_same_token and is_same_worker and is_unexpired):
            db_token = event.lease_token
            db_locked_by = event.locked_by
            db_expires_at = event.lease_expires_at
            event_status = event.status
            event_attempts = event.attempts
            await work_db.rollback()
            logger.warning(
                "Fencing violation: worker %s lost lease on event %s (db_token=%s, held_token=%s, db_locked_by=%s, db_expires_at=%s, now=%s)",
                claimed.locked_by,
                claimed.event_id,
                db_token,
                claimed.lease_token,
                db_locked_by,
                db_expires_at,
                now,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "lease_status": "lost",
                    "failure_category": "stale_lease",
                },
            )
            return ProcessResult(
                success=False,
                status=event_status,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event_attempts,
                message=f"Worker '{claimed.locked_by}' lost lease ownership for event {claimed.event_id}.",
                lease_lost=True,
                failure_category="stale_lease",
            )

        # 4. Signature verification check
        sig_verified = getattr(claimed, "signature_verified", True) and getattr(event, "signature_verified", True)
        if not sig_verified:
            event.attempts += 1
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now
            event.quarantine_reason = "Unverified webhook signature"
            event.error_message = sanitize_billing_error_message(
                f"Event {claimed.provider_event_id}: signature_verified is False."
            )
            event.next_retry_at = None
            event.lease_token = None
            event.locked_by = None
            event.lease_expires_at = None
            await work_db.commit()
            return ProcessResult(
                success=False,
                status=WebhookProcessingStatus.quarantined,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message="Quarantined: unverified webhook signature",
                failure_category="unverified_event",
            )

        # 5. Scope validation
        configured_merchant = expected_merchant_id
        if configured_merchant and configured_merchant != "default":
            if event.merchant_account_id.strip() != configured_merchant.strip():
                event.attempts += 1
                event.status = WebhookProcessingStatus.quarantined
                event.quarantined_at = now
                event.quarantine_reason = f"Merchant mismatch: expected {configured_merchant}, got {event.merchant_account_id}"
                event.error_message = sanitize_billing_error_message(event.quarantine_reason)
                event.next_retry_at = None
                event.lease_token = None
                event.locked_by = None
                event.lease_expires_at = None
                await work_db.commit()
                return ProcessResult(
                    success=False,
                    status=WebhookProcessingStatus.quarantined,
                    event_id=claimed.event_id,
                    provider_event_id=claimed.provider_event_id,
                    attempts=event.attempts,
                    message="Quarantined: merchant account mismatch",
                    failure_category="scope_mismatch",
                )

        if expected_environment:
            if event.environment.strip().lower() != expected_environment.strip().lower():
                event.attempts += 1
                event.status = WebhookProcessingStatus.quarantined
                event.quarantined_at = now
                event.quarantine_reason = f"Environment mismatch: expected {expected_environment}, got {event.environment}"
                event.error_message = sanitize_billing_error_message(event.quarantine_reason)
                event.next_retry_at = None
                event.lease_token = None
                event.locked_by = None
                event.lease_expires_at = None
                await work_db.commit()
                return ProcessResult(
                    success=False,
                    status=WebhookProcessingStatus.quarantined,
                    event_id=claimed.event_id,
                    provider_event_id=claimed.provider_event_id,
                    attempts=event.attempts,
                    message="Quarantined: environment mismatch",
                    failure_category="scope_mismatch",
                )

        # 6. Dispatch lookup check
        if handler is None:
            event.attempts += 1
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now
            event.quarantine_reason = f"Unknown event type: '{claimed.event_type}'"
            event.error_message = sanitize_billing_error_message(
                f"No handler registered for event type '{claimed.event_type}'"
            )
            event.next_retry_at = None
            event.lease_token = None
            event.locked_by = None
            event.lease_expires_at = None
            await work_db.commit()
            return ProcessResult(
                success=False,
                status=WebhookProcessingStatus.quarantined,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message=f"Quarantined: unknown event type '{claimed.event_type}'",
                failure_category="unknown_event",
            )

        # 7. Execute handler within inner savepoint
        handler_failed = False
        handler_exc: Exception | None = None

        try:
            async with work_db.begin_nested():
                sig = inspect.signature(handler)
                params = list(sig.parameters.values())
                if params and params[0].name in ("event", "claimed", "ctx", "context"):
                    await handler(claimed, work_db)
                else:
                    await handler(claimed.payload, work_db)
        except asyncio.CancelledError:
            await work_db.rollback()
            logger.warning(
                "Worker %s cancelled during processing of event %s. Transaction rolled back; lease left for recovery.",
                claimed.locked_by,
                claimed.event_id,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "failure_category": "cancelled",
                },
            )
            raise
        except Exception as exc:
            handler_failed = True
            handler_exc = exc

        # 8. Happy path: Handler succeeded
        if not handler_failed:
            now_commit = now_fn() if now_fn is not None else datetime.now(timezone.utc)
            if event.lease_expires_at is None or event.lease_expires_at < now_commit:
                await work_db.rollback()
                logger.warning(
                    "Worker %s lease expired prior to commit for event %s",
                    claimed.locked_by,
                    claimed.event_id,
                    extra={
                        "worker_id": claimed.locked_by,
                        "event_id": str(claimed.event_id),
                        "failure_category": "stale_lease",
                    },
                )
                return ProcessResult(
                    success=False,
                    status=event.status,
                    event_id=claimed.event_id,
                    provider_event_id=claimed.provider_event_id,
                    attempts=event.attempts,
                    message="Worker lease expired prior to commit",
                    lease_lost=True,
                    failure_category="stale_lease",
                )

            event.status = WebhookProcessingStatus.processed
            event.processed_at = now_commit
            event.error_message = None
            event.next_retry_at = None
            event.lease_token = None
            event.locked_by = None
            event.lease_expires_at = None

            try:
                await work_db.commit()
            except Exception as commit_exc:
                await work_db.rollback()
                return await _handle_commit_failure(session_factory, claimed, commit_exc, max_attempts, now_commit)

            logger.info(
                "Worker %s successfully processed event %s (provider_event_id=%s, type=%s)",
                claimed.locked_by,
                claimed.event_id,
                claimed.provider_event_id,
                claimed.event_type,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "failure_category": "none",
                },
            )
            return ProcessResult(
                success=True,
                status=WebhookProcessingStatus.processed,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message="Successfully processed webhook event",
            )

        # 9. Failure path: Classify and record outcome in outer work_db transaction
        if isinstance(handler_exc, StaleLeaseError):
            await work_db.rollback()
            logger.warning(
                "Worker %s aborted domain execution: lost lease on event %s: %s",
                claimed.locked_by,
                claimed.event_id,
                handler_exc,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "failure_category": "stale_lease",
                },
            )
            return ProcessResult(
                success=False,
                status=event.status,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message=f"Fencing rejected stale worker during domain execution: {handler_exc}",
                lease_lost=True,
                failure_category="stale_lease",
                error=handler_exc,
            )

        now_outcome = now_fn() if now_fn is not None else datetime.now(timezone.utc)
        if event.lease_expires_at is None or event.lease_expires_at < now_outcome:
            await work_db.rollback()
            return ProcessResult(
                success=False,
                status=event.status,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message="Worker lease expired while handling error",
                lease_lost=True,
                failure_category="stale_lease",
                error=handler_exc,
            )

        # 9A. Reconciliation Conflict
        if isinstance(handler_exc, RazorpayWebhookReconciliationConflictError):
            if handler_exc.reason == "unbound_provider_customer" and claimed.attempts == 0:
                is_qualifying = await _check_unbound_race_qualification(
                    work_db=work_db,
                    claimed=claimed,
                    exc=handler_exc,
                    now=now_outcome,
                )
                if is_qualifying:
                    event.attempts += 1
                    event.status = WebhookProcessingStatus.failed
                    event.processed_at = now_outcome
                    event.next_retry_at = now_outcome + timedelta(seconds=2)
                    event.error_message = sanitize_billing_error_message(str(handler_exc))
                    event.lease_token = None
                    event.locked_by = None
                    event.lease_expires_at = None
                    try:
                        await work_db.commit()
                    except Exception as commit_exc:
                        await work_db.rollback()
                        return await _handle_commit_failure(session_factory, claimed, commit_exc, max_attempts, now_outcome)

                    logger.info(
                        "Worker %s scheduled 2s race retry for unbound event %s",
                        claimed.locked_by,
                        claimed.event_id,
                        extra={
                            "worker_id": claimed.locked_by,
                            "event_id": str(claimed.event_id),
                            "failure_category": "concurrent_binding_race",
                        },
                    )
                    return ProcessResult(
                        success=False,
                        status=WebhookProcessingStatus.failed,
                        event_id=claimed.event_id,
                        provider_event_id=claimed.provider_event_id,
                        attempts=event.attempts,
                        message="Concurrent binding race: scheduled fresh session retry after 2s",
                        failure_category="concurrent_binding_race",
                        error=handler_exc,
                    )

            # Non-qualifying conflict or exhausted attempts: atomic upsert + quarantine
            from app.services.billing.razorpay.handler_base import upsert_payment_reconciliation

            diag_payload = dict(handler_exc.diagnostic_payload or {})
            diag_payload.setdefault("event_id", str(claimed.event_id))
            diag_payload.setdefault("provider_event_id", claimed.provider_event_id)
            diag_payload.setdefault("event_type", claimed.event_type)

            verified_org_id = handler_exc.organization_id
            if verified_org_id is not None:
                try:
                    from app.models.organization import Organization

                    org_res = await work_db.execute(
                        select(Organization.id).where(Organization.id == verified_org_id)
                    )
                    if org_res.scalar_one_or_none() is None:
                        verified_org_id = None
                        diag_payload["unverified_claimed_organization_id"] = str(handler_exc.organization_id)
                except Exception:
                    pass

            await upsert_payment_reconciliation(
                db=work_db,
                case_key=handler_exc.case_key,
                reason=handler_exc.reason,
                provider=claimed.provider,
                merchant_account_id=claimed.merchant_account_id,
                environment=claimed.environment,
                organization_id=verified_org_id,
                subscription_id=getattr(handler_exc, "committed_subscription_id", None),
                raw_event_id=handler_exc.raw_event_id or claimed.event_id,
                diagnostic_data=diag_payload,
                now=now_outcome,
            )

            event.attempts += 1
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now_outcome
            event.quarantine_reason = (
                f"Reconciliation conflict: {handler_exc.reason} ({handler_exc.case_key})"
            )
            event.error_message = sanitize_billing_error_message(str(handler_exc))
            event.next_retry_at = None
            event.lease_token = None
            event.locked_by = None
            event.lease_expires_at = None

            try:
                await work_db.commit()
            except Exception as commit_exc:
                await work_db.rollback()
                return await _handle_commit_failure(session_factory, claimed, commit_exc, max_attempts, now_outcome)

            logger.warning(
                "Worker %s quarantined event %s due to reconciliation conflict: %s (%s)",
                claimed.locked_by,
                claimed.event_id,
                handler_exc.reason,
                handler_exc.case_key,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "failure_category": "reconciliation_conflict",
                    "case_key": handler_exc.case_key,
                },
            )
            return ProcessResult(
                success=False,
                status=WebhookProcessingStatus.quarantined,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message=f"Quarantined: reconciliation conflict ({handler_exc.case_key})",
                failure_category="reconciliation_conflict",
                reconciliation_upserted=True,
                error=handler_exc,
            )

        # 9B. Classify Client and DB errors
        client_mapped = classify_client_error(handler_exc)
        if client_mapped is not None:
            handler_exc = client_mapped

        db_mapped = classify_db_error_to_razorpay_exception(handler_exc)
        if db_mapped is not None:
            handler_exc = db_mapped

        # 9C. Permanent failures
        if isinstance(handler_exc, (RazorpayWebhookPermanentError, BillingWebhookPermanentError)):
            event.attempts += 1
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now_outcome
            sanitized_err = sanitize_billing_error_message(str(handler_exc))
            event.quarantine_reason = f"Permanent error: {sanitized_err}"
            event.error_message = sanitized_err
            event.next_retry_at = None
            event.lease_token = None
            event.locked_by = None
            event.lease_expires_at = None

            try:
                await work_db.commit()
            except Exception as commit_exc:
                await work_db.rollback()
                return await _handle_commit_failure(session_factory, claimed, commit_exc, max_attempts, now_outcome)

            logger.warning(
                "Worker %s quarantined event %s due to permanent error: %s",
                claimed.locked_by,
                claimed.event_id,
                sanitized_err,
                extra={
                    "worker_id": claimed.locked_by,
                    "event_id": str(claimed.event_id),
                    "failure_category": "permanent_failure",
                },
            )
            return ProcessResult(
                success=False,
                status=WebhookProcessingStatus.quarantined,
                event_id=claimed.event_id,
                provider_event_id=claimed.provider_event_id,
                attempts=event.attempts,
                message="Quarantined: permanent error",
                failure_category="permanent_failure",
                error=handler_exc,
            )

        # 9D. Retryable failures / Unexpected Internal Errors (ED-01)
        is_retryable = isinstance(
            handler_exc, (RazorpayWebhookRetryableError, BillingWebhookRetryableError)
        )
        event.attempts += 1
        event.processed_at = now_outcome
        sanitized_err = sanitize_billing_error_message(str(handler_exc))
        event.error_message = sanitized_err

        category = "retryable_failure" if is_retryable else "unexpected_internal_error"

        if event.attempts >= max_attempts:
            event.status = WebhookProcessingStatus.quarantined
            event.quarantined_at = now_outcome
            event.quarantine_reason = (
                f"Quarantined: exceeded max attempts ({event.attempts}/{max_attempts}) [{category}]"
            )
            event.next_retry_at = None
        else:
            event.status = WebhookProcessingStatus.failed
            delay_sec = get_retry_backoff_seconds(event.attempts)
            event.next_retry_at = now_outcome + timedelta(seconds=delay_sec)

        event.lease_token = None
        event.locked_by = None
        event.lease_expires_at = None

        try:
            await work_db.commit()
        except Exception as commit_exc:
            await work_db.rollback()
            return await _handle_commit_failure(session_factory, claimed, commit_exc, max_attempts, now_outcome)

        logger.warning(
            "Worker %s recorded %s for event %s (attempts=%d/%d, status=%s): %s",
            claimed.locked_by,
            category,
            claimed.event_id,
            event.attempts,
            max_attempts,
            event.status.value,
            sanitized_err,
            extra={
                "worker_id": claimed.locked_by,
                "event_id": str(claimed.event_id),
                "failure_category": category,
                "attempt_number": event.attempts,
            },
        )
        return ProcessResult(
            success=False,
            status=event.status,
            event_id=claimed.event_id,
            provider_event_id=claimed.provider_event_id,
            attempts=event.attempts,
            message=f"Outcome recorded: {event.status.value}",
            failure_category=category,
            error=handler_exc,
        )


__all__ = [
    "DEFAULT_LEASE_DURATION_SECONDS",
    "MAX_WEBHOOK_ATTEMPTS",
    "WEBHOOK_RETRY_BACKOFF_SCHEDULE",
    "ClaimedWebhookEvent",
    "ProcessResult",
    "ProcessingOutcome",
    "RazorpayBusinessRejectionError",
    "RazorpayDatabasePermanentError",
    "RazorpayDatabaseRetryableError",
    "RazorpayMissingEntityIdError",
    "RazorpayPayloadValidationError",
    "RazorpayProviderPermanentError",
    "RazorpayProviderTransientError",
    "RazorpayScopeMismatchError",
    "RazorpayTransientRaceRetryError",
    "RazorpayUnknownEventError",
    "RazorpayUnverifiedEventError",
    "RazorpayWebhookDispatcher",
    "RazorpayWebhookEngineError",
    "RazorpayWebhookHandler",
    "RazorpayWebhookPermanentError",
    "RazorpayWebhookReconciliationConflictError",
    "RazorpayWebhookRetryableError",
    "StaleLeaseError",
    "claim_next_razorpay_webhook_event",
    "classify_client_error",
    "classify_db_error_to_razorpay_exception",
    "classify_db_exception",
    "default_razorpay_dispatcher",
    "execute_claimed_razorpay_webhook_event",
    "get_retry_backoff_seconds",
    "record_razorpay_webhook_outcome",
    "sanitize_error_message",
]

