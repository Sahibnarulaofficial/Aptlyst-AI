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

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from typing import Any, NoReturn
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionFactory, AsyncSessionLocal
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
from app.services.billing.razorpay.handler_base import (
    stage_or_update_payment_reconciliation,
)

logger = logging.getLogger(__name__)

PROVISIONING_LEASE_SECONDS = 30.0
PROVIDER_CUSTOMER_UNIQUE_INDEX = "uq_billing_customer_provisions_provider_customer"
CORRUPTED_RESOLVED_ERROR_CODE = "resolved_missing_customer_id"


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

    Attributes:
        original_error: Primary error when reconciliation persistence failed during recovery.
        recovery_error: Secondary error if fallback recovery transition also failed.
        fallback_committed: True only when fallback ambiguous commit completed cleanly;
            False when skipped due to concurrent changes or unconfirmed/failed commit;
            None when fallback recovery was not executed.
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: Exception | None = None,
        recovery_error: Exception | None = None,
        fallback_committed: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.original_error = original_error
        self.recovery_error = recovery_error
        self.fallback_committed = fallback_committed


class RazorpayCustomerPersistenceError(RazorpayCustomerError):
    """Raised when customer creation succeeded on Razorpay, but local database persistence failed.

    Attributes:
        customer_id: The remote customer ID returned by Razorpay that could not be persisted.
        original_error: Underlying primary database exception that blocked persistence.
        recovery_error: Secondary exception raised if durable recovery/reconciliation also failed.
    """

    def __init__(
        self,
        message: str,
        customer_id: str,
        *,
        original_error: Exception | None = None,
        recovery_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.customer_id = customer_id
        self.original_error = original_error
        self.recovery_error = recovery_error


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


async def _safe_rollback(db: AsyncSession) -> None:
    """Safely rollback any open database transaction without masking caller errors."""
    try:
        await db.rollback()
    except Exception:
        pass


async def _lock_organization(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> Organization | None:
    """Acquire a FOR UPDATE lock on the parent Organization in populate_existing mode."""
    stmt = (
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _record_superseded_customer_reconciliation(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    stale_claim_op_id: str,
    reason: str,
    details: dict[str, Any] | None = None,
    *,
    organization_exists: bool = True,
) -> None:
    """Record a PaymentReconciliation entry for a customer created remotely by a superseded worker.

    Preserves the remote customer ID for audit and reconciliation without touching
    or corrupting the newer provisioning claim.
    """
    merchant_account_id, environment = _get_provisioning_scope()
    case_key = f"customer_provision:superseded:{stale_claim_op_id}"

    diagnostic_payload: dict[str, Any] = {
        "organization_id": str(organization_id),
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
        organization_id=organization_id if organization_exists else None,
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
        await _safe_rollback(db)


async def _find_provision(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    for_update: bool = False,
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
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _build_reconciliation_case_key(prefix: str, *parts: str) -> str:
    """Build a deterministic <=255-char PaymentReconciliation case_key."""
    raw = ":".join([prefix, *parts])
    if len(raw) <= 255:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    head = raw[: 255 - 1 - len(digest)]
    return f"{head}:{digest}"


_DB_URI_CREDENTIAL_RE = re.compile(
    r"(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:/@\s]+:)[^@\s]+(@)",
    re.IGNORECASE,
)
_DB_KV_PASSWORD_RE = re.compile(
    r"(\bpassword\s*=\s*)([^\s,;'\"]+)",
    re.IGNORECASE,
)


def _safe_getattr(obj: Any, attr_name: str) -> Any:
    """Safely read an attribute without raising on broken properties or proxies."""
    if obj is None:
        return None
    try:
        return getattr(obj, attr_name, None)
    except Exception:
        return None


def _coerce_diagnostic_str(value: Any) -> str | None:
    """Return a trimmed non-empty string only when value is a genuine string."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _safe_exception_str(obj: Any) -> str:
    """Safely format an exception or object as text."""
    try:
        return str(obj)
    except Exception:
        return type(obj).__name__


def _sanitize_db_diagnostic_text(text: str | None) -> str | None:
    """Redact credentials, connection URL passwords, and gateway secrets from diagnostic text."""
    if text is None:
        return None
    sanitized = sanitize_razorpay_error(text)
    sanitized = _DB_URI_CREDENTIAL_RE.sub(r"\1[REDACTED]\2", sanitized)
    sanitized = _DB_KV_PASSWORD_RE.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def _iter_db_exception_chain(exc: Exception, *, max_nodes: int = 32) -> list[Any]:
    """Traverse SQLAlchemy/DBAPI/asyncpg exception chains breadth-first without cycles."""
    chain: list[Any] = []
    visited: set[int] = set()
    queue: deque[Any] = deque()

    orig = _safe_getattr(exc, "orig")
    if orig is not None:
        queue.append(orig)
    queue.append(exc)

    while queue and len(chain) < max_nodes:
        current = queue.popleft()
        if current is None:
            continue
        node_id = id(current)
        if node_id in visited:
            continue
        visited.add(node_id)
        chain.append(current)

        nested_orig = _safe_getattr(current, "orig")
        if nested_orig is not None and id(nested_orig) not in visited:
            queue.append(nested_orig)
        cause = _safe_getattr(current, "__cause__")
        if cause is not None and id(cause) not in visited:
            queue.append(cause)
        context = _safe_getattr(current, "__context__")
        if context is not None and id(context) not in visited:
            queue.append(context)

    return chain


def _extract_db_error_details(exc: Exception) -> dict[str, Any]:
    """Extract non-secret diagnostic details from SQLAlchemy/DBAPI/asyncpg exception chains."""
    orig = _safe_getattr(exc, "orig")
    pgcode: str | None = None
    constraint_name: str | None = None
    detail: str | None = None

    for node in _iter_db_exception_chain(exc):
        diag = _safe_getattr(node, "diag")

        if pgcode is None:
            for candidate in (
                _safe_getattr(node, "pgcode"),
                _safe_getattr(node, "sqlstate"),
                _safe_getattr(diag, "sqlstate"),
                _safe_getattr(diag, "pgcode"),
            ):
                pgcode = _coerce_diagnostic_str(candidate)
                if pgcode is not None:
                    break

        if constraint_name is None:
            for candidate in (
                _safe_getattr(node, "constraint_name"),
                _safe_getattr(diag, "constraint_name"),
            ):
                constraint_name = _coerce_diagnostic_str(candidate)
                if constraint_name is not None:
                    break

        if detail is None:
            for candidate in (
                _safe_getattr(node, "detail"),
                _safe_getattr(node, "message_detail"),
                _safe_getattr(diag, "message_detail"),
                _safe_getattr(diag, "detail"),
            ):
                detail = _coerce_diagnostic_str(candidate)
                if detail is not None:
                    break

        if pgcode is not None and constraint_name is not None and detail is not None:
            break

    return {
        "error_type": type(exc).__name__,
        "error_message": _sanitize_db_diagnostic_text(_safe_exception_str(exc)),
        "orig_error": _sanitize_db_diagnostic_text(_safe_exception_str(orig)) if orig is not None else None,
        "pgcode": pgcode,
        "constraint_name": constraint_name,
        "detail": _sanitize_db_diagnostic_text(detail),
    }


def _is_provider_customer_collision_error(exc: Exception) -> bool:
    """Determine whether an IntegrityError references uq_billing_customer_provisions_provider_customer."""
    if not isinstance(exc, IntegrityError):
        return False
    details = _extract_db_error_details(exc)
    if details["constraint_name"] == PROVIDER_CUSTOMER_UNIQUE_INDEX:
        return True
    combined = " ".join(
        part
        for part in (details["error_message"], details["orig_error"], details["detail"])
        if part
    ).lower()
    return PROVIDER_CUSTOMER_UNIQUE_INDEX in combined


def _classify_invalid_resolved_customer_id(raw_customer_id: Any) -> str:
    """Classify malformed provider_customer_id values stored on a resolved provision."""
    if raw_customer_id is None:
        return "null_customer_id"
    if isinstance(raw_customer_id, str):
        if raw_customer_id == "":
            return "empty_customer_id"
        return "whitespace_customer_id"
    return "invalid_customer_id_type"


async def _find_conflicting_customer_owner(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
) -> BillingCustomerProvision | None:
    """Detect whether provider_customer_id is already owned by another organization in scope."""
    merchant_account_id, environment = _get_provisioning_scope()
    stmt = (
        select(BillingCustomerProvision)
        .where(
            BillingCustomerProvision.provider == "razorpay",
            BillingCustomerProvision.merchant_account_id == merchant_account_id,
            BillingCustomerProvision.environment == environment,
            BillingCustomerProvision.provider_customer_id == customer_id,
            BillingCustomerProvision.organization_id != organization_id,
        )
        .limit(1)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None and row.organization_id == organization_id:
        return None
    return row


def _raise_with_preserved_chain(
    error_to_raise: Exception,
    *,
    cause: Exception | None,
    context: Exception | None = None,
) -> NoReturn:
    """Raise an exception with explicit __cause__ and __context__ across independent sessions."""
    if context is not None and context is not cause:
        try:
            raise context
        except Exception:
            if cause is not None:
                raise error_to_raise from cause
            raise error_to_raise
    if cause is not None:
        raise error_to_raise from cause
    raise error_to_raise


@dataclass(frozen=True)
class _CorruptedResolvedFallbackResult:
    """Outcome of the fresh independent D-1 fallback recovery transaction."""

    outcome: str
    error: Exception | None = None
    observed_status: CustomerProvisionStatus | None = None


@dataclass(frozen=True)
class _CorruptedResolvedPrimaryResult:
    """Outcome of the primary TX attempt to fail-closed a corrupted resolved mapping."""

    succeeded: bool
    case_key: str
    expected_provision_id: uuid.UUID
    anomaly_kind: str
    persist_error: Exception | None = None


async def _mark_corrupted_resolved_ambiguous_after_reconciliation_failure(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    expected_provision_id: uuid.UUID,
    diagnostic_code: str,
) -> _CorruptedResolvedFallbackResult:
    """Persist ambiguous fallback in a fresh locked TX after D-1 reconciliation failure."""
    try:
        locked_org = await _lock_organization(db, organization_id)
        if locked_org is None:
            await _safe_rollback(db)
            logger.error(
                "Organization %s became unavailable before D-1 fallback recovery for provision %s.",
                organization_id,
                expected_provision_id,
            )
            return _CorruptedResolvedFallbackResult(outcome="org_unavailable")

        fresh_provision = await _find_provision(db, organization_id, for_update=True)
        if fresh_provision is None:
            await _safe_rollback(db)
            logger.error(
                "BillingCustomerProvision %s not found for organization %s during D-1 fallback recovery.",
                expected_provision_id,
                organization_id,
            )
            return _CorruptedResolvedFallbackResult(outcome="provision_missing")

        # Capture scalar state before any rollback can expire fresh_provision on an AsyncSession.
        observed_provision_id = fresh_provision.id
        observed_status = fresh_provision.status
        observed_raw_id = fresh_provision.provider_customer_id

        if observed_provision_id != expected_provision_id:
            await _safe_rollback(db)
            logger.warning(
                "Skipping D-1 fallback overwrite for organization %s: provision replaced "
                "(expected=%s, observed=%s, status=%s).",
                organization_id,
                expected_provision_id,
                observed_provision_id,
                observed_status.value,
            )
            return _CorruptedResolvedFallbackResult(
                outcome="provision_replaced",
                observed_status=observed_status,
            )

        if observed_status != CustomerProvisionStatus.resolved:
            await _safe_rollback(db)
            logger.warning(
                "Skipping D-1 fallback overwrite for organization %s provision %s: "
                "status changed concurrently to '%s'.",
                organization_id,
                expected_provision_id,
                observed_status.value,
            )
            return _CorruptedResolvedFallbackResult(
                outcome="state_changed",
                observed_status=observed_status,
            )

        if isinstance(observed_raw_id, str) and observed_raw_id.strip():
            await _safe_rollback(db)
            logger.info(
                "Skipping D-1 fallback overwrite for organization %s provision %s: "
                "resolved mapping was concurrently repaired.",
                organization_id,
                expected_provision_id,
            )
            return _CorruptedResolvedFallbackResult(
                outcome="concurrent_repaired",
                observed_status=observed_status,
            )

        fresh_provision.status = CustomerProvisionStatus.ambiguous
        fresh_provision.provider_customer_id = None
        fresh_provision.claim_operation_id = None
        fresh_provision.lease_expires_at = None
        fresh_provision.last_error_code = diagnostic_code
        await db.commit()
        return _CorruptedResolvedFallbackResult(
            outcome="committed_ambiguous",
            observed_status=CustomerProvisionStatus.ambiguous,
        )
    except Exception as fallback_err:
        await _safe_rollback(db)
        logger.error(
            "D-1 fallback ambiguous update failed for organization %s provision %s: %s",
            organization_id,
            expected_provision_id,
            fallback_err,
        )
        return _CorruptedResolvedFallbackResult(
            outcome="fallback_failed",
            error=fallback_err,
        )


async def _attempt_corrupted_resolved_in_tx(
    db: AsyncSession,
    organization_id: uuid.UUID,
    provision: BillingCustomerProvision,
    *,
    merchant_account_id: str,
    environment: str,
) -> _CorruptedResolvedPrimaryResult:
    """Attempt D-1 ambiguous transition + reconciliation inside the active locked transaction.

    Rolls back the active session on failure and returns scalar diagnostics so any fallback
    transaction can run in a separate, non-nested session.
    """
    expected_provision_id = provision.id
    raw_customer_id = provision.provider_customer_id
    anomaly_kind = _classify_invalid_resolved_customer_id(raw_customer_id)
    diagnostic_code = CORRUPTED_RESOLVED_ERROR_CODE
    case_key = _build_reconciliation_case_key(
        "customer_provision:anomaly:resolved_missing_id",
        str(organization_id),
    )
    logger.error(
        "Integrity anomaly detected for organization %s: BillingCustomerProvision %s has "
        "status='resolved' with invalid provider_customer_id (%s). Failing closed.",
        organization_id,
        expected_provision_id,
        anomaly_kind,
    )

    provision.status = CustomerProvisionStatus.ambiguous
    provision.provider_customer_id = None
    provision.claim_operation_id = None
    provision.lease_expires_at = None
    provision.last_error_code = diagnostic_code

    try:
        await stage_or_update_payment_reconciliation(
            db=db,
            case_key=case_key,
            reason=diagnostic_code,
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            organization_id=organization_id,
            diagnostic_data={
                "organization_id": str(organization_id),
                "provision_id": str(expected_provision_id),
                "previous_status": CustomerProvisionStatus.resolved.value,
                "anomaly_type": anomaly_kind,
                "corrupted_provider_customer_id": (
                    raw_customer_id if isinstance(raw_customer_id, str) else None
                ),
                "corrupted_value_length": (
                    len(raw_customer_id) if isinstance(raw_customer_id, str) else None
                ),
            },
        )
        await db.commit()
        return _CorruptedResolvedPrimaryResult(
            succeeded=True,
            case_key=case_key,
            expected_provision_id=expected_provision_id,
            anomaly_kind=anomaly_kind,
        )
    except Exception as persist_err:
        await _safe_rollback(db)
        logger.error(
            "Failed to persist integrity-anomaly reconciliation for corrupted resolved "
            "provision %s on organization %s: %s",
            expected_provision_id,
            organization_id,
            persist_err,
        )
        return _CorruptedResolvedPrimaryResult(
            succeeded=False,
            case_key=case_key,
            expected_provision_id=expected_provision_id,
            anomaly_kind=anomaly_kind,
            persist_error=persist_err,
        )


def _raise_for_corrupted_resolved_outcome(
    organization_id: uuid.UUID,
    primary: _CorruptedResolvedPrimaryResult,
    fallback_result: _CorruptedResolvedFallbackResult | None,
) -> NoReturn:
    """Build and raise RazorpayCustomerReconciliationRequiredError from D-1/E-1 outcomes."""
    if primary.succeeded or fallback_result is None:
        raise RazorpayCustomerReconciliationRequiredError(
            f"Customer provisioning for organization {organization_id} had corrupted 'resolved' "
            f"state ({primary.anomaly_kind}); marked ambiguous with reconciliation case '{primary.case_key}'."
        )

    persist_err = primary.persist_error
    anomaly_kind = primary.anomaly_kind
    diagnostic_code = CORRUPTED_RESOLVED_ERROR_CODE

    if fallback_result.outcome == "committed_ambiguous":
        _raise_with_preserved_chain(
            RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning for organization {organization_id} had corrupted 'resolved' "
                f"state ({anomaly_kind}); reconciliation persistence failed "
                f"({type(persist_err).__name__}: {persist_err}), fallback committed ambiguous state "
                f"({diagnostic_code}). Reconciliation required before retry.",
                original_error=persist_err,
                recovery_error=None,
                fallback_committed=True,
            ),
            cause=persist_err,
        )
    if fallback_result.outcome == "concurrent_repaired":
        _raise_with_preserved_chain(
            RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning for organization {organization_id} had corrupted 'resolved' "
                f"state ({anomaly_kind}); reconciliation persistence failed "
                f"({type(persist_err).__name__}: {persist_err}), and fallback observed concurrently "
                f"repaired 'resolved' mapping without overwriting it.",
                original_error=persist_err,
                recovery_error=None,
                fallback_committed=False,
            ),
            cause=persist_err,
        )
    if fallback_result.outcome in ("state_changed", "provision_replaced"):
        observed = (
            fallback_result.observed_status.value
            if fallback_result.observed_status is not None
            else "unknown"
        )
        _raise_with_preserved_chain(
            RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning for organization {organization_id} had corrupted 'resolved' "
                f"state ({anomaly_kind}); reconciliation persistence failed "
                f"({type(persist_err).__name__}: {persist_err}), and fallback observed concurrent "
                f"state change ({fallback_result.outcome}, status='{observed}') without overwriting it.",
                original_error=persist_err,
                recovery_error=None,
                fallback_committed=False,
            ),
            cause=persist_err,
        )
    if fallback_result.outcome in ("org_unavailable", "provision_missing"):
        _raise_with_preserved_chain(
            RazorpayCustomerReconciliationRequiredError(
                f"Customer provisioning for organization {organization_id} has corrupted 'resolved' "
                f"state ({anomaly_kind}); reconciliation persistence failed "
                f"({type(persist_err).__name__}: {persist_err}), and fallback aborted because "
                f"target became unavailable ({fallback_result.outcome}). Commit outcome unconfirmed.",
                original_error=persist_err,
                recovery_error=None,
                fallback_committed=False,
            ),
            cause=persist_err,
        )

    fallback_err = fallback_result.error
    _raise_with_preserved_chain(
        RazorpayCustomerReconciliationRequiredError(
            f"Customer provisioning for organization {organization_id} has corrupted 'resolved' "
            f"state ({anomaly_kind}); reconciliation persistence failed "
            f"({type(persist_err).__name__}: {persist_err}), and fallback ambiguous transition failed "
            f"with unconfirmed commit outcome ({type(fallback_err).__name__}: {fallback_err}). "
            f"Reconciliation required before retry.",
            original_error=persist_err,
            recovery_error=fallback_err,
            fallback_committed=False,
        ),
        cause=fallback_err,
        context=persist_err,
    )


async def _run_corrupted_resolved_fallback_and_raise(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    primary: _CorruptedResolvedPrimaryResult,
) -> NoReturn:
    """Execute independent E-1 fallback session (when needed) and raise fail-closed error."""
    if primary.succeeded:
        _raise_for_corrupted_resolved_outcome(organization_id, primary, None)

    try:
        async with session_factory() as fallback_db:
            fallback_result = await _mark_corrupted_resolved_ambiguous_after_reconciliation_failure(
                db=fallback_db,
                organization_id=organization_id,
                expected_provision_id=primary.expected_provision_id,
                diagnostic_code=CORRUPTED_RESOLVED_ERROR_CODE,
            )
    except Exception as session_err:
        logger.error(
            "D-1 fallback session failed for organization %s provision %s: %s",
            organization_id,
            primary.expected_provision_id,
            session_err,
        )
        fallback_result = _CorruptedResolvedFallbackResult(
            outcome="fallback_failed",
            error=session_err,
        )

    _raise_for_corrupted_resolved_outcome(organization_id, primary, fallback_result)


async def _handle_corrupted_resolved_provision(
    db: AsyncSession,
    organization_id: uuid.UUID,
    provision: BillingCustomerProvision,
    *,
    merchant_account_id: str,
    environment: str,
) -> NoReturn:
    """Fail closed when a resolved provision lacks a valid provider_customer_id (D-1/E-1)."""
    primary = await _attempt_corrupted_resolved_in_tx(
        db=db,
        organization_id=organization_id,
        provision=provision,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )
    if primary.succeeded:
        _raise_for_corrupted_resolved_outcome(organization_id, primary, None)

    fallback_result = await _mark_corrupted_resolved_ambiguous_after_reconciliation_failure(
        db=db,
        organization_id=organization_id,
        expected_provision_id=primary.expected_provision_id,
        diagnostic_code=CORRUPTED_RESOLVED_ERROR_CODE,
    )
    _raise_for_corrupted_resolved_outcome(organization_id, primary, fallback_result)


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
        locked_org = await _lock_organization(db, organization_id)
        provision = (
            await _find_provision(db, organization_id, for_update=True)
            if locked_org is not None
            else None
        )
        if provision is not None and provision.status == CustomerProvisionStatus.pending:
            if claim_op_id is not None and provision.claim_operation_id != claim_op_id:
                logger.warning(
                    "Skipping claim cleanup for organization %s: claim not owned by operation (%s != %s)",
                    organization_id,
                    provision.claim_operation_id,
                    claim_op_id,
                )
                await _safe_rollback(db)
                return

            provision.status = CustomerProvisionStatus.failed
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = last_error_code
            await db.commit()
        else:
            await _safe_rollback(db)
    except Exception as cleanup_err:
        logger.warning(
            "Failed to clean up pending claim for organization %s: %s",
            organization_id,
            cleanup_err,
        )
        await _safe_rollback(db)


async def _cleanup_claim_with_factory(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    claim_op_id: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Execute definite-failure claim cleanup inside its own independent session."""
    try:
        async with session_factory() as cleanup_db:
            await _cleanup_claim(
                db=cleanup_db,
                organization_id=organization_id,
                claim_op_id=claim_op_id,
                last_error_code=last_error_code,
            )
    except Exception as session_err:
        logger.warning(
            "Failed to open cleanup session for organization %s: %s",
            organization_id,
            session_err,
        )


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
        locked_org = await _lock_organization(db, organization_id)
        provision = (
            await _find_provision(db, organization_id, for_update=True)
            if locked_org is not None
            else None
        )
        if provision is not None:
            if provision.status == CustomerProvisionStatus.resolved:
                logger.warning(
                    "Skipping ambiguous transition for organization %s: already resolved to %s",
                    organization_id,
                    provision.provider_customer_id,
                )
                await _safe_rollback(db)
                return

            if claim_op_id is not None and provision.claim_operation_id != claim_op_id:
                logger.warning(
                    "Skipping ambiguous transition for organization %s: claim not owned by operation (%s != %s)",
                    organization_id,
                    provision.claim_operation_id,
                    claim_op_id,
                )
                await _safe_rollback(db)
                return

            provision.status = CustomerProvisionStatus.ambiguous
            if customer_id and isinstance(customer_id, str):
                provision.provider_customer_id = customer_id.strip()
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = last_error_code
            await db.commit()
        else:
            await _safe_rollback(db)
    except Exception as amb_err:
        logger.warning(
            "Failed to record ambiguous claim for organization %s: %s",
            organization_id,
            amb_err,
        )
        await _safe_rollback(db)


async def _record_ambiguous_claim_with_factory(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    claim_op_id: str | None = None,
    customer_id: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Record an ambiguous remote claim outcome inside its own independent session."""
    try:
        async with session_factory() as ambig_db:
            await _record_ambiguous_claim(
                db=ambig_db,
                organization_id=organization_id,
                claim_op_id=claim_op_id,
                customer_id=customer_id,
                last_error_code=last_error_code,
            )
    except Exception as session_err:
        logger.warning(
            "Failed to open ambiguous-claim session for organization %s: %s",
            organization_id,
            session_err,
        )


async def _mark_claim_ambiguous_after_reconciliation_failure(
    db: AsyncSession,
    organization_id: uuid.UUID,
    claim_op_id: str,
) -> Exception | None:
    """Fallback TX after reconciliation failure so pending claim does not silently stay pending."""
    try:
        locked_org = await _lock_organization(db, organization_id)
        provision = (
            await _find_provision(db, organization_id, for_update=True)
            if locked_org is not None
            else None
        )
        if provision is not None and provision.claim_operation_id == claim_op_id:
            provision.status = CustomerProvisionStatus.ambiguous
            provision.provider_customer_id = None
            provision.claim_operation_id = None
            provision.lease_expires_at = None
            provision.last_error_code = "reconciliation_persist_failed"
            await db.commit()
        else:
            await _safe_rollback(db)
        return None
    except Exception as state_err:
        await _safe_rollback(db)
        logger.error(
            "Fallback status update after reconciliation failure also failed for organization %s: %s",
            organization_id,
            state_err,
        )
        return state_err


async def _mark_claim_ambiguous_after_reconciliation_failure_with_factory(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    claim_op_id: str,
) -> Exception | None:
    """Run ambiguous claim fallback inside a fresh independent session."""
    try:
        async with session_factory() as fallback_db:
            return await _mark_claim_ambiguous_after_reconciliation_failure(
                db=fallback_db,
                organization_id=organization_id,
                claim_op_id=claim_op_id,
            )
    except Exception as session_err:
        logger.error(
            "Failed to open fallback ambiguous-claim session for organization %s: %s",
            organization_id,
            session_err,
        )
        return session_err


@dataclass(frozen=True)
class _PersistenceRecoveryHandoff:
    """Scalar recovery outcome returned before exiting a closed TX_A2 recovery session."""

    action: str
    error_message: str = ""
    cause: Exception | None = None
    context: Exception | None = None
    recovery_error: Exception | None = None
    claim_owned: bool = False
    secondary_integrity: IntegrityError | None = None


def _build_collision_stage_inputs(
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    locked_org: Organization | None,
    provision: BillingCustomerProvision | None,
    conflict_owner: BillingCustomerProvision | None,
) -> tuple[bool, uuid.UUID | None, str, dict[str, Any], uuid.UUID | None]:
    """Capture scalar metadata and apply fail-closed provision fields for D-6 collision recovery."""
    claim_owned = provision is not None and provision.claim_operation_id == claim_op_id
    if claim_owned and provision is not None:
        # Never write the conflicting provider_customer_id again; mark ambiguous fail-closed.
        provision.status = CustomerProvisionStatus.ambiguous
        provision.provider_customer_id = None
        provision.claim_operation_id = None
        provision.lease_expires_at = None
        provision.last_error_code = "customer_id_org_collision"

    recon_org_id = organization_id if locked_org is not None else None
    owner_org_id = conflict_owner.organization_id if conflict_owner is not None else None
    case_key = _build_reconciliation_case_key(
        "customer_provision:collision",
        str(organization_id),
        customer_id,
    )
    diagnostic_data: dict[str, Any] = {
        "affected_organization_id": str(organization_id),
        "conflicting_organization_id": (
            str(owner_org_id) if owner_org_id is not None else None
        ),
        "conflicting_provision_id": (
            str(conflict_owner.id) if conflict_owner is not None else None
        ),
        "conflicting_status": (
            conflict_owner.status.value if conflict_owner is not None else None
        ),
        "provider_customer_id": customer_id,
        "claim_operation_id": claim_op_id,
        "claim_owned": claim_owned,
        "constraint": PROVIDER_CUSTOMER_UNIQUE_INDEX,
        "original_error_details": _extract_db_error_details(original_error),
    }
    return claim_owned, recon_org_id, case_key, diagnostic_data, owner_org_id


async def _recover_collision_in_current_tx(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    locked_org: Organization | None,
    provision: BillingCustomerProvision | None,
    conflict_owner: BillingCustomerProvision | None,
    merchant_account_id: str,
    environment: str,
) -> _PersistenceRecoveryHandoff:
    """Persist D-6 cross-org collision recovery in `db` and return a scalar handoff."""
    claim_owned, recon_org_id, case_key, diagnostic_data, owner_org_id = (
        _build_collision_stage_inputs(
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            original_error=original_error,
            locked_org=locked_org,
            provision=provision,
            conflict_owner=conflict_owner,
        )
    )
    try:
        await stage_or_update_payment_reconciliation(
            db=db,
            case_key=case_key,
            reason="customer_id_org_collision",
            provider="razorpay",
            merchant_account_id=merchant_account_id,
            environment=environment,
            organization_id=recon_org_id,
            diagnostic_data=diagnostic_data,
        )
        await db.commit()
    except Exception as recovery_err:
        await _safe_rollback(db)
        logger.error(
            "Reconciliation persistence failed during cross-org customer collision recovery "
            "for organization %s (customer=%s, original_error=%s): %s",
            organization_id,
            customer_id,
            original_error,
            recovery_err,
        )
        return _PersistenceRecoveryHandoff(
            action="collision_recon_failed",
            recovery_error=recovery_err,
            claim_owned=claim_owned,
        )

    owner_desc = (
        f" (existing owner organization {owner_org_id} preserved)"
        if owner_org_id is not None
        else ""
    )
    return _PersistenceRecoveryHandoff(
        action="raise_simple",
        error_message=(
            f"Customer {customer_id} created on Razorpay collides with an existing mapping{owner_desc}. "
            f"Organization {organization_id} marked ambiguous and reconciliation case '{case_key}' recorded "
            f"(original error: {type(original_error).__name__}: {original_error})."
        ),
        cause=original_error,
    )


async def _recover_loaded_state_to_handoff(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    locked_org: Organization | None,
    provision: BillingCustomerProvision | None,
    merchant_account_id: str,
    environment: str,
) -> _PersistenceRecoveryHandoff:
    """Recover from TX_A2 database failure in `db`, returning handoffs before any secondary TX."""
    if isinstance(original_error, IntegrityError):
        conflict_owner = await _find_conflicting_customer_owner(
            db,
            organization_id,
            customer_id,
        )
        if conflict_owner is not None or _is_provider_customer_collision_error(original_error):
            return await _recover_collision_in_current_tx(
                db=db,
                organization_id=organization_id,
                customer_id=customer_id,
                claim_op_id=claim_op_id,
                original_error=original_error,
                locked_org=locked_org,
                provision=provision,
                conflict_owner=conflict_owner,
                merchant_account_id=merchant_account_id,
                environment=environment,
            )

    if provision is not None and provision.claim_operation_id == claim_op_id:
        provision.status = CustomerProvisionStatus.ambiguous
        provision.provider_customer_id = customer_id
        provision.claim_operation_id = None
        provision.lease_expires_at = None
        provision.last_error_code = "db_persistence_failure"
        try:
            await db.commit()
        except IntegrityError as secondary_integrity:
            await _safe_rollback(db)
            return _PersistenceRecoveryHandoff(
                action="secondary_integrity",
                secondary_integrity=secondary_integrity,
            )
        except Exception as fallback_err:
            await _safe_rollback(db)
            logger.error(
                "Secondary failure persisting ambiguous fallback state for organization %s "
                "(customer=%s, original_error=%s): %s",
                organization_id,
                customer_id,
                original_error,
                fallback_err,
            )
            return _PersistenceRecoveryHandoff(
                action="raise_simple",
                error_message=(
                    f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                    f"local database persistence failed ({type(original_error).__name__}: {original_error}), and "
                    f"recovery failed ({type(fallback_err).__name__}: {fallback_err})."
                ),
                cause=fallback_err,
                context=original_error,
                recovery_error=fallback_err,
            )
    else:
        await _record_superseded_customer_reconciliation(
            db=db,
            organization_id=organization_id,
            customer_id=customer_id,
            stale_claim_op_id=claim_op_id,
            reason="persistence_failure_stale_claim",
            details={"error": str(original_error)},
            organization_exists=(locked_org is not None),
        )

    return _PersistenceRecoveryHandoff(
        action="raise_simple",
        error_message=(
            f"Customer {customer_id} was created on Razorpay, but local database persistence failed. "
            f"Organization {organization_id} is in an ambiguous state requiring reconciliation: {original_error}"
        ),
        cause=original_error,
    )


async def _raise_from_collision_recon_failed(
    session_factory: AsyncSessionFactory | None,
    direct_db: AsyncSession | None,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    recovery_err: Exception,
    claim_owned: bool,
) -> NoReturn:
    """Execute independent claim-ambiguous fallback (if claim_owned) and raise."""
    state_err: Exception | None = None
    if claim_owned:
        if session_factory is not None:
            state_err = await _mark_claim_ambiguous_after_reconciliation_failure_with_factory(
                session_factory=session_factory,
                organization_id=organization_id,
                claim_op_id=claim_op_id,
            )
        elif direct_db is not None:
            state_err = await _mark_claim_ambiguous_after_reconciliation_failure(
                db=direct_db,
                organization_id=organization_id,
                claim_op_id=claim_op_id,
            )
    state_note = (
        f"; fallback ambiguous update also failed ({type(state_err).__name__}: {state_err})"
        if state_err is not None
        else (
            "; claim transitioned to ambiguous without conflicting customer_id"
            if claim_owned
            else ""
        )
    )
    _raise_with_preserved_chain(
        RazorpayCustomerPersistenceError(
            f"Cross-organization collision persisting customer {customer_id} for organization {organization_id} "
            f"(original error: {type(original_error).__name__}: {original_error}); reconciliation persistence failed "
            f"({type(recovery_err).__name__}: {recovery_err}){state_note}.",
            customer_id=customer_id,
            original_error=original_error,
            recovery_error=recovery_err,
        ),
        cause=recovery_err,
        context=original_error,
    )


async def _finalize_persistence_recovery_handoff_and_raise(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    handoff: _PersistenceRecoveryHandoff,
    *,
    merchant_account_id: str,
    environment: str,
) -> NoReturn:
    """Complete any follow-up recovery TXs outside the closed session and raise."""
    if handoff.action == "collision_recon_failed":
        assert handoff.recovery_error is not None
        await _raise_from_collision_recon_failed(
            session_factory=session_factory,
            direct_db=None,
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            original_error=original_error,
            recovery_err=handoff.recovery_error,
            claim_owned=handoff.claim_owned,
        )

    if handoff.action == "secondary_integrity":
        secondary_integrity = handoff.secondary_integrity
        assert secondary_integrity is not None
        secondary_handoff: _PersistenceRecoveryHandoff | None = None
        try:
            async with session_factory() as secondary_db:
                try:
                    fresh_org = await _lock_organization(secondary_db, organization_id)
                    fresh_prov = (
                        await _find_provision(secondary_db, organization_id, for_update=True)
                        if fresh_org is not None
                        else None
                    )
                    conflict_owner = await _find_conflicting_customer_owner(
                        secondary_db,
                        organization_id,
                        customer_id,
                    )
                    if conflict_owner is not None or _is_provider_customer_collision_error(
                        secondary_integrity
                    ):
                        secondary_handoff = await _recover_collision_in_current_tx(
                            db=secondary_db,
                            organization_id=organization_id,
                            customer_id=customer_id,
                            claim_op_id=claim_op_id,
                            original_error=secondary_integrity,
                            locked_org=fresh_org,
                            provision=fresh_prov,
                            conflict_owner=conflict_owner,
                            merchant_account_id=merchant_account_id,
                            environment=environment,
                        )
                    else:
                        await _safe_rollback(secondary_db)
                except Exception:
                    await _safe_rollback(secondary_db)
                    raise
        except Exception as secondary_err:
            if (
                secondary_handoff is not None
                and secondary_handoff.action == "collision_recon_failed"
                and secondary_handoff.recovery_error is not None
            ):
                logger.error(
                    "Secondary integrity session cleanup failed after collision reconciliation "
                    "failure for organization %s (customer=%s, original_error=%s, "
                    "secondary_integrity=%s, recon_error=%s): %s",
                    organization_id,
                    customer_id,
                    original_error,
                    secondary_integrity,
                    secondary_handoff.recovery_error,
                    secondary_err,
                )
                await _raise_from_collision_recon_failed(
                    session_factory=session_factory,
                    direct_db=None,
                    organization_id=organization_id,
                    customer_id=customer_id,
                    claim_op_id=claim_op_id,
                    original_error=secondary_integrity,
                    recovery_err=secondary_handoff.recovery_error,
                    claim_owned=secondary_handoff.claim_owned,
                )

            logger.error(
                "Secondary integrity recovery failed for organization %s with customer %s "
                "(original_error=%s, secondary_integrity=%s): %s",
                organization_id,
                customer_id,
                original_error,
                secondary_integrity,
                secondary_err,
            )
            _raise_with_preserved_chain(
                RazorpayCustomerPersistenceError(
                    f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                    f"primary persistence failed ({type(original_error).__name__}: {original_error}), "
                    f"fallback persistence hit IntegrityError ({secondary_integrity}), and "
                    f"secondary integrity recovery failed ({type(secondary_err).__name__}: {secondary_err}).",
                    customer_id=customer_id,
                    original_error=original_error,
                    recovery_error=secondary_err,
                ),
                cause=secondary_err,
                context=secondary_integrity,
            )

        if secondary_handoff is not None:
            if secondary_handoff.action == "collision_recon_failed":
                assert secondary_handoff.recovery_error is not None
                await _raise_from_collision_recon_failed(
                    session_factory=session_factory,
                    direct_db=None,
                    organization_id=organization_id,
                    customer_id=customer_id,
                    claim_op_id=claim_op_id,
                    original_error=secondary_integrity,
                    recovery_err=secondary_handoff.recovery_error,
                    claim_owned=secondary_handoff.claim_owned,
                )
            _raise_with_preserved_chain(
                RazorpayCustomerPersistenceError(
                    secondary_handoff.error_message,
                    customer_id=customer_id,
                    original_error=secondary_integrity,
                    recovery_error=secondary_handoff.recovery_error,
                ),
                cause=secondary_handoff.cause or secondary_integrity,
                context=secondary_handoff.context,
            )

        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"primary persistence failed ({type(original_error).__name__}: {original_error}), and "
                f"fallback persistence hit IntegrityError ({secondary_integrity}).",
                customer_id=customer_id,
                original_error=original_error,
                recovery_error=secondary_integrity,
            ),
            cause=secondary_integrity,
            context=original_error,
        )

    _raise_with_preserved_chain(
        RazorpayCustomerPersistenceError(
            handoff.error_message,
            customer_id=customer_id,
            original_error=original_error,
            recovery_error=handoff.recovery_error,
        ),
        cause=handoff.cause or original_error,
        context=handoff.context,
    )


async def _handle_persistence_failure_with_factory(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    merchant_account_id: str,
    environment: str,
) -> NoReturn:
    """Recover from non-IntegrityError TX_A2 failure using independent owned sessions."""
    logger.error(
        "Database failure persisting resolved Razorpay customer %s for organization %s: %s",
        customer_id,
        organization_id,
        original_error,
    )
    handoff: _PersistenceRecoveryHandoff | None = None
    try:
        async with session_factory() as recovery_db:
            try:
                locked_org = await _lock_organization(recovery_db, organization_id)
                provision = (
                    await _find_provision(recovery_db, organization_id, for_update=True)
                    if locked_org is not None
                    else None
                )
                handoff = await _recover_loaded_state_to_handoff(
                    db=recovery_db,
                    organization_id=organization_id,
                    customer_id=customer_id,
                    claim_op_id=claim_op_id,
                    original_error=original_error,
                    locked_org=locked_org,
                    provision=provision,
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                )
            except Exception as fallback_err:
                await _safe_rollback(recovery_db)
                raise fallback_err
    except Exception as fallback_err:
        logger.error(
            "Secondary failure attempting to record ambiguous state for organization %s with customer %s: %s. "
            "Pending claim left intact for lease-based recovery.",
            organization_id,
            customer_id,
            fallback_err,
        )
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"local database persistence failed ({type(original_error).__name__}: {original_error}), and "
                f"recovery failed ({type(fallback_err).__name__}: {fallback_err}).",
                customer_id=customer_id,
                original_error=original_error,
                recovery_error=fallback_err,
            ),
            cause=fallback_err,
            context=original_error,
        )

    assert handoff is not None
    await _finalize_persistence_recovery_handoff_and_raise(
        session_factory=session_factory,
        organization_id=organization_id,
        customer_id=customer_id,
        claim_op_id=claim_op_id,
        original_error=original_error,
        handoff=handoff,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )


async def _handle_tx2_integrity_error_with_factory(
    session_factory: AsyncSessionFactory,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    tx2_integrity_error: IntegrityError,
    *,
    merchant_account_id: str,
    environment: str,
) -> str:
    """Handle TX_A2 IntegrityError in a fresh reload session (and non-nested follow-up TXs)."""
    reload_corrupted: _CorruptedResolvedPrimaryResult | None = None
    reload_handoff: _PersistenceRecoveryHandoff | None = None
    reload_failed_err: Exception | None = None
    recovery_failed_err: Exception | None = None
    resolved_commit_failed: tuple[str, Exception] | None = None

    try:
        async with session_factory() as reload_db:
            try:
                reloaded_org = await _lock_organization(reload_db, organization_id)
                reloaded_prov = (
                    await _find_provision(reload_db, organization_id, for_update=True)
                    if reloaded_org is not None
                    else None
                )
            except Exception as reload_err:
                await _safe_rollback(reload_db)
                reload_failed_err = reload_err
            else:
                if (
                    reloaded_prov is not None
                    and reloaded_prov.status == CustomerProvisionStatus.resolved
                ):
                    raw_reloaded = reloaded_prov.provider_customer_id
                    if isinstance(raw_reloaded, str) and raw_reloaded.strip():
                        reloaded_id = raw_reloaded.strip()
                        try:
                            await reload_db.commit()
                            return reloaded_id
                        except Exception as commit_err:
                            await _safe_rollback(reload_db)
                            resolved_commit_failed = (reloaded_id, commit_err)
                    else:
                        reload_corrupted = await _attempt_corrupted_resolved_in_tx(
                            db=reload_db,
                            organization_id=organization_id,
                            provision=reloaded_prov,
                            merchant_account_id=merchant_account_id,
                            environment=environment,
                        )
                else:
                    try:
                        reload_handoff = await _recover_loaded_state_to_handoff(
                            db=reload_db,
                            organization_id=organization_id,
                            customer_id=customer_id,
                            claim_op_id=claim_op_id,
                            original_error=tx2_integrity_error,
                            locked_org=reloaded_org,
                            provision=reloaded_prov,
                            merchant_account_id=merchant_account_id,
                            environment=environment,
                        )
                    except Exception as rec_err:
                        await _safe_rollback(reload_db)
                        recovery_failed_err = rec_err
    except Exception as session_err:
        if reload_failed_err is None and recovery_failed_err is None:
            reload_failed_err = session_err

    if resolved_commit_failed is not None:
        reloaded_id, commit_err = resolved_commit_failed
        logger.error(
            "Failed to finalize concurrent resolved customer %s for organization %s "
            "after IntegrityError (%s): %s",
            reloaded_id,
            organization_id,
            tx2_integrity_error,
            commit_err,
        )
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Failed to finalize concurrent resolved customer {reloaded_id} for "
                f"organization {organization_id} after IntegrityError ({tx2_integrity_error}): {commit_err}",
                customer_id=reloaded_id,
                original_error=tx2_integrity_error,
                recovery_error=commit_err,
            ),
            cause=commit_err,
            context=tx2_integrity_error,
        )

    if reload_corrupted is not None:
        await _run_corrupted_resolved_fallback_and_raise(
            session_factory=session_factory,
            organization_id=organization_id,
            primary=reload_corrupted,
        )

    if reload_failed_err is not None:
        logger.error(
            "Failed to reload provisioning state after IntegrityError for organization %s "
            "(customer=%s, original_error=%s): %s",
            organization_id,
            customer_id,
            tx2_integrity_error,
            reload_failed_err,
        )
        state_err = await _mark_claim_ambiguous_after_reconciliation_failure_with_factory(
            session_factory=session_factory,
            organization_id=organization_id,
            claim_op_id=claim_op_id,
        )
        state_note = (
            f"; fallback ambiguous update also failed ({type(state_err).__name__}: {state_err})"
            if state_err is not None
            else ""
        )
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"local persistence hit IntegrityError ({tx2_integrity_error}), and recovery reload failed "
                f"({type(reload_failed_err).__name__}: {reload_failed_err}){state_note}.",
                customer_id=customer_id,
                original_error=tx2_integrity_error,
                recovery_error=reload_failed_err,
            ),
            cause=reload_failed_err,
            context=tx2_integrity_error,
        )

    if recovery_failed_err is not None:
        logger.error(
            "Recovery failed after IntegrityError for organization %s "
            "(customer=%s, original_error=%s): %s",
            organization_id,
            customer_id,
            tx2_integrity_error,
            recovery_failed_err,
        )
        state_err = await _mark_claim_ambiguous_after_reconciliation_failure_with_factory(
            session_factory=session_factory,
            organization_id=organization_id,
            claim_op_id=claim_op_id,
        )
        state_note = (
            f"; fallback ambiguous update also failed ({type(state_err).__name__}: {state_err})"
            if state_err is not None
            else ""
        )
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"local persistence hit IntegrityError ({tx2_integrity_error}), and recovery failed "
                f"({type(recovery_failed_err).__name__}: {recovery_failed_err}){state_note}.",
                customer_id=customer_id,
                original_error=tx2_integrity_error,
                recovery_error=recovery_failed_err,
            ),
            cause=recovery_failed_err,
            context=tx2_integrity_error,
        )

    assert reload_handoff is not None
    await _finalize_persistence_recovery_handoff_and_raise(
        session_factory=session_factory,
        organization_id=organization_id,
        customer_id=customer_id,
        claim_op_id=claim_op_id,
        original_error=tx2_integrity_error,
        handoff=reload_handoff,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )


async def _recover_cross_org_collision_in_tx(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    locked_org: Organization | None,
    provision: BillingCustomerProvision | None,
    conflict_owner: BillingCustomerProvision | None,
    merchant_account_id: str,
    environment: str,
) -> NoReturn:
    """Persist D-6 cross-org collision recovery in the supplied session `db`."""
    handoff = await _recover_collision_in_current_tx(
        db=db,
        organization_id=organization_id,
        customer_id=customer_id,
        claim_op_id=claim_op_id,
        original_error=original_error,
        locked_org=locked_org,
        provision=provision,
        conflict_owner=conflict_owner,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )
    if handoff.action == "collision_recon_failed":
        assert handoff.recovery_error is not None
        await _raise_from_collision_recon_failed(
            session_factory=None,
            direct_db=db,
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            original_error=original_error,
            recovery_err=handoff.recovery_error,
            claim_owned=handoff.claim_owned,
        )
    _raise_with_preserved_chain(
        RazorpayCustomerPersistenceError(
            handoff.error_message,
            customer_id=customer_id,
            original_error=original_error,
        ),
        cause=original_error,
    )


async def _recover_persistence_failure_in_tx(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
    *,
    locked_org: Organization | None,
    provision: BillingCustomerProvision | None,
    merchant_account_id: str,
    environment: str,
) -> NoReturn:
    """Recover from TX_A2 database failure inside a direct supplied session `db`."""
    handoff = await _recover_loaded_state_to_handoff(
        db=db,
        organization_id=organization_id,
        customer_id=customer_id,
        claim_op_id=claim_op_id,
        original_error=original_error,
        locked_org=locked_org,
        provision=provision,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )
    if handoff.action == "collision_recon_failed":
        assert handoff.recovery_error is not None
        await _raise_from_collision_recon_failed(
            session_factory=None,
            direct_db=db,
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            original_error=original_error,
            recovery_err=handoff.recovery_error,
            claim_owned=handoff.claim_owned,
        )
    if handoff.action == "secondary_integrity":
        secondary_integrity = handoff.secondary_integrity
        assert secondary_integrity is not None
        fresh_org = await _lock_organization(db, organization_id)
        fresh_prov = (
            await _find_provision(db, organization_id, for_update=True)
            if fresh_org is not None
            else None
        )
        conflict_owner = await _find_conflicting_customer_owner(
            db,
            organization_id,
            customer_id,
        )
        if conflict_owner is not None or _is_provider_customer_collision_error(secondary_integrity):
            await _recover_cross_org_collision_in_tx(
                db=db,
                organization_id=organization_id,
                customer_id=customer_id,
                claim_op_id=claim_op_id,
                original_error=secondary_integrity,
                locked_org=fresh_org,
                provision=fresh_prov,
                conflict_owner=conflict_owner,
                merchant_account_id=merchant_account_id,
                environment=environment,
            )
        await _safe_rollback(db)
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"primary persistence failed ({type(original_error).__name__}: {original_error}), and "
                f"fallback persistence hit IntegrityError ({secondary_integrity}).",
                customer_id=customer_id,
                original_error=original_error,
                recovery_error=secondary_integrity,
            ),
            cause=secondary_integrity,
            context=original_error,
        )
    _raise_with_preserved_chain(
        RazorpayCustomerPersistenceError(
            handoff.error_message,
            customer_id=customer_id,
            original_error=original_error,
            recovery_error=handoff.recovery_error,
        ),
        cause=handoff.cause or original_error,
        context=handoff.context,
    )


async def _handle_persistence_failure(
    db: AsyncSession,
    organization_id: uuid.UUID,
    customer_id: str,
    claim_op_id: str,
    original_error: Exception,
) -> NoReturn:
    """Handle database failure after successful remote customer creation on a direct session `db`.

    Preserved for isolated unit helpers; get_or_create_razorpay_customer() executes each
    recovery phase in independent owned sessions via _handle_persistence_failure_with_factory().
    """
    logger.error(
        "Database failure persisting resolved Razorpay customer %s for organization %s: %s",
        customer_id,
        organization_id,
        original_error,
    )
    merchant_account_id, environment = _get_provisioning_scope()
    try:
        locked_org = await _lock_organization(db, organization_id)
        provision = (
            await _find_provision(db, organization_id, for_update=True)
            if locked_org is not None
            else None
        )
        await _recover_persistence_failure_in_tx(
            db=db,
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            original_error=original_error,
            locked_org=locked_org,
            provision=provision,
            merchant_account_id=merchant_account_id,
            environment=environment,
        )
    except RazorpayCustomerPersistenceError:
        raise
    except Exception as fallback_err:
        logger.error(
            "Secondary failure attempting to record ambiguous state for organization %s with customer %s: %s. "
            "Pending claim left intact for lease-based recovery.",
            organization_id,
            customer_id,
            fallback_err,
        )
        await _safe_rollback(db)
        _raise_with_preserved_chain(
            RazorpayCustomerPersistenceError(
                f"Customer {customer_id} was created on Razorpay for organization {organization_id}, "
                f"local database persistence failed ({type(original_error).__name__}: {original_error}), and "
                f"recovery failed ({type(fallback_err).__name__}: {fallback_err}).",
                customer_id=customer_id,
                original_error=original_error,
                recovery_error=fallback_err,
            ),
            cause=fallback_err,
            context=original_error,
        )


async def get_or_create_razorpay_customer(
    organization_id: uuid.UUID,
    user_email: str | None = None,
    organization_name: str | None = None,
    session_factory: AsyncSessionFactory | None = None,
) -> str:
    """Obtain or create a Razorpay customer for an organization using owned per-phase sessions.

    Lifecycle Invariants:
    1. Dedicated Storage: Uses BillingCustomerProvision exclusively. Never creates, modifies,
       or demotes a Subscription row.
    2. Caller Session Non-Interference: Accepts scalar organization_id and manages every database
       phase via independent sessions from `session_factory` (defaulting to AsyncSessionLocal).
       Never commits, rolls back, flushes, or touches any caller AsyncSession or identity map.
    3. Idempotency: If a verified customer association exists, returns it immediately without calling Razorpay.
    4. Claim Serialization: Acquires Organization FOR UPDATE -> BillingCustomerProvision FOR UPDATE
       in an independent TX_A1 session and commits a durable pending claim with claim_operation_id
       and lease_expires_at before exiting the session and invoking Razorpay.
    5. Transaction Boundary Safety: Every phase session closes completely before external HTTP
       calls and before recovery/fallback sessions open (no nested DB sessions).
    6. Robust HTTP Failure Classification:
       - Definite client errors (400, 401, 404, 422) and pre-dispatch network errors clean up the claim
         in an independent cleanup session.
       - Server 5xx errors, HTTP 408 request timeouts, HTTP 429 rate limits, and post-dispatch
         network errors persist an 'ambiguous' state in an independent session and prohibit retries.
       - Malformed success (200/201 with missing/null ID) transitions to ambiguous and never releases the claim.
    7. Persistence Safety & D-1/D-6/E-1 Protections: Protects successful gateway responses against
       database failures and cross-organization collisions in dedicated TX_A2 / recovery / fallback sessions.

    Args:
        organization_id: Scalar UUID of the committed Organization.
        user_email: Optional email of the initiating authenticated user.
        organization_name: Optional fallback display name for customer creation on Razorpay.
        session_factory: Independent async context-manager factory returning an AsyncSession
            (defaults to AsyncSessionLocal when omitted or None).

    Returns:
        The verified Razorpay customer ID (e.g. 'cust_xxx').

    Raises:
        RazorpayBillingDisabledError: If Razorpay billing is disabled in settings.
        RazorpayConfigurationError: If Razorpay configuration is incomplete.
        RazorpayCustomerProvisioningInProgressError: If provisioning is currently underway.
        RazorpayCustomerReconciliationRequiredError: If state is ambiguous, corrupted resolved, or lease expired.
        RazorpayAmbiguousNetworkError: If HTTP request timed out, reset, returned 5xx/408/429, or was malformed.
        RazorpayCustomerPersistenceError: If remote call succeeded but DB persistence failed.
        RazorpayCustomerError: If API call definitively fails, org is unavailable, or response is invalid.
    """
    # Validate scope upfront before opening any database session or transaction.
    merchant_account_id, environment = _get_provisioning_scope()
    factory = session_factory if session_factory is not None else AsyncSessionLocal

    # -------------------------------------------------------------------------
    # Phase A / TX_A1: Owned Session — Locked Inspection & Claim Establishment
    # Lock order: Organization FOR UPDATE -> BillingCustomerProvision FOR UPDATE
    # -------------------------------------------------------------------------
    org_name = ""
    claim_op_id = ""
    client: Any = None
    tx1_integrity_error = False
    tx1_corrupted: _CorruptedResolvedPrimaryResult | None = None

    async with factory() as tx1_db:
        locked_org = await _lock_organization(tx1_db, organization_id)
        if locked_org is None:
            await _safe_rollback(tx1_db)
            raise RazorpayCustomerError(
                f"Organization {organization_id} is unavailable for customer provisioning."
            )

        provision = await _find_provision(tx1_db, organization_id, for_update=True)

        if provision is not None:
            # Case 1.1: Resolved customer ID already exists
            if provision.status == CustomerProvisionStatus.resolved:
                raw_customer_id = provision.provider_customer_id
                if isinstance(raw_customer_id, str) and raw_customer_id.strip():
                    resolved_id = raw_customer_id.strip()
                    try:
                        await tx1_db.commit()
                    except Exception:
                        await _safe_rollback(tx1_db)
                        raise
                    return resolved_id
                tx1_corrupted = await _attempt_corrupted_resolved_in_tx(
                    db=tx1_db,
                    organization_id=organization_id,
                    provision=provision,
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                )

            # Case 1.2: In-progress provisioning claim
            elif provision.status == CustomerProvisionStatus.pending:
                now_utc = datetime.now(timezone.utc)
                lease_expires = provision.lease_expires_at
                if lease_expires is not None:
                    if lease_expires.tzinfo is None:
                        lease_expires = lease_expires.replace(tzinfo=timezone.utc)
                    if now_utc < lease_expires:
                        await _safe_rollback(tx1_db)
                        raise RazorpayCustomerProvisioningInProgressError(
                            f"Customer provisioning in progress for organization {organization_id} (lease active)."
                        )

                # Lease expired: Remote call may have succeeded while worker crashed.
                # Do NOT assume remote request failed; mark ambiguous to require reconciliation.
                provision.status = CustomerProvisionStatus.ambiguous
                provision.claim_operation_id = None
                provision.lease_expires_at = None
                provision.last_error_code = "lease_expired"
                try:
                    await tx1_db.commit()
                except Exception:
                    await _safe_rollback(tx1_db)
                raise RazorpayCustomerReconciliationRequiredError(
                    f"Customer provisioning lease expired for organization {organization_id}; "
                    f"remote state is ambiguous and requires reconciliation before retry."
                )

            # Case 1.3: Ambiguous outcome recorded from previous failure
            elif provision.status == CustomerProvisionStatus.ambiguous:
                await _safe_rollback(tx1_db)
                raise RazorpayCustomerReconciliationRequiredError(
                    f"Customer provisioning for organization {organization_id} is in an ambiguous state. "
                    f"Reconciliation required before retry."
                )

        if tx1_corrupted is None:
            # Pre-HTTP Gate: Check billing enabled status & client configuration
            if not is_razorpay_enabled():
                await _safe_rollback(tx1_db)
                raise RazorpayBillingDisabledError(
                    "Razorpay billing is currently disabled. Set RAZORPAY_ENABLED=true to enable."
                )

            try:
                client = get_razorpay_client()
            except Exception:
                await _safe_rollback(tx1_db)
                raise

            # Claim Provisioning inside the same locked transaction (TX_A1)
            org_name = (locked_org.name or organization_name or "").strip() or str(organization_id)
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
                    organization_id=organization_id,
                    provider="razorpay",
                    merchant_account_id=merchant_account_id,
                    environment=environment,
                    provider_customer_id=None,
                    status=CustomerProvisionStatus.pending,
                    claim_operation_id=claim_op_id,
                    lease_expires_at=lease_expires,
                    last_error_code=None,
                )
                tx1_db.add(provision)

            try:
                await tx1_db.commit()
            except IntegrityError:
                await _safe_rollback(tx1_db)
                tx1_integrity_error = True
            except Exception:
                await _safe_rollback(tx1_db)
                raise

    # Execute D-1 / E-1 fallback strictly after tx1_db has exited cleanly.
    if tx1_corrupted is not None:
        await _run_corrupted_resolved_fallback_and_raise(
            session_factory=factory,
            organization_id=organization_id,
            primary=tx1_corrupted,
        )

    # Execute TX_A1 IntegrityError reload in a fresh independent session after tx1_db exited.
    if tx1_integrity_error:
        reload_corrupted: _CorruptedResolvedPrimaryResult | None = None
        async with factory() as reload_db:
            try:
                reloaded_org = await _lock_organization(reload_db, organization_id)
                reloaded = (
                    await _find_provision(reload_db, organization_id, for_update=True)
                    if reloaded_org is not None
                    else None
                )
                if reloaded is not None and reloaded.status == CustomerProvisionStatus.resolved:
                    raw_reloaded = reloaded.provider_customer_id
                    if isinstance(raw_reloaded, str) and raw_reloaded.strip():
                        reloaded_id = raw_reloaded.strip()
                        try:
                            await reload_db.commit()
                        except Exception:
                            await _safe_rollback(reload_db)
                            raise
                        return reloaded_id
                    reload_corrupted = await _attempt_corrupted_resolved_in_tx(
                        db=reload_db,
                        organization_id=organization_id,
                        provision=reloaded,
                        merchant_account_id=merchant_account_id,
                        environment=environment,
                    )
                else:
                    await _safe_rollback(reload_db)
            except Exception as reload_err:
                await _safe_rollback(reload_db)
                logger.error(
                    "Failed to reload provisioning state after TX1 IntegrityError for organization %s: %s",
                    organization_id,
                    reload_err,
                )
                raise

        if reload_corrupted is not None:
            await _run_corrupted_resolved_fallback_and_raise(
                session_factory=factory,
                organization_id=organization_id,
                primary=reload_corrupted,
            )

        raise RazorpayCustomerProvisioningInProgressError(
            f"Customer provisioning claim conflict for organization {organization_id}."
        )

    # -------------------------------------------------------------------------
    # Phase A / HTTP: External Gateway Invocation (Strictly Outside Any DB Session)
    # -------------------------------------------------------------------------
    params: dict[str, Any] = {
        "name": org_name,
        "email": user_email.strip() if user_email and user_email.strip() else None,
        "notes": {
            "organization_id": str(organization_id),
        },
        "fail_existing": 0,
    }

    try:
        customer = await client.create_customer(**params)
    except RazorpayAuthError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Authentication error creating Razorpay customer for org %s: %s", organization_id, sanitized_msg)
        await _cleanup_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="401")
        raise RazorpayCustomerError(f"Razorpay authentication error: {sanitized_msg}") from exc
    except RazorpayRateLimitError as exc:
        # HTTP 429: Reassessed conservatively. Do NOT release claim.
        # Prefer preserving the claim in ambiguous state for controlled recovery over unsafe immediate retry.
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Rate limit creating Razorpay customer for organization %s: %s", organization_id, sanitized_msg)
        await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="429")
        raise RazorpayAmbiguousNetworkError(
            f"Razorpay rate limit exceeded (HTTP 429) for organization {organization_id}; "
            f"claim preserved in ambiguous state for controlled recovery: {sanitized_msg}"
        ) from exc
    except RazorpayServer5xxError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Server 5xx error creating Razorpay customer for org %s (status=%s): %s", organization_id, exc.status_code, sanitized_msg)
        await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code=str(exc.status_code))
        raise RazorpayAmbiguousNetworkError(
            f"Ambiguous server error (HTTP {exc.status_code}) while creating Razorpay customer for organization {organization_id}. "
            f"Customer may exist remotely; automated retry is prohibited to prevent duplicate customers. "
            f"Reconciliation required: {sanitized_msg}"
        ) from exc
    except RazorpayAPIError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        if exc.status_code == 408:
            # HTTP 408 Request Timeout: Ambiguous outcome!
            logger.error("HTTP 408 timeout creating Razorpay customer for organization %s: %s", organization_id, sanitized_msg)
            await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="408")
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous HTTP 408 request timeout from Razorpay for organization {organization_id}. "
                f"Customer may exist remotely; automated retry is prohibited: {sanitized_msg}"
            ) from exc
        elif exc.status_code is not None and exc.status_code < 500:
            # Definite 4xx client rejection (400 bad request, 404, 422).
            # Razorpay rejected the request synchronously before creating customer.
            logger.error("Client error creating Razorpay customer for organization %s (status=%s): %s", organization_id, exc.status_code, sanitized_msg)
            await _cleanup_claim_with_factory(factory, organization_id, claim_op_id, last_error_code=str(exc.status_code))
            raise RazorpayCustomerError(f"Razorpay customer creation failed: {sanitized_msg}") from exc
        else:
            # Uncertain status code or server error: treat as ambiguous
            logger.error("Ambiguous error creating Razorpay customer for organization %s (status=%s): %s", organization_id, exc.status_code, sanitized_msg)
            await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code=str(exc.status_code))
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous error communicating with Razorpay for organization {organization_id}: {sanitized_msg}"
            ) from exc
    except RazorpayNetworkError as exc:
        sanitized_msg = sanitize_razorpay_error(str(exc))
        if exc.is_pre_dispatch:
            # Demonstrably failed before request reached Razorpay (ConnectTimeout, ConnectError, PoolTimeout).
            # Customer definitely not created. Safe to clean claim.
            logger.error("Pre-dispatch network error for organization %s: %s", organization_id, sanitized_msg)
            await _cleanup_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="pre_dispatch_network_error")
            raise RazorpayCustomerError(f"Razorpay connection failed before dispatch: {sanitized_msg}") from exc
        else:
            # Post-dispatch or uncertain (ReadTimeout, connection reset, RemoteProtocolError).
            # AMBIGUOUS! Customer may exist remotely. Prohibit blind retry.
            logger.error("Ambiguous post-dispatch network error for organization %s: %s", organization_id, sanitized_msg)
            await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="post_dispatch_network_error")
            raise RazorpayAmbiguousNetworkError(
                f"Ambiguous network timeout/reset while creating Razorpay customer for organization {organization_id}. "
                f"Customer may exist remotely; automated retry is prohibited to prevent duplicate customers. "
                f"Reconciliation required: {sanitized_msg}"
            ) from exc
    except Exception as exc:
        # Unexpected exception during HTTP call: conservatively treat as ambiguous.
        sanitized_msg = sanitize_razorpay_error(str(exc))
        logger.error("Unexpected error creating Razorpay customer for organization %s: %s", organization_id, sanitized_msg)
        await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="unexpected_exception")
        raise RazorpayAmbiguousNetworkError(
            f"Unexpected error communicating with Razorpay for organization {organization_id} (ambiguous): {sanitized_msg}"
        ) from exc

    customer_id = customer.get("id") if isinstance(customer, dict) else None
    if not customer_id or not isinstance(customer_id, str) or not customer_id.strip():
        # Task 3 Requirement 1: HTTP 200/201 with missing/null ID must transition to ambiguous, NOT cleanup!
        logger.error("Razorpay returned HTTP success with invalid/missing customer ID for organization %s", organization_id)
        await _record_ambiguous_claim_with_factory(factory, organization_id, claim_op_id, last_error_code="malformed_success")
        raise RazorpayAmbiguousNetworkError(
            f"Razorpay customer creation returned HTTP success but missing or malformed customer ID for organization {organization_id}; "
            f"marked ambiguous to prevent duplicate customer creation."
        )

    customer_id = customer_id.strip()

    # -------------------------------------------------------------------------
    # Phase A / TX_A2: Owned Session — Resolution & Persistence
    # -------------------------------------------------------------------------
    tx2_integrity_error: IntegrityError | None = None
    tx2_failure: Exception | None = None

    async with factory() as tx2_db:
        try:
            locked_org_final = await _lock_organization(tx2_db, organization_id)
        except Exception as exc:
            await _safe_rollback(tx2_db)
            tx2_failure = exc
        else:
            if locked_org_final is None:
                logger.error(
                    "Organization %s became unavailable before Razorpay customer %s could be persisted.",
                    organization_id,
                    customer_id,
                )
                await _record_superseded_customer_reconciliation(
                    db=tx2_db,
                    organization_id=organization_id,
                    customer_id=customer_id,
                    stale_claim_op_id=claim_op_id,
                    reason="organization_unavailable_after_customer_created",
                    organization_exists=False,
                )
                raise RazorpayCustomerError(
                    f"Organization {organization_id} became unavailable before Razorpay customer {customer_id} "
                    f"could be persisted. Remote customer preserved in reconciliation."
                )

            try:
                target_provision = await _find_provision(tx2_db, organization_id, for_update=True)
            except Exception as exc:
                await _safe_rollback(tx2_db)
                tx2_failure = exc
            else:
                if target_provision is None or target_provision.claim_operation_id != claim_op_id:
                    persisted_claim_op_id = (
                        target_provision.claim_operation_id if target_provision is not None else None
                    )
                    persisted_status = (
                        target_provision.status.value if target_provision is not None else None
                    )
                    logger.warning(
                        "Stale worker detected for organization %s: claim operation %s superseded or lost ownership "
                        "(persisted claim: %s). Preserving remote customer %s in PaymentReconciliation.",
                        organization_id,
                        claim_op_id,
                        persisted_claim_op_id,
                        customer_id,
                    )
                    await _record_superseded_customer_reconciliation(
                        db=tx2_db,
                        organization_id=organization_id,
                        customer_id=customer_id,
                        stale_claim_op_id=claim_op_id,
                        reason="stale_worker_customer_created",
                        details={
                            "persisted_claim_op_id": persisted_claim_op_id,
                            "persisted_status": persisted_status,
                        },
                        organization_exists=True,
                    )
                    raise RazorpayCustomerError(
                        f"Customer provisioning claim {claim_op_id} for organization {organization_id} was superseded "
                        f"or lost ownership before remote response completed. Remote customer {customer_id} preserved in reconciliation."
                    )

                target_provision.provider_customer_id = customer_id
                target_provision.status = CustomerProvisionStatus.resolved
                target_provision.claim_operation_id = None
                target_provision.lease_expires_at = None
                target_provision.last_error_code = None

                try:
                    await tx2_db.commit()
                    return customer_id
                except IntegrityError as exc:
                    await _safe_rollback(tx2_db)
                    tx2_integrity_error = exc
                except Exception as exc:
                    await _safe_rollback(tx2_db)
                    tx2_failure = exc

    if tx2_integrity_error is not None:
        return await _handle_tx2_integrity_error_with_factory(
            session_factory=factory,
            organization_id=organization_id,
            customer_id=customer_id,
            claim_op_id=claim_op_id,
            tx2_integrity_error=tx2_integrity_error,
            merchant_account_id=merchant_account_id,
            environment=environment,
        )

    assert tx2_failure is not None
    await _handle_persistence_failure_with_factory(
        session_factory=factory,
        organization_id=organization_id,
        customer_id=customer_id,
        claim_op_id=claim_op_id,
        original_error=tx2_failure,
        merchant_account_id=merchant_account_id,
        environment=environment,
    )
