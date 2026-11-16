"""Domain service for organization-scoped Razorpay Subscription Checkout.

Guarantees & Lifecycle Invariants:
1. Single Active Checkout Attempt: Enforces at most one active attempt per organization
   across ('creating', 'recovery_required', 'open', 'payment_pending') via partial unique index
   and application-level concurrency control.
2. Provider Scope Integrity: Sourced strictly from validated configuration via _get_provisioning_scope().
   Test mode cannot create live provisions; live mode cannot use test provisions.
3. Dedicated Customer Provisioning: Resolves customer association via the decoupled
   get_or_create_razorpay_customer() service, never mutating or demoting Subscription rows.
4. Transaction Boundary Safety: Never holds open database transactions or row locks
   across external HTTP requests (subscription creation or payment lookup).
5. Robust Failure Classification & Recovery:
   - Definite client errors (400, 401, 404, 422) transition the attempt to 'failed' to release
     the active constraint.
   - Ambiguous errors (5xx, 408, 429, timeouts, network disconnects) transition to
     'recovery_required', retaining the reservation for idempotent recovery.
6. Server-Authoritative Verification:
   - Never trusts browser-supplied subscription IDs as the source of truth.
   - Cryptographic HMAC-SHA256 signature verification uses server-owned subscription ID.
   - Constant-time comparison prevents timing attacks.
7. Gateway Payment Verification Outside Database Transactions:
   - Queries Razorpay payment API to verify captured vs authorized payment status.
   - Validates metadata and subscription association.
8. Entitlement Protection:
   - Signature verification and checkout completion NEVER grant Pro tier or quota.
   - Does NOT mutate Subscription.plan_tier.
   - Does NOT create OrganizationBillingPeriod.
   - Paid entitlements remain strictly and exclusively governed by the Full-Payment Settlement policy.
9. Idempotency:
   - Replays of open sessions return the existing session.
   - Replays of verified callbacks return the existing verification result without re-executing state transitions.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionFactory
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
)
from app.models.subscription import (
    CheckoutAttemptStatus,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayBillingDisabledError,
    RazorpayClientError,
    RazorpayConfigurationError,
    RazorpayNetworkError,
    RazorpayRateLimitError,
    RazorpayServer5xxError,
    get_razorpay_client,
    is_razorpay_enabled,
)
from app.services.billing.razorpay.customer import (
    _get_provisioning_scope,
    get_or_create_razorpay_customer,
)
from app.services.billing.razorpay.handler_base import (
    is_terminal_subscription,
    stage_or_update_payment_reconciliation,
)

logger = logging.getLogger(__name__)

CHECKOUT_LEASE_SECONDS = 60.0
DEFAULT_SUBSCRIPTION_EXPIRY_DAYS = 1
RECOVERY_PAGE_SIZE = 50
RECOVERY_MAX_PAGES = 3
RECOVERY_CLOCK_SKEW_SECONDS = 300


# =============================================================================
# Domain Exceptions
# =============================================================================


class RazorpayCheckoutError(Exception):
    """Raised when a Razorpay checkout operation fails definitively."""


class ActiveSubscriptionExistsError(RazorpayCheckoutError):
    """Raised when an organization already possesses an active or trialing Pro subscription."""


class CheckoutInProgressError(RazorpayCheckoutError):
    """Raised when an active checkout attempt is currently in-flight or payment is pending."""


class CheckoutRecoveryRequiredError(RazorpayCheckoutError):
    """Raised when an attempt experienced an ambiguous outcome requiring recovery or reconciliation."""


class RazorpayCheckoutNotFoundError(RazorpayCheckoutError):
    """Raised when a referenced checkout attempt is not found."""


class RazorpayCheckoutSignatureError(RazorpayCheckoutError):
    """Raised when signature verification fails or callback data is mismatched/malformed."""


class RazorpayCheckoutPaymentError(RazorpayCheckoutError):
    """Raised when payment status from Razorpay indicates failure."""


class RazorpayCheckoutConflictError(RazorpayCheckoutError):
    """Raised when a callback conflicts with the current attempt state."""


class RazorpayCheckoutAmbiguousError(RazorpayCheckoutError):
    """Raised when network timeout or server error leaves checkout session outcome ambiguous."""


# =============================================================================
# Result Dataclasses
# =============================================================================


@dataclass(frozen=True)
class RazorpaySubscriptionCheckoutResult:
    """Immutable result of a successful Razorpay subscription checkout creation or reuse."""

    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    provider: str
    subscription_id: str
    key_id: str
    plan_id: str
    checkout_url: str | None
    status: CheckoutAttemptStatus


@dataclass(frozen=True)
class RazorpayCheckoutVerificationResult:
    """Immutable result of a verified Razorpay checkout callback."""

    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    provider: str
    subscription_id: str
    payment_id: str
    status: CheckoutAttemptStatus
    payment_status: str


# =============================================================================
# Signature Verification Helpers
# =============================================================================


def verify_razorpay_subscription_signature(
    payment_id: str,
    subscription_id: str,
    signature: str,
    secret: str,
) -> bool:
    """Verify Razorpay subscription payment signature using HMAC-SHA256.

    Standard Razorpay Subscription signature payload:
        payment_id + '|' + subscription_id

    Uses constant-time comparison (hmac.compare_digest) to prevent timing attacks.
    """
    if not payment_id or not subscription_id or not signature or not secret:
        return False

    payment_id_str = str(payment_id).strip()
    subscription_id_str = str(subscription_id).strip()
    signature_str = str(signature).strip()
    secret_str = str(secret).strip()

    if not payment_id_str or not subscription_id_str or not signature_str or not secret_str:
        return False

    payload = f"{payment_id_str}|{subscription_id_str}".encode("utf-8")
    expected_signature = hmac.new(
        secret_str.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_str)


def _parse_timestamp(val: Any) -> datetime | None:
    """Convert a Unix timestamp integer or datetime into a UTC timezone-aware datetime."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    return None


async def _safe_rollback(db: AsyncSession) -> None:
    """Safely rollback an open database transaction without masking primary errors."""
    try:
        await db.rollback()
    except Exception:
        pass


# =============================================================================
# Subscription Checkout Creation Service
# =============================================================================


async def create_razorpay_subscription_checkout(
    db: AsyncSession,
    organization: Organization,
    user_email: str | None = None,
    idempotency_key: str | None = None,
    customer_session_factory: AsyncSessionFactory | None = None,
) -> RazorpaySubscriptionCheckoutResult:
    """Initiate or reuse a Razorpay subscription checkout attempt for an organization.

    Guarantees:
    1. Validates billing configuration and provider scope upfront.
    2. Phase A (Isolated Customer Provisioning): Runs get_or_create_razorpay_customer() in its
       own committed transaction lifecycle strictly before Phase B acquires checkout locks.
    3. Phase B (Fresh Locked Checkout Transaction): Acquires Organization FOR UPDATE ->
       BillingCustomerProvision FOR UPDATE (populate_existing=True) -> Subscription FOR UPDATE ->
       BillingCheckoutAttempt FOR UPDATE, revalidating organization existence and customer mapping.
    4. Enforces single active checkout attempt per organization and expires stale 'open' attempts
       atomically inside the same Phase B transaction (no mid-lock commits).
    5. Commits durable 'creating' attempt BEFORE invoking external Razorpay API.
    6. External API request runs with zero open database transactions or row locks.
    7. Classifies network/server failures into 'failed' (definite) vs 'recovery_required' (ambiguous).
    8. Records successful remote subscription as 'open' with server-owned subscription ID.
    9. NEVER grants Pro entitlements or provisions billing periods.
    """
    # -------------------------------------------------------------------------
    # Pre-Flight Configuration Validation (Zero DB / HTTP Calls)
    # -------------------------------------------------------------------------
    if not is_razorpay_enabled():
        raise RazorpayBillingDisabledError(
            "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
        )

    merchant_account_id, environment = _get_provisioning_scope()

    plan_id = (settings.razorpay_pro_plan_id or "").strip()
    if not plan_id:
        raise RazorpayConfigurationError(
            "RAZORPAY_PRO_PLAN_ID is not configured. Configure a valid Razorpay plan identifier."
        )

    key_id = (settings.razorpay_key_id or "").strip()
    if not key_id:
        raise RazorpayConfigurationError("RAZORPAY_KEY_ID is not configured.")

    currency = (settings.razorpay_plan_currency or "USD").strip().upper()

    # -------------------------------------------------------------------------
    # Phase A: Isolated Customer Provisioning (Independent TX + HTTP Boundary)
    # -------------------------------------------------------------------------
    org_id = organization.id
    org_name = getattr(organization, "name", None)

    # Close any prior caller read transaction (e.g., API RBAC / org lookup)
    # so customer provisioning executes in its own isolated transaction scope.
    if hasattr(db, "in_transaction") and db.in_transaction():
        await db.commit()

    customer_id = await get_or_create_razorpay_customer(
        organization_id=org_id,
        user_email=user_email,
        organization_name=org_name,
        session_factory=customer_session_factory,
    )

    # Guarantee Phase A left zero open database transactions before Phase B opens.
    if hasattr(db, "in_transaction") and db.in_transaction():
        await db.commit()

    # -------------------------------------------------------------------------
    # Phase B: Fresh Transaction — Checkout Locks, Revalidation & Attempt Staging
    # Lock order (L1B -> L3 -> L6):
    #   Organization FOR UPDATE
    #     -> BillingCustomerProvision FOR UPDATE
    #     -> Subscription FOR UPDATE
    #     -> BillingCheckoutAttempt FOR UPDATE
    # Zero external HTTP calls permitted in Phase B.
    # -------------------------------------------------------------------------
    try:
        stmt_org_lock = (
            select(Organization)
            .where(Organization.id == organization.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res_org = await db.execute(stmt_org_lock)
        locked_org = res_org.scalar_one_or_none()
        if locked_org is None:
            await _safe_rollback(db)
            raise RazorpayCheckoutNotFoundError(
                f"Organization {organization.id} is unavailable or was removed before checkout creation."
            )

        stmt_prov_lock = (
            select(BillingCustomerProvision)
            .where(
                BillingCustomerProvision.organization_id == organization.id,
                BillingCustomerProvision.provider == "razorpay",
                BillingCustomerProvision.merchant_account_id == merchant_account_id,
                BillingCustomerProvision.environment == environment,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res_prov = await db.execute(stmt_prov_lock)
        locked_provision = res_prov.scalar_one_or_none()

        if locked_provision is None:
            await _safe_rollback(db)
            raise RazorpayCheckoutConflictError(
                f"Razorpay customer mapping for organization {organization.id} disappeared between "
                f"customer provisioning and checkout creation."
            )

        if locked_provision.status != CustomerProvisionStatus.resolved:
            await _safe_rollback(db)
            raise RazorpayCheckoutConflictError(
                f"Razorpay customer mapping for organization {organization.id} transitioned to "
                f"'{locked_provision.status.value}' between customer provisioning and checkout creation."
            )

        revalidated_customer_id = (
            locked_provision.provider_customer_id.strip()
            if isinstance(locked_provision.provider_customer_id, str)
            and locked_provision.provider_customer_id.strip()
            else None
        )
        if not revalidated_customer_id:
            await _safe_rollback(db)
            raise RazorpayCheckoutConflictError(
                f"Razorpay customer mapping for organization {organization.id} is missing a resolved customer ID."
            )

        if revalidated_customer_id != customer_id:
            await _safe_rollback(db)
            raise RazorpayCheckoutConflictError(
                f"Razorpay customer mapping for organization {organization.id} changed between provisioning "
                f"('{customer_id}') and checkout creation ('{revalidated_customer_id}')."
            )

        # Check if organization already possesses an active Pro subscription under lock
        stmt_sub = (
            select(Subscription)
            .where(
                Subscription.organization_id == organization.id,
                Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]),
                Subscription.plan_tier == "pro",
            )
            .order_by(Subscription.id.asc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res_sub = await db.execute(stmt_sub)
        if res_sub.scalar_one_or_none() is not None:
            await _safe_rollback(db)
            raise ActiveSubscriptionExistsError(
                f"Organization {organization.id} already has an active Pro subscription."
            )

        # Evaluate timestamp after acquiring Phase B row locks
        now_utc = datetime.now(timezone.utc)

        # Inspect existing active checkout attempts for this organization in current scope under lock
        stmt_active = (
            select(BillingCheckoutAttempt)
            .where(
                BillingCheckoutAttempt.organization_id == organization.id,
                BillingCheckoutAttempt.provider == "razorpay",
                BillingCheckoutAttempt.merchant_account_id == merchant_account_id,
                BillingCheckoutAttempt.environment == environment,
                BillingCheckoutAttempt.status.in_([
                    CheckoutAttemptStatus.creating,
                    CheckoutAttemptStatus.recovery_required,
                    CheckoutAttemptStatus.open,
                    CheckoutAttemptStatus.payment_pending,
                ]),
            )
            .order_by(BillingCheckoutAttempt.created_at.desc(), BillingCheckoutAttempt.id.asc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res_active = await db.execute(stmt_active)
        active_attempt = res_active.scalar_one_or_none()

        if active_attempt is not None:
            # Case A: Existing 'open' attempt with valid subscription
            if active_attempt.status == CheckoutAttemptStatus.open:
                open_expires_at = _parse_timestamp(active_attempt.session_expires_at)
                if open_expires_at is not None and now_utc >= open_expires_at:
                    logger.info(
                        "Expiring open checkout attempt %s for organization %s (expired at %s)",
                        active_attempt.id,
                        organization.id,
                        active_attempt.session_expires_at,
                    )
                    # Flush inside the same Phase B transaction; never commit mid-lock before staging replacement.
                    active_attempt.status = CheckoutAttemptStatus.expired
                    await db.flush()
                elif active_attempt.provider_subscription_id:
                    reused_attempt_id = active_attempt.id
                    reused_sub_id = active_attempt.provider_subscription_id
                    reused_checkout_url = active_attempt.checkout_url
                    await db.commit()
                    return RazorpaySubscriptionCheckoutResult(
                        attempt_id=reused_attempt_id,
                        organization_id=organization.id,
                        provider="razorpay",
                        subscription_id=reused_sub_id,
                        key_id=key_id,
                        plan_id=plan_id,
                        checkout_url=reused_checkout_url,
                        status=CheckoutAttemptStatus.open,
                    )
                else:
                    await _safe_rollback(db)
                    raise CheckoutRecoveryRequiredError(
                        f"Open checkout attempt {active_attempt.id} lacks a subscription ID and requires recovery."
                    )

            # Case B: In-progress payment pending verification
            elif active_attempt.status == CheckoutAttemptStatus.payment_pending:
                await _safe_rollback(db)
                raise CheckoutInProgressError(
                    f"A checkout payment is currently pending verification for organization {organization.id}."
                )

            # Case C: Previous ambiguous outcome requiring reconciliation
            elif active_attempt.status == CheckoutAttemptStatus.recovery_required:
                await _safe_rollback(db)
                raise CheckoutRecoveryRequiredError(
                    f"A previous checkout attempt ({active_attempt.id}) requires recovery or reconciliation "
                    f"before a new subscription checkout can be initiated."
                )

            # Case D: Attempt is currently in 'creating' state
            elif active_attempt.status == CheckoutAttemptStatus.creating:
                lease_expires = _parse_timestamp(active_attempt.attempt_lease_expires_at)
                if lease_expires is not None and now_utc < lease_expires:
                    await _safe_rollback(db)
                    raise CheckoutInProgressError(
                        f"Subscription checkout creation is already in progress for organization {organization.id}."
                    )

                # Lease expired while in 'creating' state: durably mark recovery_required
                logger.warning(
                    "Checkout attempt %s lease expired while creating; transitioning to recovery_required.",
                    active_attempt.id,
                )
                active_attempt.status = CheckoutAttemptStatus.recovery_required
                active_attempt.error_message = "creation_lease_expired"
                await db.commit()
                raise CheckoutRecoveryRequiredError(
                    f"Checkout attempt {active_attempt.id} creation lease expired; recovery required before retry."
                )
    except Exception:
        if not hasattr(db, "in_transaction") or db.in_transaction():
            await _safe_rollback(db)
        raise

    # Durable Attempt Staging (Atomic with any expired-open update in Phase B)
    attempt_id = uuid.uuid4()
    effective_idempotency_key = idempotency_key or f"rzp_sub_{organization.id}_{uuid.uuid4().hex[:12]}"
    lease_expires_at = now_utc + timedelta(seconds=CHECKOUT_LEASE_SECONDS)

    attempt = BillingCheckoutAttempt(
        id=attempt_id,
        organization_id=organization.id,
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        provider_customer_id=revalidated_customer_id,
        provider_subscription_id=None,
        provider_order_id=None,
        provider_session_id=None,
        idempotency_key=effective_idempotency_key,
        status=CheckoutAttemptStatus.creating,
        amount_minor_units=0,
        currency=currency,
        checkout_url=None,
        request_payload={
            "plan_id": plan_id,
            "customer_id": revalidated_customer_id,
            "currency": currency,
        },
        attempt_lease_expires_at=lease_expires_at,
        session_expires_at=None,
        error_message=None,
    )
    db.add(attempt)

    try:
        await db.commit()
    except IntegrityError as exc:
        await _safe_rollback(db)
        logger.warning(
            "Integrity conflict staging checkout attempt for organization %s: %s",
            organization.id,
            exc,
        )
        raise CheckoutInProgressError(
            f"Concurrent checkout attempt detected for organization {organization.id}."
        ) from exc
    except Exception:
        await _safe_rollback(db)
        raise

    # Guarantee transaction and locks are completely released before Phase 3 HTTP call
    if hasattr(db, "in_transaction") and db.in_transaction():
        raise AssertionError(
            "Phase B checkout staging attempted external HTTP while DB transaction was still open."
        )

    # -------------------------------------------------------------------------
    # Phase 3: External Gateway Invocation (Zero Open DB Transactions)
    # -------------------------------------------------------------------------
    client = get_razorpay_client()
    notes: dict[str, str] = {
        "organization_id": str(organization.id),
        "attempt_id": str(attempt_id),
    }

    try:
        sub_data = await client.create_subscription(
            plan_id=plan_id,
            total_count=120,
            customer_id=revalidated_customer_id,
            customer_notify=1,
            notes=notes,
        )
    except RazorpayAuthError as exc:
        # Definite auth error: safe to mark failed and release active constraint
        logger.error(
            "Razorpay subscription creation authentication failed for organization %s: %s",
            organization.id,
            exc,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.failed,
            error_message=str(exc),
        )
        raise RazorpayCheckoutError(f"Subscription creation authentication failed: {exc}") from exc
    except RazorpayRateLimitError as exc:
        # HTTP 429: Ambiguous outcome; retain reservation for recovery
        logger.error(
            "Razorpay rate limit creating subscription for organization %s: %s",
            organization.id,
            exc,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message="rate_limit_exceeded",
        )
        raise RazorpayCheckoutAmbiguousError(
            f"Razorpay rate limit exceeded while creating subscription for organization {organization.id}: {exc}"
        ) from exc
    except RazorpayServer5xxError as exc:
        # Ambiguous gateway outcome: must retain reservation for recovery
        logger.error(
            "Ambiguous server error from Razorpay creating subscription for organization %s (status=%s): %s",
            organization.id,
            exc.status_code,
            exc,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message=f"server_5xx_{exc.status_code}",
        )
        raise RazorpayCheckoutAmbiguousError(
            f"Razorpay gateway returned ambiguous 5xx outcome: {exc}"
        ) from exc
    except RazorpayAPIError as exc:
        if exc.status_code == 408:
            logger.error(
                "HTTP 408 timeout creating subscription for organization %s: %s",
                organization.id,
                exc,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message="http_408_timeout",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"HTTP 408 timeout creating subscription for organization {organization.id}: {exc}"
            ) from exc
        elif exc.status_code is not None and exc.status_code < 500:
            # Definite 4xx client rejection (400, 404, 422)
            logger.error(
                "Client error creating subscription for organization %s (status=%s): %s",
                organization.id,
                exc.status_code,
                exc,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.failed,
                error_message=str(exc),
            )
            raise RazorpayCheckoutError(f"Subscription creation rejected: {exc}") from exc
        else:
            logger.error(
                "Ambiguous API error creating subscription for organization %s: %s",
                organization.id,
                exc,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message=f"api_error_{exc.status_code}",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Ambiguous API error from Razorpay for organization {organization.id}: {exc}"
            ) from exc
    except RazorpayNetworkError as exc:
        if exc.is_pre_dispatch:
            # Connect failure before dispatch: safe to mark failed
            logger.error(
                "Pre-dispatch connection failure creating subscription for organization %s: %s",
                organization.id,
                exc,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.failed,
                error_message=str(exc),
            )
            raise RazorpayCheckoutError(f"Pre-dispatch connection failure: {exc}") from exc
        else:
            # Post-dispatch disconnect / timeout: outcome ambiguous!
            logger.error(
                "Post-dispatch network failure creating subscription for organization %s: %s",
                organization.id,
                exc,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message="post_dispatch_network_timeout",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Network outcome ambiguous while creating subscription for organization {organization.id}: {exc}"
            ) from exc
    except RazorpayClientError as exc:
        logger.error(
            "Razorpay client error creating subscription for organization %s: %s",
            organization.id,
            exc,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.failed,
            error_message=str(exc),
        )
        raise RazorpayCheckoutError(f"Subscription creation client error: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "Unexpected error creating Razorpay subscription for organization %s: %s",
            organization.id,
            exc,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message="unexpected_gateway_error",
        )
        raise RazorpayCheckoutAmbiguousError(
            f"Unexpected error during subscription creation: {exc}"
        ) from exc

    # -------------------------------------------------------------------------
    # Phase 4: Remote Success Persistence
    # -------------------------------------------------------------------------
    sub_id = sub_data.get("id") if isinstance(sub_data, dict) else None
    if not sub_id or not isinstance(sub_id, str) or not sub_id.strip():
        logger.error(
            "Razorpay returned HTTP success with missing subscription ID for organization %s",
            organization.id,
        )
        await _record_attempt_outcome(
            db=db,
            attempt_id=attempt_id,
            status=CheckoutAttemptStatus.recovery_required,
            error_message="malformed_success_response",
        )
        raise RazorpayCheckoutAmbiguousError(
            "Razorpay returned success but subscription ID is missing."
        )

    sub_id = sub_id.strip()
    short_url = sub_data.get("short_url")
    expire_by_ts = sub_data.get("expire_by")
    session_expires_at = _parse_timestamp(expire_by_ts) or (now_utc + timedelta(days=DEFAULT_SUBSCRIPTION_EXPIRY_DAYS))

    await _record_attempt_success(
        db=db,
        attempt_id=attempt_id,
        subscription_id=sub_id,
        checkout_url=short_url,
        session_expires_at=session_expires_at,
    )

    return RazorpaySubscriptionCheckoutResult(
        attempt_id=attempt_id,
        organization_id=organization.id,
        provider="razorpay",
        subscription_id=sub_id,
        key_id=key_id,
        plan_id=plan_id,
        checkout_url=short_url,
        status=CheckoutAttemptStatus.open,
    )


# =============================================================================
# Subscription Checkout Recovery / Reconciliation Service
# =============================================================================


async def recover_razorpay_checkout_attempt(
    db: AsyncSession,
    organization_id: uuid.UUID,
    attempt_id: uuid.UUID,
    known_subscription_id: str | None = None,
    definitive_nonexistence_evidence: str | None = None,
) -> RazorpaySubscriptionCheckoutResult:
    """Recover or reconcile a checkout attempt in recovery_required or expired creating state.

    Guarantees & Invariants:
    1. Mandatory Invariant: Absence of a known subscription ID is NEVER treated as evidence
       that Razorpay did not create the subscription.
    2. Active reservations are NEVER released while the remote outcome remains unknown.
    3. If a reliable remote lookup is unavailable or inconclusive, preserves 'recovery_required'.
    4. If multiple candidate subscriptions are found, preserves ambiguity and creates a
       PaymentReconciliation review case.
    5. Only marks an attempt 'failed' when defensible evidence of non-creation is explicitly provided.
    6. Concurrency & Stale Worker Protection: Uses recovery claim leases to prevent stale workers
       from overwriting newer recovery state.
    """
    if not is_razorpay_enabled():
        raise RazorpayBillingDisabledError(
            "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
        )

    merchant_account_id, environment = _get_provisioning_scope()
    now_utc = datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Phase 1: Pre-HTTP Attempt Validation & Claim Acquisition
    # -------------------------------------------------------------------------
    stmt = (
        select(BillingCheckoutAttempt)
        .where(
            BillingCheckoutAttempt.id == attempt_id,
            BillingCheckoutAttempt.organization_id == organization_id,
            BillingCheckoutAttempt.provider == "razorpay",
            BillingCheckoutAttempt.merchant_account_id == merchant_account_id,
            BillingCheckoutAttempt.environment == environment,
        )
    )
    res = await db.execute(stmt)
    attempt = res.scalar_one_or_none()

    if attempt is None:
        raise RazorpayCheckoutNotFoundError(
            f"Checkout attempt {attempt_id} not found for organization {organization_id} in scope {environment}."
        )

    # 1.1 State Eligibility Checks
    if attempt.status == CheckoutAttemptStatus.open:
        key_id = (settings.razorpay_key_id or "").strip()
        plan_id = (settings.razorpay_pro_plan_id or "").strip()
        return RazorpaySubscriptionCheckoutResult(
            attempt_id=attempt.id,
            organization_id=organization_id,
            provider="razorpay",
            subscription_id=attempt.provider_subscription_id or "",
            key_id=key_id,
            plan_id=plan_id,
            checkout_url=attempt.checkout_url,
            status=CheckoutAttemptStatus.open,
        )

    if attempt.status == CheckoutAttemptStatus.complete:
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is already completed."
        )

    if attempt.status in (CheckoutAttemptStatus.failed, CheckoutAttemptStatus.expired):
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is in terminal '{attempt.status.value}' state and cannot be recovered."
        )

    if attempt.status == CheckoutAttemptStatus.creating:
        lease_expires = attempt.attempt_lease_expires_at
        if lease_expires is not None:
            if lease_expires.tzinfo is None:
                lease_expires = lease_expires.replace(tzinfo=timezone.utc)
            if now_utc < lease_expires:
                raise CheckoutInProgressError(
                    f"Attempt {attempt_id} creation lease is still active; recovery not yet permissible."
                )

    # 1.2 Defensible Non-Creation Evidence Handling
    if definitive_nonexistence_evidence and str(definitive_nonexistence_evidence).strip():
        evidence_str = str(definitive_nonexistence_evidence).strip()
        logger.info(
            "Reconciling attempt %s as definitively failed with proof: %s",
            attempt_id,
            evidence_str,
        )
        reconciliation = PaymentReconciliation(
            id=uuid.uuid4(),
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            organization_id=organization_id,
            payment_id=None,
            invoice_id=None,
            raw_event_id=None,
            reason="definitive_nonexistence_evidence",
            status=ReconciliationStatus.resolved_ignored,
            diagnostic_payload={
                "attempt_id": str(attempt_id),
                "evidence": evidence_str,
                "reconciled_at": now_utc.isoformat(),
            },
        )
        db.add(reconciliation)
        attempt.status = CheckoutAttemptStatus.failed
        attempt.error_message = evidence_str
        await db.commit()
        raise RazorpayCheckoutError(
            f"Attempt {attempt_id} marked failed with defensible evidence: {evidence_str}"
        )

    # 1.3 Acquire Recovery Claim (Lease) for Concurrency Safety
    recovery_claim_id = f"rec_claim_{uuid.uuid4().hex[:12]}"
    attempt.attempt_lease_expires_at = now_utc + timedelta(seconds=CHECKOUT_LEASE_SECONDS)
    payload_copy = dict(attempt.request_payload or {})
    payload_copy["recovery_claim_id"] = recovery_claim_id
    attempt.request_payload = payload_copy

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise RazorpayCheckoutError(f"Failed to acquire recovery lease for attempt {attempt_id}: {exc}") from exc

    # Ensure no DB transaction is held across external HTTP requests
    if db.in_transaction():
        await db.commit()

    # -------------------------------------------------------------------------
    # Phase 2: Gateway Investigation (Strictly Outside DB Transaction)
    # -------------------------------------------------------------------------
    client = get_razorpay_client()
    expected_plan_id = (attempt.request_payload.get("plan_id") or settings.razorpay_pro_plan_id or "").strip()
    expected_cust_id = (attempt.provider_customer_id or "").strip()

    candidate_sub_data: dict[str, Any] | None = None
    target_sub_id: str | None = None

    if known_subscription_id and known_subscription_id.strip():
        # Case 2A: Explicit known subscription identifier provided
        target_sub_id = known_subscription_id.strip()
        try:
            candidate_sub_data = await client.get_subscription(target_sub_id)
        except RazorpayAPIError as exc:
            logger.error("Failed to fetch remote subscription %s during recovery: %s", target_sub_id, exc)
            raise RazorpayCheckoutAmbiguousError(
                f"Recovery lookup failed: unable to verify subscription {target_sub_id} on Razorpay: {exc}"
            ) from exc
        except (RazorpayNetworkError, RazorpayServer5xxError, RazorpayRateLimitError) as exc:
            logger.error("Network/server timeout verifying subscription %s: %s", target_sub_id, exc)
            raise RazorpayCheckoutAmbiguousError(
                f"Recovery network failure verifying subscription {target_sub_id}: {exc}"
            ) from exc

        if not isinstance(candidate_sub_data, dict) or candidate_sub_data.get("id") != target_sub_id:
            raise RazorpayCheckoutAmbiguousError(
                f"Recovery failed: invalid subscription data returned by gateway for {target_sub_id}."
            )

        # Strict validation of remote subscription ownership & plan
        remote_plan = (candidate_sub_data.get("plan_id") or "").strip()
        if expected_plan_id and remote_plan and remote_plan != expected_plan_id:
            logger.error(
                "Remote subscription %s plan mismatch: expected '%s', got '%s'",
                target_sub_id,
                expected_plan_id,
                remote_plan,
            )
            raise RazorpayCheckoutConflictError(
                f"Remote subscription {target_sub_id} belongs to a different plan ('{remote_plan}')."
            )

        remote_cust = (candidate_sub_data.get("customer_id") or "").strip()
        if expected_cust_id and remote_cust and remote_cust != expected_cust_id:
            logger.error(
                "Remote subscription %s customer mismatch: expected '%s', got '%s'",
                target_sub_id,
                expected_cust_id,
                remote_cust,
            )
            raise RazorpayCheckoutConflictError(
                f"Remote subscription {target_sub_id} belongs to a different customer ('{remote_cust}')."
            )

        sub_notes = candidate_sub_data.get("notes") or {}
        if isinstance(sub_notes, dict):
            noted_org = sub_notes.get("organization_id")
            if noted_org and str(noted_org).strip() != str(organization_id):
                raise RazorpayCheckoutConflictError(
                    f"Remote subscription {target_sub_id} organization mismatch during recovery."
                )
            noted_att = sub_notes.get("attempt_id")
            if noted_att and str(noted_att).strip() != str(attempt_id):
                raise RazorpayCheckoutConflictError(
                    f"Remote subscription {target_sub_id} attempt mismatch during recovery."
                )

    else:
        # Case 2B: Unknown subscription ID -> Attempt remote lookup via bounded pagination
        if attempt.created_at is None:
            logger.error(
                "Cannot recover attempt %s: missing created_at timestamp. Failing closed.",
                attempt_id,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message="recovery_lookup_inconclusive_missing_created_at",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Checkout attempt {attempt_id} lacks a created_at timestamp; recovery cannot establish a safe search window."
            )

        attempt_created_at = attempt.created_at
        if attempt_created_at.tzinfo is None:
            attempt_created_at = attempt_created_at.replace(tzinfo=timezone.utc)
        attempt_ts = int(attempt_created_at.timestamp())
        from_time = max(0, attempt_ts - RECOVERY_CLOCK_SKEW_SECONDS)
        window_lower = from_time
        window_upper = attempt_ts + int(CHECKOUT_LEASE_SECONDS) + RECOVERY_CLOCK_SKEW_SECONDS

        matching_candidates: list[dict[str, Any]] = []
        search_exhausted = False
        search_incomplete_reason: str | None = None

        for page_idx in range(RECOVERY_MAX_PAGES):
            skip = page_idx * RECOVERY_PAGE_SIZE
            try:
                list_res = await client.list_subscriptions(
                    plan_id=expected_plan_id,
                    from_time=from_time,
                    count=RECOVERY_PAGE_SIZE,
                    skip=skip,
                )
            except (RazorpayNetworkError, RazorpayServer5xxError, RazorpayRateLimitError, RazorpayAPIError) as exc:
                logger.error(
                    "Gateway error listing subscriptions (page %d, skip %d) for attempt %s: %s",
                    page_idx + 1,
                    skip,
                    attempt_id,
                    exc,
                )
                search_incomplete_reason = f"gateway_error_page_{page_idx + 1}: {exc}"
                break
            except Exception as exc:
                logger.error(
                    "Unexpected error listing subscriptions (page %d, skip %d) for attempt %s: %s",
                    page_idx + 1,
                    skip,
                    attempt_id,
                    exc,
                )
                search_incomplete_reason = f"unexpected_error_page_{page_idx + 1}: {exc}"
                break

            if not isinstance(list_res, dict) or not isinstance(list_res.get("items"), list):
                logger.error(
                    "Malformed subscription list response from gateway (page %d, skip %d) for attempt %s",
                    page_idx + 1,
                    skip,
                    attempt_id,
                )
                search_incomplete_reason = f"malformed_response_page_{page_idx + 1}"
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
                    # Rule 1: If notes.attempt_id is present and non-empty, it MUST equal current attempt ID.
                    if item_attempt_id != str(attempt_id):
                        # Belongs to another attempt: discard candidate, NEVER fall back!
                        continue

                    # Valid attempt ID match: verify plan, customer, org match
                    remote_plan = (item.get("plan_id") or "").strip()
                    remote_cust = (item.get("customer_id") or "").strip()
                    remote_org = str(item_notes.get("organization_id", "")).strip()

                    if expected_plan_id and remote_plan and remote_plan != expected_plan_id:
                        continue
                    if expected_cust_id and remote_cust and remote_cust != expected_cust_id:
                        continue
                    if remote_org and remote_org != str(organization_id):
                        continue

                    matching_candidates.append(item)
                else:
                    # Rule 3: Allow fallback matching ONLY when notes.attempt_id is genuinely absent or empty.
                    # Rule 4: Fallback candidates must additionally pass:
                    # - Exact organization ID match
                    remote_org = str(item_notes.get("organization_id", "")).strip()
                    if not remote_org or remote_org != str(organization_id):
                        continue

                    # - Exact expected Razorpay customer ID match
                    remote_cust = (item.get("customer_id") or "").strip()
                    if not remote_cust or not expected_cust_id or remote_cust != expected_cust_id:
                        continue

                    # - Exact expected plan ID match
                    remote_plan = (item.get("plan_id") or "").strip()
                    if not remote_plan or not expected_plan_id or remote_plan != expected_plan_id:
                        continue

                    # - Defensible creation-time window tied to checkout attempt
                    # Rule 5: If creation time is missing, malformed, or cannot be validated, do not accept
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

            # Stop early only when the API returns a page containing fewer than RECOVERY_PAGE_SIZE items
            if len(page_items) < RECOVERY_PAGE_SIZE:
                search_exhausted = True
                break
        else:
            # Reached MAX_PAGES (page 3) and it returned a full page of 50 items.
            # Search is incomplete because unexamined results may exist on subsequent pages.
            search_exhausted = False
            search_incomplete_reason = (
                f"page_ceiling_reached_{RECOVERY_MAX_PAGES}_pages_unexamined_results_may_exist"
            )

        if not search_exhausted or search_incomplete_reason:
            logger.warning(
                "Subscription recovery search for attempt %s was inconclusive/incomplete (%s). Preserving recovery_required.",
                attempt_id,
                search_incomplete_reason,
            )
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message=f"recovery_lookup_inconclusive_{search_incomplete_reason}",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Remote subscription lookup for attempt {attempt_id} was inconclusive or incomplete ({search_incomplete_reason}). "
                f"Reservation preserved in recovery_required to prevent duplicate creation."
            )

        if len(matching_candidates) == 0:
            # Inconclusive! Zero list results does NOT prove non-creation.
            logger.warning(
                "Remote subscription lookup for attempt %s returned 0 matching candidates. Inconclusive.",
                attempt_id,
            )
            # Update error_message to reflect inconclusive outcome, preserve recovery_required
            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message="recovery_lookup_inconclusive_no_candidates",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Remote subscription lookup for attempt {attempt_id} was inconclusive (no definitive candidates found). "
                f"Reservation preserved in recovery_required to prevent duplicate creation."
            )

        if len(matching_candidates) > 1:
            # Multiple candidates found! Must preserve ambiguity and record PaymentReconciliation
            candidate_ids = [str(c.get("id")) for c in matching_candidates if c.get("id")]
            logger.error(
                "Multiple (%d) candidate subscriptions found on Razorpay for attempt %s: %s",
                len(matching_candidates),
                attempt_id,
                candidate_ids,
            )
            try:
                reconciliation = PaymentReconciliation(
                    id=uuid.uuid4(),
                    provider="razorpay",
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                    organization_id=organization_id,
                    payment_id=None,
                    invoice_id=None,
                    raw_event_id=None,
                    reason="multiple_recovery_candidates_found",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={
                        "attempt_id": str(attempt_id),
                        "candidate_subscription_ids": candidate_ids,
                        "detected_at": now_utc.isoformat(),
                    },
                )
                db.add(reconciliation)
                await db.commit()
            except Exception as rec_exc:
                logger.warning("Failed to commit reconciliation case for multiple candidates: %s", rec_exc)

            await _record_attempt_outcome(
                db=db,
                attempt_id=attempt_id,
                status=CheckoutAttemptStatus.recovery_required,
                error_message=f"multiple_candidates_found: {','.join(candidate_ids)}",
            )
            raise RazorpayCheckoutAmbiguousError(
                f"Multiple matching remote subscriptions found on gateway for attempt {attempt_id}: {candidate_ids}. "
                f"Preserved in recovery_required; manual reconciliation case created."
            )

        # Exactly 1 candidate found
        candidate_sub_data = matching_candidates[0]
        target_sub_id = str(candidate_sub_data.get("id", "")).strip()
        if not target_sub_id:
            raise RazorpayCheckoutAmbiguousError(
                f"Recovery candidate found for attempt {attempt_id} had missing or empty subscription ID."
            )

    # -------------------------------------------------------------------------
    # Phase 3: Stale Worker Ownership Check & Persistence (Short Write Tx)
    # -------------------------------------------------------------------------
    stmt_lock = (
        select(BillingCheckoutAttempt)
        .where(BillingCheckoutAttempt.id == attempt_id)
        .with_for_update()
    )
    res_lock = await db.execute(stmt_lock)
    locked_attempt = res_lock.scalar_one_or_none()

    if locked_attempt is None:
        raise RazorpayCheckoutNotFoundError(f"Attempt {attempt_id} disappeared during recovery.")

    current_claim = locked_attempt.request_payload.get("recovery_claim_id") if isinstance(locked_attempt.request_payload, dict) else None
    if current_claim != recovery_claim_id:
        logger.warning(
            "Stale recovery worker detected on attempt %s: claim %s was superseded by %s",
            attempt_id,
            recovery_claim_id,
            current_claim,
        )
        if target_sub_id:
            # Preserve the recovered candidate in PaymentReconciliation so it is never lost
            try:
                reconciliation = PaymentReconciliation(
                    id=uuid.uuid4(),
                    provider="razorpay",
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                    organization_id=organization_id,
                    payment_id=None,
                    invoice_id=None,
                    raw_event_id=None,
                    reason="superseded_recovery_worker",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={
                        "attempt_id": str(attempt_id),
                        "recovered_subscription_id": target_sub_id,
                        "worker_claim_id": recovery_claim_id,
                        "superseded_by": current_claim,
                        "detected_at": now_utc.isoformat(),
                    },
                )
                db.add(reconciliation)
                await db.commit()
            except Exception as rec_exc:
                logger.warning("Failed to commit superseded recovery reconciliation: %s", rec_exc)

        raise RazorpayCheckoutConflictError(
            f"Recovery operation for attempt {attempt_id} was superseded by another worker."
        )

    # Valid worker ownership: persist recovered subscription
    short_url = candidate_sub_data.get("short_url") if isinstance(candidate_sub_data, dict) else None
    expire_by_ts = candidate_sub_data.get("expire_by") if isinstance(candidate_sub_data, dict) else None
    session_expires_at = _parse_timestamp(expire_by_ts) or (now_utc + timedelta(days=DEFAULT_SUBSCRIPTION_EXPIRY_DAYS))

    locked_attempt.provider_subscription_id = target_sub_id
    locked_attempt.provider_session_id = target_sub_id
    locked_attempt.checkout_url = short_url
    locked_attempt.session_expires_at = session_expires_at
    locked_attempt.status = CheckoutAttemptStatus.open
    locked_attempt.error_message = None

    payload_final = dict(locked_attempt.request_payload or {})
    payload_final["recovery_evidence"] = {
        "recovered_at": now_utc.isoformat(),
        "subscription_id": target_sub_id,
        "method": "known_id" if known_subscription_id else "filtered_remote_lookup",
        "claim_id": recovery_claim_id,
    }
    locked_attempt.request_payload = payload_final

    await db.commit()

    key_id = (settings.razorpay_key_id or "").strip()
    plan_id = (settings.razorpay_pro_plan_id or "").strip()

    return RazorpaySubscriptionCheckoutResult(
        attempt_id=attempt_id,
        organization_id=organization_id,
        provider="razorpay",
        subscription_id=target_sub_id,
        key_id=key_id,
        plan_id=plan_id,
        checkout_url=short_url,
        status=CheckoutAttemptStatus.open,
    )


# =============================================================================
# Checkout Callback Verification Service
# =============================================================================


def _extract_checkout_payment_binding(
    attempt: BillingCheckoutAttempt,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Extract authoritative payment binding from column and verification payload."""
    bound_payment_id = (
        attempt.provider_payment_id.strip()
        if isinstance(attempt.provider_payment_id, str) and attempt.provider_payment_id.strip()
        else None
    )
    verification_data = (
        attempt.request_payload.get("verification")
        if isinstance(attempt.request_payload, dict)
        else None
    )
    verified_payment_id = (
        str(verification_data["payment_id"]).strip()
        if isinstance(verification_data, dict)
        and verification_data.get("payment_id")
        and str(verification_data["payment_id"]).strip()
        else None
    )
    if bound_payment_id and verified_payment_id and bound_payment_id != verified_payment_id:
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt.id} has conflicting payment binding "
            f"('{bound_payment_id}' vs '{verified_payment_id}')."
        )
    associated_payment_id = bound_payment_id or verified_payment_id
    return (
        bound_payment_id,
        verification_data if isinstance(verification_data, dict) else None,
        associated_payment_id,
    )


def _is_attempt_session_expired(
    attempt: BillingCheckoutAttempt,
    now_utc: datetime,
) -> bool:
    """Return True when attempt is expired by status or open session deadline."""
    if attempt.status == CheckoutAttemptStatus.expired:
        return True
    if attempt.status == CheckoutAttemptStatus.open and attempt.session_expires_at is not None:
        expires_at = _parse_timestamp(attempt.session_expires_at)
        if expires_at is not None and now_utc >= expires_at:
            return True
    return False


def _validate_attempt_binding_attributes(
    attempt: BillingCheckoutAttempt,
    *,
    organization_id: uuid.UUID,
    merchant_account_id: str,
    environment: str,
    server_sub_id: str,
    pay_id: str,
) -> None:
    """Validate all composite identity attributes required before binding provider_payment_id."""
    if (
        attempt.organization_id != organization_id
        or (attempt.provider or "").strip().lower() != "razorpay"
        or (attempt.merchant_account_id or "").strip() != merchant_account_id
        or (attempt.environment or "").strip().lower() != environment
        or (attempt.provider_subscription_id or "").strip() != server_sub_id
        or not pay_id
    ):
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt.id} lacks consistent composite identity attributes for payment binding."
        )


def _apply_verified_payment_metadata(
    locked_attempt: BillingCheckoutAttempt,
    payment_data: dict[str, Any],
    verification_payload: dict[str, Any],
) -> None:
    """Apply validated payment metadata and verification payload onto locked attempt."""
    if payment_data.get("amount") is not None:
        try:
            amount_val = int(payment_data["amount"])
            if amount_val >= 0:
                locked_attempt.amount_minor_units = amount_val
        except (ValueError, TypeError):
            pass
    if payment_data.get("currency"):
        currency_code = str(payment_data["currency"]).strip().upper()
        if len(currency_code) == 3:
            locked_attempt.currency = currency_code

    payload_copy = dict(locked_attempt.request_payload or {})
    payload_copy["verification"] = verification_payload
    locked_attempt.request_payload = payload_copy


async def _commit_checkout_verification(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    operation: str,
) -> None:
    """Commit checkout verification changes atomically with explicit rollback on failure."""
    try:
        await db.commit()
    except IntegrityError as exc:
        try:
            await db.rollback()
        except Exception:
            logger.exception(
                "Rollback failed after IntegrityError during %s for attempt %s",
                operation,
                attempt_id,
            )
        logger.warning(
            "Database integrity conflict during %s for attempt %s: %s",
            operation,
            attempt_id,
            exc,
        )
        raise RazorpayCheckoutConflictError(
            f"Checkout verification conflict on attempt {attempt_id} during {operation}."
        ) from exc
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception(
                "Rollback failed after database error during %s for attempt %s",
                operation,
                attempt_id,
            )
        raise


async def verify_razorpay_checkout_callback(
    db: AsyncSession,
    organization_id: uuid.UUID,
    attempt_id: uuid.UUID,
    razorpay_payment_id: str,
    razorpay_subscription_id: str,
    razorpay_signature: str,
) -> RazorpayCheckoutVerificationResult:
    """Verify a Razorpay checkout callback, validate payment status, and bind payment ID.

    Requirements & Guarantees:
    1. Resolves expected subscription ID from the server-side checkout attempt.
    2. Never trusts browser-supplied subscription ID; rejects mismatches immediately.
    3. Cryptographic HMAC-SHA256 signature verification using constant-time comparison
       before idempotent replay responses or gateway calls.
    4. Retrieves payment status from Razorpay API outside database transactions.
    5. Validates payment association, merchant account, and environment.
    6. Atomically binds provider_payment_id alongside transition to 'complete' strictly for
       verified 'captured' payments; 'authorized' moves to 'payment_pending' without binding.
    7. Preserves expired attempts and terminal subscriptions (never resurrects; records
       PaymentReconciliation with deterministic case_key for delayed captures).
    8. Idempotent: repeated callbacks with matching payment ID return existing verified result.
    9. Entitlement Protection: NEVER marks subscription as Pro or provisions paid quota.
    """
    # -------------------------------------------------------------------------
    # 1. Input Sanitization & Configuration Validation
    # -------------------------------------------------------------------------
    pay_id = str(razorpay_payment_id).strip() if razorpay_payment_id else ""
    client_sub_id = str(razorpay_subscription_id).strip() if razorpay_subscription_id else ""
    sig = str(razorpay_signature).strip() if razorpay_signature else ""

    if not pay_id or not client_sub_id or not sig:
        raise RazorpayCheckoutSignatureError(
            "Callback data is incomplete. Missing payment_id, subscription_id, or signature."
        )

    merchant_account_id, environment = _get_provisioning_scope()
    key_secret = settings.razorpay_key_secret.get_secret_value().strip()
    if not key_secret:
        raise RazorpayConfigurationError("RAZORPAY_KEY_SECRET is not configured.")

    # -------------------------------------------------------------------------
    # 2. Server-Side Checkout Attempt Resolution
    # -------------------------------------------------------------------------
    stmt = (
        select(BillingCheckoutAttempt)
        .where(
            BillingCheckoutAttempt.id == attempt_id,
            BillingCheckoutAttempt.organization_id == organization_id,
            BillingCheckoutAttempt.provider == "razorpay",
            BillingCheckoutAttempt.merchant_account_id == merchant_account_id,
            BillingCheckoutAttempt.environment == environment,
        )
    )
    res = await db.execute(stmt)
    attempt = res.scalar_one_or_none()

    if attempt is None:
        raise RazorpayCheckoutNotFoundError(
            f"Checkout attempt {attempt_id} not found for organization {organization_id} in scope {environment}."
        )

    # -------------------------------------------------------------------------
    # 3. Server-Owned Subscription & Cryptographic Signature Verification
    # -------------------------------------------------------------------------
    server_sub_id = (attempt.provider_subscription_id or "").strip()
    if not server_sub_id:
        raise RazorpayCheckoutError(
            f"Checkout attempt {attempt_id} does not have an associated server-owned subscription ID."
        )

    if client_sub_id != server_sub_id:
        logger.warning(
            "Subscription ID mismatch on attempt %s: browser supplied '%s', server owns '%s'",
            attempt_id,
            client_sub_id,
            server_sub_id,
        )
        raise RazorpayCheckoutSignatureError(
            "Subscription ID mismatch: browser-supplied subscription ID does not match the server-owned checkout attempt."
        )

    is_valid_signature = verify_razorpay_subscription_signature(
        payment_id=pay_id,
        subscription_id=server_sub_id,
        signature=sig,
        secret=key_secret,
    )
    if not is_valid_signature:
        logger.warning(
            "Invalid Razorpay subscription signature for attempt %s, payment %s",
            attempt_id,
            pay_id,
        )
        raise RazorpayCheckoutSignatureError("Invalid checkout signature.")

    # -------------------------------------------------------------------------
    # 4. Pre-HTTP Binding, Lifecycle & Idempotent Replay Validation
    # -------------------------------------------------------------------------
    bound_payment_id, verification_data, associated_payment_id = _extract_checkout_payment_binding(attempt)

    if associated_payment_id and associated_payment_id != pay_id:
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is already associated with payment "
            f"'{associated_payment_id}', cannot verify different payment '{pay_id}'."
        )

    if attempt.status in (
        CheckoutAttemptStatus.failed,
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
    ):
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is in '{attempt.status.value}' state and cannot be verified."
        )

    now_utc = datetime.now(timezone.utc)
    is_expired_pre_http = _is_attempt_session_expired(attempt, now_utc)

    # Inspect local subscription state (read-only pre-check)
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == merchant_account_id,
            Subscription.environment == environment,
            Subscription.provider_subscription_id == server_sub_id,
        )
    )
    res_sub = await db.execute(stmt_sub)
    pre_sub = res_sub.scalar_one_or_none()

    if pre_sub is not None and pre_sub.organization_id != organization_id:
        raise RazorpayCheckoutConflictError(
            f"Subscription '{server_sub_id}' belongs to a different organization."
        )

    is_sub_terminal_pre_http = pre_sub is not None and is_terminal_subscription(pre_sub.status)

    # Idempotent replay when already completed and cleanly bound (or pre-bound captured verification)
    already_bound_and_complete = (
        attempt.status == CheckoutAttemptStatus.complete
        and (
            bound_payment_id == pay_id
            or (
                bound_payment_id is None
                and verification_data is not None
                and str(verification_data.get("payment_id", "")).strip() == pay_id
                and str(verification_data.get("payment_status", "")).strip().lower() == "captured"
            )
        )
    )
    if already_bound_and_complete:
        logger.info(
            "Idempotent replay of completed checkout verification for attempt %s (payment %s)",
            attempt_id,
            pay_id,
        )
        return RazorpayCheckoutVerificationResult(
            attempt_id=attempt.id,
            organization_id=organization_id,
            provider="razorpay",
            subscription_id=server_sub_id,
            payment_id=pay_id,
            status=CheckoutAttemptStatus.complete,
            payment_status=(
                str(verification_data.get("payment_status", "captured"))
                if verification_data is not None
                else "captured"
            ),
        )

    # -------------------------------------------------------------------------
    # 5. Retrieve Payment Status from Razorpay API (Outside Database Transactions)
    # -------------------------------------------------------------------------
    if db.in_transaction():
        await db.commit()

    client = get_razorpay_client()
    try:
        payment_data = await client.get_payment(pay_id)
    except RazorpayClientError as exc:
        logger.error(
            "Failed to retrieve payment %s from Razorpay API for attempt %s: %s",
            pay_id,
            attempt_id,
            exc,
        )
        if is_sub_terminal_pre_http and pre_sub is not None:
            pre_sub_status_label = (
                pre_sub.status.value
                if isinstance(pre_sub.status, SubscriptionStatus)
                else str(pre_sub.status)
            )
            raise RazorpayCheckoutConflictError(
                f"Subscription '{server_sub_id}' is in terminal '{pre_sub_status_label}' state and cannot complete checkout verification."
            ) from exc
        if is_expired_pre_http:
            stmt_exp = (
                select(BillingCheckoutAttempt)
                .where(
                    BillingCheckoutAttempt.id == attempt_id,
                    BillingCheckoutAttempt.organization_id == organization_id,
                    BillingCheckoutAttempt.provider == "razorpay",
                    BillingCheckoutAttempt.merchant_account_id == merchant_account_id,
                    BillingCheckoutAttempt.environment == environment,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            res_exp = await db.execute(stmt_exp)
            exp_attempt = res_exp.scalar_one_or_none()
            if exp_attempt is not None and exp_attempt.status == CheckoutAttemptStatus.open and _is_attempt_session_expired(exp_attempt, datetime.now(timezone.utc)):
                exp_attempt.status = CheckoutAttemptStatus.expired
                await _commit_checkout_verification(
                    db,
                    attempt_id=attempt_id,
                    operation="expired checkout status preservation",
                )
            elif db.in_transaction():
                await db.rollback()
            raise RazorpayCheckoutConflictError(
                f"Checkout attempt {attempt_id} has expired and cannot be verified."
            ) from exc
        raise RazorpayCheckoutPaymentError(
            f"Payment verification failed: could not retrieve payment details from Razorpay: {exc}"
        ) from exc

    if not isinstance(payment_data, dict) or payment_data.get("id") != pay_id:
        raise RazorpayCheckoutPaymentError("Payment verification failed: invalid payment data returned by gateway.")
    if "entity" in payment_data and str(payment_data.get("entity", "")).strip().lower() != "payment":
        raise RazorpayCheckoutPaymentError("Payment verification failed: invalid payment entity returned by gateway.")

    payment_status = str(payment_data.get("status", "")).strip().lower()

    # 5.1 Validate Payment-to-Subscription Binding (Fail Closed)
    payment_sub_id = payment_data.get("subscription_id")
    if not payment_sub_id or not str(payment_sub_id).strip():
        logger.error(
            "Payment %s has no associated subscription_id on Razorpay for attempt %s",
            pay_id,
            attempt_id,
        )
        raise RazorpayCheckoutPaymentError(
            "Payment verification failed: payment has no associated subscription_id."
        )

    if str(payment_sub_id).strip() != server_sub_id:
        logger.error(
            "Payment %s subscription mismatch: expected %s, got %s",
            pay_id,
            server_sub_id,
            payment_sub_id,
        )
        raise RazorpayCheckoutConflictError(
            f"Payment subscription ID '{payment_sub_id}' does not match expected subscription '{server_sub_id}'."
        )

    # 5.2 Validate Metadata, Merchant Account & Customer Association if Present
    payment_notes = payment_data.get("notes") or {}
    if isinstance(payment_notes, dict):
        noted_org_id = payment_notes.get("organization_id")
        if noted_org_id and str(noted_org_id).strip() != str(organization_id):
            logger.error(
                "Payment %s organization mismatch: expected %s, got %s",
                pay_id,
                organization_id,
                noted_org_id,
            )
            raise RazorpayCheckoutConflictError("Payment metadata organization mismatch.")

        noted_attempt_id = payment_notes.get("attempt_id")
        if noted_attempt_id and str(noted_attempt_id).strip() != str(attempt_id):
            logger.error(
                "Payment %s checkout attempt mismatch: expected %s, got %s",
                pay_id,
                attempt_id,
                noted_attempt_id,
            )
            raise RazorpayCheckoutConflictError("Payment metadata checkout attempt mismatch.")

    payment_account_id = payment_data.get("account_id")
    if (
        payment_account_id
        and merchant_account_id != "default"
        and str(payment_account_id).strip() != merchant_account_id
    ):
        logger.error(
            "Payment %s merchant account mismatch: expected %s, got %s",
            pay_id,
            merchant_account_id,
            payment_account_id,
        )
        raise RazorpayCheckoutConflictError("Payment merchant account mismatch.")

    payment_customer_id = payment_data.get("customer_id")
    if (
        payment_customer_id
        and attempt.provider_customer_id
        and str(payment_customer_id).strip() != attempt.provider_customer_id.strip()
    ):
        logger.error(
            "Payment %s customer mismatch: expected %s, got %s",
            pay_id,
            attempt.provider_customer_id,
            payment_customer_id,
        )
        raise RazorpayCheckoutConflictError("Payment customer mismatch.")

    # -------------------------------------------------------------------------
    # 6. Persist Verification Outcome Under Row Locks (Short Write Transaction)
    # -------------------------------------------------------------------------
    now_lock = datetime.now(timezone.utc)
    new_verification_payload: dict[str, Any] = {
        "payment_id": pay_id,
        "payment_status": payment_status,
        "amount": payment_data.get("amount"),
        "currency": payment_data.get("currency"),
        "method": payment_data.get("method"),
        "verified_at": now_lock.isoformat(),
    }

    # Acquire Subscription lock before BillingCheckoutAttempt lock to stay aligned with
    # webhook agreement handlers (agreement_handlers.py locks Subscription -> BillingCheckoutAttempt).
    stmt_sub_lock = (
        select(Subscription)
        .where(
            Subscription.provider == "razorpay",
            Subscription.merchant_account_id == merchant_account_id,
            Subscription.environment == environment,
            Subscription.provider_subscription_id == server_sub_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_sub_lock = await db.execute(stmt_sub_lock)
    locked_sub = res_sub_lock.scalar_one_or_none()

    if locked_sub is not None and locked_sub.organization_id != organization_id:
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutConflictError(
            f"Subscription '{server_sub_id}' belongs to a different organization."
        )

    # Re-fetch attempt with row lock + populate_existing=True to bypass identity-map staleness
    stmt_lock = (
        select(BillingCheckoutAttempt)
        .where(
            BillingCheckoutAttempt.id == attempt_id,
            BillingCheckoutAttempt.organization_id == organization_id,
            BillingCheckoutAttempt.provider == "razorpay",
            BillingCheckoutAttempt.merchant_account_id == merchant_account_id,
            BillingCheckoutAttempt.environment == environment,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    res_lock = await db.execute(stmt_lock)
    locked_attempt = res_lock.scalar_one_or_none()

    if locked_attempt is None:
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutNotFoundError(
            f"Checkout attempt {attempt_id} not found for organization {organization_id} in scope {environment}."
        )

    if (locked_attempt.provider_subscription_id or "").strip() != server_sub_id:
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} subscription binding changed concurrently."
        )

    try:
        locked_bound_pay_id, locked_verification_data, locked_associated_pay_id = _extract_checkout_payment_binding(locked_attempt)
    except RazorpayCheckoutConflictError:
        if db.in_transaction():
            await db.rollback()
        raise

    if locked_associated_pay_id and locked_associated_pay_id != pay_id:
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is already associated with payment "
            f"'{locked_associated_pay_id}', cannot verify different payment '{pay_id}'."
        )

    locked_already_bound_and_complete = (
        locked_attempt.status == CheckoutAttemptStatus.complete
        and (
            locked_bound_pay_id == pay_id
            or (
                locked_bound_pay_id is None
                and locked_verification_data is not None
                and str(locked_verification_data.get("payment_id", "")).strip() == pay_id
                and str(locked_verification_data.get("payment_status", "")).strip().lower() == "captured"
            )
        )
    )

    if locked_already_bound_and_complete:
        if db.in_transaction():
            await db.rollback()
        logger.info(
            "Attempt %s is already complete and bound to payment %s; ignoring stale status '%s'",
            attempt_id,
            pay_id,
            payment_status,
        )
        target_attempt_status = CheckoutAttemptStatus.complete
        result_payment_status = (
            str(locked_verification_data.get("payment_status", "captured"))
            if locked_verification_data is not None
            else "captured"
        )
    elif locked_sub is not None and is_terminal_subscription(locked_sub.status):
        logger.warning(
            "Refusing checkout verification for terminal subscription %s (status=%s, attempt=%s, payment=%s)",
            server_sub_id,
            locked_sub.status,
            attempt_id,
            pay_id,
        )
        if payment_status == "captured":
            await stage_or_update_payment_reconciliation(
                db=db,
                case_key=f"payment_for_terminal_subscription:{locked_sub.id}:{pay_id}",
                reason="payment_for_terminal_subscription",
                provider="razorpay",
                merchant_account_id=merchant_account_id,
                environment=environment,
                organization_id=organization_id,
                subscription_id=locked_sub.id,
                diagnostic_data={
                    "attempt_id": str(attempt_id),
                    "provider_payment_id": pay_id,
                    "provider_subscription_id": server_sub_id,
                    "subscription_status": (
                        locked_sub.status.value
                        if isinstance(locked_sub.status, SubscriptionStatus)
                        else str(locked_sub.status)
                    ),
                    "payment_status": payment_status,
                    "amount": payment_data.get("amount"),
                    "currency": payment_data.get("currency"),
                    "detected_at": now_lock.isoformat(),
                },
                now=now_lock,
            )
            await _commit_checkout_verification(
                db,
                attempt_id=attempt_id,
                operation="terminal subscription reconciliation",
            )
        elif db.in_transaction():
            await db.rollback()
        sub_status_label = (
            locked_sub.status.value
            if isinstance(locked_sub.status, SubscriptionStatus)
            else str(locked_sub.status)
        )
        raise RazorpayCheckoutConflictError(
            f"Subscription '{server_sub_id}' is in terminal '{sub_status_label}' state and cannot complete checkout verification."
        )
    elif _is_attempt_session_expired(locked_attempt, now_lock):
        state_changed = False
        if locked_attempt.status == CheckoutAttemptStatus.open:
            locked_attempt.status = CheckoutAttemptStatus.expired
            state_changed = True

        if payment_status == "captured":
            await stage_or_update_payment_reconciliation(
                db=db,
                case_key=f"delayed_capture_expired_checkout:{locked_attempt.id}:{pay_id}",
                reason="delayed_capture_after_checkout_expiration",
                provider="razorpay",
                merchant_account_id=merchant_account_id,
                environment=environment,
                organization_id=organization_id,
                subscription_id=locked_sub.id if locked_sub is not None else None,
                diagnostic_data={
                    "attempt_id": str(attempt_id),
                    "provider_payment_id": pay_id,
                    "provider_subscription_id": server_sub_id,
                    "payment_status": payment_status,
                    "amount": payment_data.get("amount"),
                    "currency": payment_data.get("currency"),
                    "session_expires_at": (
                        locked_attempt.session_expires_at.isoformat()
                        if locked_attempt.session_expires_at is not None
                        else None
                    ),
                    "detected_at": now_lock.isoformat(),
                },
                now=now_lock,
            )
            await _commit_checkout_verification(
                db,
                attempt_id=attempt_id,
                operation="expired checkout reconciliation",
            )
            raise RazorpayCheckoutConflictError(
                f"Checkout attempt {attempt_id} has expired; captured payment '{pay_id}' recorded for reconciliation."
            )

        if state_changed:
            await _commit_checkout_verification(
                db,
                attempt_id=attempt_id,
                operation="expired checkout transition",
            )
        elif db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} has expired and cannot be verified."
        )
    elif locked_attempt.status in (
        CheckoutAttemptStatus.failed,
        CheckoutAttemptStatus.creating,
        CheckoutAttemptStatus.recovery_required,
    ):
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutConflictError(
            f"Checkout attempt {attempt_id} is in '{locked_attempt.status.value}' state and cannot be verified."
        )
    elif payment_status == "failed":
        logger.warning(
            "Payment %s for attempt %s has failed status on Razorpay",
            pay_id,
            attempt_id,
        )
        if locked_attempt.status == CheckoutAttemptStatus.complete:
            if db.in_transaction():
                await db.rollback()
            raise RazorpayCheckoutPaymentError("Payment was marked as failed by the payment provider.")

        locked_attempt.status = CheckoutAttemptStatus.failed
        locked_attempt.error_message = f"payment_failed: {payment_data.get('error_description')}"
        await _commit_checkout_verification(
            db,
            attempt_id=attempt_id,
            operation="failed payment outcome",
        )
        raise RazorpayCheckoutPaymentError("Payment was marked as failed by the payment provider.")
    elif payment_status == "refunded":
        if db.in_transaction():
            await db.rollback()
        raise RazorpayCheckoutPaymentError("Payment has been refunded by the payment provider.")
    elif locked_attempt.status == CheckoutAttemptStatus.complete:
        # Attempt was already marked 'complete' (e.g., via subscription.activated webhook)
        # without an established provider_payment_id. Bind ONLY when verified 'captured'.
        target_attempt_status = CheckoutAttemptStatus.complete
        if payment_status == "captured":
            try:
                _validate_attempt_binding_attributes(
                    locked_attempt,
                    organization_id=organization_id,
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                    server_sub_id=server_sub_id,
                    pay_id=pay_id,
                )
            except RazorpayCheckoutConflictError:
                if db.in_transaction():
                    await db.rollback()
                raise
            locked_attempt.provider_payment_id = pay_id
            locked_attempt.error_message = None
            _apply_verified_payment_metadata(locked_attempt, payment_data, new_verification_payload)
            await _commit_checkout_verification(
                db,
                attempt_id=attempt_id,
                operation="complete payment binding backfill",
            )
            result_payment_status = "captured"
        else:
            if locked_verification_data is None:
                _apply_verified_payment_metadata(locked_attempt, payment_data, new_verification_payload)
                await _commit_checkout_verification(
                    db,
                    attempt_id=attempt_id,
                    operation="complete uncaptured verification metadata",
                )
            elif db.in_transaction():
                await db.rollback()
            result_payment_status = payment_status
    elif payment_status == "captured":
        try:
            _validate_attempt_binding_attributes(
                locked_attempt,
                organization_id=organization_id,
                merchant_account_id=merchant_account_id,
                environment=environment,
                server_sub_id=server_sub_id,
                pay_id=pay_id,
            )
        except RazorpayCheckoutConflictError:
            if db.in_transaction():
                await db.rollback()
            raise
        target_attempt_status = CheckoutAttemptStatus.complete
        locked_attempt.status = CheckoutAttemptStatus.complete
        locked_attempt.provider_payment_id = pay_id
        locked_attempt.error_message = None
        _apply_verified_payment_metadata(locked_attempt, payment_data, new_verification_payload)
        await _commit_checkout_verification(
            db,
            attempt_id=attempt_id,
            operation="captured payment binding",
        )
        result_payment_status = "captured"
    else:
        # Authorized or uncaptured pending payment: NEVER bind provider_payment_id
        target_attempt_status = CheckoutAttemptStatus.payment_pending
        locked_attempt.status = CheckoutAttemptStatus.payment_pending
        locked_attempt.error_message = None
        _apply_verified_payment_metadata(locked_attempt, payment_data, new_verification_payload)
        await _commit_checkout_verification(
            db,
            attempt_id=attempt_id,
            operation="pending payment verification",
        )
        result_payment_status = payment_status

    # -------------------------------------------------------------------------
    # 7. Entitlement Protection Invariant Assertion
    # -------------------------------------------------------------------------
    # VERIFICATION ALONE NEVER PROVISIONS PAID QUOTA.
    # Subscription.plan_tier is UNTOUCHED.
    # OrganizationBillingPeriod is UNTOUCHED.
    # Full-Payment Settlement policy remains the sole authority.

    return RazorpayCheckoutVerificationResult(
        attempt_id=attempt_id,
        organization_id=organization_id,
        provider="razorpay",
        subscription_id=server_sub_id,
        payment_id=pay_id,
        status=target_attempt_status,
        payment_status=result_payment_status,
    )


# =============================================================================
# Internal Persistence Helpers
# =============================================================================


async def _record_attempt_outcome(
    db: AsyncSession,
    attempt_id: uuid.UUID,
    status: CheckoutAttemptStatus,
    error_message: str | None = None,
) -> None:
    """Safely update attempt terminal or recovery state in a short database transaction."""
    try:
        stmt = (
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res = await db.execute(stmt)
        attempt = res.scalar_one_or_none()
        if attempt is not None:
            attempt.status = status
            attempt.error_message = error_message
            await db.commit()
        else:
            await _safe_rollback(db)
    except Exception as exc:
        logger.warning(
            "Failed to record checkout attempt %s outcome %s: %s",
            attempt_id,
            status,
            exc,
        )
        await _safe_rollback(db)


async def _record_attempt_success(
    db: AsyncSession,
    attempt_id: uuid.UUID,
    subscription_id: str,
    checkout_url: str | None,
    session_expires_at: datetime | None,
) -> None:
    """Record successful remote subscription creation on the checkout attempt."""
    try:
        stmt = (
            select(BillingCheckoutAttempt)
            .where(BillingCheckoutAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        res = await db.execute(stmt)
        attempt = res.scalar_one_or_none()
        if attempt is not None:
            attempt.provider_subscription_id = subscription_id
            attempt.provider_session_id = subscription_id
            attempt.checkout_url = checkout_url
            attempt.session_expires_at = session_expires_at
            attempt.status = CheckoutAttemptStatus.open
            attempt.error_message = None
            await db.commit()
        else:
            await _safe_rollback(db)
    except Exception:
        await _safe_rollback(db)
        raise
