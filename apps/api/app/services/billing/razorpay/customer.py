"""Domain service for organization-scoped Razorpay customer management.

Guarantees:
1. Idempotency & Reuse: Reuses existing customer associated with the organization matching
   (provider='razorpay', merchant_account_id, environment) in BillingCustomerProvision.
2. Complete Domain Decoupling: Customer provisioning operates strictly on BillingCustomerProvision.
   It NEVER creates, mutates, or demotes a Subscription row.
3. Provider Isolation: Never reuses a Stripe customer ID as a Razorpay customer ID.
4. Safe Metadata: Supplies only safe organization identification in notes (organization_id).
5. No Synthetic Identity: Never fabricates customer email addresses or contact numbers.
6. Concurrency Serialization: Establishes a durable pending provisioning claim before calling
   the gateway, preventing concurrent duplicate customer creation on Razorpay.
7. Operation Claim Ownership: Employs claim_operation_id to verify that stale workers
   cannot overwrite newer provisioning states.
8. Independent Lease Timing: Uses lease_expires_at, independent of entity updated_at triggers.
9. Transaction Boundary Safety: Never holds open database transactions or row locks across
   external HTTP network calls.
10. Rigorous HTTP Failure Classification:
    - Definite client rejections (400, 401, 404, 422) and pre-dispatch network errors
      safely clean up the claim so retry can proceed.
    - Ambiguous server errors (500, 502, 503, 504), post-dispatch read timeouts, connection resets,
      HTTP 408 request timeouts, and HTTP 429 rate limits transition the claim to ambiguous and
      prohibit automated blind retries.
    - HTTP 200/201 responses with missing, null, or malformed customer IDs transition the claim
      to ambiguous and never release it.
11. Database Persistence Failure Safety: If customer creation succeeds on Razorpay but local
    database commit fails, the customer ID is preserved in an ambiguous reconciliation state
    and the claim is never released.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.organization import Organization
from app.models.payment import (
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
    ReconciliationStatus,
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
    sanitize_razorpay_error,
)

logger = logging.getLogger(__name__)

PROVISIONING_LEASE_SECONDS = 30.0


class RazorpayCustomerError(Exception):
    """Raised when Razorpay customer management operations fail."""


class RazorpayAmbiguousNetworkError(RazorpayCustomerError):
    """Raised when network connectivity, timeouts, 5xx server responses, or malformed success
    leave customer provisioning in an ambiguous state (the customer may or may not have been
    created remotely). Blind retries are prohibited to prevent duplicate customer creation.
    """


class RazorpayCustomerProvisioningInProgressError(RazorpayCustomerError):
    """Raised when another worker is currently provisioning a customer for this organization."""


class RazorpayCustomerReconciliationRequiredError(RazorpayCustomerError):
    """Raised when customer provisioning is in an ambiguous or expired lease state
    and requires explicit reconciliation before a new create request can proceed.
    """


class RazorpayCustomerPersistenceError(RazorpayCustomerError):
    """Raised when customer creation succeeded on Razorpay, but local database persistence failed.

    Attributes:
        customer_id: The remote customer ID returned by Razorpay that could not be persisted.
    """

    def __init__(self, message: str, customer_id: str) -> None:
        super().__init__(message)
        self.customer_id = customer_id


def _get_provisioning_scope() -> tuple[str, str]:
    """Retrieve and strictly validate the Razorpay provisioning scope.

    Fails closed if merchant_account_id or environment is unconfigured or invalid.
    Guarantees:
    - merchant_account_id is explicitly non-empty and non-default in production.
    - environment is strictly either 'test' or 'live'.
    - Model server defaults ('default', 'live') can never override missing service configuration.
    """
    env = (settings.razorpay_environment or "").strip().lower()
    if env not in ("test", "live"):
        raise RazorpayConfigurationError(
            f"Invalid Razorpay environment '{settings.razorpay_environment}'. Must be 'test' or 'live'."
        )

    merchant_id = (settings.razorpay_merchant_account_id or "").strip()
    if not merchant_id:
        raise RazorpayConfigurationError(
            "Razorpay merchant account ID must be explicitly configured."
        )

    app_env = getattr(settings, "environment", "development").strip().lower()
    if app_env == "production":
        if merchant_id.lower() in ("default", "placeholder"):
            raise RazorpayConfigurationError(
                "Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured with a non-default account identifier."
            )
        if env != "live":
            raise RazorpayConfigurationError(
                "Production RAZORPAY_ENVIRONMENT must be 'live'."
            )
    else:
        if env != "test":
            raise RazorpayConfigurationError(
                "Non-production RAZORPAY_ENVIRONMENT must be 'test'."
            )

    return merchant_id, env


async def _record_superseded_customer_reconciliation(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    stale_claim_op_id: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a PaymentReconciliation entry for a customer created remotely by a superseded worker.

    Preserves the remote customer ID for audit and reconciliation without touching
    or corrupting the newer provisioning claim.
    """
    merchant_account_id, environment = _get_provisioning_scope()
    case_key = f"customer_provision:superseded:{stale_claim_op_id}"

    diagnostic_payload: dict[str, Any] = {
        "provider_customer_id": customer_id,
        "stale_claim_operation_id": stale_claim_op_id,
        "reason": reason,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    if details:
        diagnostic_payload.update(details)

    reconciliation = PaymentReconciliation(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        case_key=case_key,
        organization_id=organization_id,
        subscription_id=None,
        invoice_id=None,
        payment_id=None,
        raw_event_id=None,
        reason=reason,
        status=ReconciliationStatus.pending_review,
        diagnostic_payload=diagnostic_payload,
    )
    db.add(reconciliation)
    try:
        await db.commit()
        logger.info(
            "Created PaymentReconciliation record %s for superseded customer %s (claim: %s)",
            case_key,
            customer_id,
            stale_claim_op_id,
        )
    except Exception as recon_err:
        logger.error(
            "Failed to persist PaymentReconciliation for superseded customer %s on org %s: %s",
            customer_id,
            organization_id,
            recon_err,
        )
        try:
            await db.rollback()
        except Exception:
            pass


async def _find_provision(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> BillingCustomerProvision | None:
    """Retrieve the customer provisioning record scoped to Razorpay and current environment."""
    merchant_account_id, environment = _get_provisioning_scope()
    stmt = (
        select(BillingCustomerProvision)
        .where(
            BillingCustomerProvision.organization_id == organization_id,
            BillingCustomerProvision.provider == "razorpay",
            BillingCustomerProvision.merchant_account_id == merchant_account_id,
            BillingCustomerProvision.environment == environment,
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_razorpay_customer_id(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> str | None:
    """Retrieve the verified Razorpay customer ID associated with an organization in the current environment.

    Only returns resolved customer IDs. In-progress or ambiguous states return None.

    Args:
        db: Database async session.
        organization_id: UUID of the organization.

    Returns:
        The provider_customer_id string if exists and verified, otherwise None.
    """
    provision = await _find_provision(db, organization_id)
    if (
        provision is not None
        and provision.status == CustomerProvisionStatus.resolved
        and provision.provider_customer_id
        and isinstance(provision.provider_customer_id, str)
        and provision.provider_customer_id.strip()
    ):
        return provision.provider_customer_id.strip()
    return None


async def _cleanup_claim(
    db: AsyncSession,
    organization_id: uuid.UUID,
    claim_op_id: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Clean up a pending claim if customer creation definitively failed remotely.

    Only cleans up if the claim is still owned by claim_op_id.
    Transitions the provision record to 'failed' to maintain audit integrity.
    """
    try:
        provision = await _find_provision(db, organization_id)
        if provision is not None and provision.status == CustomerProvisionStatus.pending:
            if claim_op_id is not None and provision.claim_operation_id != claim_op_id:
                logger.warning(
                    "Skipping claim cleanup for organization %s: claim not owned by operation (%s != %s)",
                    organization_id,
                    provision.claim_operation_id,
                    claim_op_id,
                )
                return

            provision.status = CustomerProvisionStatus.failed
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = last_error_code
            await db.commit()
    except Exception as cleanup_err:
        logger.warning(
            "Failed to clean up pending claim for organization %s: %s",
            organization_id,
            cleanup_err,
        )
        try:
            await db.rollback()
        except Exception:
            pass


async def _record_ambiguous_claim(
    db: AsyncSession,
    organization_id: uuid.UUID,
    claim_op_id: str | None = None,
    customer_id: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Transition a claim to ambiguous state after an uncertain remote outcome.

    Preserves customer_id in provider_customer_id if known.
    """
    try:
        provision = await _find_provision(db, organization_id)
        if provision is not None:
            if provision.status == CustomerProvisionStatus.resolved:
                logger.warning(
                    "Skipping ambiguous transition for organization %s: already resolved to %s",
                    organization_id,
                    provision.provider_customer_id,
                )
                return

            if claim_op_id is not None and provision.claim_operation_id != claim_op_id:
                logger.warning(
                    "Skipping ambiguous transition for organization %s: claim not owned by operation (%s != %s)",
                    organization_id,
                    provision.claim_operation_id,
                    claim_op_id,
                )
                return

            provision.status = CustomerProvisionStatus.ambiguous
            if customer_id and isinstance(customer_id, str):
                provision.provider_customer_id = customer_id.strip()
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = last_error_code
            await db.commit()
    except Exception as amb_err:
        logger.warning(
            "Failed to record ambiguous claim for organization %s: %s",
            organization_id,
            amb_err,
        )
        try:
            await db.rollback()
        except Exception:
            pass


async def _handle_persistence_failure(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
) -> None:
    """Handle database failure after successful remote customer creation on Razorpay.

    Guarantees:
    1. NEVER releases the provisioning claim (customer was created remotely).
    2. If claim is still owned by claim_op_id, preserves returned customer_id in provider_customer_id with ambiguous status.
    3. If claim ownership was lost, preserves customer_id in PaymentReconciliation without modifying provision.
    4. Logs error with diagnostic context (customer_id, org_id) without leaking credentials.
    5. Raises RazorpayCustomerPersistenceError so callers know customer exists on Razorpay.
    """
    logger.error(
        "Database failure persisting resolved Razorpay customer %s for organization %s: %s",
        customer_id,
        organization_id,
        original_error,
    )
    try:
        provision = await _find_provision(db, organization_id)
        if provision is not None and provision.claim_operation_id == claim_op_id:
            provision.status = CustomerProvisionStatus.ambiguous
            provision.provider_customer_id = customer_id
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = "db_persistence_failure"
            await db.commit()
        else:
            # Stale worker during persistence failure: claim was lost or superseded.
            # Preserve customer_id in PaymentReconciliation without modifying provision.
            await _record_superseded_customer_reconciliation(
                db=db,
                organization_id=organization_id,
                customer_id=customer_id,
                stale_claim_op_id=claim_op_id,
                reason="persistence_failure_stale_claim",
                details={"error": str(original_error)},
            )
    except Exception as fallback_err:
        logger.error(
            "Secondary failure attempting to record ambiguous state for organization %s with customer %s: %s. "
            "Pending claim left intact for lease-based recovery.",
            organization_id,
            customer_id,
            fallback_err,
        )
        try:
            await db.rollback()
        except Exception:
            pass

    raise RazorpayCustomerPersistenceError(
        f"Customer {customer_id} was created on Razorpay, but local database persistence failed. "
        f"Organization {organization_id} is in an ambiguous state requiring reconciliation: {original_error}",
        customer_id=customer_id,
    ) from original_error


async def get_or_create_razorpay_customer(
    db: AsyncSession,
    organization: Organization,
    user_email: str | None = None,
) -> str:
    """Obtain or create a Razorpay customer for an organization.

    Lifecycle Invariants:
    1. Dedicated Storage: Uses BillingCustomerProvision exclusively. Never creates, modifies,
       or demotes a Subscription row.
    2. Idempotency: If a verified customer association exists, returns it immediately without calling Razorpay.
    3. Claim Serialization: Serializes concurrent requests by committing a durable pending claim with
       claim_operation_id and lease_expires_at before invoking Razorpay.
    4. Transaction Isolation: Releases all database transactions before issuing external HTTP calls.
    5. Robust HTTP Failure Classification:
       - Definite client errors (400, 401, 404, 422) and pre-dispatch network errors clean up the claim.
       - Server 5xx errors, HTTP 408 request timeouts, HTTP 429 rate limits, and post-dispatch
         network errors persist an 'ambiguous' state and prohibit automated blind retries.
       - Malformed success (200/201 with missing/null ID) transitions to ambiguous and never releases the claim.
    6. Persistence Safety: Protects successful gateway responses against database failure by recording
       the returned customer ID in an ambiguous state and never releasing the claim.
    7. Ownership Verification: Verifies claim ownership before finalizing so stale workers cannot overwrite newer states.

    Args:
        db: Database async session.
        organization: Organization entity.
        user_email: Optional email of the initiating authenticated user.

    Returns:
        The verified Razorpay customer ID (e.g. 'cust_xxx').

    Raises:
        RazorpayBillingDisabledError: If Razorpay billing is disabled in settings.
        RazorpayConfigurationError: If Razorpay configuration is incomplete.
        RazorpayCustomerProvisioningInProgressError: If provisioning is currently underway.
        RazorpayCustomerReconciliationRequiredError: If state is ambiguous or lease expired.
        RazorpayAmbiguousNetworkError: If HTTP request timed out, reset, returned 5xx/408/429, or was malformed.
        RazorpayCustomerPersistenceError: If remote call succeeded but DB persistence failed.
        RazorpayCustomerError: If API call definitively fails or returns invalid response.
    """
    # -------------------------------------------------------------------------
    # Phase 1: Database Inspection (Short Read Transaction)
    # -------------------------------------------------------------------------
    provision = await _find_provision(db, organization.id)

    if provision is not None:
        # Case 1.1: Resolved customer ID already exists
        if provision.status == CustomerProvisionStatus.resolved:
            if provision.provider_customer_id and provision.provider_customer_id.strip():
                return provision.provider_customer_id.strip()

        # Case 1.2: In-progress provisioning claim
        if provision.status == CustomerProvisionStatus.pending:
            now_utc = datetime.now(timezone.utc)
            lease_expires = provision.lease_expires_at
            if lease_expires is not None:
                if lease_expires.tzinfo is None:
                    lease_expires = lease_expires.replace(tzinfo=timezone.utc)
                if now_utc < lease_expires:
                    raise RazorpayCustomerProvisioningInProgressError(
                        f"Customer provisioning in progress for organization {organization.id} (lease active)."
                    )

            # Lease expired: Remote call may have succeeded while worker crashed.
            # Do NOT assume remote request failed; mark ambiguous to require reconciliation.
            provision.status = CustomerProvisionStatus.ambiguous
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = "lease_expired"
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            raise RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning lease expired for organization {organization.id}; "
                f"remote state is ambiguous and requires reconciliation before retry."
            )

        # Case 1.3: Ambiguous outcome recorded from previous failure
        if provision.status == CustomerProvisionStatus.ambiguous:
            raise RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning for organization {organization.id} is in an ambiguous state. "
                f"Reconciliation required before retry."
            )

    # -------------------------------------------------------------------------
    # Pre-HTTP Gate: Check billing enabled status & client configuration
    # -------------------------------------------------------------------------
    if not is_razorpay_enabled():
        raise RazorpayBillingDisabledError(
            "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
        )

    client = get_razorpay_client()

    # -------------------------------------------------------------------------
    # Phase 2: Claim Provisioning (Short Write Transaction)
    # -------------------------------------------------------------------------
    merchant_account_id, environment = _get_provisioning_scope()
    claim_op_id = uuid.uuid4().hex[:12]
    now_utc = datetime.now(timezone.utc)
    lease_expires = now_utc + timedelta(seconds=PROVISIONING_LEASE_SECONDS)

    if provision is not None:
        provision.status = CustomerProvisionStatus.pending
        provision.claim_operation_id = claim_op_id
        provision.lease_expires_at = lease_expires
        provision.last_error_code = None
    else:
        provision = BillingCustomerProvision(
            id=uuid.uuid4(),
            organization_id=organization.id,
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            provider_customer_id=None,
            status=CustomerProvisionStatus.pending,
            claim_operation_id=claim_op_id,
            lease_expires_at=lease_expires,
            last_error_code=None,
        )
        db.add(provision)

    try:
        await db.commit()
    except IntegrityError:
        # Concurrent worker established claim or resolved customer
        await db.rollback()
        reloaded = await _find_provision(db, organization.id)
        if (
            reloaded is not None
            and reloaded.status == CustomerProvisionStatus.resolved
            and reloaded.provider_customer_id
        ):
            return reloaded.provider_customer_id
        raise RazorpayCustomerProvisioningInProgressError(
            f"Customer provisioning claim conflict for organization {organization.id}."
        )

    # -------------------------------------------------------------------------
    # Phase 3: External Gateway Invocation (Strictly Outside DB Transaction)
    # -------------------------------------------------------------------------
    params: dict[str, Any] = {
        "name": organization.name.strip() if organization.name else str(organization.id),
        "email": user_email.strip() if user_email and user_email.strip() else None,
        "notes": {
            "organization_id": str(organization.id),
        },
        "fail_existing": 0,
    }

    try:
        customer = await client.create_customer(**params)
    except RazorpayAuthError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Authentication error creating Razorpay customer for org %s: %s", organization.id, sanitized_msg)
        await _cleanup_claim(db, organization.id, claim_op_id, last_error_code="401")
        raise RazorpayCustomerError(f"Razorpay authentication error: {sanitized_msg}") from exc
    except RazorpayRateLimitError as exc:
        # HTTP 429: Reassessed conservatively. Do NOT release claim.
        # Prefer preserving the claim in ambiguous state for controlled recovery over unsafe immediate retry.
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Rate limit creating Razorpay customer for organization %s: %s", organization.id, sanitized_msg)
        await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code="429")
        raise RazorpayAmbiguousNetworkError(
            f"Razorpay rate limit exceeded (HTTP 429) for organization {organization.id}; "
            f"claim preserved in ambiguous state for controlled recovery: {sanitized_msg}"
        ) from exc
    except RazorpayServer5xxError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Server 5xx error creating Razorpay customer for org %s (status=%s): %s", organization.id, exc.status_code, sanitized_msg)
        await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code=str(exc.status_code))
        raise RazorpayAmbiguousNetworkError(
            f"Ambiguous server error (HTTP {exc.status_code}) while creating Razorpay customer for organization {organization.id}. "
            f"Customer may exist remotely; automated retry is prohibited to prevent duplicate customers. "
            f"Reconciliation required: {sanitized_msg}"
        ) from exc
    except RazorpayAPIError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        if exc.status_code == 408:
            # HTTP 408 Request Timeout: Ambiguous outcome!
            logger.error("HTTP 408 timeout creating Razorpay customer for organization %s: %s", organization.id, sanitized_msg)
            await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code="408")
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous HTTP 408 request timeout from Razorpay for organization {organization.id}. "
                f"Customer may exist remotely; automated retry is prohibited: {sanitized_msg}"
            ) from exc
        elif exc.status_code is not None and exc.status_code < 500:
            # Definite 4xx client rejection (400 bad request, 404, 422).
            # Razorpay rejected the request synchronously before creating customer.
            logger.error("Client error creating Razorpay customer for organization %s (status=%s): %s", organization.id, exc.status_code, sanitized_msg)
            await _cleanup_claim(db, organization.id, claim_op_id, last_error_code=str(exc.status_code))
            raise RazorpayCustomerError(f"Razorpay customer creation failed: {sanitized_msg}") from exc
        else:
            # Uncertain status code or server error: treat as ambiguous
            logger.error("Ambiguous error creating Razorpay customer for organization %s (status=%s): %s", organization.id, exc.status_code, sanitized_msg)
            await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code=str(exc.status_code))
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous error communicating with Razorpay for organization {organization.id}: {sanitized_msg}"
            ) from exc
    except RazorpayNetworkError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        if exc.is_pre_dispatch:
            # Demonstrably failed before request reached Razorpay (ConnectTimeout, ConnectError, PoolTimeout).
            # Customer definitely not created. Safe to clean claim.
            logger.error("Pre-dispatch network error for organization %s: %s", organization.id, sanitized_msg)
            await _cleanup_claim(db, organization.id, claim_op_id, last_error_code="pre_dispatch_network_error")
            raise RazorpayCustomerError(f"Razorpay connection failed before dispatch: {sanitized_msg}") from exc
        else:
            # Post-dispatch or uncertain (ReadTimeout, connection reset, RemoteProtocolError).
            # AMBIGUOUS! Customer may exist remotely. Prohibit blind retry.
            logger.error("Ambiguous post-dispatch network error for organization %s: %s", organization.id, sanitized_msg)
            await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code="post_dispatch_network_error")
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous network timeout/reset while creating Razorpay customer for organization {organization.id}. "
                f"Customer may exist remotely; automated retry is prohibited to prevent duplicate customers. "
                f"Reconciliation required: {sanitized_msg}"
            ) from exc
    except Exception as exc:
        # Unexpected exception during HTTP call: conservatively treat as ambiguous.
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Unexpected error creating Razorpay customer for organization %s: %s", organization.id, sanitized_msg)
        await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code="unexpected_exception")
        raise RazorpayAmbiguousNetworkError(
            f"Unexpected error communicating with Razorpay for organization {organization.id} (ambiguous): {sanitized_msg}"
        ) from exc

    # -------------------------------------------------------------------------
    # Phase 4: Resolution & Persistence (Short Write Transaction)
    # -------------------------------------------------------------------------
    customer_id = customer.get("id") if isinstance(customer, dict) else None
    if not customer_id or not isinstance(customer_id, str) or not customer_id.strip():
        # Task 3 Requirement 1: HTTP 200/201 with missing/null ID must transition to ambiguous, NOT cleanup!
        logger.error("Razorpay returned HTTP success with invalid/missing customer ID for organization %s", organization.id)
        await _record_ambiguous_claim(db, organization.id, claim_op_id, last_error_code="malformed_success")
        raise RazorpayAmbiguousNetworkError(
            f"Razorpay customer creation returned HTTP success but missing or malformed customer ID for organization {organization.id}; "
            f"marked ambiguous to prevent duplicate customer creation."
        )

    customer_id = customer_id.strip()

    target_provision = await _find_provision(db, organization.id)
    if target_provision is None or target_provision.claim_operation_id != claim_op_id:
        logger.warning(
            "Stale worker detected for organization %s: claim operation %s superseded or lost ownership "
            "(persisted claim: %s). Preserving remote customer %s in PaymentReconciliation.",
            organization.id,
            claim_op_id,
            target_provision.claim_operation_id if target_provision else None,
            customer_id,
        )
        await _record_superseded_customer_reconciliation(
            db=db,
            organization_id=organization.id,
            customer_id=customer_id,
            stale_claim_op_id=claim_op_id,
            reason="stale_worker_customer_created",
            details={
                "persisted_claim_op_id": target_provision.claim_operation_id if target_provision else None,
                "persisted_status": target_provision.status.value if target_provision else None,
            },
        )
        raise RazorpayCustomerError(
            f"Customer provisioning claim {claim_op_id} for organization {organization.id} was superseded "
            f"or lost ownership before remote response completed. Remote customer {customer_id} preserved in reconciliation."
        )

    target_provision.provider_customer_id = customer_id
    target_provision.status = CustomerProvisionStatus.resolved
    target_provision.claim_operation_id = None
    target_provision.lease_expires_at = None
    target_provision.last_error_code = None

    try:
        await db.commit()
        return customer_id
    except IntegrityError as exc:
        await db.rollback()
        reloaded_id = await get_razorpay_customer_id(db, organization.id)
        if reloaded_id:
            return reloaded_id
        await _handle_persistence_failure(db, organization.id, customer_id, claim_op_id, exc)
    except Exception as exc:
        await db.rollback()
        await _handle_persistence_failure(db, organization.id, customer_id, claim_op_id, exc)
