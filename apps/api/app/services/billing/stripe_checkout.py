"""Domain service for organization-scoped Stripe Checkout Sessions.

Guarantees:
1. Single Active Checkout Attempt: At most one in-flight or open checkout attempt
   per organization across 'creating', 'recovery_required', 'open', and 'payment_pending'.
2. Concurrency Safety: Serializes concurrent creation requests for the same tenant
   via parent organization row locking (SELECT ... FOR UPDATE) and partial unique index.
3. Durable Attempt State: Persists full request payload and attempt-scoped idempotency key
   before invoking external Stripe APIs.
4. Ambiguous Crash Recovery: Network timeouts and ambiguous outcomes transition to
   'recovery_required', retaining the tenant reservation without releasing partial uniqueness.
5. Transaction Boundary Safety: Never holds open database transactions or locks across
   external Stripe network calls.
6. Local Authority & Non-Misrepresentation: Checkout session creation never grants
   paid entitlements or modifies subscription tier. Local subscription authority is updated
   only upon verified webhook receipt.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import urllib.parse
import uuid

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.organization import Organization
from app.models.subscription import (
    CheckoutAttemptStatus,
    StripeCheckoutAttempt,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
    get_stripe_client,
    is_stripe_enabled,
)
from app.services.billing.stripe_customer import (
    get_or_create_stripe_customer,
    get_stripe_customer_id,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Domain Exceptions
# =============================================================================


class StripeCheckoutError(Exception):
    """Raised when a checkout session operation fails definitively."""


class ActiveSubscriptionExistsError(StripeCheckoutError):
    """Raised when an organization already possesses an active or trialing subscription."""


class CheckoutInProgressError(StripeCheckoutError):
    """Raised when an active checkout attempt is currently in-flight or payment is pending."""


class CheckoutRecoveryRequiredError(StripeCheckoutError):
    """Raised when an attempt experienced an ambiguous outcome requiring recovery replay."""


# =============================================================================
# Result Dataclass
# =============================================================================


@dataclass(frozen=True)
class StripeCheckoutSessionResult:
    """Immutable result of a successful Stripe Checkout Session creation or recovery."""

    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    stripe_session_id: str
    checkout_url: str
    session_expires_at: datetime | None
    status: CheckoutAttemptStatus


# =============================================================================
# Constants
# =============================================================================

IDEMPOTENCY_SAFE_REPLAY_CUTOFF_HOURS: int = 22
"""Conservative idempotency replay cutoff: 2 hours below Stripe's 24h minimum retention.

Stripe guarantees idempotency key retention for at least 24 hours. We use 22 hours as a
conservative buffer to avoid attempting a replay where the outcome is no longer retrievable.
Attempts older than this threshold must be blocked from replay and held for manual reconciliation.
"""

# =============================================================================
# Allowed State Transition Policy
# =============================================================================

# Maps each current status to the set of statuses it is permitted to transition into.
# Terminal states (complete, expired, failed) map to an empty set — no transitions allowed.
# This guard prevents stale checkout-service writes from overwriting webhook-advanced states.
_ALLOWED_TRANSITIONS: dict[CheckoutAttemptStatus, set[CheckoutAttemptStatus]] = {
    CheckoutAttemptStatus.creating: {
        CheckoutAttemptStatus.open,
        CheckoutAttemptStatus.recovery_required,
        CheckoutAttemptStatus.failed,
    },
    CheckoutAttemptStatus.open: {
        CheckoutAttemptStatus.complete,
        CheckoutAttemptStatus.payment_pending,
        CheckoutAttemptStatus.expired,
        CheckoutAttemptStatus.failed,
        CheckoutAttemptStatus.recovery_required,
    },
    CheckoutAttemptStatus.payment_pending: {
        CheckoutAttemptStatus.complete,
    },
    CheckoutAttemptStatus.recovery_required: {
        CheckoutAttemptStatus.open,
        CheckoutAttemptStatus.failed,
        CheckoutAttemptStatus.recovery_required,  # Allow updating error_message only
    },
    CheckoutAttemptStatus.complete: set(),   # Terminal: immutable
    CheckoutAttemptStatus.expired: set(),    # Terminal: immutable
    CheckoutAttemptStatus.failed: set(),     # Terminal: immutable
}


# =============================================================================
# URL & Parsing Helpers
# =============================================================================


def build_checkout_return_url(base_url: str, **params: str) -> str:
    """Safely append or merge query parameters into a return URL, preserving existing query and fragment.

    Args:
        base_url: Base return URL from configuration.
        **params: Query parameter key-value pairs to append/merge.

    Returns:
        Safe merged URL string preserving {CHECKOUT_SESSION_ID} placeholder unencoded.

    Raises:
        ValueError: If base_url is invalid, has no host, or uses an unsupported scheme.
    """
    if not base_url or not base_url.strip():
        raise ValueError("base_url must not be empty")

    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: '{parsed.scheme}'. Expected 'http' or 'https'.")
    if not parsed.netloc:
        raise ValueError("Invalid URL: missing host netloc")

    # Parse existing query parameters into a dictionary
    existing_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    param_dict = dict(existing_pairs)

    # Merge / overwrite with new params
    for k, v in params.items():
        param_dict[k] = str(v)

    # Encode query string, preserving Stripe template placeholder '{CHECKOUT_SESSION_ID}' unencoded
    new_query = urllib.parse.urlencode(param_dict, doseq=True, safe="{}")

    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        new_query,
        parsed.fragment,
    ))


def _validate_stripe_checkout_url(url: str) -> None:
    """Validate that a returned Stripe checkout URL meets security criteria."""
    if not url or not isinstance(url, str):
        raise StripeCheckoutError("Stripe checkout URL must be a non-empty string.")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise StripeCheckoutError(
            f"Stripe checkout URL has an invalid scheme '{parsed.scheme}'. Expected 'http' or 'https'."
        )
    if not parsed.netloc or not parsed.hostname or any(c.isspace() for c in parsed.netloc):
        raise StripeCheckoutError("Stripe checkout URL must contain a valid, non-empty host without whitespace.")


def _parse_stripe_timestamp(val: Any) -> datetime | None:
    """Convert a Stripe integer Unix timestamp or datetime into a UTC timezone-aware datetime."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    return None


def classify_stripe_error(exc: Exception) -> str:
    """Classify Stripe API or network errors into definitive vs ambiguous outcomes.

    Returns:
        "failed": Definitive permanent failure (e.g. 400 invalid request, auth failure).
                  The organization's attempt reservation can be safely released.
        "recovery_required": Ambiguous outcome (e.g. network timeout, connection reset, 5xx).
                             The attempt reservation is retained for idempotent replay.

    Note:
        stripe.IdempotencyError is checked BEFORE stripe.InvalidRequestError because
        IdempotencyError is a subclass of InvalidRequestError. An idempotency conflict
        must retain the reservation for reconciliation, not release it.
    """
    if isinstance(exc, stripe.AuthenticationError):
        return "failed"
    if isinstance(exc, stripe.PermissionError):
        return "failed"
    # IMPORTANT: Check IdempotencyError before InvalidRequestError — it is a subclass.
    # Idempotency conflicts are ambiguous and must retain the reservation.
    if isinstance(exc, stripe.IdempotencyError):
        return "recovery_required"
    if isinstance(exc, stripe.InvalidRequestError):
        # 400 Bad Request, 404 Not Found (e.g. missing price, customer deleted)
        return "failed"
    if isinstance(exc, (stripe.APIConnectionError, stripe.APIError)):
        return "recovery_required"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "recovery_required"
    # Fallback: treat any other unexpected exception as ambiguous to protect financial safety
    return "recovery_required"


# =============================================================================
# Database State Helpers
# =============================================================================


async def get_active_checkout_attempt(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> StripeCheckoutAttempt | None:
    """Retrieve the most recent active checkout attempt for an organization, if any.

    Active states include: 'creating', 'recovery_required', 'open', and 'payment_pending'.
    """
    stmt = (
        select(StripeCheckoutAttempt)
        .where(
            StripeCheckoutAttempt.organization_id == organization_id,
            StripeCheckoutAttempt.status.in_([
                CheckoutAttemptStatus.creating,
                CheckoutAttemptStatus.recovery_required,
                CheckoutAttemptStatus.open,
                CheckoutAttemptStatus.payment_pending,
            ]),
        )
        .order_by(StripeCheckoutAttempt.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _update_attempt_status(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    status: CheckoutAttemptStatus,
    stripe_session_id: str | None = None,
    checkout_url: str | None = None,
    session_expires_at: datetime | None = None,
    error_message: str | None = None,
    stripe_subscription_id: str | None = None,
) -> StripeCheckoutAttempt | None:
    """Update attempt state in a clean, isolated database transaction.

    Enforces the allowed state-transition policy. Illegal transitions are logged
    and silently blocked — the current attempt state is returned unchanged — to
    prevent stale checkout-service writes from overwriting webhook-advanced states.
    """
    if db.in_transaction():
        await db.commit()

    stmt = (
        select(StripeCheckoutAttempt)
        .where(StripeCheckoutAttempt.id == attempt_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    attempt = result.scalar_one_or_none()
    if not attempt:
        return None

    # Enforce allowed state-transition policy
    allowed = _ALLOWED_TRANSITIONS.get(attempt.status, set())
    if status != attempt.status and status not in allowed:
        logger.warning(
            "Blocked illegal attempt state transition %s → %s for attempt %s. "
            "Current state is preserved.",
            attempt.status,
            status,
            attempt_id,
        )
        await db.commit()
        return attempt

    attempt.status = status
    if stripe_session_id is not None:
        attempt.stripe_session_id = stripe_session_id
    if stripe_subscription_id is not None:
        attempt.stripe_subscription_id = stripe_subscription_id
    if checkout_url is not None:
        attempt.checkout_url = checkout_url
    if session_expires_at is not None:
        attempt.session_expires_at = session_expires_at
    if status == CheckoutAttemptStatus.open:
        attempt.error_message = None
    elif error_message is not None:
        attempt.error_message = error_message
    attempt.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(attempt)
    return attempt


# =============================================================================
# Stripe External API Dispatchers
# =============================================================================


def _call_stripe_create_session(
    request_payload: dict[str, Any],
    idempotency_key: str,
) -> Any:
    """Invoke the external Stripe Checkout Sessions creation API."""
    client = get_stripe_client()
    v1 = getattr(client, "v1", None)
    if v1 is not None and hasattr(v1, "checkout"):
        checkout = v1.checkout
    else:
        checkout = getattr(client, "checkout", None)
    if checkout is None:
        raise StripeConfigurationError("Stripe client does not provide a checkout service")
    sessions = getattr(checkout, "sessions", getattr(checkout, "session", None))
    if sessions is None or not hasattr(sessions, "create"):
        raise StripeConfigurationError("Stripe client does not provide a checkout sessions service")

    return sessions.create(
        params=request_payload,
        options={"idempotency_key": idempotency_key},
    )


def _retrieve_stripe_session(
    session_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Retrieve an actual Stripe Checkout Session status and subscription ID from Stripe.

    This is a synchronous helper that must be called OUTSIDE any open database
    transaction or organization row lock. The caller is responsible for committing
    any active transaction before invoking this function.

    Returns:
        (stripe_status, payment_status, stripe_subscription_id) on success.
        (None, None, None) on an ambiguous error (network failure, 5xx) — treat as
        recovery_required.

    Raises:
        StripeCheckoutError: On a definitive retrieval failure (e.g. auth error,
            permanent 4xx that is not idempotency-related).
    """
    try:
        client = get_stripe_client()
        v1 = getattr(client, "v1", None)
        if v1 is not None and hasattr(v1, "checkout"):
            checkout = v1.checkout
        else:
            checkout = getattr(client, "checkout", None)
        if checkout is None:
            raise StripeConfigurationError("Stripe client does not provide a checkout service")
        sessions = getattr(checkout, "sessions", getattr(checkout, "session", None))
        if sessions is None or not hasattr(sessions, "retrieve"):
            raise StripeConfigurationError("Stripe client does not provide session retrieval")

        session = sessions.retrieve(session_id)
        stripe_status = (
            getattr(session, "status", None)
            or (session.get("status") if isinstance(session, dict) else None)
        )
        payment_status = (
            getattr(session, "payment_status", None)
            or (session.get("payment_status") if isinstance(session, dict) else None)
        )
        raw_sub = getattr(session, "subscription", None) or (
            session.get("subscription") if isinstance(session, dict) else None
        )
        if isinstance(raw_sub, str):
            stripe_subscription_id = raw_sub
        elif raw_sub is not None:
            stripe_subscription_id = getattr(raw_sub, "id", None) or (
                raw_sub.get("id") if isinstance(raw_sub, dict) else None
            )
        else:
            stripe_subscription_id = None

        return (
            str(stripe_status) if stripe_status else None,
            str(payment_status) if payment_status else None,
            str(stripe_subscription_id) if stripe_subscription_id else None,
        )
    except (StripeCheckoutError, StripeConfigurationError):
        raise
    except Exception as exc:
        classification = classify_stripe_error(exc)
        if classification == "failed":
            raise StripeCheckoutError(
                f"Definitive failure retrieving Stripe session '{session_id}': {exc}"
            ) from exc
        # Ambiguous: network failure / 5xx — treat as recovery_required
        logger.warning(
            "Ambiguous error retrieving Stripe session '%s': %s. Treating as recovery_required.",
            session_id,
            exc,
        )
        return None, None, None


# =============================================================================
# Expired Open Attempt Verification
# =============================================================================


async def _handle_locally_expired_open_attempt(
    db: AsyncSession,
    attempt: StripeCheckoutAttempt,
    org_id: uuid.UUID,
) -> "StripeCheckoutSessionResult | None":
    """Verify an apparently-expired open attempt against Stripe before releasing the reservation.

    Safety contract:
    - Commits any active database transaction before calling Stripe (releases org lock).
    - Calls Stripe with ZERO database transaction held.
    - Reacquires locks in STRICT hierarchical order:
        1. Parent Organization row lock FIRST (SELECT Organization ... FOR UPDATE)
        2. Checkout attempt lock SECOND (SELECT StripeCheckoutAttempt ... FOR UPDATE)
    - Rechecks attempt state to respect any webhook-driven state advances that occurred
      during the external Stripe network call.
    - Commits state changes atomically before releasing locks.

    Returns:
        StripeCheckoutSessionResult if the session is still usable (open) — reuse it.
        None if Stripe confirms the session is genuinely expired — caller may proceed
        to create a fresh attempt.

    Raises:
        CheckoutInProgressError: If Stripe confirms payment completed or payment is pending.
        CheckoutRecoveryRequiredError: If Stripe retrieval outcome is ambiguous.
    """
    session_id = attempt.stripe_session_id or ""

    # Step 1: Release any active transaction / org lock BEFORE calling Stripe.
    if db.in_transaction():
        await db.commit()

    # Step 2: Retrieve actual Stripe session status (no DB transaction held).
    try:
        stripe_status, payment_status, stripe_sub_id = _retrieve_stripe_session(session_id)
    except StripeCheckoutError:
        # Definitive retrieval failure — treat conservatively as recovery_required.
        stripe_status, payment_status, stripe_sub_id = None, None, None

    # Step 3: Reacquire locks in STRICT hierarchical order:
    # 1. Organization row lock FIRST
    org_stmt = (
        select(Organization.id)
        .where(Organization.id == org_id)
        .with_for_update()
    )
    await db.execute(org_stmt)

    # 2. Checkout attempt lock SECOND
    reload_stmt = (
        select(StripeCheckoutAttempt)
        .where(StripeCheckoutAttempt.id == attempt.id)
        .with_for_update()
    )
    result = await db.execute(reload_stmt)
    fresh_attempt = result.scalar_one_or_none()

    if fresh_attempt is None:
        # Attempt was deleted (edge case) — release locks and treat as expired.
        await db.commit()
        return None

    now = datetime.now(timezone.utc)

    # Step 4: If a concurrent worker or webhook already advanced state while we were retrieving, respect that.
    if fresh_attempt.status not in (CheckoutAttemptStatus.open,):
        await db.commit()
        if fresh_attempt.status == CheckoutAttemptStatus.complete:
            raise CheckoutInProgressError(
                "Checkout session has been completed. Subscription is being activated."
            )
        if fresh_attempt.status == CheckoutAttemptStatus.payment_pending:
            raise CheckoutInProgressError(
                "A payment is pending confirmation for this session."
            )
        # expired / failed / recovery_required — caller proceeds to fresh attempt
        return None

    # Step 5: Apply Stripe's authoritative response atomically.
    if stripe_status is None:
        # Ambiguous retrieval — retain reservation for recovery.
        fresh_attempt.status = CheckoutAttemptStatus.recovery_required
        fresh_attempt.updated_at = now
        await db.commit()
        raise CheckoutRecoveryRequiredError(
            "Could not verify session status with Stripe. Retaining reservation for recovery."
        )

    if stripe_status == "open":
        # Session is still valid at Stripe — sync expiration and reuse.
        fresh_attempt.updated_at = now
        await db.commit()
        return StripeCheckoutSessionResult(
            attempt_id=fresh_attempt.id,
            organization_id=fresh_attempt.organization_id,
            stripe_session_id=fresh_attempt.stripe_session_id or "",
            checkout_url=fresh_attempt.checkout_url or "",
            session_expires_at=fresh_attempt.session_expires_at,
            status=CheckoutAttemptStatus.open,
        )

    if stripe_status == "complete":
        if payment_status == "paid":
            fresh_attempt.status = CheckoutAttemptStatus.complete
        else:
            fresh_attempt.status = CheckoutAttemptStatus.payment_pending
        if stripe_sub_id:
            fresh_attempt.stripe_subscription_id = stripe_sub_id
        fresh_attempt.updated_at = now
        await db.commit()
        raise CheckoutInProgressError(
            "A checkout session was recently completed. Subscription activation is in progress."
        )

    if stripe_status == "expired":
        # Stripe confirms the session is expired — release reservation.
        fresh_attempt.status = CheckoutAttemptStatus.expired
        fresh_attempt.updated_at = now
        await db.commit()
        return None  # Caller proceeds to fresh attempt creation.

    # Unknown status from Stripe — treat conservatively.
    fresh_attempt.status = CheckoutAttemptStatus.recovery_required
    fresh_attempt.updated_at = now
    await db.commit()
    raise CheckoutRecoveryRequiredError(
        f"Unknown Stripe session status '{stripe_status}'. Retaining reservation."
    )


async def _is_attempt_reconciled(
    db: AsyncSession,
    organization_id: uuid.UUID,
    attempt: StripeCheckoutAttempt,
) -> bool:
    """Determine whether a completed checkout attempt has been reliably reconciled.

    Reconciliation Contract:
    1. A completed attempt is reconciled ONLY when the corresponding Stripe subscription ID
       is reliably associated with a local Subscription record belonging to the organization.
    2. If attempt.stripe_subscription_id is None, the attempt cannot be proven reconciled;
       it remains unreconciled (blocking checkout).
    3. If an unrelated local subscription exists (different stripe_subscription_id),
       it does NOT count as reconciliation.
    4. If the corresponding local subscription was previously reconciled and later canceled,
       the durable matching stripe_subscription_id proves this attempt was reconciled,
       allowing a legitimate future resubscription.
    """
    if not attempt.stripe_subscription_id:
        return False

    sub_stmt = (
        select(Subscription.id)
        .where(
            Subscription.organization_id == organization_id,
            Subscription.stripe_subscription_id == attempt.stripe_subscription_id,
        )
        .limit(1)
    )
    result = await db.execute(sub_stmt)
    matching_sub = result.scalar_one_or_none()
    return matching_sub is not None


# =============================================================================
# Core Domain Operations
# =============================================================================


async def create_stripe_checkout_session(
    db: AsyncSession,
    organization: Organization,
    user_email: str | None = None,
) -> StripeCheckoutSessionResult:
    """Initiate or reuse an organization-scoped Stripe Checkout Session for Pro tier.

    Lifecycle Invariants:
    1. If an active or trialing subscription exists, rejects with ActiveSubscriptionExistsError.
    2. If an unexpired open attempt exists, reuses it without calling Stripe.
    3. If an open attempt is locally expired, verifies the actual Stripe session status
       before releasing the reservation. Only transitions to expired if Stripe confirms it.
    4. If an in-flight attempt has a valid lease, rejects with CheckoutInProgressError.
    5. If an attempt lease expired or is marked recovery_required, triggers recovery replay.
    6. Resolves the Stripe customer BEFORE acquiring the organization row lock, preventing
       customer creation from committing inside the lock and releasing it prematurely.
    7. Atomically reserves tenant slot with status='creating' and short lease before calling Stripe.
    8. Holds ZERO database transactions or row locks across the external Stripe network call.
    9. On success, transitions attempt to 'open' with Stripe-provided expiration.
    10. On definitive error, transitions to 'failed' and releases reservation.
    11. On ambiguous error, transitions to 'recovery_required' and retains reservation.
    12. NEVER grants paid entitlements locally on checkout session creation.

    Args:
        db: Database async session.
        organization: Organization entity requesting checkout.
        user_email: Optional email of the initiating user.

    Returns:
        StripeCheckoutSessionResult with attempt details and checkout redirect URL.
    """
    # 1. Validate billing configuration before modifying database state
    if not is_stripe_enabled():
        raise StripeBillingDisabledError(
            "Stripe billing is currently disabled. Set STRIPE_ENABLED=true to enable."
        )

    pro_price_id = settings.stripe_pro_price_id.strip() if settings.stripe_pro_price_id else ""
    if not pro_price_id:
        raise StripeConfigurationError(
            "Stripe Pro price ID is not configured. Set STRIPE_PRO_PRICE_ID."
        )

    billing_return_url = settings.stripe_billing_return_url.strip() if settings.stripe_billing_return_url else ""
    if not billing_return_url:
        raise StripeConfigurationError(
            "Stripe billing return URL is not configured. Set STRIPE_BILLING_RETURN_URL."
        )

    # 2. Resolve or create the Stripe customer BEFORE acquiring the org row lock.
    #
    #    Rationale: get_or_create_stripe_customer may issue an INSERT + COMMIT to persist a
    #    new customer record. If this commit occurred while we held the org row lock
    #    (SELECT org FOR UPDATE), that commit would release the lock prematurely — a second
    #    concurrent request could then interleave its own state checks against the unlocked row.
    #    By resolving the customer first in its own isolated transaction, the lock section
    #    covers only the idempotency-critical operations: subscription check, active-attempt
    #    check, and attempt insertion.
    if db.in_transaction():
        await db.commit()
    customer_id = await get_or_create_stripe_customer(db, organization, user_email)

    # 3. Release any leftover transaction before acquiring the org lock.
    if db.in_transaction():
        await db.commit()

    # 4. Lock parent organization row to serialize concurrent checkout attempts.
    org_stmt = (
        select(Organization.id)
        .where(Organization.id == organization.id)
        .with_for_update()
    )
    await db.execute(org_stmt)

    # 5. Check for existing active or trialing subscription.
    sub_stmt = (
        select(Subscription.id)
        .where(
            Subscription.organization_id == organization.id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]),
        )
        .limit(1)
    )
    existing_sub = (await db.execute(sub_stmt)).scalar_one_or_none()
    if existing_sub:
        await db.rollback()
        raise ActiveSubscriptionExistsError(
            f"Organization {organization.id} already has an active or trialing subscription."
        )

    # 5.5. Check for completed checkout attempts that have not yet been reconciled into local subscription
    comp_stmt = (
        select(StripeCheckoutAttempt)
        .where(
            StripeCheckoutAttempt.organization_id == organization.id,
            StripeCheckoutAttempt.status == CheckoutAttemptStatus.complete,
        )
        .order_by(StripeCheckoutAttempt.created_at.desc())
    )
    comp_result = await db.execute(comp_stmt)
    completed_attempts = comp_result.scalars().all()
    for comp_att in completed_attempts:
        if not await _is_attempt_reconciled(db, organization.id, comp_att):
            await db.rollback()
            raise CheckoutInProgressError(
                "A checkout session was recently completed for this organization. "
                "Subscription activation is pending confirmation."
            )

    # 6. Check for existing active checkout attempts.
    now = datetime.now(timezone.utc)
    active_attempt = await get_active_checkout_attempt(db, organization.id)

    if active_attempt:
        if active_attempt.status == CheckoutAttemptStatus.open:
            if active_attempt.session_expires_at and active_attempt.session_expires_at > now:
                # Reuse existing unexpired open session without calling Stripe.
                await db.commit()
                return StripeCheckoutSessionResult(
                    attempt_id=active_attempt.id,
                    organization_id=active_attempt.organization_id,
                    stripe_session_id=active_attempt.stripe_session_id or "",
                    checkout_url=active_attempt.checkout_url or "",
                    session_expires_at=active_attempt.session_expires_at,
                    status=CheckoutAttemptStatus.open,
                )
            else:
                # Local expiration timestamp indicates session may be expired.
                # SAFETY: Verify actual Stripe session status before releasing reservation.
                # _handle_locally_expired_open_attempt commits before calling Stripe and
                # re-acquires the lock afterward.
                result = await _handle_locally_expired_open_attempt(
                    db, active_attempt, organization.id
                )
                if result is not None:
                    # Stripe says session is still open — reuse it.
                    return result
                # result is None → Stripe confirmed expired → re-lock and proceed to fresh attempt.
                await db.execute(org_stmt)

        elif active_attempt.status == CheckoutAttemptStatus.payment_pending:
            await db.rollback()
            raise CheckoutInProgressError(
                "A subscription payment is currently pending confirmation for this organization."
            )

        elif active_attempt.status == CheckoutAttemptStatus.creating:
            if active_attempt.attempt_lease_expires_at > now:
                # In-flight creation lease is valid; concurrent worker is creating.
                await db.rollback()
                raise CheckoutInProgressError(
                    "A checkout session is currently being created for this organization. Please try again shortly."
                )
            else:
                # Lease expired mid-flight; mark recovery_required and trigger recovery replay.
                active_attempt.status = CheckoutAttemptStatus.recovery_required
                active_attempt.updated_at = now
                await db.commit()
                return await recover_checkout_attempt(db, active_attempt)

        elif active_attempt.status == CheckoutAttemptStatus.recovery_required:
            # Explicit recovery replay required.
            await db.commit()
            return await recover_checkout_attempt(db, active_attempt)

    # 7. Construct request payload and return URLs.
    attempt_id = uuid.uuid4()
    success_url = build_checkout_return_url(
        billing_return_url,
        session_id="{CHECKOUT_SESSION_ID}",
        status="success",
    )
    cancel_url = build_checkout_return_url(
        billing_return_url,
        status="cancelled",
    )
    request_payload: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [
            {
                "price": pro_price_id,
                "quantity": 1,
            }
        ],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "subscription_data": {
            "metadata": {
                "organization_id": str(organization.id),
            }
        },
        "metadata": {
            "organization_id": str(organization.id),
            "attempt_id": str(attempt_id),
        },
    }

    # 8. Atomically insert new attempt with status='creating' and lease.
    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    attempt = StripeCheckoutAttempt(
        id=attempt_id,
        organization_id=organization.id,
        stripe_customer_id=customer_id,
        stripe_session_id=None,
        checkout_url=None,
        request_payload=request_payload,
        status=CheckoutAttemptStatus.creating,
        attempt_lease_expires_at=lease_expires_at,
        session_expires_at=None,
        error_message=None,
    )
    db.add(attempt)
    try:
        await db.commit()
    except IntegrityError:
        # Race condition: concurrent worker secured the active attempt index.
        await db.rollback()
        raise CheckoutInProgressError(
            "A checkout attempt is already in progress for this organization."
        )

    # 9. Execute external Stripe call outside of database transaction.
    return await _execute_and_record_attempt(db, attempt)


async def _execute_and_record_attempt(
    db: AsyncSession,
    attempt: StripeCheckoutAttempt,
) -> StripeCheckoutSessionResult:
    """Execute external Stripe checkout session creation call and record state transition."""
    idempotency_key = f"checkout-attempt-{attempt.id}"
    try:
        session = _call_stripe_create_session(
            request_payload=attempt.request_payload,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        classification = classify_stripe_error(exc)
        if classification == "failed":
            await _update_attempt_status(
                db,
                attempt_id=attempt.id,
                status=CheckoutAttemptStatus.failed,
                error_message=str(exc),
            )
            raise StripeCheckoutError(f"Stripe checkout session creation failed: {exc}") from exc
        else:
            await _update_attempt_status(
                db,
                attempt_id=attempt.id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message=str(exc),
            )
            raise CheckoutRecoveryRequiredError(
                f"Ambiguous Stripe checkout outcome. Retaining tenant reservation for recovery: {exc}"
            ) from exc

    # Validate returned Stripe session
    session_id = getattr(session, "id", None) or (session.get("id") if isinstance(session, dict) else None)
    checkout_url = getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None)
    expires_at_raw = getattr(session, "expires_at", None) or (session.get("expires_at") if isinstance(session, dict) else None)

    if not session_id or not isinstance(session_id, str):
        err_msg = "Stripe checkout creation returned an invalid or missing session ID"
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.failed,
            error_message=err_msg,
        )
        raise StripeCheckoutError(err_msg)

    if not checkout_url or not isinstance(checkout_url, str):
        err_msg = "Stripe checkout creation returned an invalid or missing checkout URL"
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.failed,
            error_message=err_msg,
        )
        raise StripeCheckoutError(err_msg)

    try:
        _validate_stripe_checkout_url(checkout_url)
    except StripeCheckoutError as exc:
        attempt.status = CheckoutAttemptStatus.failed
        attempt.error_message = str(exc)
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.failed,
            error_message=str(exc),
        )
        raise

    session_expires_at = _parse_stripe_timestamp(expires_at_raw)

    attempt.status = CheckoutAttemptStatus.open
    attempt.stripe_session_id = session_id
    attempt.checkout_url = checkout_url
    attempt.session_expires_at = session_expires_at
    attempt.error_message = None

    # Transition attempt to open
    await _update_attempt_status(
        db,
        attempt_id=attempt.id,
        status=CheckoutAttemptStatus.open,
        stripe_session_id=session_id,
        checkout_url=checkout_url,
        session_expires_at=session_expires_at,
        error_message=None,
    )

    return StripeCheckoutSessionResult(
        attempt_id=attempt.id,
        organization_id=attempt.organization_id,
        stripe_session_id=session_id,
        checkout_url=checkout_url,
        session_expires_at=session_expires_at,
        status=CheckoutAttemptStatus.open,
    )


async def recover_checkout_attempt(
    db: AsyncSession,
    attempt: StripeCheckoutAttempt,
) -> StripeCheckoutSessionResult:
    """Replay an ambiguous or crashed checkout attempt using stored payload and idempotency key.

    Guarantees:
    1. Uses byte-for-byte identical request_payload from the attempt record.
    2. Uses attempt-scoped idempotency key ('checkout-attempt-{attempt.id}').
    3. If Stripe has already created the session, Stripe returns the existing session.
    4. If recovery succeeds, transitions attempt to 'open'.
    5. If recovery encounters a definitive error, transitions to 'failed'.
    6. If recovery encounters an ambiguous error or expired idempotency window where
       the outcome cannot be proven, KEEPS 'recovery_required' to prevent double charging.
    7. Blocks replay if the attempt is older than the safe idempotency window (22 hours).
       Stripe retains idempotency keys for at least 24 hours; we use 22h as a safe cutoff.
    """
    # Guard: check idempotency retention window before attempting Stripe replay.
    #
    # Stripe guarantees idempotency key retention for at least 24 hours. Beyond this window,
    # replaying the same key may result in a NEW Stripe Checkout Session being created rather
    # than retrieving the original — causing a potential duplicate charge. We therefore block
    # replay once the attempt exceeds our conservative 22-hour cutoff and require manual
    # reconciliation instead.
    created_at = attempt.created_at
    if created_at is None:
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message="Attempt creation time is unknown; blocking replay for manual reconciliation.",
        )
        raise CheckoutRecoveryRequiredError(
            "Attempt creation time is unknown; blocking replay for manual reconciliation."
        )

    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=IDEMPOTENCY_SAFE_REPLAY_CUTOFF_HOURS)
    if created_at < cutoff:
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message=(
                f"Attempt is older than the {IDEMPOTENCY_SAFE_REPLAY_CUTOFF_HOURS}-hour safe "
                "idempotency replay window. Blocked for manual billing reconciliation."
            ),
        )
        raise CheckoutRecoveryRequiredError(
            f"Attempt is older than the {IDEMPOTENCY_SAFE_REPLAY_CUTOFF_HOURS}-hour safe "
            "idempotency replay window. Retaining reservation for manual billing reconciliation."
        )

    idempotency_key = f"checkout-attempt-{attempt.id}"
    try:
        session = _call_stripe_create_session(
            request_payload=attempt.request_payload,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        classification = classify_stripe_error(exc)
        if classification == "failed":
            await _update_attempt_status(
                db,
                attempt_id=attempt.id,
                status=CheckoutAttemptStatus.failed,
                error_message=str(exc),
            )
            raise StripeCheckoutError(f"Stripe checkout recovery replay failed: {exc}") from exc
        else:
            # Keep recovery_required on ambiguous or retention errors.
            await _update_attempt_status(
                db,
                attempt_id=attempt.id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message=str(exc),
            )
            raise CheckoutRecoveryRequiredError(
                f"Ambiguous Stripe checkout recovery outcome. Retaining tenant reservation: {exc}"
            ) from exc

    session_id = getattr(session, "id", None) or (session.get("id") if isinstance(session, dict) else None)
    checkout_url = getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None)
    expires_at_raw = getattr(session, "expires_at", None) or (session.get("expires_at") if isinstance(session, dict) else None)

    if not session_id or not checkout_url:
        err_msg = "Stripe checkout recovery returned an invalid or missing session"
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.failed,
            error_message=err_msg,
        )
        raise StripeCheckoutError(err_msg)

    try:
        _validate_stripe_checkout_url(checkout_url)
    except StripeCheckoutError as exc:
        attempt.status = CheckoutAttemptStatus.failed
        attempt.error_message = str(exc)
        await _update_attempt_status(
            db,
            attempt_id=attempt.id,
            status=CheckoutAttemptStatus.failed,
            error_message=str(exc),
        )
        raise

    session_expires_at = _parse_stripe_timestamp(expires_at_raw)

    attempt.status = CheckoutAttemptStatus.open
    attempt.stripe_session_id = session_id
    attempt.checkout_url = checkout_url
    attempt.session_expires_at = session_expires_at
    attempt.error_message = None

    await _update_attempt_status(
        db,
        attempt_id=attempt.id,
        status=CheckoutAttemptStatus.open,
        stripe_session_id=session_id,
        checkout_url=checkout_url,
        session_expires_at=session_expires_at,
        error_message=None,
    )

    return StripeCheckoutSessionResult(
        attempt_id=attempt.id,
        organization_id=attempt.organization_id,
        stripe_session_id=session_id,
        checkout_url=checkout_url,
        session_expires_at=session_expires_at,
        status=CheckoutAttemptStatus.open,
    )
