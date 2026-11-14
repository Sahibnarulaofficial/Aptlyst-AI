"""Comprehensive tests for D-04A Stage 2: Razorpay Webhook Exception Hierarchy Unification.

Verifies:
1. Root Identity & Alias Parity (UNIT-EXC-01):
   - RazorpayWebhookHandlerError is RazorpayWebhookEngineError
   - issubclass(RazorpayWebhookEngineError, BillingWebhookError)
   - issubclass(RazorpayWebhookHandlerError, BillingWebhookError)
2. Canonical Class Inheritance & Subclassing (UNIT-EXC-02):
   - Retryable hierarchy & concrete subclasses
   - Permanent hierarchy & concrete subclasses
   - Cross-module parity between webhook_engine, handler_base, and razorpay.__init__
3. StaleLeaseError Disjointness (UNIT-EXC-03):
   - Inherits from RazorpayWebhookEngineError and BillingWebhookLeaseError
   - Disjoint from Retryable and Permanent hierarchies
4. Package Export Parity & Ingress Isolation (UNIT-EXC-04):
   - Zero circular imports
   - Re-export identity across modules
   - Ingress RazorpayWebhookError remains isolated
5. Reconciliation Conflict Constructor & FK Safety (UNIT-EXC-05):
   - Dedicated kwargs: case_key, reason, diagnostic_payload
   - Backward-compatible kwargs: subscription_id, invoice_id, payment_id
   - Preferred kwargs: committed_subscription_id, committed_invoice_id, committed_payment_id
   - Automatic sanitization of diagnostic_payload
6. Structured Exception Metadata & Sanitized Message (UNIT-EXC-06):
   - failure_category, error_code, constraint_name
   - .sanitized_message property redacts secrets
7. Client Error Classification (UNIT-API-01):
   - Rate limits (429) -> RazorpayProviderTransientError
   - Server errors (5xx) -> RazorpayProviderTransientError
   - Pre-dispatch network error -> RazorpayProviderTransientError
   - Post-dispatch network error -> RazorpayProviderPermanentError
   - asyncio.TimeoutError -> RazorpayProviderTransientError
   - Auth errors (401) -> RazorpayProviderPermanentError
   - Config / disabled errors -> RazorpayProviderPermanentError
   - 4xx errors -> RazorpayProviderPermanentError
8. Database Exception Classification (UNIT-API-02):
   - classify_db_exception returns DbErrorClassification
   - classify_db_error_to_razorpay_exception maps permanent vs retryable DB errors
9. Sanitization Helper Delegation (UNIT-SAN-01):
   - webhook_engine.sanitize_error_message delegates to sanitize_billing_error_message
"""

from __future__ import annotations

import asyncio
from typing import Any
import uuid

import pytest

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
)
import app.services.billing.razorpay as rp
from app.services.billing.razorpay import handler_base as rzp_hb
from app.services.billing.razorpay import webhook_engine as rzp_we
from app.services.billing.razorpay import webhook_ingress as rzp_wi
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


# =============================================================================
# UNIT-EXC-01: Root Identity & Alias Parity
# =============================================================================


class TestRootIdentityAndAliasParity:
    """Verifies that Razorpay webhook root exception is unified and reparented."""

    def test_handler_error_is_engine_error_alias(self) -> None:
        """Flaw D04-02 resolution: RazorpayWebhookHandlerError IS RazorpayWebhookEngineError."""
        assert rzp_hb.RazorpayWebhookHandlerError is rzp_we.RazorpayWebhookEngineError
        assert rp.RazorpayWebhookHandlerError is rp.RazorpayWebhookEngineError

    def test_engine_error_inherits_from_billing_webhook_error(self) -> None:
        """RazorpayWebhookEngineError inherits from provider-neutral BillingWebhookError."""
        assert issubclass(rzp_we.RazorpayWebhookEngineError, BillingWebhookError)
        assert issubclass(rzp_hb.RazorpayWebhookHandlerError, BillingWebhookError)
        assert issubclass(rp.RazorpayWebhookEngineError, BillingWebhookError)

    def test_instantiation_root_attributes(self) -> None:
        """Root engine error inherits standard metadata fields."""
        err = rzp_we.RazorpayWebhookEngineError(
            "Test message with rzp_live_SECRET123",
            failure_category="custom_category",
            error_code="ERR_01",
            constraint_name="uq_test",
        )
        assert err.failure_category == "custom_category"
        assert err.error_code == "ERR_01"
        assert err.constraint_name == "uq_test"
        assert "rzp_live_SECRET123" not in err.sanitized_message
        assert "[REDACTED_KEY]" in err.sanitized_message


# =============================================================================
# UNIT-EXC-02: Canonical Class Inheritance & Subclassing
# =============================================================================


class TestCanonicalClassInheritance:
    """Verifies all subclasses adhere to the canonical dual-hierarchy blueprint."""

    def test_retryable_hierarchy(self) -> None:
        """All retryable classes inherit from RazorpayWebhookRetryableError and BillingWebhookRetryableError."""
        retryable_classes = [
            rzp_we.RazorpayWebhookRetryableError,
            rzp_we.RazorpayDatabaseRetryableError,
            rzp_we.RazorpayProviderTransientError,
            rzp_we.RazorpayTransientRaceRetryError,
        ]
        for cls in retryable_classes:
            assert issubclass(cls, rzp_we.RazorpayWebhookEngineError), f"{cls} must inherit from EngineError"
            assert issubclass(cls, rzp_we.RazorpayWebhookRetryableError), f"{cls} must inherit from RetryableError"
            assert issubclass(cls, BillingWebhookRetryableError), f"{cls} must inherit from BillingWebhookRetryableError"
            assert cls.is_retryable is True

    def test_permanent_hierarchy(self) -> None:
        """All permanent classes inherit from RazorpayWebhookPermanentError and BillingWebhookPermanentError."""
        permanent_classes = [
            rzp_we.RazorpayWebhookPermanentError,
            rzp_we.RazorpayPayloadValidationError,
            rzp_we.RazorpayMissingEntityIdError,
            rzp_we.RazorpayScopeMismatchError,
            rzp_we.RazorpayUnverifiedEventError,
            rzp_we.RazorpayUnknownEventError,
            rzp_we.RazorpayBusinessRejectionError,
            rzp_we.RazorpayDatabasePermanentError,
            rzp_we.RazorpayProviderPermanentError,
            rzp_we.RazorpayWebhookReconciliationConflictError,
        ]
        for cls in permanent_classes:
            assert issubclass(cls, rzp_we.RazorpayWebhookEngineError), f"{cls} must inherit from EngineError"
            assert issubclass(cls, rzp_we.RazorpayWebhookPermanentError), f"{cls} must inherit from PermanentError"
            assert issubclass(cls, BillingWebhookPermanentError), f"{cls} must inherit from BillingWebhookPermanentError"
            assert cls.is_retryable is False

    def test_specific_subclass_relationships(self) -> None:
        """Verifies specific semantic parent-child relationships."""
        # MissingEntityId is a specialized PayloadValidation error
        assert issubclass(rzp_we.RazorpayMissingEntityIdError, rzp_we.RazorpayPayloadValidationError)
        assert issubclass(rzp_we.RazorpayMissingEntityIdError, BillingWebhookValidationError)

        # ScopeMismatch inherits from provider-neutral ScopeMismatch
        assert issubclass(rzp_we.RazorpayScopeMismatchError, BillingWebhookScopeMismatchError)

        # UnknownEvent inherits from provider-neutral UnknownEvent
        assert issubclass(rzp_we.RazorpayUnknownEventError, BillingWebhookUnknownEventError)

        # Conflict inherits from provider-neutral Conflict
        assert issubclass(rzp_we.RazorpayWebhookReconciliationConflictError, BillingWebhookConflictError)


# =============================================================================
# UNIT-EXC-03: StaleLeaseError Disjointness
# =============================================================================


class TestStaleLeaseErrorDisjointness:
    """Verifies StaleLeaseError is strictly disjoint from Retryable and Permanent errors."""

    def test_stale_lease_inheritance(self) -> None:
        assert issubclass(rzp_we.StaleLeaseError, rzp_we.RazorpayWebhookEngineError)
        assert issubclass(rzp_we.StaleLeaseError, BillingWebhookLeaseError)

    def test_stale_lease_disjoint_from_retryable(self) -> None:
        assert not issubclass(rzp_we.StaleLeaseError, rzp_we.RazorpayWebhookRetryableError)
        assert not issubclass(rzp_we.StaleLeaseError, BillingWebhookRetryableError)

    def test_stale_lease_disjoint_from_permanent(self) -> None:
        assert not issubclass(rzp_we.StaleLeaseError, rzp_we.RazorpayWebhookPermanentError)
        assert not issubclass(rzp_we.StaleLeaseError, BillingWebhookPermanentError)

    def test_stale_lease_attributes(self) -> None:
        err = rzp_we.StaleLeaseError("Worker lost lease.")
        assert err.is_retryable is False
        assert err.failure_category == "stale_lease"


# =============================================================================
# UNIT-EXC-04: Package Export Parity & Ingress Isolation
# =============================================================================


class TestPackageExportParityAndIngressIsolation:
    """Verifies that all modules export the exact same canonical exception instances."""

    def test_cross_module_identity(self) -> None:
        classes_to_check = [
            "RazorpayWebhookEngineError",
            "RazorpayWebhookRetryableError",
            "RazorpayWebhookPermanentError",
            "StaleLeaseError",
            "RazorpayDatabaseRetryableError",
            "RazorpayProviderTransientError",
            "RazorpayTransientRaceRetryError",
            "RazorpayPayloadValidationError",
            "RazorpayMissingEntityIdError",
            "RazorpayScopeMismatchError",
            "RazorpayUnverifiedEventError",
            "RazorpayUnknownEventError",
            "RazorpayBusinessRejectionError",
            "RazorpayDatabasePermanentError",
            "RazorpayProviderPermanentError",
            "RazorpayWebhookReconciliationConflictError",
        ]
        for name in classes_to_check:
            we_cls = getattr(rzp_we, name)
            hb_cls = getattr(rzp_hb, name)
            rp_cls = getattr(rp, name)
            assert we_cls is hb_cls, f"{name}: webhook_engine and handler_base must export the same class"
            assert we_cls is rp_cls, f"{name}: webhook_engine and razorpay package must export the same class"

    def test_http_ingress_error_remains_isolated(self) -> None:
        """HTTP ingress error RazorpayWebhookError is NOT part of worker BillingWebhookError tree."""
        assert not issubclass(rzp_wi.RazorpayWebhookError, BillingWebhookError)
        assert not issubclass(rzp_wi.RazorpayWebhookError, rzp_we.RazorpayWebhookEngineError)
        assert rp.RazorpayWebhookError is rzp_wi.RazorpayWebhookError


# =============================================================================
# UNIT-EXC-05: Reconciliation Conflict Constructor & FK Safety
# =============================================================================


class TestReconciliationConflictConstructor:
    """Verifies RazorpayWebhookReconciliationConflictError constructor and FK safety."""

    def test_legacy_keyword_arguments(self) -> None:
        sub_id = uuid.uuid4()
        inv_id = uuid.uuid4()
        pay_id = uuid.uuid4()
        org_id = uuid.uuid4()
        raw_evt_id = uuid.uuid4()

        err = rzp_we.RazorpayWebhookReconciliationConflictError(
            "Conflict occurred",
            case_key="case_123",
            reason="conflicting_tenant_records",
            organization_id=org_id,
            subscription_id=sub_id,
            invoice_id=inv_id,
            payment_id=pay_id,
            raw_event_id=raw_evt_id,
        )
        assert err.case_key == "case_123"
        assert err.reason == "conflicting_tenant_records"
        assert err.organization_id == org_id
        assert err.subscription_id == sub_id
        assert err.committed_subscription_id == sub_id
        assert err.invoice_id == inv_id
        assert err.committed_invoice_id == inv_id
        assert err.payment_id == pay_id
        assert err.committed_payment_id == pay_id
        assert err.raw_event_id == raw_evt_id
        assert err.failure_category == "reconciliation_conflict"
        assert err.is_retryable is False

    def test_committed_keyword_arguments(self) -> None:
        sub_id = uuid.uuid4()
        inv_id = uuid.uuid4()
        pay_id = uuid.uuid4()

        err = rzp_we.RazorpayWebhookReconciliationConflictError(
            "Conflict occurred",
            case_key="case_456",
            reason="unbound_provider_customer",
            committed_subscription_id=sub_id,
            committed_invoice_id=inv_id,
            committed_payment_id=pay_id,
        )
        assert err.committed_subscription_id == sub_id
        assert err.subscription_id == sub_id
        assert err.committed_invoice_id == inv_id
        assert err.invoice_id == inv_id
        assert err.committed_payment_id == pay_id
        assert err.payment_id == pay_id

    def test_committed_kwargs_precedence_over_legacy(self) -> None:
        sub_legacy = uuid.uuid4()
        sub_committed = uuid.uuid4()

        err = rzp_we.RazorpayWebhookReconciliationConflictError(
            "Conflict occurred",
            case_key="case_789",
            reason="tenant_mismatch",
            subscription_id=sub_legacy,
            committed_subscription_id=sub_committed,
        )
        assert err.committed_subscription_id == sub_committed
        assert err.subscription_id == sub_committed

    def test_diagnostic_payload_automatic_sanitization(self) -> None:
        """Raw credentials/PII inside diagnostic_payload are sanitized automatically."""
        raw_diag = {
            "customer_email": "alice@example.com",
            "notes": "secret_key=rzp_live_abc123xyz key_secret=supersecret",
            "subscription_id": "sub_12345678901234",
        }
        err = rzp_we.RazorpayWebhookReconciliationConflictError(
            "Conflict with dirty diagnostics",
            case_key="case_leak",
            reason="dirty_test",
            diagnostic_payload=raw_diag,
        )
        # Email and secret must be stripped
        diag = err.diagnostic_payload
        assert "rzp_live_abc123xyz" not in str(diag)
        assert "supersecret" not in str(diag)
        assert "alice@example.com" not in str(diag)


# =============================================================================
# UNIT-EXC-06: Structured Exception Metadata & Sanitized Message
# =============================================================================


class TestStructuredExceptionMetadata:
    """Verifies that all exceptions expose structured telemetry attributes."""

    def test_structured_metadata_defaults(self) -> None:
        err = rzp_we.RazorpayPayloadValidationError("Payload missing amount")
        assert err.failure_category == "validation"
        assert err.error_code is None
        assert err.constraint_name is None
        assert err.is_retryable is False

    def test_structured_metadata_overrides(self) -> None:
        err = rzp_we.RazorpayDatabasePermanentError(
            "Trigger abort",
            failure_category="trigger_violation",
            error_code="P0001",
            constraint_name="trg_enforce_immutability",
        )
        assert err.failure_category == "trigger_violation"
        assert err.error_code == "P0001"
        assert err.constraint_name == "trg_enforce_immutability"
        assert err.is_retryable is False


# =============================================================================
# UNIT-API-01: Razorpay Client Error Classification
# =============================================================================


class TestClientErrorClassification:
    """Verifies classify_client_error mapping for all Razorpay client errors."""

    def test_rate_limit_error_is_transient(self) -> None:
        exc = RazorpayRateLimitError("Too Many Requests")
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderTransientError)
        assert classified.error_code == "429"
        assert classified.is_retryable is True

    def test_server_5xx_error_is_transient(self) -> None:
        exc = RazorpayServer5xxError("Bad Gateway", status_code=502)
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderTransientError)
        assert classified.error_code == "502"
        assert classified.is_retryable is True

    def test_pre_dispatch_network_error_is_transient(self) -> None:
        exc = RazorpayNetworkError("Connection timed out", is_pre_dispatch=True)
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderTransientError)
        assert classified.error_code == "PRE_DISPATCH_NETWORK_ERROR"
        assert classified.is_retryable is True

    def test_post_dispatch_network_error_is_permanent(self) -> None:
        exc = RazorpayNetworkError("Read timeout after sending POST", is_pre_dispatch=False)
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderPermanentError)
        assert classified.error_code == "POST_DISPATCH_NETWORK_ERROR"
        assert classified.is_retryable is False

    def test_asyncio_timeout_is_transient(self) -> None:
        exc = asyncio.TimeoutError()
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderTransientError)
        assert classified.error_code == "TIMEOUT"
        assert classified.is_retryable is True

    def test_auth_error_is_permanent(self) -> None:
        exc = RazorpayAuthError("Invalid API key")
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderPermanentError)
        assert classified.error_code == "401"
        assert classified.is_retryable is False

    def test_billing_disabled_error_is_permanent(self) -> None:
        exc = RazorpayBillingDisabledError("Razorpay billing is disabled")
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderPermanentError)
        assert classified.is_retryable is False

    def test_configuration_error_is_permanent(self) -> None:
        exc = RazorpayConfigurationError("Missing secret")
        classified = rzp_we.classify_client_error(exc)
        assert isinstance(classified, rzp_we.RazorpayProviderPermanentError)
        assert classified.is_retryable is False

    def test_api_4xx_errors_are_permanent(self) -> None:
        for status in [400, 401, 403, 404, 422]:
            exc = RazorpayAPIError(f"HTTP {status}", status_code=status)
            classified = rzp_we.classify_client_error(exc)
            assert isinstance(classified, rzp_we.RazorpayProviderPermanentError)
            assert classified.error_code == str(status)
            assert classified.is_retryable is False

    def test_api_5xx_errors_are_transient(self) -> None:
        for status in [500, 502, 503, 504]:
            exc = RazorpayAPIError(f"HTTP {status}", status_code=status)
            classified = rzp_we.classify_client_error(exc)
            assert isinstance(classified, rzp_we.RazorpayProviderTransientError)
            assert classified.error_code == str(status)
            assert classified.is_retryable is True

    def test_already_engine_error_returned_unmodified(self) -> None:
        existing = rzp_we.RazorpayPayloadValidationError("Already classified")
        assert rzp_we.classify_client_error(existing) is existing

    def test_unrelated_exception_returns_none(self) -> None:
        assert rzp_we.classify_client_error(KeyError("missing_key")) is None
        assert rzp_we.classify_client_error(ValueError("bad value")) is None


# =============================================================================
# UNIT-API-02: Database Error Classification
# =============================================================================


class TestDatabaseErrorClassification:
    """Verifies classify_db_exception and classify_db_error_to_razorpay_exception."""

    def test_classify_db_exception_delegation(self) -> None:
        """classify_db_exception returns a DbErrorClassification."""
        from sqlalchemy.exc import DBAPIError
        pg_err = Exception()
        setattr(pg_err, "pgcode", "40001")
        sa_err = DBAPIError("SELECT 1", {}, pg_err)

        res = rzp_we.classify_db_exception(sa_err)
        assert isinstance(res, DbErrorClassification)
        assert res.disposition == DbRetryDisposition.RETRYABLE
        assert res.details.sqlstate == "40001"

    def test_classify_db_error_to_razorpay_exception_retryable(self) -> None:
        from sqlalchemy.exc import DBAPIError
        pg_err = Exception()
        setattr(pg_err, "pgcode", "40001")
        sa_err = DBAPIError("SELECT 1", {}, pg_err)

        rzp_err = rzp_we.classify_db_error_to_razorpay_exception(sa_err)
        assert isinstance(rzp_err, rzp_we.RazorpayDatabaseRetryableError)
        assert rzp_err.error_code == "40001"
        assert rzp_err.is_retryable is True

    def test_classify_db_error_to_razorpay_exception_permanent(self) -> None:
        from sqlalchemy.exc import DBAPIError
        pg_err = Exception()
        setattr(pg_err, "pgcode", "P0001")
        sa_err = DBAPIError("INSERT ...", {}, pg_err)

        rzp_err = rzp_we.classify_db_error_to_razorpay_exception(sa_err)
        assert isinstance(rzp_err, rzp_we.RazorpayDatabasePermanentError)
        assert rzp_err.error_code == "P0001"
        assert rzp_err.is_retryable is False

    def test_classify_db_error_to_razorpay_exception_unknown_returns_none(self) -> None:
        rzp_err = rzp_we.classify_db_error_to_razorpay_exception(RuntimeError("Unknown error"))
        assert rzp_err is None


# =============================================================================
# UNIT-SAN-01: Sanitization Helper Delegation
# =============================================================================


class TestSanitizationDelegation:
    """Verifies sanitize_error_message delegates to sanitize_billing_error_message."""

    def test_secret_redaction(self) -> None:
        raw = "Error with rzp_live_SECRET123 and sk_live_STRIPESECRET456"
        sanitized = rzp_we.sanitize_error_message(raw)
        assert "rzp_live_SECRET123" not in sanitized
        assert "sk_live_STRIPESECRET456" not in sanitized
        assert "[REDACTED_KEY]" in sanitized

    def test_identifier_preservation(self) -> None:
        raw = "Event 12345678-1234-5678-1234-567812345678 with sub_12345678901234 and SQLSTATE 40001"
        sanitized = rzp_we.sanitize_error_message(raw)
        assert "12345678-1234-5678-1234-567812345678" in sanitized
        assert "sub_12345678901234" in sanitized
        assert "40001" in sanitized
