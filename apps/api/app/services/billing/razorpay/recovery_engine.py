"""Durable background recovery engine for Razorpay checkout attempts.

Stage 2D implementation of ADR-006 Decision C:
1. Atomic Leased Claiming:
   - Uses SELECT ... FOR UPDATE SKIP LOCKED to claim eligible attempts in 'recovery_required' state.
   - Enforces NULL-safe eligibility predicate:
     status = 'recovery_required'
     AND next_recovery_retry_at IS NOT NULL
     AND next_recovery_retry_at <= :now
     AND (recovery_claim_expires_at IS NULL OR recovery_claim_expires_at <= :now)
     AND recovery_attempts < :max_attempts
   - Generates and stamps unpredictable UUID recovery_claim_id, sets recovery_claim_expires_at = now + lease_duration.
   - Commits claim transaction immediately, releasing row locks before gateway inquiry.
2. Gateway Investigation Outside DB Transactions:
   - Queries Razorpay API strictly outside any database transaction or row lock.
   - Reuses existing provider client abstraction with bounded pagination (up to 3 pages).
3. Stale Worker Fencing & Outcome Persistence:
   - Reacquires row lock via SELECT ... FOR UPDATE.
   - Strictly verifies attempt.recovery_claim_id == claimed.recovery_claim_id and status == 'recovery_required'.
   - Stale workers (whose lease expired or was superseded) abort without mutating attempt state.
4. Bounded Exponential Backoff & Quarantine:
   - Inconclusive business outcomes increment recovery_attempts and schedule next_recovery_retry_at using
     schedule [60s, 300s, 900s, 1800s, 3600s] +/- 15% jitter.
   - Gateway outages / 5xx / timeouts schedule transient retry without incrementing attempt budget.
   - Upon exhausting 5 attempts, sets next_recovery_retry_at = NULL, stages PaymentReconciliation,
     and permanently quarantines the attempt in recovery_required to preserve active reservation.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
import random
from typing import Any
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import (
    ALLOWED_RAZORPAY_ENVIRONMENTS,
    PROHIBITED_PRODUCTION_MERCHANT_IDS,
    get_canonical_razorpay_merchant_account_id,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
    validate_razorpay_runtime_scope,
)

PROHIBITED_PRODUCTION_MERCHANT_NAMES: frozenset[str] = PROHIBITED_PRODUCTION_MERCHANT_IDS | {
    "test",
    "dev",
    "stage",
    "staging",
    "ci",
}
from app.models.payment import (
    BillingCheckoutAttempt,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayBillingDisabledError,
    RazorpayClient,
    RazorpayConfigurationError,
    RazorpayNetworkError,
    RazorpayRateLimitError,
    RazorpayServer5xxError,
    get_razorpay_client,
    is_razorpay_enabled,
)
from app.services.billing.razorpay.handler_base import (
    stage_or_update_payment_reconciliation,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.services.billing.razorpay.recovery_engine")

# =============================================================================
# ADR-006 Section 5.3 Constants & Retry Configuration
# =============================================================================

# Bounded backoff schedule: Delta_t in {60s, 300s, 900s, 1800s, 3600s}
RECOVERY_BACKOFF_SCHEDULE: tuple[int, ...] = (60, 300, 900, 1800, 3600)
DEFAULT_RECOVERY_LEASE_DURATION_SECONDS: int = 60
MAX_RECOVERY_ATTEMPTS: int = 5
MAX_TRANSIENT_RECOVERIES: int = 10
MAX_EXPIRED_LEASE_RECOVERIES: int = 5
RECOVERY_JITTER_RATIO: float = 0.15  # +/- 15% jitter
DEFAULT_TRANSIENT_BACKOFF_SECONDS: int = 60
RECOVERY_PAGE_SIZE: int = 50
RECOVERY_MAX_PAGES: int = 3
RECOVERY_CLOCK_SKEW_SECONDS: int = 300
CHECKOUT_LEASE_SECONDS: int = 60
DEFAULT_SUBSCRIPTION_EXPIRY_DAYS: int = 7


# =============================================================================
# Exceptions
# =============================================================================


class RazorpayRecoveryError(Exception):
    """Base exception for Razorpay recovery operations."""


class StaleRecoveryClaimError(RazorpayRecoveryError):
    """Raised when a worker attempts to persist recovery for a claim that expired or was superseded."""


class RecoveryEligibilityError(RazorpayRecoveryError):
    """Raised when an attempt is not eligible for recovery (e.g. terminal, wrong env, wrong provider)."""


class RecoveryQuarantinedError(RazorpayRecoveryError):
    """Raised when an attempt has exhausted the maximum recovery attempt budget and is quarantined."""


class RecoveryAmbiguousError(RazorpayRecoveryError):
    """Raised when recovery inquiry is inconclusive or ambiguous."""


# =============================================================================
# Data Structures
# =============================================================================


class RecoveryOutcome(str, Enum):
    """Possible outcomes of a recovery execution step."""

    RECOVERED = "recovered"
    RETRY_SCHEDULED = "retry_scheduled"
    TRANSIENT_BACKOFF = "transient_backoff"
    QUARANTINED = "quarantined"
    FAILED_DEFINITIVE = "failed_definitive"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ClaimedRecoveryAttempt:
    """Represents a checkout attempt successfully leased for background recovery."""

    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    status: CheckoutAttemptStatus
    recovery_claim_id: uuid.UUID
    recovery_claim_expires_at: datetime
    recovery_attempts: int
    created_at: datetime
    request_payload: dict[str, Any]
    provider_customer_id: str | None
    provider_subscription_id: str | None
    worker_id: str


@dataclass(frozen=True)
class RecoveryExecutionResult:
    """Outcome of executing recovery on a claimed checkout attempt."""

    attempt_id: uuid.UUID
    outcome: RecoveryOutcome
    subscription_id: str | None = None
    recovery_attempts: int = 0
    next_recovery_retry_at: datetime | None = None
    error_message: str | None = None
    reconciliation_id: uuid.UUID | None = None


# =============================================================================
# Bounded Backoff & Jitter Helpers
# =============================================================================


def compute_next_recovery_retry(
    attempt_count: int,
    now: datetime,
    apply_jitter: bool = True,
    jitter_fn: Callable[[float], float] | None = None,
) -> datetime:
    """Compute the next recovery retry timestamp according to ADR-006 Section 5.3.

    Backoff intervals:
        attempt 1: 60s
        attempt 2: 300s
        attempt 3: 900s
        attempt 4: 1800s
        attempt 5+: 3600s

    Args:
        attempt_count: The 1-based attempt number that just completed.
        now: Current UTC datetime.
        apply_jitter: If True, applies +/- 15% randomized or injected jitter.
        jitter_fn: Optional callable taking base interval float and returning jittered float.

    Returns:
        Next retry UTC datetime.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1-based indexing into schedule; clamp to last entry (3600s)
    idx = max(0, min(attempt_count - 1, len(RECOVERY_BACKOFF_SCHEDULE) - 1))
    base_seconds = float(RECOVERY_BACKOFF_SCHEDULE[idx])

    if apply_jitter:
        if jitter_fn is not None:
            delay_seconds = jitter_fn(base_seconds)
        else:
            jitter_factor = random.uniform(-RECOVERY_JITTER_RATIO, RECOVERY_JITTER_RATIO)
            delay_seconds = base_seconds * (1.0 + jitter_factor)
    else:
        delay_seconds = base_seconds

    delay_seconds = max(1.0, delay_seconds)
    return now + timedelta(seconds=delay_seconds)


# =============================================================================
# Atomic Claim Operations
# =============================================================================


async def claim_next_recovery_attempt(
    db: AsyncSession,
    worker_id: str,
    lease_duration_seconds: int = DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    max_attempts: int = MAX_RECOVERY_ATTEMPTS,
    provider: str = "razorpay",
    environment: str | None = None,
    merchant_account_id: str | None = None,
    now: datetime | None = None,
) -> ClaimedRecoveryAttempt | None:
    """Atomically claim the single highest-priority eligible recovery attempt.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent lock contention across workers.
    Enforces NULL-safe ADR-006 eligibility predicate and commits the lease immediately.
    """
    batch = await claim_recovery_batch(
        db=db,
        worker_id=worker_id,
        limit=1,
        lease_duration_seconds=lease_duration_seconds,
        max_attempts=max_attempts,
        provider=provider,
        environment=environment,
        merchant_account_id=merchant_account_id,
        now=now,
    )
    return batch[0] if batch else None


async def claim_recovery_batch(
    db: AsyncSession,
    worker_id: str,
    limit: int = 10,
    lease_duration_seconds: int = DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    max_attempts: int = MAX_RECOVERY_ATTEMPTS,
    provider: str = "razorpay",
    environment: str | None = None,
    merchant_account_id: str | None = None,
    now: datetime | None = None,
) -> list[ClaimedRecoveryAttempt]:
    """Atomically claim a batch of eligible recovery attempts.

    Guarantees:
    1. Null-Safe Eligibility:
       status = 'recovery_required'
       AND next_recovery_retry_at IS NOT NULL
       AND next_recovery_retry_at <= :now
       AND (recovery_claim_expires_at IS NULL OR recovery_claim_expires_at <= :now)
       AND recovery_attempts < :max_attempts
    2. Bounded Leased Claiming:
       Stamps unpredictable fresh UUID recovery_claim_id and sets recovery_claim_expires_at.
    3. Immediate Commit:
       Commits claim transaction immediately so row locks are released before gateway dispatch.
    4. Tenant & Environment Scoping:
       Respects provider, environment, and merchant_account_id.
    """
    if limit < 1:
        return []

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    _, is_prod = validate_deployment_environment(getattr(settings, "environment", None))
    required_env = resolve_required_razorpay_environment(getattr(settings, "environment", None))
    if environment is not None:
        clean_env = environment.strip().lower() if isinstance(environment, str) else ""
        if clean_env not in ALLOWED_RAZORPAY_ENVIRONMENTS:
            raise ValueError(
                f"Invalid Razorpay recovery environment '{environment}'. Must be 'test' or 'live'."
            )
        if clean_env != required_env:
            raise ValueError(
                f"{'Production' if is_prod else 'Non-production'} billing recovery strictly requires "
                f"environment='{required_env}', got '{clean_env}'."
            )
        target_env = clean_env
    else:
        target_env = required_env

    target_merchant: str | None = None
    cfg_merchant = getattr(settings, "razorpay_merchant_account_id", None)
    is_cfg_prohibited = not cfg_merchant or cfg_merchant.strip().lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES

    if merchant_account_id is not None:
        clean_merchant = merchant_account_id.strip() if isinstance(merchant_account_id, str) else ""
        if not clean_merchant:
            raise ValueError("Razorpay merchant_account_id cannot be an empty string.")
        if is_prod and clean_merchant.lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES:
            raise ValueError(
                f"Production billing recovery rejects prohibited merchant account '{merchant_account_id}'."
            )
        if not is_cfg_prohibited and clean_merchant != cfg_merchant.strip():
            raise ValueError(
                f"Billing recovery strictly requires canonical merchant account '{cfg_merchant.strip()}', "
                f"got '{clean_merchant}'."
            )
        target_merchant = clean_merchant
    else:
        if is_prod:
            if is_cfg_prohibited:
                raise ValueError("Production billing recovery requires an explicit canonical live merchant account.")
            target_merchant = cfg_merchant.strip()
        else:
            if not is_cfg_prohibited:
                target_merchant = cfg_merchant.strip()

    stmt = (
        select(BillingCheckoutAttempt)
        .where(
            BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
            BillingCheckoutAttempt.provider == provider,
            BillingCheckoutAttempt.environment == target_env,
            BillingCheckoutAttempt.next_recovery_retry_at.is_not(None),
            BillingCheckoutAttempt.next_recovery_retry_at <= now_utc,
            or_(
                BillingCheckoutAttempt.recovery_claim_expires_at.is_(None),
                BillingCheckoutAttempt.recovery_claim_expires_at <= now_utc,
            ),
            BillingCheckoutAttempt.recovery_attempts < max_attempts,
        )
    )

    if target_merchant is not None:
        stmt = stmt.where(BillingCheckoutAttempt.merchant_account_id == target_merchant)

    # Order by earliest scheduled retry first, then oldest created attempt
    stmt = (
        stmt.order_by(
            BillingCheckoutAttempt.next_recovery_retry_at.asc(),
            BillingCheckoutAttempt.created_at.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(stmt)
    attempts = list(result.scalars().all())

    if not attempts:
        return []

    claimed_attempts: list[ClaimedRecoveryAttempt] = []
    expires_at = now_utc + timedelta(seconds=lease_duration_seconds)
    has_mutations = False

    for att in attempts:
        payload_copy = dict(att.request_payload or {})
        expired_lease_count = int(payload_copy.get("expired_lease_count", 0))

        if att.recovery_claim_expires_at is not None and att.recovery_claim_expires_at <= now_utc:
            expired_lease_count += 1
            payload_copy["expired_lease_count"] = expired_lease_count

        if expired_lease_count >= MAX_EXPIRED_LEASE_RECOVERIES:
            logger.error(
                "Attempt %s has exceeded maximum expired lease recoveries (%d). Quarantining.",
                att.id,
                expired_lease_count,
            )
            att.next_recovery_retry_at = None
            att.recovery_claim_id = None
            att.recovery_claim_expires_at = None
            att.error_message = "repeated_worker_lease_expiry_or_crashes"
            att.request_payload = payload_copy
            flag_modified(att, "request_payload")
            await stage_or_update_payment_reconciliation(
                db=db,
                case_key=f"recovery_worker_crashes:{att.id}",
                reason="repeated_worker_crash_or_lease_expiry",
                provider=att.provider,
                merchant_account_id=att.merchant_account_id,
                environment=att.environment,
                organization_id=att.organization_id,
                diagnostic_data={
                    "attempt_id": str(att.id),
                    "expired_lease_count": expired_lease_count,
                    "quarantined_at": now_utc.isoformat(),
                },
                now=now_utc,
            )
            has_mutations = True
            continue

        fresh_claim_id = uuid.uuid4()
        att.recovery_claim_id = fresh_claim_id
        att.recovery_claim_expires_at = expires_at

        # Mirror in request_payload for debugging and payload audit compatibility
        payload_copy["recovery_claim_id"] = str(fresh_claim_id)
        payload_copy["claimed_by_worker"] = worker_id
        payload_copy["claimed_at"] = now_utc.isoformat()
        att.request_payload = payload_copy
        flag_modified(att, "request_payload")
        has_mutations = True

        claimed_attempts.append(
            ClaimedRecoveryAttempt(
                attempt_id=att.id,
                organization_id=att.organization_id,
                provider=att.provider,
                merchant_account_id=att.merchant_account_id,
                environment=att.environment,
                status=att.status,
                recovery_claim_id=fresh_claim_id,
                recovery_claim_expires_at=expires_at,
                recovery_attempts=att.recovery_attempts or 0,
                created_at=att.created_at,
                request_payload=payload_copy,
                provider_customer_id=att.provider_customer_id,
                provider_subscription_id=att.provider_subscription_id,
                worker_id=worker_id,
            )
        )

    # Commit claim transaction to disk, releasing row locks
    if has_mutations:
        await db.commit()
        for att in attempts:
            await db.refresh(att)

    logger.info(
        "Worker %s successfully claimed %d recovery attempt(s) (lease_expires_at=%s)",
        worker_id,
        len(claimed_attempts),
        expires_at.isoformat(),
    )

    return claimed_attempts


# =============================================================================
# Gateway Investigation & Outcome Execution
# =============================================================================


async def execute_claimed_recovery_attempt(
    session_factory: Callable[[], AsyncSession],
    claimed: ClaimedRecoveryAttempt,
    client: RazorpayClient | None = None,
    max_attempts: int = MAX_RECOVERY_ATTEMPTS,
    now_fn: Callable[[], datetime] | None = None,
    apply_jitter: bool = True,
    jitter_fn: Callable[[float], float] | None = None,
    known_subscription_id: str | None = None,
    definitive_nonexistence_evidence: str | None = None,
    expected_environment: str | None = None,
    expected_merchant_id: str | None = None,
) -> RecoveryExecutionResult:
    """Execute recovery on a claimed checkout attempt with complete transaction safety.

    Guarantees:
    1. Zero DB Locks During Remote Gateway Calls:
       External inquiries execute with zero open transactions or row locks held.
    2. Provider-Neutral & Environment Scoping:
       Rejects environment/merchant mismatches fail-closed.
    3. Defensible Evidence Requirement:
       Marks 'failed' only when explicit definitive nonexistence evidence is provided.
    4. Stale-Worker Fencing:
       Validates claim ownership before committing mutations. If superseded, aborts and rolls back.
    5. Bounded Retries & Permanent Quarantine:
       Inconclusive searches schedule backoff; reaching max_attempts stages PaymentReconciliation
       and sets next_recovery_retry_at = NULL.
    """
    now_utc = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    # 1. Environment & Scope Validations
    _, is_prod = validate_deployment_environment(getattr(settings, "environment", None))
    required_env = resolve_required_razorpay_environment(getattr(settings, "environment", None))
    if claimed.environment != required_env:
        logger.error(
            "Environment safety violation: attempt %s has environment '%s', but deployment requires '%s'. Quarantining.",
            claimed.attempt_id,
            claimed.environment,
            required_env,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message=f"environment_safety_violation_{claimed.environment}_ne_{required_env}",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    if expected_environment and claimed.environment != expected_environment:
        logger.error(
            "Environment mismatch on attempt %s: expected '%s', got '%s'",
            claimed.attempt_id,
            expected_environment,
            claimed.environment,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message=f"environment_mismatch_{claimed.environment}_ne_{expected_environment}",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    cfg_merchant = getattr(settings, "razorpay_merchant_account_id", None)
    is_cfg_prohibited = not cfg_merchant or cfg_merchant.strip().lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES

    if is_prod:
        if is_cfg_prohibited or claimed.merchant_account_id.lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES:
            logger.error("Production recovery attempt %s rejected due to prohibited or unconfigured merchant.", claimed.attempt_id)
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message="production_prohibited_or_unconfigured_merchant",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )
        if claimed.merchant_account_id != cfg_merchant.strip():
            logger.error(
                "Merchant account mismatch on attempt %s: attempt has '%s', but canonical is '%s'",
                claimed.attempt_id,
                claimed.merchant_account_id,
                cfg_merchant.strip(),
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message=f"merchant_account_mismatch_{claimed.merchant_account_id}_ne_{cfg_merchant.strip()}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )
    else:
        if not is_cfg_prohibited and claimed.merchant_account_id != cfg_merchant.strip():
            logger.error(
                "Merchant account mismatch on attempt %s: attempt has '%s', but canonical is '%s'",
                claimed.attempt_id,
                claimed.merchant_account_id,
                cfg_merchant.strip(),
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message=f"merchant_account_mismatch_{claimed.merchant_account_id}_ne_{cfg_merchant.strip()}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

    if expected_merchant_id and claimed.merchant_account_id != expected_merchant_id:
        logger.error(
            "Merchant account mismatch on attempt %s: expected '%s', got '%s'",
            claimed.attempt_id,
            expected_merchant_id,
            claimed.merchant_account_id,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message="merchant_account_mismatch",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    # 2. Defensible Nonexistence Proof Handling
    if definitive_nonexistence_evidence and str(definitive_nonexistence_evidence).strip():
        evidence_str = str(definitive_nonexistence_evidence).strip()
        logger.info(
            "Reconciling attempt %s as definitively failed with proof: %s",
            claimed.attempt_id,
            evidence_str,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.FAILED_DEFINITIVE,
            error_message=evidence_str,
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    # 3. Gateway Inquiry (Strictly Outside DB Transactions)
    try:
        rzp_client = client or get_razorpay_client(
            expected_merchant_account_id=canonical_merchant,
            expected_environment=canonical_env,
        )
    except Exception as exc:
        logger.error(
            "Failed to resolve provider client for attempt %s: %s",
            claimed.attempt_id,
            exc,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message=f"provider_client_configuration_error: {sanitize_billing_error_message(exc)}",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    client_merchant = getattr(rzp_client, "merchant_account_id", None)
    if client_merchant and str(client_merchant).strip() != canonical_merchant:
        logger.error(
            "Injected client merchant '%s' does not match canonical merchant '%s' for attempt %s",
            client_merchant,
            canonical_merchant,
            claimed.attempt_id,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message=f"client_merchant_mismatch_{client_merchant}_ne_{canonical_merchant}",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    client_env = getattr(rzp_client, "environment", None)
    if client_env and str(client_env).strip().lower() != canonical_env:
        logger.error(
            "Injected client environment '%s' does not match canonical environment '%s' for attempt %s",
            client_env,
            canonical_env,
            claimed.attempt_id,
        )
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.QUARANTINED,
            error_message=f"client_environment_mismatch_{client_env}_ne_{canonical_env}",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )
    expected_plan_id = (
        claimed.request_payload.get("plan_id")
        or getattr(settings, "razorpay_pro_plan_id", "")
        or ""
    ).strip()
    expected_cust_id = (claimed.provider_customer_id or "").strip()

    candidate_sub_data: dict[str, Any] | None = None
    target_sub_id: str | None = None

    if known_subscription_id and known_subscription_id.strip():
        # Case A: Explicit Known Subscription ID
        target_sub_id = known_subscription_id.strip()
        try:
            candidate_sub_data = await rzp_client.get_subscription(target_sub_id)
        except (RazorpayServer5xxError, RazorpayRateLimitError, RazorpayNetworkError) as exc:
            logger.warning(
                "Transient gateway failure verifying known subscription %s for attempt %s: %s",
                target_sub_id,
                claimed.attempt_id,
                exc,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.TRANSIENT_BACKOFF,
                error_message=f"transient_gateway_failure: {sanitize_billing_error_message(exc)}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )
        except (RazorpayAPIError, RazorpayAuthError, RazorpayConfigurationError) as exc:
            logger.error(
                "Permanent/client gateway error verifying known subscription %s for attempt %s: %s",
                target_sub_id,
                claimed.attempt_id,
                exc,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message=f"gateway_error: {sanitize_billing_error_message(exc)}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error verifying known subscription %s for attempt %s: %s",
                target_sub_id,
                claimed.attempt_id,
                exc,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message=f"unexpected_inquiry_error: {sanitize_billing_error_message(exc)}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        if not isinstance(candidate_sub_data, dict) or candidate_sub_data.get("id") != target_sub_id:
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message="invalid_subscription_payload_from_gateway",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        # Validate remote subscription ownership & plan
        remote_plan = (candidate_sub_data.get("plan_id") or "").strip()
        if expected_plan_id and remote_plan and remote_plan != expected_plan_id:
            logger.error(
                "Remote subscription %s plan mismatch: expected '%s', got '%s'",
                target_sub_id,
                expected_plan_id,
                remote_plan,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message=f"plan_mismatch: remote={remote_plan}, expected={expected_plan_id}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        remote_cust = (candidate_sub_data.get("customer_id") or "").strip()
        if expected_cust_id and remote_cust and remote_cust != expected_cust_id:
            logger.error(
                "Remote subscription %s customer mismatch: expected '%s', got '%s'",
                target_sub_id,
                expected_cust_id,
                remote_cust,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message=f"customer_mismatch: remote={remote_cust}, expected={expected_cust_id}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        sub_notes = candidate_sub_data.get("notes") or {}
        if isinstance(sub_notes, dict):
            noted_org = sub_notes.get("organization_id")
            if noted_org and str(noted_org).strip() != str(claimed.organization_id):
                return await _record_recovery_outcome(
                    session_factory=session_factory,
                    claimed=claimed,
                    outcome=RecoveryOutcome.QUARANTINED,
                    error_message=f"organization_mismatch: remote_noted_org={noted_org}",
                    now_utc=now_utc,
                    max_attempts=max_attempts,
                    apply_jitter=apply_jitter,
                    jitter_fn=jitter_fn,
                )
            noted_att = sub_notes.get("attempt_id")
            if noted_att and str(noted_att).strip() != str(claimed.attempt_id):
                return await _record_recovery_outcome(
                    session_factory=session_factory,
                    claimed=claimed,
                    outcome=RecoveryOutcome.QUARANTINED,
                    error_message=f"attempt_mismatch: remote_noted_attempt={noted_att}",
                    now_utc=now_utc,
                    max_attempts=max_attempts,
                    apply_jitter=apply_jitter,
                    jitter_fn=jitter_fn,
                )

    else:
        # Case B: Unknown Subscription ID -> Search Gateway via Bounded Pagination
        if claimed.created_at is None:
            logger.error("Attempt %s lacks created_at timestamp. Failing closed.", claimed.attempt_id)
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.QUARANTINED,
                error_message="missing_created_at_timestamp",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        attempt_created_at = claimed.created_at
        if attempt_created_at.tzinfo is None:
            attempt_created_at = attempt_created_at.replace(tzinfo=timezone.utc)
        attempt_ts = int(attempt_created_at.timestamp())
        from_time = max(0, attempt_ts - RECOVERY_CLOCK_SKEW_SECONDS)
        window_lower = from_time
        window_upper = attempt_ts + int(CHECKOUT_LEASE_SECONDS) + RECOVERY_CLOCK_SKEW_SECONDS

        matching_candidates: list[dict[str, Any]] = []
        search_exhausted = False
        search_incomplete_reason: str | None = None
        is_transient_failure = False

        for page_idx in range(RECOVERY_MAX_PAGES):
            skip = page_idx * RECOVERY_PAGE_SIZE
            try:
                list_res = await rzp_client.list_subscriptions(
                    plan_id=expected_plan_id,
                    from_time=from_time,
                    count=RECOVERY_PAGE_SIZE,
                    skip=skip,
                )
            except (RazorpayServer5xxError, RazorpayRateLimitError, RazorpayNetworkError) as exc:
                logger.warning(
                    "Gateway transient outage listing subscriptions (page %d) for attempt %s: %s",
                    page_idx + 1,
                    claimed.attempt_id,
                    exc,
                )
                search_incomplete_reason = f"gateway_transient_error: {sanitize_billing_error_message(exc)}"
                is_transient_failure = True
                break
            except Exception as exc:
                logger.error(
                    "Error listing subscriptions (page %d) for attempt %s: %s",
                    page_idx + 1,
                    claimed.attempt_id,
                    exc,
                )
                search_incomplete_reason = f"gateway_error_page_{page_idx + 1}: {sanitize_billing_error_message(exc)}"
                break

            if not isinstance(list_res, dict) or not isinstance(list_res.get("items"), list):
                search_incomplete_reason = f"malformed_gateway_response_page_{page_idx + 1}"
                break

            page_items = list_res.get("items", [])

            for item in page_items:
                if not isinstance(item, dict):
                    continue
                item_notes = item.get("notes") or {}
                if not isinstance(item_notes, dict):
                    item_notes = {}

                raw_item_attempt_id = item_notes.get("attempt_id")
                item_attempt_id = (
                    str(raw_item_attempt_id).strip() if raw_item_attempt_id is not None else ""
                )

                if item_attempt_id:
                    # Strict attempt ID match
                    if item_attempt_id != str(claimed.attempt_id):
                        continue
                    remote_plan = (item.get("plan_id") or "").strip()
                    remote_cust = (item.get("customer_id") or "").strip()
                    remote_org = str(item_notes.get("organization_id", "")).strip()

                    if expected_plan_id and remote_plan and remote_plan != expected_plan_id:
                        continue
                    if expected_cust_id and remote_cust and remote_cust != expected_cust_id:
                        continue
                    if remote_org and remote_org != str(claimed.organization_id):
                        continue

                    matching_candidates.append(item)
                else:
                    # Fallback candidate match
                    remote_org = str(item_notes.get("organization_id", "")).strip()
                    if not remote_org or remote_org != str(claimed.organization_id):
                        continue
                    remote_cust = (item.get("customer_id") or "").strip()
                    if not remote_cust or not expected_cust_id or remote_cust != expected_cust_id:
                        continue
                    remote_plan = (item.get("plan_id") or "").strip()
                    if not remote_plan or not expected_plan_id or remote_plan != expected_plan_id:
                        continue

                    raw_created_at = item.get("created_at")
                    if raw_created_at is None:
                        continue
                    try:
                        remote_created_at_ts = int(raw_created_at)
                    except (ValueError, TypeError):
                        continue

                    if not (window_lower <= remote_created_at_ts <= window_upper):
                        continue

                    matching_candidates.append(item)

            if len(page_items) < RECOVERY_PAGE_SIZE:
                search_exhausted = True
                break
        else:
            search_exhausted = False
            search_incomplete_reason = f"page_ceiling_reached_{RECOVERY_MAX_PAGES}_pages"

        if is_transient_failure:
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.TRANSIENT_BACKOFF,
                error_message=search_incomplete_reason or "gateway_transient_error",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        if not search_exhausted or search_incomplete_reason:
            logger.warning(
                "Subscription search for attempt %s was incomplete (%s). Scheduling retry.",
                claimed.attempt_id,
                search_incomplete_reason,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message=f"recovery_search_incomplete_{search_incomplete_reason}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        if len(matching_candidates) == 0:
            logger.info(
                "Subscription search for attempt %s returned 0 matching candidates. Scheduling retry.",
                claimed.attempt_id,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message="recovery_lookup_inconclusive_no_candidates",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
            )

        if len(matching_candidates) > 1:
            candidate_ids = [str(c.get("id")) for c in matching_candidates if c.get("id")]
            logger.error(
                "Multiple (%d) candidate subscriptions found on Razorpay for attempt %s: %s",
                len(matching_candidates),
                claimed.attempt_id,
                candidate_ids,
            )
            return await _record_recovery_outcome(
                session_factory=session_factory,
                claimed=claimed,
                outcome=RecoveryOutcome.RETRY_SCHEDULED,
                error_message=f"multiple_candidates_found: {','.join(candidate_ids)}",
                now_utc=now_utc,
                max_attempts=max_attempts,
                apply_jitter=apply_jitter,
                jitter_fn=jitter_fn,
                candidate_ids=candidate_ids,
            )

        candidate_sub_data = matching_candidates[0]
        target_sub_id = str(candidate_sub_data.get("id", "")).strip()

    if not target_sub_id:
        return await _record_recovery_outcome(
            session_factory=session_factory,
            claimed=claimed,
            outcome=RecoveryOutcome.RETRY_SCHEDULED,
            error_message="missing_target_subscription_id_after_search",
            now_utc=now_utc,
            max_attempts=max_attempts,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )

    # 4. Successful candidate verified: Persist RECOVERED outcome under write lock
    short_url = candidate_sub_data.get("short_url") if isinstance(candidate_sub_data, dict) else None
    expire_by_ts = candidate_sub_data.get("expire_by") if isinstance(candidate_sub_data, dict) else None
    session_expires_at = None
    if expire_by_ts is not None:
        try:
            session_expires_at = datetime.fromtimestamp(int(expire_by_ts), tz=timezone.utc)
        except (ValueError, TypeError):
            session_expires_at = None
    if session_expires_at is None:
        session_expires_at = now_utc + timedelta(days=DEFAULT_SUBSCRIPTION_EXPIRY_DAYS)

    return await _record_recovery_outcome(
        session_factory=session_factory,
        claimed=claimed,
        outcome=RecoveryOutcome.RECOVERED,
        target_sub_id=target_sub_id,
        short_url=short_url,
        session_expires_at=session_expires_at,
        now_utc=now_utc,
        max_attempts=max_attempts,
        apply_jitter=apply_jitter,
        jitter_fn=jitter_fn,
    )


# =============================================================================
# Stale Worker Fencing & Outcome Persistence
# =============================================================================


async def _record_recovery_outcome(
    session_factory: Callable[[], AsyncSession],
    claimed: ClaimedRecoveryAttempt,
    outcome: RecoveryOutcome,
    target_sub_id: str | None = None,
    short_url: str | None = None,
    session_expires_at: datetime | None = None,
    error_message: str | None = None,
    candidate_ids: list[str] | None = None,
    now_utc: datetime | None = None,
    max_attempts: int = MAX_RECOVERY_ATTEMPTS,
    apply_jitter: bool = True,
    jitter_fn: Callable[[float], float] | None = None,
) -> RecoveryExecutionResult:
    """Record recovery outcome in a short database transaction with fencing token verification."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    sanitized_error = sanitize_billing_error_message(error_message) if error_message else None
    rec_id: uuid.UUID | None = None

    async with session_factory() as db:
        stmt = (
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == claimed.attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res = await db.execute(stmt)
        attempt = res.scalar_one_or_none()

        if attempt is None:
            raise RazorpayRecoveryError(f"Checkout attempt {claimed.attempt_id} disappeared during recovery.")

        # Stale-Worker Fencing Check:
        current_claim_col = attempt.recovery_claim_id
        current_claim_payload = None
        if isinstance(attempt.request_payload, dict):
            raw_claim = attempt.request_payload.get("recovery_claim_id")
            if raw_claim:
                try:
                    current_claim_payload = uuid.UUID(str(raw_claim))
                except ValueError:
                    current_claim_payload = raw_claim

        claimed_id = claimed.recovery_claim_id
        is_valid_owner = (
            (current_claim_col == claimed_id or str(current_claim_col) == str(claimed_id))
            or (current_claim_col is None and str(current_claim_payload) == str(claimed_id))
        )

        if not is_valid_owner or attempt.status != CheckoutAttemptStatus.recovery_required:
            logger.warning(
                "Stale recovery worker detected on attempt %s: claim %s was superseded by col=%s payload=%s (status=%s)",
                claimed.attempt_id,
                claimed_id,
                current_claim_col,
                current_claim_payload,
                attempt.status,
            )
            if target_sub_id:
                # Stage superseded reconciliation case so the candidate subscription is not orphaned
                case_key = f"recovery_superseded:{claimed.attempt_id}:{target_sub_id}"
                diag_data = {
                    "attempt_id": str(claimed.attempt_id),
                    "recovered_subscription_id": target_sub_id,
                    "worker_claim_id": str(claimed_id),
                    "superseded_by_col": str(current_claim_col) if current_claim_col else None,
                    "superseded_by_payload": str(current_claim_payload) if current_claim_payload else None,
                    "detected_at": now.isoformat(),
                }
                rec = await stage_or_update_payment_reconciliation(
                    db=db,
                    case_key=case_key,
                    reason="superseded_recovery_worker",
                    provider=claimed.provider,
                    merchant_account_id=claimed.merchant_account_id,
                    environment=claimed.environment,
                    organization_id=claimed.organization_id,
                    diagnostic_data=diag_data,
                    now=now,
                )
                rec.diagnostic_payload = {
                    **diag_data,
                    **rec.diagnostic_payload,
                }
                rec_id = rec.id
                await db.commit()

            raise StaleRecoveryClaimError(
                f"Recovery operation for attempt {claimed.attempt_id} was superseded by another worker."
            )

        # Valid worker ownership: apply intended state transition
        if outcome == RecoveryOutcome.RECOVERED:
            attempt.status = CheckoutAttemptStatus.open
            attempt.provider_subscription_id = target_sub_id
            attempt.provider_session_id = target_sub_id
            attempt.checkout_url = short_url
            attempt.session_expires_at = session_expires_at
            attempt.next_recovery_retry_at = None
            attempt.recovery_claim_id = None
            attempt.recovery_claim_expires_at = None
            attempt.error_message = None

            payload_final = dict(attempt.request_payload or {})
            payload_final["recovery_evidence"] = {
                "recovered_at": now.isoformat(),
                "subscription_id": target_sub_id,
                "claim_id": str(claimed_id),
            }
            attempt.request_payload = payload_final
            flag_modified(attempt, "request_payload")

            await db.commit()
            return RecoveryExecutionResult(
                attempt_id=claimed.attempt_id,
                outcome=RecoveryOutcome.RECOVERED,
                subscription_id=target_sub_id,
                recovery_attempts=claimed.recovery_attempts,
                next_recovery_retry_at=None,
                error_message=None,
            )

        elif outcome == RecoveryOutcome.FAILED_DEFINITIVE:
            case_key = f"recovery_definitive_failure:{claimed.attempt_id}"
            rec = await stage_or_update_payment_reconciliation(
                db=db,
                case_key=case_key,
                reason="definitive_nonexistence_evidence",
                provider=claimed.provider,
                merchant_account_id=claimed.merchant_account_id,
                environment=claimed.environment,
                organization_id=claimed.organization_id,
                diagnostic_data={
                    "attempt_id": str(claimed.attempt_id),
                    "evidence": sanitized_error,
                    "reconciled_at": now.isoformat(),
                },
                now=now,
            )
            rec.status = ReconciliationStatus.resolved_ignored
            rec_id = rec.id
            attempt.status = CheckoutAttemptStatus.failed
            attempt.error_message = sanitized_error
            attempt.next_recovery_retry_at = None
            attempt.recovery_claim_id = None
            attempt.recovery_claim_expires_at = None

            await db.commit()
            return RecoveryExecutionResult(
                attempt_id=claimed.attempt_id,
                outcome=RecoveryOutcome.FAILED_DEFINITIVE,
                subscription_id=None,
                recovery_attempts=claimed.recovery_attempts,
                next_recovery_retry_at=None,
                error_message=sanitized_error,
                reconciliation_id=rec_id,
            )

        elif outcome == RecoveryOutcome.TRANSIENT_BACKOFF:
            payload_transient = dict(attempt.request_payload or {})
            transient_count = int(payload_transient.get("transient_failures", 0)) + 1
            payload_transient["transient_failures"] = transient_count
            attempt.request_payload = payload_transient
            flag_modified(attempt, "request_payload")

            if transient_count >= MAX_TRANSIENT_RECOVERIES:
                # Quota of transient retries exceeded; quarantine to prevent infinite loop
                case_key = f"recovery_transient_exhausted:{claimed.attempt_id}"
                rec = await stage_or_update_payment_reconciliation(
                    db=db,
                    case_key=case_key,
                    reason="transient_retry_budget_exhausted",
                    provider=claimed.provider,
                    merchant_account_id=claimed.merchant_account_id,
                    environment=claimed.environment,
                    organization_id=claimed.organization_id,
                    diagnostic_data={
                        "attempt_id": str(claimed.attempt_id),
                        "transient_failures": transient_count,
                        "last_error": sanitized_error,
                        "quarantined_at": now.isoformat(),
                    },
                    now=now,
                )
                rec_id = rec.id
                attempt.next_recovery_retry_at = None
                attempt.recovery_claim_id = None
                attempt.recovery_claim_expires_at = None
                attempt.error_message = f"transient_retry_budget_exhausted: {sanitized_error}"

                await db.commit()
                return RecoveryExecutionResult(
                    attempt_id=claimed.attempt_id,
                    outcome=RecoveryOutcome.QUARANTINED,
                    subscription_id=None,
                    recovery_attempts=claimed.recovery_attempts,
                    next_recovery_retry_at=None,
                    error_message=f"transient_retry_budget_exhausted: {sanitized_error}",
                    reconciliation_id=rec_id,
                )

            # Exponential backoff on transient failures
            base_transient = min(1800.0, float(DEFAULT_TRANSIENT_BACKOFF_SECONDS) * (1.5 ** (transient_count - 1)))
            if apply_jitter:
                if jitter_fn is not None:
                    delay_transient = jitter_fn(base_transient)
                else:
                    jitter_factor = random.uniform(-RECOVERY_JITTER_RATIO, RECOVERY_JITTER_RATIO)
                    delay_transient = base_transient * (1.0 + jitter_factor)
            else:
                delay_transient = base_transient

            next_retry = now + timedelta(seconds=max(1.0, delay_transient))
            attempt.next_recovery_retry_at = next_retry
            attempt.recovery_claim_id = None
            attempt.recovery_claim_expires_at = None
            attempt.error_message = sanitized_error

            await db.commit()
            return RecoveryExecutionResult(
                attempt_id=claimed.attempt_id,
                outcome=RecoveryOutcome.TRANSIENT_BACKOFF,
                subscription_id=None,
                recovery_attempts=claimed.recovery_attempts,
                next_recovery_retry_at=next_retry,
                error_message=sanitized_error,
            )

        # Check for budget exhaustion on retry_scheduled or quarantined
        new_attempts = claimed.recovery_attempts + 1

        if candidate_ids and len(candidate_ids) > 1:
            case_key = f"recovery_multiple_candidates:{claimed.attempt_id}"
            rec = await stage_or_update_payment_reconciliation(
                db=db,
                case_key=case_key,
                reason="multiple_recovery_candidates_found",
                provider=claimed.provider,
                merchant_account_id=claimed.merchant_account_id,
                environment=claimed.environment,
                organization_id=claimed.organization_id,
                diagnostic_data={
                    "attempt_id": str(claimed.attempt_id),
                    "candidate_subscription_ids": candidate_ids,
                    "detected_at": now.isoformat(),
                },
                now=now,
            )
            rec.status = ReconciliationStatus.pending_review
            rec_id = rec.id

        if outcome == RecoveryOutcome.QUARANTINED or new_attempts >= max_attempts:
            case_key = (
                f"recovery_quarantined:{claimed.attempt_id}"
                if outcome == RecoveryOutcome.QUARANTINED
                else f"recovery_budget_exhausted:{claimed.attempt_id}"
            )
            reason_str = (
                "recovery_safety_quarantined"
                if outcome == RecoveryOutcome.QUARANTINED
                else "recovery_budget_exhausted"
            )
            rec = await stage_or_update_payment_reconciliation(
                db=db,
                case_key=case_key,
                reason=reason_str,
                provider=claimed.provider,
                merchant_account_id=claimed.merchant_account_id,
                environment=claimed.environment,
                organization_id=claimed.organization_id,
                diagnostic_data={
                    "attempt_id": str(claimed.attempt_id),
                    "attempts_count": new_attempts,
                    "last_error": sanitized_error,
                    "quarantined_at": now.isoformat(),
                },
                now=now,
            )
            rec.status = ReconciliationStatus.pending_review
            rec_id = rec.id
            attempt.recovery_attempts = new_attempts
            attempt.next_recovery_retry_at = None  # EXCLUDED from future worker queries!
            attempt.recovery_claim_id = None
            attempt.recovery_claim_expires_at = None
            attempt.error_message = f"{reason_str}: {sanitized_error}"

            await db.commit()
            return RecoveryExecutionResult(
                attempt_id=claimed.attempt_id,
                outcome=RecoveryOutcome.QUARANTINED,
                subscription_id=None,
                recovery_attempts=new_attempts,
                next_recovery_retry_at=None,
                error_message=f"{reason_str}: {sanitized_error}",
                reconciliation_id=rec_id,
            )

        # Normal retry scheduled
        next_retry = compute_next_recovery_retry(
            attempt_count=new_attempts,
            now=now,
            apply_jitter=apply_jitter,
            jitter_fn=jitter_fn,
        )
        attempt.recovery_attempts = new_attempts
        attempt.next_recovery_retry_at = next_retry
        attempt.recovery_claim_id = None
        attempt.recovery_claim_expires_at = None
        attempt.error_message = sanitized_error

        await db.commit()
        return RecoveryExecutionResult(
            attempt_id=claimed.attempt_id,
            outcome=RecoveryOutcome.RETRY_SCHEDULED,
            subscription_id=None,
            recovery_attempts=new_attempts,
            next_recovery_retry_at=next_retry,
            error_message=sanitized_error,
            reconciliation_id=rec_id,
        )
