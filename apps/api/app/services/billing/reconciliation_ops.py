"""Operator reconciliation inspection and audited resolution services for Aptlyst AI.

Architecture & Safety Guarantees:
1. Read-Only Inspection vs. Audited Resolution:
   - Strictly separates read-only inspection queries from audited mutations on
     ``PaymentReconciliation`` (``payment_reconciliations``).
   - Quarantined webhook events (``BillingWebhookEvent``) and exhausted checkout recovery
     attempts (``BillingCheckoutAttempt``) are intentionally **read-only** in Stage 2K-B:
     ``BillingWebhookEvent`` lacks an append-only operator redrive audit trail and conflates
     cumulative attempts with the worker retry limit, while ``BillingCheckoutAttempt`` relies
     on leased claim fencing and active-attempt uniqueness where blind retries or lease resets
     cannot be proven safe without a dedicated migration and external gateway verification.
   - Legacy ``BillingReconciliation`` (``billing_reconciliations``) is Stripe-only and lacks
     ``environment``, ``merchant_account_id``, ``case_key``, and ``updated_at`` columns; it is
     exposed strictly for read-only visibility.

2. Environment & Tenant Isolation (Fail-Closed):
   - Validates ``environment`` against deployment policy via
     ``validate_deployment_environment`` and ``resolve_required_razorpay_environment``.
   - Rejects ``'live'`` access inside non-production deployments and ``'test'`` access inside
     production deployments.
   - Verifies ``PaymentReconciliation.environment`` matches the validated scope before
     reading/mutating and enforces optional ``organization_id`` and
     ``merchant_account_id`` ownership filters.

3. State Machine & Concurrency Controls:
   - Permitted operator resolution transitions on ``PaymentReconciliation``:
     ``pending_review`` -> ``resolved_refunded`` | ``resolved_ignored``.
   - ``resolved_refunded`` records only an operator's verified ledger determination and
     NEVER initiates an upstream Razorpay refund.
   - Acquires ADR-005 Level 0A organization advisory lock (when bound to an organization)
     before row-locking ``PaymentReconciliation`` via ``SELECT ... FOR UPDATE``.
   - Preserves idempotency for exact retries by the same operator and fails closed on
     conflicting concurrent resolutions or invalid transitions.

4. Zero Secret / PII Leakage:
   - Sanitizes all diagnostic payloads, error messages, and resolution notes using
     ``sanitize_conflict_diagnostic_payload`` and ``sanitize_billing_error_message``.
   - Never returns raw webhook ``payload`` or ``request_payload`` objects verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import (
    ALLOWED_RAZORPAY_ENVIRONMENTS,
    PROHIBITED_PRODUCTION_MERCHANT_IDS,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
)
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingPayment,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    BillingReconciliation,
    ReconciliationStatus,
    Subscription,
    WebhookProcessingStatus,
)
from app.services.billing.advisory_lock import acquire_advisory_lock
from app.services.billing.webhook_errors import (
    sanitize_billing_error_message,
    sanitize_conflict_diagnostic_payload,
)

logger = logging.getLogger(__name__)

MIN_RESOLUTION_NOTES_LENGTH: int = 5
MAX_RESOLUTION_NOTES_LENGTH: int = 2000
MAX_PAGE_LIMIT: int = 100
DEFAULT_PAGE_LIMIT: int = 25

ALLOWED_OPERATOR_TARGET_STATUSES: frozenset[ReconciliationStatus] = frozenset({
    ReconciliationStatus.resolved_refunded,
    ReconciliationStatus.resolved_ignored,
})

# Allowed non-PII diagnostic keys surfaced from quarantined webhook/recovery payloads
_SAFE_ENTITY_REF_KEYS: tuple[str, ...] = (
    "subscription_id",
    "payment_id",
    "invoice_id",
    "customer_id",
    "order_id",
    "plan_id",
)


# =============================================================================
# Exceptions (Explicit Fail-Closed Hierarchy)
# =============================================================================


class ReconciliationOpsError(Exception):
    """Base exception for operator billing reconciliation operations."""


class InvalidOperatorInputError(ReconciliationOpsError):
    """Raised when operator identity, resolution notes, or pagination arguments are invalid."""


class EnvironmentIsolationError(ReconciliationOpsError):
    """Raised when an operation violates deployment or payment environment boundaries."""


class ReconciliationNotFoundError(ReconciliationOpsError):
    """Raised when a requested case, webhook event, or checkout attempt is not found."""


class ReconciliationTenantMismatchError(ReconciliationOpsError):
    """Raised when a record's organization does not match the requested tenant scope."""


class ReconciliationScopeMismatchError(ReconciliationOpsError):
    """Raised when provider or merchant account provenance does not match the expected scope."""


class InvalidReconciliationTransitionError(ReconciliationOpsError):
    """Raised when an invalid or unsupported status transition is attempted."""


class ConcurrentReconciliationConflictError(ReconciliationOpsError):
    """Raised when a concurrent or competing resolution conflict is detected."""


# =============================================================================
# Structured Result DTOs (Zero Secrets / Unnecessary PII)
# =============================================================================


@dataclass(frozen=True)
class ReconciliationSummary:
    """Compact sanitized summary of a PaymentReconciliation record."""

    id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    case_key: str
    reason: str
    status: ReconciliationStatus
    organization_id: uuid.UUID | None
    subscription_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    payment_id: uuid.UUID | None
    raw_event_id: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "merchant_account_id": self.merchant_account_id,
            "environment": self.environment,
            "case_key": self.case_key,
            "reason": self.reason,
            "status": self.status.value,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "subscription_id": str(self.subscription_id) if self.subscription_id else None,
            "invoice_id": str(self.invoice_id) if self.invoice_id else None,
            "payment_id": str(self.payment_id) if self.payment_id else None,
            "raw_event_id": str(self.raw_event_id) if self.raw_event_id else None,
            "resolved_by": str(self.resolved_by) if self.resolved_by else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ReconciliationDetail:
    """Detailed sanitized view of a PaymentReconciliation case and linked references."""

    summary: ReconciliationSummary
    organization_name: str | None
    subscription_status: str | None
    provider_subscription_id: str | None
    provider_payment_id: str | None
    payment_status: str | None
    payment_amount_minor_units: int | None
    payment_currency: str | None
    raw_provider_event_id: str | None
    resolution_notes: str | None
    diagnostic_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary.to_dict(),
            "organization_name": self.organization_name,
            "subscription_status": self.subscription_status,
            "provider_subscription_id": self.provider_subscription_id,
            "provider_payment_id": self.provider_payment_id,
            "payment_status": self.payment_status,
            "payment_amount_minor_units": self.payment_amount_minor_units,
            "payment_currency": self.payment_currency,
            "raw_provider_event_id": self.raw_provider_event_id,
            "resolution_notes": self.resolution_notes,
            "diagnostic_payload": self.diagnostic_payload,
        }


@dataclass(frozen=True)
class PaginatedReconciliations:
    """Paginated read-only result of PaymentReconciliation records."""

    items: tuple[ReconciliationSummary, ...]
    total_count: int
    limit: int
    offset: int
    environment: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ReconciliationResolutionResult:
    """Audited outcome of resolving a PaymentReconciliation case."""

    case: ReconciliationSummary
    resolution_notes: str
    changed: bool
    idempotent_replay: bool
    notice: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "idempotent_replay": self.idempotent_replay,
            "notice": self.notice,
            "resolution_notes": self.resolution_notes,
            "case": self.case.to_dict(),
        }


@dataclass(frozen=True)
class QuarantinedWebhookSummary:
    """Read-only sanitized summary of a quarantined BillingWebhookEvent."""

    id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    provider_event_id: str
    event_type: str
    status: str
    attempts: int
    signature_verified: bool
    received_at: datetime
    processed_at: datetime | None
    quarantined_at: datetime | None
    quarantine_reason: str | None
    error_message: str | None
    entity_refs: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "merchant_account_id": self.merchant_account_id,
            "environment": self.environment,
            "provider_event_id": self.provider_event_id,
            "event_type": self.event_type,
            "status": self.status,
            "attempts": self.attempts,
            "signature_verified": self.signature_verified,
            "received_at": self.received_at.isoformat(),
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "quarantined_at": self.quarantined_at.isoformat() if self.quarantined_at else None,
            "quarantine_reason": self.quarantine_reason,
            "error_message": self.error_message,
            "entity_refs": self.entity_refs,
        }


@dataclass(frozen=True)
class PaginatedQuarantinedWebhooks:
    """Paginated read-only result of quarantined BillingWebhookEvent records."""

    items: tuple[QuarantinedWebhookSummary, ...]
    total_count: int
    limit: int
    offset: int
    environment: str
    redrive_supported: bool = False
    redrive_limitation: str = (
        "Automated webhook re-drive is disabled in Stage 2K-B: BillingWebhookEvent lacks an "
        "append-only operator redrive audit log and uses cumulative attempts for retry gating."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "redrive_supported": self.redrive_supported,
            "redrive_limitation": self.redrive_limitation,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ExhaustedRecoverySummary:
    """Read-only sanitized summary of an exhausted/quarantined BillingCheckoutAttempt."""

    id: uuid.UUID
    organization_id: uuid.UUID
    provider: str
    merchant_account_id: str
    environment: str
    status: str
    recovery_attempts: int
    provider_customer_id: str | None
    provider_subscription_id: str | None
    provider_order_id: str | None
    provider_payment_id: str | None
    transient_failures: int
    expired_lease_count: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    linked_reconciliation_id: uuid.UUID | None = None
    linked_reconciliation_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "organization_id": str(self.organization_id),
            "provider": self.provider,
            "merchant_account_id": self.merchant_account_id,
            "environment": self.environment,
            "status": self.status,
            "recovery_attempts": self.recovery_attempts,
            "provider_customer_id": self.provider_customer_id,
            "provider_subscription_id": self.provider_subscription_id,
            "provider_order_id": self.provider_order_id,
            "provider_payment_id": self.provider_payment_id,
            "transient_failures": self.transient_failures,
            "expired_lease_count": self.expired_lease_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "linked_reconciliation_id": (
                str(self.linked_reconciliation_id) if self.linked_reconciliation_id else None
            ),
            "linked_reconciliation_status": self.linked_reconciliation_status,
        }


@dataclass(frozen=True)
class PaginatedExhaustedRecoveries:
    """Paginated read-only result of exhausted/quarantined checkout recovery attempts."""

    items: tuple[ExhaustedRecoverySummary, ...]
    total_count: int
    limit: int
    offset: int
    environment: str
    retry_supported: bool = False
    retry_limitation: str = (
        "Automated recovery retry is disabled in Stage 2K-B: resetting recovery_attempts or "
        "claim leases bypasses bounded quarantine budgets and risks unsafe retries under "
        "active checkout attempt exclusivity without gateway verification and audit storage."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "retry_supported": self.retry_supported,
            "retry_limitation": self.retry_limitation,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class LegacyStripeReconciliationSummary:
    """Read-only summary of a legacy Stripe-only BillingReconciliation row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    event_id: str
    existing_subscription_id: str
    incoming_subscription_id: str
    reason: str
    status: str
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_notes: str | None
    created_at: datetime
    diagnostic_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": "stripe_legacy",
            "environment": None,
            "organization_id": str(self.organization_id),
            "event_id": self.event_id,
            "existing_subscription_id": self.existing_subscription_id,
            "incoming_subscription_id": self.incoming_subscription_id,
            "reason": self.reason,
            "status": self.status,
            "resolved_by": str(self.resolved_by) if self.resolved_by else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_notes": self.resolution_notes,
            "created_at": self.created_at.isoformat(),
            "diagnostic_payload": self.diagnostic_payload,
        }


@dataclass(frozen=True)
class PaginatedLegacyReconciliations:
    """Paginated read-only result of legacy Stripe BillingReconciliation rows."""

    items: tuple[LegacyStripeReconciliationSummary, ...]
    total_count: int
    limit: int
    offset: int
    read_only_notice: str = (
        "Legacy Stripe billing_reconciliations rows lack environment and merchant_account_id "
        "columns and remain strictly read-only in Stage 2K-B."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "read_only_notice": self.read_only_notice,
            "items": [item.to_dict() for item in self.items],
        }


# =============================================================================
# Validation & Sanitization Helpers
# =============================================================================


def validate_operator_environment(
    environment: str | None,
    *,
    deployment_environment: str | None = None,
    require_explicit: bool = True,
) -> str:
    """Validate requested billing environment against deployment isolation policy.

    Fail-closed rules:
    - Deployment environment must be recognized (production vs non-production).
    - Production deployments permit only 'live'.
    - Non-production deployments permit only 'test'.
    """
    try:
        resolved_deployment = (
            getattr(settings, "environment", None)
            if deployment_environment is None
            else deployment_environment
        )
        _, is_prod = validate_deployment_environment(resolved_deployment)
        required_rzp_env = resolve_required_razorpay_environment(resolved_deployment)
    except ValueError as exc:
        raise EnvironmentIsolationError(str(exc)) from exc

    if environment is None:
        if require_explicit:
            raise EnvironmentIsolationError(
                "Explicit environment ('test' or 'live') is required for this operation."
            )
        return required_rzp_env

    if not isinstance(environment, str) or not environment.strip():
        raise EnvironmentIsolationError(
            "Environment must be non-empty ('test' or 'live')."
        )

    clean_env = environment.strip().lower()
    if clean_env not in ALLOWED_RAZORPAY_ENVIRONMENTS:
        raise EnvironmentIsolationError(
            f"Unsupported payment environment '{environment}'. Must be 'test' or 'live'."
        )

    if clean_env != required_rzp_env:
        scope_label = "production" if is_prod else "non-production"
        raise EnvironmentIsolationError(
            f"Environment isolation violation: {scope_label} deployment strictly requires "
            f"environment='{required_rzp_env}', got '{clean_env}'."
        )

    return clean_env


def validate_merchant_account_filter(
    merchant_account_id: str | None,
    *,
    environment: str,
) -> str | None:
    """Validate optional merchant account filter against environment policy."""
    if merchant_account_id is None:
        return None
    if not isinstance(merchant_account_id, str) or not merchant_account_id.strip():
        raise InvalidOperatorInputError("merchant_account_id filter must be non-empty.")
    clean = merchant_account_id.strip()
    if environment == "live" and clean.lower() in PROHIBITED_PRODUCTION_MERCHANT_IDS:
        raise EnvironmentIsolationError(
            f"Prohibited placeholder merchant_account_id '{clean}' for 'live' environment."
        )
    return clean


def validate_operator_uuid(operator_id: uuid.UUID | str | None) -> uuid.UUID:
    """Validate non-empty, non-nil operator UUID for audited mutations."""
    if operator_id is None:
        raise InvalidOperatorInputError("A non-empty operator UUID is required.")
    if isinstance(operator_id, uuid.UUID):
        parsed = operator_id
    elif isinstance(operator_id, str):
        raw = operator_id.strip()
        if not raw:
            raise InvalidOperatorInputError("Operator identity cannot be empty.")
        try:
            parsed = uuid.UUID(raw)
        except ValueError as exc:
            raise InvalidOperatorInputError(
                "Operator identity must be a valid UUID matching resolved_by schema."
            ) from exc
    else:
        raise InvalidOperatorInputError("Operator identity must be a valid UUID.")

    if parsed.int == 0:
        raise InvalidOperatorInputError("Nil UUID (00000000-...) is not a permitted operator ID.")
    return parsed


def validate_resolution_notes(notes: str | None) -> str:
    """Validate and sanitize non-empty meaningful resolution notes."""
    if not isinstance(notes, str):
        raise InvalidOperatorInputError("Resolution notes must be a non-empty string.")
    stripped = notes.strip()
    if len(stripped) < MIN_RESOLUTION_NOTES_LENGTH:
        raise InvalidOperatorInputError(
            f"Resolution notes must be at least {MIN_RESOLUTION_NOTES_LENGTH} non-whitespace characters."
        )
    if len(stripped) > MAX_RESOLUTION_NOTES_LENGTH:
        raise InvalidOperatorInputError(
            f"Resolution notes cannot exceed {MAX_RESOLUTION_NOTES_LENGTH} characters."
        )
    sanitized = sanitize_billing_error_message(stripped, max_chars=MAX_RESOLUTION_NOTES_LENGTH).strip()
    if len(sanitized) < MIN_RESOLUTION_NOTES_LENGTH:
        raise InvalidOperatorInputError(
            "Resolution notes must contain meaningful text after sanitization."
        )
    return sanitized


def validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Validate bounded limit and non-negative offset."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_LIMIT:
        raise InvalidOperatorInputError(
            f"Pagination limit must be an integer between 1 and {MAX_PAGE_LIMIT}."
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidOperatorInputError("Pagination offset must be a non-negative integer.")
    return limit, offset


def _sanitize_diagnostic_dict(raw: Any) -> dict[str, Any]:
    """Sanitize JSONB diagnostic payloads safely without leaking secrets/PII."""
    if not isinstance(raw, dict):
        return {}
    return sanitize_conflict_diagnostic_payload(raw, max_str_chars=512, max_depth=4)


def _extract_safe_webhook_entity_refs(payload: Any) -> dict[str, str]:
    """Extract only allowlisted non-PII gateway entity IDs from a webhook payload."""
    if not isinstance(payload, dict):
        return {}
    refs: dict[str, str] = {}
    inner = payload.get("payload")
    if isinstance(inner, dict):
        for section_name in ("subscription", "payment", "invoice", "order"):
            section = inner.get(section_name)
            if not isinstance(section, dict):
                continue
            entity = section.get("entity")
            if not isinstance(entity, dict):
                continue
            entity_id = entity.get("id")
            if isinstance(entity_id, str) and entity_id.strip():
                refs[f"{section_name}_id"] = entity_id.strip()[:128]
            for key in _SAFE_ENTITY_REF_KEYS:
                val = entity.get(key)
                if isinstance(val, str) and val.strip():
                    refs.setdefault(key, val.strip()[:128])
    return refs


def _to_reconciliation_summary(rec: PaymentReconciliation) -> ReconciliationSummary:
    return ReconciliationSummary(
        id=rec.id,
        provider=rec.provider,
        merchant_account_id=rec.merchant_account_id,
        environment=rec.environment,
        case_key=rec.case_key,
        reason=rec.reason,
        status=rec.status,
        organization_id=rec.organization_id,
        subscription_id=rec.subscription_id,
        invoice_id=rec.invoice_id,
        payment_id=rec.payment_id,
        raw_event_id=rec.raw_event_id,
        resolved_by=rec.resolved_by,
        resolved_at=rec.resolved_at,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _to_webhook_summary(event: BillingWebhookEvent) -> QuarantinedWebhookSummary:
    return QuarantinedWebhookSummary(
        id=event.id,
        provider=event.provider,
        merchant_account_id=event.merchant_account_id,
        environment=event.environment,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        status=event.status.value if hasattr(event.status, "value") else str(event.status),
        attempts=event.attempts,
        signature_verified=bool(event.signature_verified),
        received_at=event.received_at,
        processed_at=event.processed_at,
        quarantined_at=event.quarantined_at,
        quarantine_reason=(
            sanitize_billing_error_message(event.quarantine_reason, max_chars=512)
            if event.quarantine_reason
            else None
        ),
        error_message=(
            sanitize_billing_error_message(event.error_message, max_chars=512)
            if event.error_message
            else None
        ),
        entity_refs=_extract_safe_webhook_entity_refs(event.payload),
    )


def _to_recovery_summary(
    attempt: BillingCheckoutAttempt,
    linked_rec: tuple[uuid.UUID, ReconciliationStatus] | None = None,
) -> ExhaustedRecoverySummary:
    req_payload = attempt.request_payload if isinstance(attempt.request_payload, dict) else {}
    transient_failures = int(req_payload.get("transient_failures", 0) or 0)
    expired_lease_count = int(req_payload.get("expired_lease_count", 0) or 0)
    rec_id = linked_rec[0] if linked_rec else None
    rec_status = (
        linked_rec[1].value if linked_rec and hasattr(linked_rec[1], "value") else None
    )
    return ExhaustedRecoverySummary(
        id=attempt.id,
        organization_id=attempt.organization_id,
        provider=attempt.provider,
        merchant_account_id=attempt.merchant_account_id,
        environment=attempt.environment,
        status=attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status),
        recovery_attempts=attempt.recovery_attempts,
        provider_customer_id=attempt.provider_customer_id,
        provider_subscription_id=attempt.provider_subscription_id,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=attempt.provider_payment_id,
        transient_failures=transient_failures,
        expired_lease_count=expired_lease_count,
        error_message=(
            sanitize_billing_error_message(attempt.error_message, max_chars=512)
            if attempt.error_message
            else None
        ),
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
        linked_reconciliation_id=rec_id,
        linked_reconciliation_status=rec_status,
    )


# =============================================================================
# Read-Only Reconciliation Inspection Operations
# =============================================================================


async def list_payment_reconciliations(
    db: AsyncSession,
    *,
    environment: str | None = None,
    deployment_environment: str | None = None,
    status: ReconciliationStatus | None = ReconciliationStatus.pending_review,
    reason: str | None = None,
    min_age_seconds: int | None = None,
    organization_id: uuid.UUID | None = None,
    merchant_account_id: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    now: datetime | None = None,
) -> PaginatedReconciliations:
    """List PaymentReconciliation cases with strict environment isolation and pagination."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=False,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )
    clean_limit, clean_offset = validate_pagination(limit, offset)

    filters: list[Any] = [PaymentReconciliation.environment == validated_env]
    if status is not None:
        filters.append(PaymentReconciliation.status == status)
    if reason is not None:
        clean_reason = reason.strip()
        if not clean_reason:
            raise InvalidOperatorInputError("Reason filter must not be empty.")
        filters.append(PaymentReconciliation.reason == clean_reason)
    if organization_id is not None:
        filters.append(PaymentReconciliation.organization_id == organization_id)
    if validated_merchant is not None:
        filters.append(PaymentReconciliation.merchant_account_id == validated_merchant)
    if min_age_seconds is not None:
        if min_age_seconds < 0:
            raise InvalidOperatorInputError("min_age_seconds must be non-negative.")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=min_age_seconds)
        filters.append(PaymentReconciliation.created_at <= cutoff)

    count_stmt = select(func.count(PaymentReconciliation.id)).where(*filters)
    total_count = int((await db.execute(count_stmt)).scalar_one() or 0)

    list_stmt = (
        select(PaymentReconciliation)
        .where(*filters)
        .order_by(PaymentReconciliation.created_at.asc(), PaymentReconciliation.id.asc())
        .limit(clean_limit)
        .offset(clean_offset)
    )
    rows = list((await db.execute(list_stmt)).scalars().all())
    return PaginatedReconciliations(
        items=tuple(_to_reconciliation_summary(r) for r in rows),
        total_count=total_count,
        limit=clean_limit,
        offset=clean_offset,
        environment=validated_env,
    )


async def inspect_payment_reconciliation(
    db: AsyncSession,
    *,
    case_id: uuid.UUID,
    environment: str,
    deployment_environment: str | None = None,
    organization_id: uuid.UUID | None = None,
    merchant_account_id: str | None = None,
) -> ReconciliationDetail:
    """Inspect a single PaymentReconciliation case with sanitized diagnostic context."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=True,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )

    stmt = select(PaymentReconciliation).where(PaymentReconciliation.id == case_id)
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if rec is None:
        raise ReconciliationNotFoundError(f"Reconciliation case {case_id} not found.")

    if (rec.environment or "").strip().lower() != validated_env:
        raise EnvironmentIsolationError(
            f"Reconciliation case {case_id} belongs to environment '{rec.environment}', "
            f"which cannot be inspected in '{validated_env}' scope."
        )
    if organization_id is not None and rec.organization_id != organization_id:
        raise ReconciliationTenantMismatchError(
            f"Reconciliation case {case_id} does not belong to organization {organization_id}."
        )
    if validated_merchant is not None and rec.merchant_account_id != validated_merchant:
        raise ReconciliationScopeMismatchError(
            f"Reconciliation case {case_id} merchant '{rec.merchant_account_id}' does not match '{validated_merchant}'."
        )

    org_name: str | None = None
    if rec.organization_id is not None:
        org_res = await db.execute(
            select(Organization.name).where(Organization.id == rec.organization_id)
        )
        org_name = org_res.scalar_one_or_none()

    sub_status: str | None = None
    provider_sub_id: str | None = None
    if rec.subscription_id is not None:
        sub_res = await db.execute(
            select(Subscription.status, Subscription.provider_subscription_id).where(
                Subscription.id == rec.subscription_id
            )
        )
        sub_row = sub_res.one_or_none()
        if sub_row is not None:
            sub_status = (
                sub_row[0].value if hasattr(sub_row[0], "value") else str(sub_row[0])
            )
            provider_sub_id = sub_row[1]

    pay_provider_id: str | None = None
    pay_status: str | None = None
    pay_amount: int | None = None
    pay_currency: str | None = None
    if rec.payment_id is not None:
        pay_res = await db.execute(
            select(
                BillingPayment.provider_payment_id,
                BillingPayment.status,
                BillingPayment.amount_minor_units,
                BillingPayment.currency,
            ).where(BillingPayment.id == rec.payment_id)
        )
        pay_row = pay_res.one_or_none()
        if pay_row is not None:
            pay_provider_id, pay_status, pay_amount, pay_currency = pay_row

    raw_provider_event_id: str | None = None
    if rec.raw_event_id is not None:
        evt_res = await db.execute(
            select(BillingWebhookEvent.provider_event_id).where(
                BillingWebhookEvent.id == rec.raw_event_id
            )
        )
        raw_provider_event_id = evt_res.scalar_one_or_none()

    clean_notes = (
        sanitize_billing_error_message(rec.resolution_notes, max_chars=MAX_RESOLUTION_NOTES_LENGTH)
        if rec.resolution_notes
        else None
    )

    return ReconciliationDetail(
        summary=_to_reconciliation_summary(rec),
        organization_name=org_name,
        subscription_status=sub_status,
        provider_subscription_id=provider_sub_id,
        provider_payment_id=pay_provider_id,
        payment_status=pay_status,
        payment_amount_minor_units=pay_amount,
        payment_currency=pay_currency,
        raw_provider_event_id=raw_provider_event_id,
        resolution_notes=clean_notes,
        diagnostic_payload=_sanitize_diagnostic_dict(rec.diagnostic_payload),
    )


# =============================================================================
# Audited Reconciliation Resolution (Transaction + Locking + Idempotency)
# =============================================================================


async def _lock_payment_reconciliation_in_order(
    db: AsyncSession,
    case_id: uuid.UUID,
) -> PaymentReconciliation:
    """Lock PaymentReconciliation adhering strictly to ADR-005 lock hierarchy.

    Level 0A organization advisory lock (if organization_id is bound) must precede
    Level 3/row-level SELECT ... FOR UPDATE on PaymentReconciliation.
    """
    peek_stmt = select(
        PaymentReconciliation.id,
        PaymentReconciliation.organization_id,
    ).where(PaymentReconciliation.id == case_id)
    peek_row = (await db.execute(peek_stmt)).one_or_none()
    if peek_row is None:
        raise ReconciliationNotFoundError(f"Reconciliation case {case_id} not found.")

    peeked_org_id = peek_row[1]
    if peeked_org_id is not None:
        await acquire_advisory_lock(db, "organization", peeked_org_id)

    lock_stmt = (
        select(PaymentReconciliation)
        .where(PaymentReconciliation.id == case_id)
        .with_for_update()
    )
    rec = (await db.execute(lock_stmt)).scalar_one_or_none()
    if rec is None:
        raise ReconciliationNotFoundError(f"Reconciliation case {case_id} not found.")

    # If a concurrent writer bound organization_id between peek and row lock,
    # roll back and reacquire Level 0A advisory lock first to avoid lock-order inversion.
    if peeked_org_id is None and rec.organization_id is not None:
        target_org_id = rec.organization_id
        await db.rollback()
        await acquire_advisory_lock(db, "organization", target_org_id)
        rec = (await db.execute(lock_stmt)).scalar_one_or_none()
        if rec is None:
            raise ReconciliationNotFoundError(f"Reconciliation case {case_id} not found.")

    return rec


async def resolve_payment_reconciliation(
    db: AsyncSession,
    *,
    case_id: uuid.UUID,
    environment: str,
    target_status: ReconciliationStatus,
    operator_id: uuid.UUID | str,
    resolution_notes: str,
    deployment_environment: str | None = None,
    organization_id: uuid.UUID | None = None,
    merchant_account_id: str | None = None,
    now: datetime | None = None,
) -> ReconciliationResolutionResult:
    """Resolve a pending_review PaymentReconciliation case with full auditability.

    Invariants enforced:
    1. Fail-closed environment, merchant, and optional tenant isolation.
    2. Mandatory non-nil operator UUID and meaningful sanitized notes.
    3. Allowed state transitions strictly limited to:
       pending_review -> resolved_refunded | resolved_ignored.
    4. Exact retries by the same operator with identical target status and notes
       are idempotent no-ops (preserving original resolved_at).
    5. Competing operators, mismatched notes, or conflicting target statuses fail
       closed with explicit exceptions without mutating historical state.
    6. Setting status='resolved_refunded' never triggers an upstream Razorpay refund.
    """
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=True,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )
    parsed_operator_id = validate_operator_uuid(operator_id)
    clean_notes = validate_resolution_notes(resolution_notes)

    if target_status not in ALLOWED_OPERATOR_TARGET_STATUSES:
        raise InvalidReconciliationTransitionError(
            f"Unsupported operator resolution target status '{getattr(target_status, 'value', target_status)}'. "
            "Allowed transitions from 'pending_review' are only 'resolved_refunded' or 'resolved_ignored'."
        )

    now_utc = now or datetime.now(timezone.utc)

    try:
        rec = await _lock_payment_reconciliation_in_order(db, case_id)

        if (rec.environment or "").strip().lower() != validated_env:
            raise EnvironmentIsolationError(
                f"Cannot resolve reconciliation case {case_id}: case environment '{rec.environment}' "
                f"does not match validated scope '{validated_env}'."
            )

        if organization_id is not None and rec.organization_id != organization_id:
            raise ReconciliationTenantMismatchError(
                f"Cannot resolve reconciliation case {case_id}: organization mismatch."
            )

        if validated_merchant is not None and rec.merchant_account_id != validated_merchant:
            raise ReconciliationScopeMismatchError(
                f"Cannot resolve reconciliation case {case_id}: merchant account mismatch."
            )

        current_status = rec.status
        if current_status != ReconciliationStatus.pending_review:
            if current_status != target_status:
                raise InvalidReconciliationTransitionError(
                    f"Cannot transition reconciliation case {case_id} from '{current_status.value}' "
                    f"to '{target_status.value}'. Only 'pending_review' cases can be resolved."
                )

            if rec.resolved_by is None:
                raise ConcurrentReconciliationConflictError(
                    f"Reconciliation case {case_id} is already system-resolved as '{current_status.value}' "
                    "without an operator UUID and cannot be overwritten."
                )

            if rec.resolved_by != parsed_operator_id:
                raise ConcurrentReconciliationConflictError(
                    f"Reconciliation case {case_id} was already resolved as '{current_status.value}' "
                    f"by operator {rec.resolved_by}."
                )

            existing_notes = (rec.resolution_notes or "").strip()
            if existing_notes != clean_notes:
                raise ConcurrentReconciliationConflictError(
                    f"Reconciliation case {case_id} was already resolved by operator {parsed_operator_id} "
                    "with different resolution notes."
                )

            await db.rollback()
            return ReconciliationResolutionResult(
                case=_to_reconciliation_summary(rec),
                resolution_notes=existing_notes,
                changed=False,
                idempotent_replay=True,
                notice=(
                    "No-op idempotent replay: case already resolved with identical status, "
                    "operator, and notes."
                ),
            )

        rec.status = target_status
        rec.resolved_by = parsed_operator_id
        rec.resolved_at = now_utc
        rec.resolution_notes = clean_notes
        rec.updated_at = now_utc

        payload_copy = dict(rec.diagnostic_payload) if isinstance(rec.diagnostic_payload, dict) else {}
        history = list(payload_copy.get("history", [])) if isinstance(payload_copy.get("history"), list) else []
        history.append({
            "timestamp": now_utc.isoformat(),
            "action": "operator_resolution",
            "from_status": current_status.value,
            "to_status": target_status.value,
            "operator_id": str(parsed_operator_id),
            "resolution_notes": clean_notes,
        })
        payload_copy["history"] = history
        rec.diagnostic_payload = payload_copy
        flag_modified(rec, "diagnostic_payload")

        await db.commit()
        await db.refresh(rec)

        logger.info(
            "Operator %s resolved PaymentReconciliation case %s (env=%s, status=%s)",
            parsed_operator_id,
            rec.id,
            rec.environment,
            target_status.value,
        )

        notice = (
            "Marked 'resolved_refunded' (operator audit label only; no Razorpay refund was initiated "
            "or independently verified)."
            if target_status == ReconciliationStatus.resolved_refunded
            else "Marked 'resolved_ignored' (operator audit record committed)."
        )
        return ReconciliationResolutionResult(
            case=_to_reconciliation_summary(rec),
            resolution_notes=clean_notes,
            changed=True,
            idempotent_replay=False,
            notice=notice,
        )
    except ReconciliationOpsError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


# =============================================================================
# Read-Only Quarantined Webhook Inspection
# =============================================================================


async def list_quarantined_webhooks(
    db: AsyncSession,
    *,
    environment: str | None = None,
    deployment_environment: str | None = None,
    event_type: str | None = None,
    min_age_seconds: int | None = None,
    merchant_account_id: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    now: datetime | None = None,
) -> PaginatedQuarantinedWebhooks:
    """List quarantined BillingWebhookEvent rows strictly read-only."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=False,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )
    clean_limit, clean_offset = validate_pagination(limit, offset)

    filters: list[Any] = [
        BillingWebhookEvent.environment == validated_env,
        BillingWebhookEvent.status == WebhookProcessingStatus.quarantined,
    ]
    if event_type is not None:
        clean_type = event_type.strip()
        if not clean_type:
            raise InvalidOperatorInputError("event_type filter must not be empty.")
        filters.append(BillingWebhookEvent.event_type == clean_type)
    if validated_merchant is not None:
        filters.append(BillingWebhookEvent.merchant_account_id == validated_merchant)
    if min_age_seconds is not None:
        if min_age_seconds < 0:
            raise InvalidOperatorInputError("min_age_seconds must be non-negative.")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=min_age_seconds)
        filters.append(
            func.coalesce(
                BillingWebhookEvent.quarantined_at, BillingWebhookEvent.received_at
            )
            <= cutoff
        )

    count_stmt = select(func.count(BillingWebhookEvent.id)).where(*filters)
    total_count = int((await db.execute(count_stmt)).scalar_one() or 0)

    list_stmt = (
        select(BillingWebhookEvent)
        .where(*filters)
        .order_by(
            BillingWebhookEvent.quarantined_at.asc().nulls_last(),
            BillingWebhookEvent.received_at.asc(),
        )
        .limit(clean_limit)
        .offset(clean_offset)
    )
    rows = list((await db.execute(list_stmt)).scalars().all())
    return PaginatedQuarantinedWebhooks(
        items=tuple(_to_webhook_summary(row) for row in rows),
        total_count=total_count,
        limit=clean_limit,
        offset=clean_offset,
        environment=validated_env,
    )


async def inspect_quarantined_webhook(
    db: AsyncSession,
    *,
    event_id: uuid.UUID,
    environment: str,
    deployment_environment: str | None = None,
    merchant_account_id: str | None = None,
) -> QuarantinedWebhookSummary:
    """Inspect a single quarantined BillingWebhookEvent record strictly read-only."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=True,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )

    stmt = select(BillingWebhookEvent).where(BillingWebhookEvent.id == event_id)
    event = (await db.execute(stmt)).scalar_one_or_none()
    if event is None:
        raise ReconciliationNotFoundError(f"Webhook event {event_id} not found.")
    if (event.environment or "").strip().lower() != validated_env:
        raise EnvironmentIsolationError(
            f"Webhook event {event_id} belongs to environment '{event.environment}', "
            f"not '{validated_env}'."
        )
    if validated_merchant is not None and event.merchant_account_id != validated_merchant:
        raise ReconciliationScopeMismatchError(
            f"Webhook event {event_id} merchant '{event.merchant_account_id}' does not match '{validated_merchant}'."
        )
    return _to_webhook_summary(event)


# =============================================================================
# Read-Only Exhausted / Quarantined Recovery Attempt Inspection
# =============================================================================


async def _lookup_linked_attempt_reconciliations(
    db: AsyncSession,
    *,
    environment: str,
    attempt_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[uuid.UUID, ReconciliationStatus]]:
    if not attempt_ids:
        return {}
    prefixes = (
        "recovery_budget_exhausted:",
        "recovery_transient_exhausted:",
        "recovery_worker_crashes:",
        "recovery_quarantined:",
        "recovery_multiple_candidates:",
        "recovery_definitive_failure:",
    )
    case_keys = [f"{prefix}{aid}" for aid in attempt_ids for prefix in prefixes]
    stmt = select(PaymentReconciliation).where(
        PaymentReconciliation.environment == environment,
        PaymentReconciliation.case_key.in_(case_keys),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    mapping: dict[uuid.UUID, tuple[uuid.UUID, ReconciliationStatus]] = {}
    for rec in rows:
        try:
            suffix = rec.case_key.rsplit(":", 1)[-1]
            aid = uuid.UUID(suffix)
        except ValueError:
            continue
        mapping.setdefault(aid, (rec.id, rec.status))
    return mapping


async def list_exhausted_recovery_attempts(
    db: AsyncSession,
    *,
    environment: str | None = None,
    deployment_environment: str | None = None,
    organization_id: uuid.UUID | None = None,
    merchant_account_id: str | None = None,
    min_age_seconds: int | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    now: datetime | None = None,
) -> PaginatedExhaustedRecoveries:
    """List permanently quarantined/exhausted checkout recovery attempts strictly read-only."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=False,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )
    clean_limit, clean_offset = validate_pagination(limit, offset)

    filters: list[Any] = [
        BillingCheckoutAttempt.environment == validated_env,
        BillingCheckoutAttempt.status == CheckoutAttemptStatus.recovery_required,
        BillingCheckoutAttempt.next_recovery_retry_at.is_(None),
    ]
    if organization_id is not None:
        filters.append(BillingCheckoutAttempt.organization_id == organization_id)
    if validated_merchant is not None:
        filters.append(BillingCheckoutAttempt.merchant_account_id == validated_merchant)
    if min_age_seconds is not None:
        if min_age_seconds < 0:
            raise InvalidOperatorInputError("min_age_seconds must be non-negative.")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=min_age_seconds)
        filters.append(BillingCheckoutAttempt.updated_at <= cutoff)

    count_stmt = select(func.count(BillingCheckoutAttempt.id)).where(*filters)
    total_count = int((await db.execute(count_stmt)).scalar_one() or 0)

    list_stmt = (
        select(BillingCheckoutAttempt)
        .where(*filters)
        .order_by(BillingCheckoutAttempt.updated_at.asc(), BillingCheckoutAttempt.id.asc())
        .limit(clean_limit)
        .offset(clean_offset)
    )
    attempts = list((await db.execute(list_stmt)).scalars().all())
    linked_map = await _lookup_linked_attempt_reconciliations(
        db,
        environment=validated_env,
        attempt_ids=[att.id for att in attempts],
    )
    return PaginatedExhaustedRecoveries(
        items=tuple(_to_recovery_summary(att, linked_map.get(att.id)) for att in attempts),
        total_count=total_count,
        limit=clean_limit,
        offset=clean_offset,
        environment=validated_env,
    )


async def inspect_exhausted_recovery_attempt(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    environment: str,
    deployment_environment: str | None = None,
    organization_id: uuid.UUID | None = None,
    merchant_account_id: str | None = None,
) -> ExhaustedRecoverySummary:
    """Inspect a single BillingCheckoutAttempt record strictly read-only."""
    validated_env = validate_operator_environment(
        environment,
        deployment_environment=deployment_environment,
        require_explicit=True,
    )
    validated_merchant = validate_merchant_account_filter(
        merchant_account_id, environment=validated_env
    )

    stmt = select(BillingCheckoutAttempt).where(BillingCheckoutAttempt.id == attempt_id)
    attempt = (await db.execute(stmt)).scalar_one_or_none()
    if attempt is None:
        raise ReconciliationNotFoundError(f"Checkout attempt {attempt_id} not found.")
    if (attempt.environment or "").strip().lower() != validated_env:
        raise EnvironmentIsolationError(
            f"Checkout attempt {attempt_id} belongs to environment '{attempt.environment}', "
            f"not '{validated_env}'."
        )
    if organization_id is not None and attempt.organization_id != organization_id:
        raise ReconciliationTenantMismatchError(
            f"Checkout attempt {attempt_id} does not belong to organization {organization_id}."
        )
    if validated_merchant is not None and attempt.merchant_account_id != validated_merchant:
        raise ReconciliationScopeMismatchError(
            f"Checkout attempt {attempt_id} merchant '{attempt.merchant_account_id}' does not match '{validated_merchant}'."
        )

    linked_map = await _lookup_linked_attempt_reconciliations(
        db,
        environment=validated_env,
        attempt_ids=[attempt.id],
    )
    return _to_recovery_summary(attempt, linked_map.get(attempt.id))


# =============================================================================
# Legacy Stripe BillingReconciliation Read-Only Inspection
# =============================================================================


async def list_legacy_stripe_reconciliations(
    db: AsyncSession,
    *,
    status: ReconciliationStatus | None = None,
    organization_id: uuid.UUID | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> PaginatedLegacyReconciliations:
    """Read-only inspection of legacy Stripe BillingReconciliation rows."""
    clean_limit, clean_offset = validate_pagination(limit, offset)

    filters: list[Any] = []
    if status is not None:
        filters.append(BillingReconciliation.status == status)
    if organization_id is not None:
        filters.append(BillingReconciliation.organization_id == organization_id)

    count_stmt = select(func.count(BillingReconciliation.id)).where(*filters)
    total_count = int((await db.execute(count_stmt)).scalar_one() or 0)

    list_stmt = (
        select(BillingReconciliation)
        .where(*filters)
        .order_by(BillingReconciliation.created_at.asc(), BillingReconciliation.id.asc())
        .limit(clean_limit)
        .offset(clean_offset)
    )
    rows = list((await db.execute(list_stmt)).scalars().all())
    return PaginatedLegacyReconciliations(
        items=tuple(
            LegacyStripeReconciliationSummary(
                id=row.id,
                organization_id=row.organization_id,
                event_id=row.event_id,
                existing_subscription_id=row.existing_subscription_id,
                incoming_subscription_id=row.incoming_subscription_id,
                reason=row.reason,
                status=row.status.value if hasattr(row.status, "value") else str(row.status),
                resolved_by=row.resolved_by,
                resolved_at=row.resolved_at,
                resolution_notes=(
                    sanitize_billing_error_message(
                        row.resolution_notes, max_chars=MAX_RESOLUTION_NOTES_LENGTH
                    )
                    if row.resolution_notes
                    else None
                ),
                created_at=row.created_at,
                diagnostic_payload=_sanitize_diagnostic_dict(row.diagnostic_payload),
            )
            for row in rows
        ),
        total_count=total_count,
        limit=clean_limit,
        offset=clean_offset,
    )
