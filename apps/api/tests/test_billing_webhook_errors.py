"""Pure offline unit tests for `app.services.billing.webhook_errors` (D-04A Stage 1).

Coverage:
- Provider-neutral webhook exception hierarchy, inheritance, disjoint lease base,
  structured diagnostic fields, and Python exception chaining.
- Standalone import stability and non-mutation of existing Razorpay/Stripe modules.
- Cycle-safe SQLAlchemy/asyncpg/DBAPI exception chain traversal and detail extraction.
- Constraint-aware SQLSTATE 23505 classification across all 4 agreed buckets.
- Retryable, permanent, transport-disconnect, and unknown SQLSTATE classification.
- Deterministic, idempotent log & diagnostic payload sanitization (secret/PII removal
  plus preservation of UUIDs, SQLSTATEs, constraint names, payment IDs, and epochs).
- RC-01: Webhook exception classification (programming errors, Tier-2, Tier-3, chains).
- RC-04: Alert-retryable SQLSTATEs (53100, 53200, 53300), 23001 permanent, 57014 context.
- RC-05: Innermost traceback frames with host-path sanitization.
- L-01a/L-01b: Mid-marker truncation and whitespace failure_category normalization.
- L-03: Edge-case coverage (acyclic chains > max_nodes, provider hierarchy non-mutation,
         empty BillingWebhookError, uppercase UNIQUE CONSTRAINT fallback regex).

Strictly offline: requires ZERO PostgreSQL server, ZERO Supabase access, and ZERO external APIs.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import asyncpg.exceptions as asyncpg_exc
import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

import app.services.billing.razorpay.client as rzp_client
import app.services.billing.razorpay.customer as rzp_customer
import app.services.billing.razorpay.handler_base as rzp_handler_base
import app.services.billing.razorpay.webhook_engine as rzp_webhook_engine
import app.services.billing.razorpay.webhook_ingress as rzp_webhook_ingress
import app.services.billing.stripe_webhook_engine as stripe_webhook_engine
from app.services.billing.webhook_errors import (
    ALERT_RETRYABLE_PG_SQLSTATES,
    MAX_SANITIZED_ERROR_CHARS,
    MAX_TRACEBACK_FRAMES,
    PERMANENT_PG_SQLSTATES,
    RETRYABLE_PG_SQLSTATES,
    UNIQUE_CONCURRENCY_RACE_EXACT,
    UNIQUE_CONVERGENCE_CONSTRAINTS,
    UNIQUE_PERMANENT_CONSTRAINTS,
    UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES,
    BillingWebhookConflictError,
    BillingWebhookError,
    BillingWebhookLeaseError,
    BillingWebhookPermanentError,
    BillingWebhookRetryableError,
    BillingWebhookScopeMismatchError,
    BillingWebhookUnknownEventError,
    BillingWebhookValidationError,
    DbErrorClassification,
    DbErrorDetails,
    DbRetryDisposition,
    UniqueConstraintBucket,
    WebhookExceptionCategory,
    WebhookExceptionClassification,
    classify_db_exception,
    classify_pg_sqlstate,
    classify_unique_violation_constraint,
    classify_webhook_exception,
    extract_db_error_details,
    extract_safe_traceback_locations,
    is_unequivocal_programming_error,
    iter_db_exception_chain,
    sanitize_billing_error_message,
    sanitize_conflict_diagnostic_payload,
)


class _DummyDiag:
    """Psycopg2/asyncpg-style diagnostic holder."""

    def __init__(
        self,
        *,
        sqlstate: str | None = None,
        pgcode: str | None = None,
        constraint_name: str | None = None,
        table_name: str | None = None,
        schema_name: str | None = None,
        message_detail: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.sqlstate = sqlstate
        self.pgcode = pgcode
        self.constraint_name = constraint_name
        self.table_name = table_name
        self.schema_name = schema_name
        self.message_detail = message_detail
        self.detail = detail


class _DummyDbError(Exception):
    """Generic wrapped DBAPI-like error supporting optional attributes."""

    def __init__(
        self,
        message: str = "db error",
        *,
        orig: Any = None,
        sqlstate: str | None = None,
        pgcode: str | None = None,
        constraint_name: str | None = None,
        table_name: str | None = None,
        schema_name: str | None = None,
        detail: str | None = None,
        diag: Any = None,
    ) -> None:
        super().__init__(message)
        if orig is not None:
            self.orig = orig
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode
        if constraint_name is not None:
            self.constraint_name = constraint_name
        if table_name is not None:
            self.table_name = table_name
        if schema_name is not None:
            self.schema_name = schema_name
        if detail is not None:
            self.detail = detail
        if diag is not None:
            self.diag = diag


class _BrokenDescriptorError(Exception):
    """Simulates a proxy whose optional attributes raise when inspected."""

    @property
    def diag(self) -> Any:
        raise RuntimeError("diag proxy exploded")

    @property
    def constraint_name(self) -> Any:
        raise AttributeError("constraint proxy exploded")

    @property
    def sqlstate(self) -> str:
        return "23505"


class _BrokenStrError(Exception):
    """Simulates an exception whose __str__ raises."""

    def __str__(self) -> str:
        raise RuntimeError("unprintable exception")


# =============================================================================
# 1. Exception Hierarchy & Import Stability Tests
# =============================================================================


class TestBillingWebhookExceptionHierarchy:
    """Verify coherent inheritance, chaining, disjoint lease base, and isolation."""

    def test_inheritance_and_retry_flags(self) -> None:
        assert issubclass(BillingWebhookError, Exception)
        assert issubclass(BillingWebhookRetryableError, BillingWebhookError)
        assert issubclass(BillingWebhookPermanentError, BillingWebhookError)
        assert issubclass(BillingWebhookLeaseError, BillingWebhookError)

        for perm_subcls in (
            BillingWebhookValidationError,
            BillingWebhookScopeMismatchError,
            BillingWebhookConflictError,
            BillingWebhookUnknownEventError,
        ):
            assert issubclass(perm_subcls, BillingWebhookPermanentError)
            assert issubclass(perm_subcls, BillingWebhookError)
            assert not issubclass(perm_subcls, BillingWebhookRetryableError)
            assert not issubclass(perm_subcls, BillingWebhookLeaseError)
            assert perm_subcls.is_retryable is False

        assert BillingWebhookRetryableError.is_retryable is True
        assert BillingWebhookPermanentError.is_retryable is False
        assert BillingWebhookLeaseError.is_retryable is False

    def test_lease_error_strictly_disjoint_from_retry_and_permanent(self) -> None:
        lease_err = BillingWebhookLeaseError("fencing token lost")
        assert isinstance(lease_err, BillingWebhookError)
        assert not isinstance(lease_err, BillingWebhookRetryableError)
        assert not isinstance(lease_err, BillingWebhookPermanentError)
        assert lease_err.failure_category == "stale_lease"

    def test_structured_attributes_and_sanitized_message_property(self) -> None:
        err = BillingWebhookConflictError(
            "Conflict for sub_ABC123 with Bearer top_secret_jwt and rzp_live_998877",
            error_code="23505",
            constraint_name="uq_org_active_subscription",
        )
        assert err.failure_category == "reconciliation_conflict"
        assert err.error_code == "23505"
        assert err.constraint_name == "uq_org_active_subscription"

        redacted = err.sanitized_message
        assert "sub_ABC123" in redacted
        assert "top_secret_jwt" not in redacted
        assert "rzp_live_998877" not in redacted
        assert "[REDACTED_TOKEN]" in redacted
        assert "[REDACTED_KEY]" in redacted

        custom = BillingWebhookValidationError(
            "missing field",
            failure_category="missing_entity_id",
            error_code="422",
        )
        assert custom.failure_category == "missing_entity_id"
        assert custom.error_code == "422"

    def test_exception_chaining_preserved_across_wrappers(self) -> None:
        root_cause = asyncpg_exc.UniqueViolationError.new(
            {
                "M": 'duplicate key value violates unique constraint "uq_org_active_subscription"',
                "C": "23505",
                "n": "uq_org_active_subscription",
                "t": "subscriptions",
            }
        )
        caught: BillingWebhookConflictError | None = None
        try:
            try:
                raise root_cause
            except asyncpg_exc.UniqueViolationError as exc:
                raise BillingWebhookConflictError(
                    "Org already holds an active subscription",
                    error_code="23505",
                    constraint_name="uq_org_active_subscription",
                ) from exc
        except BillingWebhookConflictError as wrapped:
            caught = wrapped

        assert caught is not None
        assert caught.__cause__ is root_cause
        details = extract_db_error_details(caught)
        assert details.sqlstate == "23505"
        assert details.constraint_name == "uq_org_active_subscription"

    def test_stdlib_only_imports_and_zero_circular_dependency(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "billing"
            / "webhook_errors.py"
        )
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        assert "app" not in imported_roots
        assert "sqlalchemy" not in imported_roots
        assert "asyncpg" not in imported_roots
        assert "razorpay" not in imported_roots
        assert "stripe" not in imported_roots

    def test_existing_provider_hierarchies_unmodified_in_stage1(self) -> None:
        # In Stage 2, Razorpay webhook engine & handler are unified under BillingWebhookError:
        assert issubclass(rzp_webhook_engine.RazorpayWebhookEngineError, BillingWebhookError)
        assert issubclass(rzp_handler_base.RazorpayWebhookHandlerError, BillingWebhookError)
        # HTTP ingress, Stripe, client, and customer hierarchies remain untouched:
        assert not issubclass(rzp_webhook_ingress.RazorpayWebhookError, BillingWebhookError)
        assert not issubclass(stripe_webhook_engine.WebhookEngineError, BillingWebhookError)
        assert not issubclass(rzp_client.RazorpayClientError, BillingWebhookError)
        assert not issubclass(rzp_customer.RazorpayCustomerError, BillingWebhookError)

    def test_l01b_empty_and_whitespace_failure_category_normalized(self) -> None:
        """L-01b: Empty or whitespace failure_category falls back to class default."""
        # Empty string → default
        err_empty = BillingWebhookConflictError("msg", failure_category="")
        assert err_empty.failure_category == "reconciliation_conflict"
        # Whitespace string → default
        err_ws = BillingWebhookValidationError("msg", failure_category="   ")
        assert err_ws.failure_category == "validation"
        # None → default
        err_none = BillingWebhookPermanentError("msg", failure_category=None)
        assert err_none.failure_category == "permanent_failure"
        # Non-empty valid → preserved
        err_valid = BillingWebhookPermanentError("msg", failure_category="custom_failure")
        assert err_valid.failure_category == "custom_failure"

    def test_l03_bare_billing_webhook_error_uses_default_category(self) -> None:
        """L-03: Bare BillingWebhookError() uses default_failure_category."""
        bare = BillingWebhookError()
        assert bare.failure_category == "webhook_error"
        assert str(bare) == ""


# =============================================================================
# 2. Cycle-Safe SQLSTATE & Constraint Extraction Tests
# =============================================================================


class TestDbErrorExtraction:
    """Verify cycle-safe traversal across asyncpg, SQLAlchemy, and DBAPI wrappers."""

    def test_native_asyncpg_unique_violation_extraction(self) -> None:
        pg_err = asyncpg_exc.UniqueViolationError.new(
            {
                "M": 'duplicate key value violates unique constraint "uq_billing_payments_provider_payment"',
                "C": "23505",
                "n": "uq_billing_payments_provider_payment",
                "t": "billing_payments",
                "s": "public",
                "D": "Key (provider, provider_payment_id)=(razorpay, pay_ABC123) already exists.",
            }
        )
        details = extract_db_error_details(pg_err)
        assert isinstance(details, DbErrorDetails)
        assert details.error_type == "UniqueViolationError"
        assert details.sqlstate == "23505"
        assert details.constraint_name == "uq_billing_payments_provider_payment"
        assert details.table_name == "billing_payments"
        assert details.schema_name == "public"
        assert "pay_ABC123" in (details.sanitized_detail or "")

    def test_sqlalchemy_wrapped_error_hides_credentials_and_bound_params(self) -> None:
        pg_err = asyncpg_exc.ForeignKeyViolationError.new(
            {
                "M": 'insert or update on table "payment_reconciliations" violates foreign key constraint',
                "C": "23503",
                "n": "payment_reconciliations_organization_id_fkey",
                "t": "payment_reconciliations",
                "D": "Key (organization_id)=(550e8400-e29b-41d4-a716-446655440000) and email=(alice@example.com) invalid.",
            }
        )
        sa_err = IntegrityError(
            "INSERT INTO payment_reconciliations (organization_id, payload) "
            "VALUES ($1, $2) -- postgresql://admin:SuperSecretPass@db.internal:5432/prod",
            {"secret": "sk_live_should_never_leak", "email": "alice@example.com"},
            pg_err,
        )
        details = extract_db_error_details(sa_err)
        assert details.error_type == "IntegrityError"
        assert details.sqlstate == "23503"
        assert details.constraint_name == "payment_reconciliations_organization_id_fkey"
        assert details.table_name == "payment_reconciliations"
        assert "SuperSecretPass" not in details.sanitized_summary
        assert "sk_live_should_never_leak" not in details.sanitized_summary
        assert "alice@example.com" not in details.sanitized_summary
        assert "alice@example.com" not in (details.sanitized_detail or "")
        assert "550e8400-e29b-41d4-a716-446655440000" in (details.sanitized_detail or "")
        assert "[parameters: REDACTED]" in details.sanitized_summary

    def test_nested_cause_context_and_orig_chain(self) -> None:
        deep_diag = _DummyDiag(
            sqlstate="23505",
            constraint_name="UQ_SUBSCRIPTIONS_PROVIDER_SUBSCRIPTION",
            table_name="subscriptions",
        )
        leaf = _DummyDbError("leaf dbapi failure", diag=deep_diag)
        middle = _DummyDbError("middle context failure")
        middle.__context__ = leaf
        outer_orig = _DummyDbError("outer dbapi wrapper")
        outer_orig.__cause__ = middle
        top = OperationalError("SELECT 1", None, outer_orig)

        chain = iter_db_exception_chain(top)
        assert len(chain) >= 4
        details = extract_db_error_details(top)
        assert details.sqlstate == "23505"
        assert details.constraint_name == "uq_subscriptions_provider_subscription"
        assert details.table_name == "subscriptions"

    def test_cyclic_references_and_broken_descriptors_terminate_safely(self) -> None:
        node_a = _DummyDbError("node a", orig=None)
        node_b = _BrokenDescriptorError("node b")
        node_a.orig = node_a
        node_a.__cause__ = node_b
        node_b.__cause__ = node_a
        node_b.__context__ = node_a

        chain = iter_db_exception_chain(node_a, max_nodes=8)
        assert len(chain) == 2

        details = extract_db_error_details(node_a, max_nodes=8)
        assert details.sqlstate == "23505"
        assert details.constraint_name is None

        broken_str = _BrokenStrError()
        broken_details = extract_db_error_details(broken_str)
        assert broken_details.error_type == "_BrokenStrError"
        assert broken_details.sqlstate is None
        assert broken_details.sanitized_summary == "_BrokenStrError"

    def test_missing_sqlstate_and_missing_constraint_name(self) -> None:
        plain = RuntimeError("generic failure without db attributes")
        details = extract_db_error_details(plain)
        assert details.sqlstate is None
        assert details.constraint_name is None
        assert details.table_name is None
        assert details.sanitized_detail is None

    def test_safe_text_fallback_ignores_spoofed_bound_parameters(self) -> None:
        # Attacker sends text resembling a Postgres constraint name inside SQL parameters.
        spoofed_orig = _DummyDbError(
            'error without constraint_name; [parameters: (\'{"msg": "unique constraint \\"uq_billing_invoices_provider_invoice\\""}\',)]',
            sqlstate="23505",
        )
        details_spoofed = extract_db_error_details(spoofed_orig)
        assert details_spoofed.sqlstate == "23505"
        assert details_spoofed.constraint_name is None

        # Genuine driver message lacking explicit attribute outside [parameters: ...] is extracted.
        real_fallback = _DummyDbError(
            'duplicate key value violates unique constraint "uq_billing_invoices_provider_invoice" [parameters: ("inv_123",)]',
            sqlstate="23505",
        )
        details_real = extract_db_error_details(real_fallback)
        assert details_real.constraint_name == "uq_billing_invoices_provider_invoice"

    def test_l03_acyclic_chain_beyond_max_nodes_truncates_at_max(self) -> None:
        """L-03: An acyclic chain > max_nodes terminates at exactly max_nodes."""
        # Build a linear chain of 10 errors connected via __cause__.
        chain_length = 10
        head: Exception | None = None
        for i in range(chain_length):
            exc = RuntimeError(f"node_{i}")
            if head is not None:
                exc.__cause__ = head
            head = exc

        assert head is not None
        chain = iter_db_exception_chain(head, max_nodes=5)
        assert len(chain) == 5

    def test_l03_uppercase_unique_constraint_fallback_regex(self) -> None:
        """L-03: UNIQUE CONSTRAINT fallback regex is case-insensitive."""
        upper_msg = _DummyDbError(
            'duplicate key value violates UNIQUE CONSTRAINT "uq_billing_payments_provider_payment"',
            sqlstate="23505",
        )
        details = extract_db_error_details(upper_msg)
        assert details.constraint_name == "uq_billing_payments_provider_payment"


# =============================================================================
# 3. Constraint-Aware SQLSTATE 23505 Classification Tests
# =============================================================================


class TestUniqueViolationClassification:
    """Verify all 4 SQLSTATE 23505 constraint buckets explicitly."""

    @pytest.mark.parametrize(
        "constraint_name",
        [
            "uq_billing_invoices_provider_invoice",
            "uq_billing_payments_provider_payment",
            "uq_billing_webhook_provider_event",        # RC-02: added
            "uq_payment_reconciliations_case_key",      # RC-02: added
            "uq_stripe_invoices_invoice_id",            # RC-02: added
        ],
    )
    def test_bucket_1_every_named_idempotent_convergence_constraint(
        self,
        constraint_name: str,
    ) -> None:
        bucket = classify_unique_violation_constraint(constraint_name)
        assert bucket == UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE

        res = classify_pg_sqlstate("23505", constraint_name=constraint_name)
        assert res.disposition == DbRetryDisposition.RETRYABLE
        assert res.failure_category == "db_idempotency_race"
        assert res.unique_bucket == UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is False
        assert res.is_known is True

    @pytest.mark.parametrize(
        "constraint_name",
        [
            "uq_billing_customer_provisions_org_provider",
            # RC-02: uq_billing_customer_provisions_provider_customer moved to Bucket 3!
            "uq_subscriptions_provider_subscription",
            "uq_org_scoped_active_billing_checkout_attempt",
            "uq_checkout_attempts_provider_sub",        # RC-02: added exact
            "uq_checkout_attempts_provider_payment",    # RC-02: added exact
            "uq_org_active_checkout_attempt",           # RC-02: added exact
        ],
    )
    def test_bucket_2_representative_concurrency_race_constraints(
        self,
        constraint_name: str,
    ) -> None:
        bucket = classify_unique_violation_constraint(constraint_name)
        assert bucket == UniqueConstraintBucket.CONCURRENCY_RACE

        res = classify_pg_sqlstate("23505", constraint_name=constraint_name)
        assert res.disposition == DbRetryDisposition.RETRYABLE
        assert res.failure_category == "db_concurrent_unique_race"
        assert res.unique_bucket == UniqueConstraintBucket.CONCURRENCY_RACE
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is False
        assert res.is_known is True

    @pytest.mark.parametrize(
        "constraint_name",
        [
            "uq_org_active_subscription",
            "uq_subscriptions_provider_customer",
            "uq_billing_payments_checkout_attempt",
            "uq_billing_checkout_org_idempotency",
            "uq_bca_composite_identity",
            "uq_reconciliation_event_sub",                      # RC-02: replaces phantom
            "uq_billing_customer_provisions_provider_customer", # RC-02: moved from Bucket 2!
            "uq_org_active_billing_period",                     # RC-02: added
        ],
    )
    def test_bucket_3_every_named_permanent_integrity_conflict(
        self,
        constraint_name: str,
    ) -> None:
        bucket = classify_unique_violation_constraint(constraint_name)
        assert bucket == UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT

        pg_err = asyncpg_exc.UniqueViolationError.new(
            {
                "M": f'duplicate key value violates unique constraint "{constraint_name}"',
                "C": "23505",
                "n": constraint_name,
            }
        )
        res = classify_db_exception(IntegrityError("INSERT", {}, pg_err))
        assert res.disposition == DbRetryDisposition.PERMANENT
        assert res.failure_category == "db_permanent"
        assert res.unique_bucket == UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT
        assert res.should_retry is False
        assert res.is_permanent is True
        assert res.requires_alert is False
        assert res.is_known is True

    @pytest.mark.parametrize(
        "constraint_name",
        [
            "uq_brand_new_unknown_constraint",
            "ix_custom_unique_table_col",
            "custom_idx",
            # RC-02: Former phantom — now correctly Bucket 4.
            "uq_billing_reconciliations_event_incoming",
            # RC-02: Former wildcard prefix matches — now correctly Bucket 4.
            "uq_billing_customer_provisions_custom_future_idx",
            "uq_checkout_attempts_provider_order",
            # Non-billing constraints stay Bucket 4.
            "uq_org_member",
        ],
    )
    def test_bucket_4_unknown_constraint_names(self, constraint_name: str) -> None:
        bucket = classify_unique_violation_constraint(constraint_name)
        assert bucket == UniqueConstraintBucket.UNMAPPED_UNIQUE_VIOLATION

        res = classify_pg_sqlstate("23505", constraint_name=constraint_name)
        assert res.disposition == DbRetryDisposition.RETRYABLE_WITH_ALERT
        assert res.failure_category == "db_unmapped_unique_violation"
        assert res.unique_bucket == UniqueConstraintBucket.UNMAPPED_UNIQUE_VIOLATION
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is True
        assert res.is_known is True
        assert res.unique_bucket != UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE

    @pytest.mark.parametrize(
        "malformed_constraint",
        [
            None,
            "",
            "   ",
            "bad constraint with spaces; DROP TABLE users;",
            "123_starts_with_digit",
            "uq_" + ("x" * 200),
            "weird/slash/constraint",
        ],
    )
    def test_bucket_4_missing_or_unrecognized_constraint_formats(
        self,
        malformed_constraint: str | None,
    ) -> None:
        bucket = classify_unique_violation_constraint(malformed_constraint)
        assert bucket == UniqueConstraintBucket.UNMAPPED_UNIQUE_VIOLATION

        res = classify_pg_sqlstate("23505", constraint_name=malformed_constraint)
        assert res.disposition == DbRetryDisposition.RETRYABLE_WITH_ALERT
        assert res.failure_category == "db_unmapped_unique_violation"
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is True
        assert res.unique_bucket != UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE

    def test_case_and_whitespace_normalization_for_constraints(self) -> None:
        assert (
            classify_unique_violation_constraint("  UQ_BILLING_PAYMENTS_PROVIDER_PAYMENT  ")
            == UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE
        )
        assert (
            classify_unique_violation_constraint("UQ_ORG_ACTIVE_SUBSCRIPTION")
            == UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT
        )
        assert (
            classify_unique_violation_constraint("UQ_CHECKOUT_ATTEMPTS_PROVIDER_SUB")
            == UniqueConstraintBucket.CONCURRENCY_RACE
        )

    def test_rc02_constraint_set_pairwise_disjointness(self) -> None:
        """RC-02: Bucket sets must be pairwise disjoint to prevent misclassification."""
        assert UNIQUE_CONVERGENCE_CONSTRAINTS.isdisjoint(UNIQUE_CONCURRENCY_RACE_EXACT), (
            "Bucket 1 and Bucket 2 share constraints!"
        )
        assert UNIQUE_CONVERGENCE_CONSTRAINTS.isdisjoint(UNIQUE_PERMANENT_CONSTRAINTS), (
            "Bucket 1 and Bucket 3 share constraints!"
        )
        assert UNIQUE_CONCURRENCY_RACE_EXACT.isdisjoint(UNIQUE_PERMANENT_CONSTRAINTS), (
            "Bucket 2 and Bucket 3 share constraints!"
        )


# =============================================================================
# 4. Retryable, Permanent & Unknown SQLSTATE Classification Tests
# =============================================================================


class TestOtherSqlstateClassification:
    """Verify non-23505 SQLSTATEs, transport disconnects, and unknown handling."""

    @pytest.mark.parametrize(
        "sqlstate",
        [
            "40001",  # serialization_failure
            "40P01",  # deadlock_detected
            "55P03",  # lock_not_available
            "57P01",  # admin_shutdown
            "57P02",  # crash_shutdown
            "57P03",  # cannot_connect_now
            # NOTE: "53300" removed — now RETRYABLE_WITH_ALERT (RC-04)
            "08000",  # connection_exception
            "08003",  # connection_does_not_exist
            "08006",  # connection_failure
            "40p01",  # lowercase input normalizes cleanly
        ],
    )
    def test_retryable_sqlstates(self, sqlstate: str) -> None:
        res = classify_pg_sqlstate(sqlstate)
        assert isinstance(res, DbErrorClassification)
        assert res.disposition == DbRetryDisposition.RETRYABLE
        assert res.failure_category == "db_retryable"
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is False
        assert res.is_known is True
        assert res.unique_bucket is None

    @pytest.mark.parametrize(
        "sqlstate",
        [
            "P0001",  # raise_exception (PL/pgSQL immutability trigger)
            "23502",  # not_null_violation
            "23503",  # foreign_key_violation
            "23514",  # check_violation
            "23001",  # restrict_violation (RC-04: added)
            "22001",  # string_data_right_truncation
            "22P02",  # invalid_text_representation
            "22003",  # numeric_value_out_of_range
            "42501",  # insufficient_privilege / RLS
            "42601",  # syntax_error
            "42P01",  # undefined_table
            "p0001",  # lowercase input normalizes cleanly
        ],
    )
    def test_permanent_sqlstates(self, sqlstate: str) -> None:
        res = classify_pg_sqlstate(sqlstate)
        assert res.disposition == DbRetryDisposition.PERMANENT
        assert res.failure_category == "db_permanent"
        assert res.should_retry is False
        assert res.is_permanent is True
        assert res.requires_alert is False
        assert res.is_known is True
        assert res.unique_bucket is None

    @pytest.mark.parametrize(
        ("exc", "expected_category", "expected_sqlstate"),
        [
            (ConnectionResetError("connection reset by peer"), "db_connection_retryable", None),
            (BrokenPipeError("broken pipe"), "db_connection_retryable", None),
            (TimeoutError("db timed out"), "db_connection_retryable", None),
            (asyncpg_exc.PostgresConnectionError("closed unexpectedly"), "db_retryable", "08000"),
            (asyncpg_exc.ConnectionDoesNotExistError("connection closed"), "db_retryable", "08003"),
            (
                DBAPIError("SELECT 1", {}, type("DisconnectionError", (Exception,), {})(
                    "pool invalidated"
                )),
                "db_connection_retryable",
                None,
            ),
            (
                type("PostgresConnectionError", (Exception,), {})(
                    "synthetic disconnect without sqlstate"
                ),
                "db_connection_retryable",
                None,
            ),
        ],
    )
    def test_transport_disconnect_classification(
        self,
        exc: BaseException,
        expected_category: str,
        expected_sqlstate: str | None,
    ) -> None:
        res = classify_db_exception(exc)
        assert res.disposition == DbRetryDisposition.RETRYABLE
        assert res.failure_category == expected_category
        assert res.sqlstate == expected_sqlstate
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.is_known is True

    @pytest.mark.parametrize(
        "unknown_sqlstate",
        [
            "99999",
            "XX000",
            "P0002",
            "25P02",
            "23000",
        ],
    )
    def test_unknown_non_empty_sqlstates_remain_explicitly_unknown(
        self,
        unknown_sqlstate: str,
    ) -> None:
        res = classify_pg_sqlstate(unknown_sqlstate)
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.failure_category == "db_unknown_sqlstate"
        assert res.should_retry is False
        assert res.is_permanent is False
        assert res.requires_alert is True
        assert res.is_known is False

    @pytest.mark.parametrize("bad_input", [None, "", "INVALID", "123"])
    def test_missing_or_malformed_sqlstate_is_unknown(self, bad_input: str | None) -> None:
        res = classify_pg_sqlstate(bad_input)
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.sqlstate is None
        assert res.is_known is False
        assert res.should_retry is False
        assert res.is_permanent is False
        assert res.requires_alert is True

    def test_generic_exception_is_unknown_and_dispositions_stay_decoupled_from_event_states(
        self,
    ) -> None:
        res = classify_db_exception(KeyError("missing_key"))
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.is_known is False
        # DbRetryDisposition only models classification recommendations, not worker table statuses.
        disposition_values = {item.value for item in DbRetryDisposition}
        assert disposition_values.isdisjoint({"pending", "processed", "failed", "quarantined"})


# =============================================================================
# 4b. RC-04: Alert-Retryable SQLSTATEs and 57014 Context-Aware Tests
# =============================================================================


class TestAlertRetryableSqlstates:
    """RC-04: Verify Class 53 capacity-exhaustion and 57014 statement-timeout handling."""

    @pytest.mark.parametrize(
        "sqlstate",
        ["53100", "53200", "53300"],
    )
    def test_class_53_capacity_sqlstates_are_retryable_with_alert(self, sqlstate: str) -> None:
        """RC-04: 53100/53200/53300 → RETRYABLE_WITH_ALERT, db_resource_exhausted."""
        res = classify_pg_sqlstate(sqlstate)
        assert res.disposition == DbRetryDisposition.RETRYABLE_WITH_ALERT
        assert res.failure_category == "db_resource_exhausted"
        assert res.should_retry is True
        assert res.is_permanent is False
        assert res.requires_alert is True
        assert res.is_known is True
        assert res.unique_bucket is None

    def test_53300_removed_from_retryable_sqlstates_set(self) -> None:
        """RC-04: 53300 must NOT be in RETRYABLE_PG_SQLSTATES (moved to ALERT set)."""
        assert "53300" not in RETRYABLE_PG_SQLSTATES

    def test_53300_in_alert_retryable_sqlstates_set(self) -> None:
        """RC-04: 53300 is in ALERT_RETRYABLE_PG_SQLSTATES."""
        assert "53300" in ALERT_RETRYABLE_PG_SQLSTATES
        assert "53100" in ALERT_RETRYABLE_PG_SQLSTATES
        assert "53200" in ALERT_RETRYABLE_PG_SQLSTATES

    def test_sqlstate_sets_are_pairwise_disjoint(self) -> None:
        """RC-04: RETRYABLE, ALERT_RETRYABLE, and PERMANENT sets must be disjoint."""
        assert RETRYABLE_PG_SQLSTATES.isdisjoint(ALERT_RETRYABLE_PG_SQLSTATES), (
            "RETRYABLE and ALERT_RETRYABLE share SQLSTATEs!"
        )
        assert RETRYABLE_PG_SQLSTATES.isdisjoint(PERMANENT_PG_SQLSTATES), (
            "RETRYABLE and PERMANENT share SQLSTATEs!"
        )
        assert ALERT_RETRYABLE_PG_SQLSTATES.isdisjoint(PERMANENT_PG_SQLSTATES), (
            "ALERT_RETRYABLE and PERMANENT share SQLSTATEs!"
        )

    def test_53300_end_to_end_classify_webhook_exception(self) -> None:
        """RC-04: DB error with SQLSTATE 53300 → RETRYABLE_ERROR with requires_alert=True."""
        db_exc = _DummyDbError("too many connections", sqlstate="53300")
        cls = classify_webhook_exception(db_exc)
        assert cls.category == WebhookExceptionCategory.RETRYABLE_ERROR
        assert cls.should_retry is True
        assert cls.requires_alert is True
        assert cls.sqlstate == "53300"

    def test_53400_configuration_limit_stays_unknown(self) -> None:
        """RC-04: 53400 (configuration_limit_exceeded) stays explicitly UNKNOWN."""
        res = classify_pg_sqlstate("53400")
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.failure_category == "db_unknown_sqlstate"
        assert res.requires_alert is True
        assert res.is_known is False

    def test_57014_bare_sqlstate_is_unknown(self) -> None:
        """RC-04: classify_pg_sqlstate('57014') with no message → UNKNOWN."""
        res = classify_pg_sqlstate("57014")
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.failure_category == "db_unknown_sqlstate"
        assert res.requires_alert is True
        assert res.is_known is False

    def test_57014_statement_timeout_message_retryable_with_alert(self) -> None:
        """RC-04: DB exception with 57014 + 'statement timeout' → RETRYABLE_WITH_ALERT."""
        db_exc = _DummyDbError(
            "canceling statement due to statement timeout",
            sqlstate="57014",
        )
        res = classify_db_exception(db_exc)
        assert res.disposition == DbRetryDisposition.RETRYABLE_WITH_ALERT
        assert res.failure_category == "db_statement_timeout"
        assert res.should_retry is True
        assert res.requires_alert is True
        assert res.is_known is True

    def test_57014_user_request_message_stays_unknown(self) -> None:
        """RC-04: DB exception with 57014 + 'user request' → UNKNOWN."""
        db_exc = _DummyDbError(
            "canceling statement due to user request",
            sqlstate="57014",
        )
        res = classify_db_exception(db_exc)
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.requires_alert is True
        assert res.is_known is False

    def test_57014_spoofed_parameter_does_not_match_statement_timeout(self) -> None:
        """RC-04: Spoofed [parameters: ('statement timeout',)] on user-request cancel stays UNKNOWN."""
        db_exc = _DummyDbError(
            "canceling statement due to user request [parameters: ('statement timeout',)]",
            sqlstate="57014",
        )
        res = classify_db_exception(db_exc)
        # [parameters: ...] is stripped before matching, so spoofed content cannot match.
        assert res.disposition == DbRetryDisposition.UNKNOWN
        assert res.is_known is False

    def test_23001_restrict_violation_is_permanent(self) -> None:
        """RC-04: 23001 (restrict_violation) → PERMANENT."""
        res = classify_pg_sqlstate("23001")
        assert res.disposition == DbRetryDisposition.PERMANENT
        assert res.failure_category == "db_permanent"
        assert res.is_permanent is True
        assert res.should_retry is False
        assert res.requires_alert is False
        assert res.is_known is True


# =============================================================================
# 5. Log & Diagnostic Sanitization Tests
# =============================================================================


class TestBillingErrorSanitization:
    """Verify thorough credential/PII scrubbing, preservation, and idempotency."""

    def test_auth_headers_redacted_and_preserved_structure(self) -> None:
        msg = (
            "Webhook HTTP error: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
            "and Basic dXNlcjpzZWNyZXQ= rejected"
        )
        cleaned = sanitize_billing_error_message(msg)
        assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
        assert "dXNlcjpzZWNyZXQ=" not in cleaned
        assert "[REDACTED_TOKEN]" in cleaned
        assert sanitize_billing_error_message(cleaned) == cleaned

    def test_postgres_urls_and_libpq_passwords_redacted(self) -> None:
        msg = (
            "Failed connecting to postgresql+asyncpg://svc_user:P@ssw0rd!9@db.internal:5432/aptlyst"
            "?sslmode=require&password=query_secret_123; "
            "libpq host=pg password='my secret pass' passfile=/var/run/.pgpass"
        )
        cleaned = sanitize_billing_error_message(msg)
        assert "svc_user" not in cleaned
        assert "P@ssw0rd!9" not in cleaned
        assert "query_secret_123" not in cleaned
        assert "my secret pass" not in cleaned
        assert "/var/run/.pgpass" not in cleaned
        assert "postgresql+asyncpg://[REDACTED]@db.internal:5432/aptlyst" in cleaned
        assert "password=[REDACTED]" in cleaned
        assert "passfile=[REDACTED]" in cleaned
        assert sanitize_billing_error_message(cleaned) == cleaned

    def test_postgres_uri_with_slash_in_password(self) -> None:
        """RC-03: Password component containing '/' is fully redacted."""
        msg = "postgresql://user:p/w@db.internal/app"
        cleaned = sanitize_billing_error_message(msg)
        assert "p/w" not in cleaned
        assert "user" not in cleaned
        assert "postgresql://[REDACTED]@db.internal/app" in cleaned

    def test_uri_query_secret_stops_at_ampersand(self) -> None:
        """RC-03: Query secret value stops at '&' — next param is preserved."""
        msg = "https://api.example.com/v1?client_secret=sec123&other=ok"
        cleaned = sanitize_billing_error_message(msg)
        assert "sec123" not in cleaned
        assert "other=ok" in cleaned

    def test_sqlalchemy_parameters_redacted_while_preserving_surrounding_sql_diag(self) -> None:
        raw = (
            '(asyncpg.exceptions.UniqueViolationError) duplicate key value violates unique constraint '
            '"uq_org_active_subscription"\n'
            '[SQL: INSERT INTO subscriptions (id, organization_id) VALUES ($1, $2)]\n'
            '[parameters: (\'550e8400-e29b-41d4-a716-446655440000\', '
            '\'{"customer": {"email": "victim@example.com", "arr": [1, 2, 3]}, '
            '"secret": "sk_live_bound_param"}\')'
            ']\n'
            "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
        )
        cleaned = sanitize_billing_error_message(raw)
        assert "uq_org_active_subscription" in cleaned
        assert "[SQL: INSERT INTO subscriptions" in cleaned
        assert "[parameters: REDACTED]" in cleaned
        assert "victim@example.com" not in cleaned
        assert "sk_live_bound_param" not in cleaned
        assert "https://sqlalche.me/e/20/gkpj" in cleaned
        assert sanitize_billing_error_message(cleaned) == cleaned

        # Unclosed truncated parameter block still strips tail safely.
        truncated = "DB fail [parameters: {'token': 'sk_live_unclosed"
        cleaned_trunc = sanitize_billing_error_message(truncated)
        assert cleaned_trunc == "DB fail [parameters: REDACTED]"

    def test_razorpay_stripe_webhook_secrets_and_signatures_redacted(self) -> None:
        msg = (
            "Gateway failure rzp_live_AbCdEf987654 rzp_test_11223344 "
            "sk_live_51RealSecret999 sk_test_51TestSecret888 rk_live_Restricted111 "
            "pk_test_Publishable111 pk_live_Publishable222 whsec_WebhookSecret333 "
            "x-razorpay-signature: aabbccddeeff001122 "
            "razorpay_signature=sig99887766 "
            "stripe-signature: t=1700000000,v1=deadbeef1234,v0=cafebabe5678 "
            "key_secret=my_razorpay_key_secret "
            "signature_verified=False"
        )
        cleaned = sanitize_billing_error_message(msg)
        for leaked in (
            "rzp_live_AbCdEf987654",
            "rzp_test_11223344",
            "sk_live_51RealSecret999",
            "sk_test_51TestSecret888",
            "rk_live_Restricted111",
            "pk_test_Publishable111",
            "pk_live_Publishable222",
            "whsec_WebhookSecret333",
            "aabbccddeeff001122",
            "sig99887766",
            "deadbeef1234",
            "cafebabe5678",
            "my_razorpay_key_secret",
        ):
            assert leaked not in cleaned
        # Diagnostic boolean flag must NOT be treated as a signature secret.
        assert "signature_verified=False" in cleaned
        assert sanitize_billing_error_message(cleaned) == cleaned

    def test_emails_phones_and_pans_redacted_cleanly(self) -> None:
        msg = (
            "Customer alice.billing+prod@example.org failed with "
            "+91 98765 43210, +1-555-234-5678, phone=9876543210, "
            "(555) 123-4567, 555-234-5678; "
            "card 4111-1111-1111-1111 and 4532015112830366 next_field=ok"
        )
        cleaned = sanitize_billing_error_message(msg)
        for sensitive in (
            "alice.billing+prod@example.org",
            "98765 43210",
            "555-234-5678",
            "9876543210",
            "(555) 123-4567",
            "4111-1111-1111-1111",
            "4532015112830366",
        ):
            assert sensitive not in cleaned
        assert "[REDACTED_EMAIL]" in cleaned
        assert "[REDACTED_PHONE]" in cleaned
        assert "[REDACTED_PAN] next_field=ok" in cleaned
        assert sanitize_billing_error_message(cleaned) == cleaned

    def test_multiline_phone_does_not_eat_next_line(self) -> None:
        msg = "Call +1 555 234\n5678 attempt=2"
        cleaned = sanitize_billing_error_message(msg)
        assert "attempt=2" in cleaned

    def test_rc03_13digit_ms_epoch_and_acct_rfnd_preserved(self) -> None:
        """RC-03: 13-digit ms epochs, acct_* and rfnd_* IDs are shielded (not PAN-redacted)."""
        msg = "ts=1700000000123 acct_12345 rfnd_67890 card=2221001234567890"
        cleaned = sanitize_billing_error_message(msg)
        # 13-digit ms epoch shielded (not PAN)
        assert "1700000000123" in cleaned
        # acct_ and rfnd_ entity IDs preserved
        assert "acct_12345" in cleaned
        assert "rfnd_67890" in cleaned
        # 16-digit card number still PAN-redacted
        assert "2221001234567890" not in cleaned
        assert "[REDACTED_PAN]" in cleaned

    def test_safe_identifiers_preserved_verbatim(self) -> None:
        msg = (
            "UUID=550e8400-e29b-41d4-a716-446655440000 "
            "digit_uuid=12345678-1234-1234-1234-123456789012 "
            "SQLSTATE=23505 "
            "constraints=uq_org_active_subscription,pk_subscriptions,fk_billing_payments_checkout_attempt_composite,ix_billing_reconciliations_org_status "
            "entities=sub_12345678901234,pay_99887766554433,inv_AbC123,cust_XyZ789,order_11223344556677,evt_00112233445566 "
            "correlation=corr_550e8400,req_9988776655443322,trace-42 "
            "created_at=1700000000 updated_at=1794300000.125 iso=2026-11-10T20:50:00Z"
        )
        cleaned = sanitize_billing_error_message(msg)
        for token in (
            "550e8400-e29b-41d4-a716-446655440000",
            "12345678-1234-1234-1234-123456789012",
            "SQLSTATE=23505",
            "uq_org_active_subscription",
            "pk_subscriptions",
            "fk_billing_payments_checkout_attempt_composite",
            "ix_billing_reconciliations_org_status",
            "sub_12345678901234",
            "pay_99887766554433",
            "inv_AbC123",
            "cust_XyZ789",
            "order_11223344556677",
            "evt_00112233445566",
            "corr_550e8400",
            "req_9988776655443322",
            "trace-42",
            "1700000000",
            "1794300000.125",
            "2026-11-10T20:50:00Z",
        ):
            assert token in cleaned

    def test_repeated_sanitization_idempotency_and_length_cap(self) -> None:
        composite = (
            "Error on org 550e8400-e29b-41d4-a716-446655440000 sub_12345678901234 "
            "SQLSTATE=23505 constraint=uq_org_active_subscription pk_subscriptions "
            "epoch=1700000000 Authorization: Bearer jwt_secret_123 "
            "postgresql://admin:dbpass@db/app rzp_live_abc12345 sk_live_xyz98765 "
            "whsec_hook123 email=user@example.com phone=+91-98765-43210 "
            "card=4111-1111-1111-1111 [parameters: ('secret_val',)] "
            "x-razorpay-signature: sig_val_999 signature_verified=False"
        )
        pass1 = sanitize_billing_error_message(composite)
        pass2 = sanitize_billing_error_message(pass1)
        pass3 = sanitize_billing_error_message(pass2)
        assert pass1 == pass2 == pass3
        assert len(pass1) <= MAX_SANITIZED_ERROR_CHARS

        huge = "sub_12345 " + ("A" * 2000)
        bounded = sanitize_billing_error_message(huge)
        assert len(bounded) <= MAX_SANITIZED_ERROR_CHARS

        assert sanitize_billing_error_message(None) == ""
        assert sanitize_billing_error_message("   ") == ""
        assert sanitize_billing_error_message("anything", max_chars=0) == ""
        assert sanitize_billing_error_message(_BrokenStrError()) == "_BrokenStrError"

    def test_l01a_mid_marker_truncation_does_not_clip_inside_bracket_token(self) -> None:
        """L-01a: Truncation at max_chars steps back before an open '[' token."""
        # Craft a message where truncation would land inside [REDACTED...].
        prefix = "A" * 1010  # pad to bring us close to 1024
        msg = prefix + " [REDACTED_EMAIL]"  # would be clipped mid-token
        bounded = sanitize_billing_error_message(msg, max_chars=1024)
        # Result must not contain a dangling '[' without its ']'.
        assert "[" not in bounded or bounded.count("[") == bounded.count("]"), (
            f"Dangling '[' in result: {bounded[-50:]!r}"
        )
        assert len(bounded) <= 1024

    def test_sanitize_conflict_diagnostic_payload_strips_raw_and_bounds_depth(self) -> None:
        raw = {
            "event_type": "subscription.activated",
            "provider_subscription_id": "sub_12345678901234",
            "raw_payload": {"entity": {"email": "secret@example.com", "secret": "sk_live_999"}},
            "payload": {"anything": "bad"},
            "headers": {"authorization": "Bearer abc"},
            "x-razorpay-signature": "sig_123",
            "razorpay_signature": "sig_456",
            "email": "user@example.com",
            "phone": "+919876543210",
            "attempt_ids": [
                "550e8400-e29b-41d4-a716-446655440000",
                "Bearer leaked_in_list",
            ],
            "nested": {
                "message": "DSN postgres://u:p@h/db failed",
                "deep": {
                    "note": "still safe scalar",
                    "too_deep": {"hidden": "sk_live_deep_secret"},
                },
            },
        }
        cleaned = sanitize_conflict_diagnostic_payload(raw, max_depth=2)
        for forbidden in (
            "raw_payload",
            "payload",
            "headers",
            "x-razorpay-signature",
            "razorpay_signature",
            "email",
            "phone",
        ):
            assert forbidden not in cleaned

        assert cleaned["event_type"] == "subscription.activated"
        assert cleaned["provider_subscription_id"] == "sub_12345678901234"
        assert cleaned["attempt_ids"][0] == "550e8400-e29b-41d4-a716-446655440000"
        assert "leaked_in_list" not in cleaned["attempt_ids"][1]
        assert "postgres://[REDACTED]@h/db" in cleaned["nested"]["message"]
        assert cleaned["nested"]["deep"]["note"] == "still safe scalar"
        assert cleaned["nested"]["deep"]["too_deep"] == "[NESTED_OBJECT_OMITTED]"
        assert "sk_live_deep_secret" not in str(cleaned)

    def test_rc03_cyclic_diagnostic_payload_does_not_raise(self) -> None:
        """RC-03: Self-referential list in diagnostic payload → [CYCLE_OMITTED], no crash."""
        cyclic: list[Any] = []
        cyclic.append(cyclic)
        raw = {"x": cyclic}
        cleaned = sanitize_conflict_diagnostic_payload(raw, max_depth=2)
        # Must not raise RecursionError and must mark cycle.
        assert "x" in cleaned
        assert "[CYCLE_OMITTED]" in str(cleaned)

    def test_rc03_nested_list_depth_bounded(self) -> None:
        """RC-03: Nested list beyond max_depth is omitted, not recursed."""
        raw = {"x": [[[{"secret": "sk_live_deep"}]]]}
        cleaned = sanitize_conflict_diagnostic_payload(raw, max_depth=2)
        assert "sk_live_deep" not in str(cleaned)

    def test_rc03_sensitive_key_detection_allows_safe_keys(self) -> None:
        """RC-03: idempotency_key, signature_verified, lease_token_prefix preserved."""
        raw = {
            "idempotency_key": "k1",
            "signature_verified": False,
            "lease_token_prefix": "550e8400",
            "api_secret": "raw_secret",           # must be stripped
            "razorpay_signature": "sig_xxx",      # must be stripped
            "customer_email": "x@y.com",          # must be stripped (contains 'email')
        }
        cleaned = sanitize_conflict_diagnostic_payload(raw, max_depth=2)
        assert "idempotency_key" in cleaned
        assert cleaned["idempotency_key"] == "k1"
        assert "signature_verified" in cleaned
        assert cleaned["signature_verified"] is False
        assert "lease_token_prefix" in cleaned
        assert "api_secret" not in cleaned
        assert "razorpay_signature" not in cleaned
        assert "customer_email" not in cleaned

    def test_extract_safe_traceback_locations_excludes_local_secrets(self) -> None:
        def _inner_helper() -> None:
            local_secret = "sk_live_local_frame_secret_999"
            _ = local_secret
            raise RuntimeError("inner failure")

        caught: RuntimeError | None = None
        try:
            _inner_helper()
        except RuntimeError as exc:
            caught = exc

        assert caught is not None
        locs = extract_safe_traceback_locations(caught)
        assert len(locs) >= 1
        joined = "\n".join(locs)
        assert "test_billing_webhook_errors.py" in joined
        assert "_inner_helper" in joined
        assert "sk_live_local_frame_secret_999" not in joined
        assert extract_safe_traceback_locations(None) == []

    def test_rc05_max_frames_zero_or_negative_returns_empty(self) -> None:
        """RC-05: max_frames <= 0 → []."""
        try:
            raise RuntimeError("test")
        except RuntimeError as exc:
            assert extract_safe_traceback_locations(exc, max_frames=0) == []
            assert extract_safe_traceback_locations(exc, max_frames=-1) == []
            assert extract_safe_traceback_locations(exc, max_frames=-100) == []

    def test_rc05_excessive_max_frames_clamped_to_max_traceback_frames(self) -> None:
        """RC-05: max_frames > MAX_TRACEBACK_FRAMES is clamped safely."""
        try:
            raise RuntimeError("test")
        except RuntimeError as exc:
            locs = extract_safe_traceback_locations(exc, max_frames=100_000)
            # Should not crash and should return ≤ MAX_TRACEBACK_FRAMES entries.
            assert len(locs) <= MAX_TRACEBACK_FRAMES

    def test_rc05_innermost_frames_returned_for_deep_stack(self) -> None:
        """RC-05: Only innermost max_frames are returned for a deep call chain."""
        def make_chain(n: int) -> None:
            if n == 0:
                raise RuntimeError("crash")
            make_chain(n - 1)

        caught: RuntimeError | None = None
        try:
            make_chain(13)  # 14 frames deep
        except RuntimeError as exc:
            caught = exc

        assert caught is not None
        locs = extract_safe_traceback_locations(caught, max_frames=5)
        assert len(locs) == 5
        # Last entry must be the crash site (innermost frame).
        assert "crash" in locs[-1] or "make_chain" in locs[-1]
        # The outermost `make_chain(13)` call should NOT appear (only innermost 5).
        # (We can't guarantee exact frame names but can verify count is exactly 5.)

    def test_rc05_host_path_stripped_from_traceback_filename(self) -> None:
        """RC-05: Host filesystem prefixes are stripped; repo-relative paths preserved."""
        from app.services.billing.webhook_errors import _sanitize_traceback_filename

        # Windows workspace path → repo-relative.
        win_path = r"D:\Projects\V1\Aptlyst-AI\apps\api\app\services\billing\webhook_errors.py"
        result = _sanitize_traceback_filename(win_path)
        assert "apps/api/" in result
        assert "D:" not in result
        assert "Projects" not in result

        # Linux container path → repo-relative.
        linux_path = "/srv/deploy/apps/api/tests/test_x.py"
        result_lx = _sanitize_traceback_filename(linux_path)
        assert result_lx == "apps/api/tests/test_x.py" or "tests/test_x.py" in result_lx
        assert "/srv" not in result_lx

        # site-packages → package-relative subpath.
        sp_path = "/home/runner/.venv/lib/python3.12/site-packages/sqlalchemy/orm/session.py"
        result_sp = _sanitize_traceback_filename(sp_path)
        assert "sqlalchemy/orm/session.py" in result_sp
        assert "home" not in result_sp

        # Unrecognized path → basename only.
        priv_path = "/home/alice/secret/run.py"
        result_priv = _sanitize_traceback_filename(priv_path)
        assert result_priv == "run.py"
        assert "alice" not in result_priv

        # Pseudo-filename preserved as-is.
        assert _sanitize_traceback_filename("<string>") == "<string>"
        assert _sanitize_traceback_filename("<frozen importlib._bootstrap>") == "<frozen importlib._bootstrap>"

    # -------------------------------------------------------------------------
    # F-01: Boundary-aware compound secret-key sanitization regression tests
    # -------------------------------------------------------------------------

    def test_f01_compound_underscore_secret_keys_redacted(self) -> None:
        """F-01: Compound underscore secret keys (razorpay_key_secret, etc.) redacted."""
        cases = [
            ("razorpay_key_secret=mySuperSecret999", "razorpay_key_secret=[REDACTED]", "mySuperSecret999"),
            ("my_webhook_secret=secret123", "my_webhook_secret=[REDACTED]", "secret123"),
            ("webhook_signature=signature123", "webhook_signature=[REDACTED]", "signature123"),
            ("api_secret=secret123", "api_secret=[REDACTED]", "secret123"),
            ("client_secret=secret123", "client_secret=[REDACTED]", "secret123"),
            ("access_token=token123", "access_token=[REDACTED]", "token123"),
            ("refresh_token=token123", "refresh_token=[REDACTED]", "token123"),
            ("id_token=token123", "id_token=[REDACTED]", "token123"),
            ("auth_token=token123", "auth_token=[REDACTED]", "token123"),
            ("billing_access_token=token123", "billing_access_token=[REDACTED]", "token123"),
            ("oauth_refresh_token=token123", "oauth_refresh_token=[REDACTED]", "token123"),
            ("secret_key=key123", "secret_key=[REDACTED]", "key123"),
            ("secret_token=tok123", "secret_token=[REDACTED]", "tok123"),
            ("private_key=pk123", "private_key=[REDACTED]", "pk123"),
        ]
        for inp, expected, secret in cases:
            cleaned = sanitize_billing_error_message(inp)
            assert cleaned == expected, f"Failed for {inp}: got {cleaned}"
            assert secret not in cleaned

    def test_f01_delimiter_and_spacing_variations(self) -> None:
        """F-01: Supported delimiters (=, :), spacing, and quote variants handled."""
        delims = [
            ("razorpay_key_secret: mySuperSecret999", "razorpay_key_secret: [REDACTED]"),
            ("razorpay_key_secret = mySuperSecret999", "razorpay_key_secret = [REDACTED]"),
            ("razorpay_key_secret : mySuperSecret999", "razorpay_key_secret : [REDACTED]"),
            ("razorpay_key_secret='mySuperSecret999'", "razorpay_key_secret=[REDACTED]"),
            ('razorpay_key_secret="mySuperSecret999"', "razorpay_key_secret=[REDACTED]"),
        ]
        for inp, expected in delims:
            cleaned = sanitize_billing_error_message(inp)
            assert cleaned == expected
            assert "mySuperSecret999" not in cleaned

    def test_f01_non_secret_identifiers_and_safe_exclusions_preserved(self) -> None:
        """F-01: Ordinary non-secret identifiers and safe-value exclusions preserved."""
        non_secrets = [
            "idempotency_key=k1",
            "lease_token_prefix=550e8400",
            "order_id=order_123",
            "customer_id=cust_123",
            "case_key=case_123",
            "max_tokens=1000",
            "token_count=50",
            "token_type=Bearer",
            "routing_key=rk_1",
            "partition_key=p_1",
            "signature_algorithm=HS256",
        ]
        for ns in non_secrets:
            assert sanitize_billing_error_message(ns) == ns

        safe_exclusions = [
            "signature_verified=False",
            "signature_verified=True",
            "signature_verified=false",
            "signature_verified=true",
            "signature_verified: None",
            "signature_verified: null",
            "Authorization: Bearer [REDACTED_TOKEN]",
        ]
        for se in safe_exclusions:
            cleaned = sanitize_billing_error_message(se)
            assert "[REDACTED]" not in cleaned or "[REDACTED_TOKEN]" in cleaned
            assert cleaned == se

    def test_f01_multiple_assignments_and_idempotency(self) -> None:
        """F-01: Multiple secret assignments in one string and repeated sanitization."""
        msg = (
            "Error in worker: razorpay_key_secret=super_secret_123 and "
            "my_webhook_secret=webhook_secret_456 with "
            "api_secret=api_sec_789, client_secret=client_sec_abc, "
            "access_token=acc_tok_def, refresh_token=ref_tok_ghi, "
            "webhook_signature=wh_sig_jkl while preserving idempotency_key=safe_key_999 "
            "and signature_verified=False"
        )
        cleaned = sanitize_billing_error_message(msg)
        assert "razorpay_key_secret=[REDACTED]" in cleaned
        assert "my_webhook_secret=[REDACTED]" in cleaned
        assert "api_secret=[REDACTED]" in cleaned
        assert "client_secret=[REDACTED]" in cleaned
        assert "access_token=[REDACTED]" in cleaned
        assert "refresh_token=[REDACTED]" in cleaned
        assert "webhook_signature=[REDACTED]" in cleaned
        assert "idempotency_key=safe_key_999" in cleaned
        assert "signature_verified=False" in cleaned

        for secret in (
            "super_secret_123",
            "webhook_secret_456",
            "api_sec_789",
            "client_sec_abc",
            "acc_tok_def",
            "ref_tok_ghi",
            "wh_sig_jkl",
        ):
            assert secret not in cleaned

        # Repeated sanitization must be completely idempotent.
        pass2 = sanitize_billing_error_message(cleaned)
        pass3 = sanitize_billing_error_message(pass2)
        assert cleaned == pass2 == pass3

    def test_f01_long_malformed_inputs_and_backtracking_safety(self) -> None:
        """F-01: Long malformed inputs and pathological patterns terminate safely without ReDoS."""
        import time

        t0 = time.perf_counter()

        unclosed = "razorpay_key_secret='unclosed_quote_" + "X" * 3000
        cleaned_unclosed = sanitize_billing_error_message(unclosed)
        assert len(cleaned_unclosed) <= MAX_SANITIZED_ERROR_CHARS
        assert "unclosed_quote_" not in cleaned_unclosed

        rep_delims = "razorpay_key_secret= := =: " * 200
        cleaned_delims = sanitize_billing_error_message(rep_delims)
        assert len(cleaned_delims) <= MAX_SANITIZED_ERROR_CHARS

        pathological = "config_param=safe_val " + ("seg_" * 500)
        cleaned_path = sanitize_billing_error_message(pathological)
        assert len(cleaned_path) <= MAX_SANITIZED_ERROR_CHARS
        assert "safe_val" in cleaned_path

        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"Sanitization took too long: {elapsed:.3f}s"


# =============================================================================
# 6. RC-01: Webhook Exception Classification Tests
# =============================================================================


class TestWebhookExceptionClassification:
    """RC-01: 12-scenario regression suite for classify_webhook_exception()."""

    # Scenario 1: All 4 Tier-1 unequivocal programming error types.
    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("wrong type"),
            AttributeError("missing attr"),
            NameError("undefined name"),
            UnboundLocalError("used before assign"),
        ],
    )
    def test_tier1_programming_errors_quarantine_immediately(self, exc: Exception) -> None:
        cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.PROGRAMMING_ERROR
        assert cls.failure_category == "programming_error"
        assert cls.should_quarantine_immediately is True
        assert cls.should_retry is False
        assert cls.is_programming_error is True
        assert cls.is_permanent_error is True
        assert cls.requires_alert is True
        assert cls.is_lease_error is False

    def test_tier1_unequivocal_programming_error_types_constant(self) -> None:
        """is_unequivocal_programming_error() correctly identifies Tier-1 types."""
        for t in (TypeError, AttributeError, NameError, UnboundLocalError):
            assert is_unequivocal_programming_error(t("test")) is True
        assert is_unequivocal_programming_error(NotImplementedError()) is False
        assert is_unequivocal_programming_error(ValueError()) is False
        assert is_unequivocal_programming_error(KeyError()) is False

    # Scenario 2: Direct Tier-2 context-dependent exceptions → UNKNOWN_EXCEPTION.
    @pytest.mark.parametrize(
        "exc",
        [
            NotImplementedError("provider cap"),
            AssertionError("assert failed"),
            ZeroDivisionError("divide by zero"),
            RecursionError("max depth"),
            SyntaxError("bad syntax"),
            IndentationError("bad indent"),
        ],
    )
    def test_tier2_context_dependent_are_unknown_with_alert(self, exc: Exception) -> None:
        cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.failure_category == "unexpected_internal_error"
        assert cls.should_quarantine_immediately is False
        assert cls.should_retry is True
        assert cls.is_programming_error is False
        assert cls.requires_alert is True

    # Scenario 3: Wrapped NotImplementedError — handler explicit wrapping honored.
    def test_wrapped_not_implemented_in_permanent_error_is_permanent(self) -> None:
        exc = BillingWebhookUnknownEventError("unsupported op")
        exc.__cause__ = NotImplementedError("provider cap")
        cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.PERMANENT_ERROR
        assert cls.should_quarantine_immediately is True
        assert cls.is_programming_error is False

    def test_wrapped_not_implemented_in_retryable_error_is_retryable(self) -> None:
        exc = BillingWebhookRetryableError("temp fallback")
        exc.__cause__ = NotImplementedError("capability")
        cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.RETRYABLE_ERROR
        assert cls.should_retry is True
        assert cls.is_programming_error is False

    def test_runtime_error_from_not_implemented_stays_unknown(self) -> None:
        """NotImplementedError in __cause__ of a plain RuntimeError → UNKNOWN, not PROGRAMMING_ERROR."""
        exc = RuntimeError("dispatch")
        exc.__cause__ = NotImplementedError("stub")
        cls = classify_webhook_exception(exc)
        # RuntimeError with explicit __cause__ has causal identity → Step 5 DB fallback.
        # Classify_db_exception(RuntimeError with __cause__=NotImplementedError) → UNKNOWN.
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.is_programming_error is False

    def test_not_implemented_inside_caught_db_context_stays_unknown(self) -> None:
        """NotImplementedError raised inside except IntegrityError (ambient __context__) → UNKNOWN."""
        try:
            try:
                raise _DummyDbError("23503 fk violation", sqlstate="23503")
            except _DummyDbError:
                raise NotImplementedError("fallback capability miss")
        except NotImplementedError as exc:
            cls = classify_webhook_exception(exc)
        # exc has __context__ = DB error but NO __cause__ and NO DB attrs on exc itself.
        # Step 4 non-override guard → UNKNOWN_EXCEPTION, NOT PERMANENT_ERROR.
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.failure_category == "unexpected_internal_error"
        assert cls.requires_alert is True

    # Scenario 4: Tier-3 ordinary unknown exceptions.
    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("missing_key"),
            ValueError("invalid value"),
            IndexError("out of range"),
            RuntimeError("generic failure"),
        ],
    )
    def test_tier3_ordinary_unknown_exceptions_with_alert(self, exc: Exception) -> None:
        cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.requires_alert is True
        assert cls.is_programming_error is False

    # Scenario 5: Programming bug inside except DB block (__context__ DB non-masking).
    def test_tier1_inside_db_except_block_is_programming_error(self) -> None:
        """AttributeError raised inside except UniqueViolationError → PROGRAMMING_ERROR."""
        try:
            try:
                raise _DummyDbError("23505 unique", sqlstate="23505",
                                    constraint_name="uq_org_active_subscription")
            except _DummyDbError:
                raise AttributeError("bug in handler")
        except AttributeError as exc:
            cls = classify_webhook_exception(exc)
        assert cls.category == WebhookExceptionCategory.PROGRAMMING_ERROR
        assert cls.failure_category == "programming_error"
        assert cls.should_quarantine_immediately is True

    # Scenario 6: Nested Tier-1 inside __cause__/__context__ does NOT hijack outer.
    def test_tier1_in_cause_does_not_hijack_outer_runtime_error(self) -> None:
        exc = RuntimeError("outer")
        exc.__cause__ = TypeError("inner")
        cls = classify_webhook_exception(exc)
        # RuntimeError is Tier-3; Step 2 misses, Step 3 misses (not Tier-1), Step 4 → has __cause__ → DB.
        # DB classification of RuntimeError → UNKNOWN.
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.is_programming_error is False

    def test_tier1_in_context_of_operational_error_does_not_hijack(self) -> None:
        """AttributeError as __context__ of OperationalError → outer DB classification wins."""
        db_exc = _DummyDbError("08006 conn failure", sqlstate="08006")
        db_exc.__context__ = AttributeError("incidental bug")
        cls = classify_webhook_exception(db_exc)
        # DB error with 08006 → RETRYABLE, not PROGRAMMING_ERROR.
        assert cls.category == WebhookExceptionCategory.RETRYABLE_ERROR
        assert cls.is_programming_error is False

    def test_retryable_billing_error_from_tier1_not_programming_error(self) -> None:
        exc = BillingWebhookRetryableError("network timeout")
        exc.__cause__ = TypeError("bug")
        cls = classify_webhook_exception(exc)
        # Step 2: BillingWebhookRetryableError → RETRYABLE_ERROR, NOT PROGRAMMING_ERROR.
        assert cls.category == WebhookExceptionCategory.RETRYABLE_ERROR
        assert cls.is_programming_error is False

    # Scenario 7: Non-DB exception inside caught DB except block → UNKNOWN (non-override).
    def test_non_db_exception_in_caught_db_context_stays_unknown(self) -> None:
        """KeyError inside except ForeignKeyViolation → UNKNOWN, not PERMANENT_ERROR."""
        try:
            try:
                raise _DummyDbError("23503 fk", sqlstate="23503")
            except _DummyDbError:
                raise KeyError("metrics lookup failed")
        except KeyError as exc:
            cls = classify_webhook_exception(exc)
        # exc has __context__ = 23503 but NO __cause__ and NO DB identity on KeyError.
        # Step 4: non-DB exc without causal identity → UNKNOWN_EXCEPTION.
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.failure_category == "unexpected_internal_error"
        assert cls.requires_alert is True

    # Scenario 8: Explicit __cause__ vs. incidental __context__ priority.
    def test_explicit_cause_takes_precedence_over_incidental_context(self) -> None:
        """WrapperError with __cause__ pointing to 23505 DB error uses causal path."""
        # Create a wrapping error whose cause is a 23505 unique violation.
        cause_exc = _DummyDbError("23505 perm", sqlstate="23505",
                                  constraint_name="uq_org_active_subscription")
        ctx_exc = _DummyDbError("08006 retryable", sqlstate="08006")

        wrapper = RuntimeError("wrapper")
        wrapper.__cause__ = cause_exc
        wrapper.__context__ = ctx_exc

        cls = classify_webhook_exception(wrapper)
        # Causal path wins: 23505 uq_org_active_subscription → Bucket 3 PERMANENT → PERMANENT_ERROR.
        assert cls.category == WebhookExceptionCategory.PERMANENT_ERROR
        assert cls.sqlstate == "23505"

    # Scenario 9: Suppressed context (raise ... from None).
    def test_suppressed_context_raise_from_none_is_unknown(self) -> None:
        """raise RuntimeError from None inside except IntegrityError → UNKNOWN (context suppressed)."""
        try:
            try:
                raise _DummyDbError("23503 fk", sqlstate="23503")
            except _DummyDbError:
                raise RuntimeError("app error") from None
        except RuntimeError as exc:
            cls = classify_webhook_exception(exc)
        # `raise X from None` sets __suppress_context__=True; __context__ is severed.
        # RuntimeError has no DB identity and no __cause__ → Step 4 → UNKNOWN_EXCEPTION.
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.sqlstate is None

    # Scenario 10: Authoritative BillingWebhookError hierarchy & bare base guard.
    def test_billing_webhook_error_subclasses_classify_correctly(self) -> None:
        assert classify_webhook_exception(BillingWebhookLeaseError()).category == WebhookExceptionCategory.STALE_LEASE
        assert classify_webhook_exception(BillingWebhookLeaseError()).is_lease_error is True

        for perm_cls in (BillingWebhookValidationError, BillingWebhookScopeMismatchError,
                         BillingWebhookConflictError, BillingWebhookUnknownEventError):
            cls = classify_webhook_exception(perm_cls())
            assert cls.category == WebhookExceptionCategory.PERMANENT_ERROR
            assert cls.should_quarantine_immediately is True

        cls_retry = classify_webhook_exception(BillingWebhookRetryableError())
        assert cls_retry.category == WebhookExceptionCategory.RETRYABLE_ERROR
        assert cls_retry.should_retry is True

    def test_bare_billing_webhook_error_is_unknown_with_alert(self) -> None:
        """L-03: Bare BillingWebhookError() → UNKNOWN_EXCEPTION with requires_alert=True."""
        cls = classify_webhook_exception(BillingWebhookError())
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.requires_alert is True
        assert cls.should_retry is True
        assert cls.is_programming_error is False

    # Scenario 11: Cycle and deep-chain safety.
    def test_cycle_and_deep_chain_terminate_safely(self) -> None:
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__context__ = a
        # Should not raise RecursionError or loop forever.
        cls = classify_webhook_exception(a)
        assert cls.category in WebhookExceptionCategory.__members__.values()

        # 40-node linear acyclic chain.
        head = RuntimeError("head")
        for _ in range(39):
            prev = RuntimeError("chain")
            prev.__cause__ = head
            head = prev
        cls2 = classify_webhook_exception(head)
        assert cls2 is not None

    # Scenario 12: BaseException propagation & enum disjointness.
    def test_base_exception_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError is re-raised, not classified."""
        exc = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            classify_webhook_exception(exc)

    def test_base_exception_keyboard_interrupt_propagates(self) -> None:
        exc = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            classify_webhook_exception(exc)

    def test_base_exception_system_exit_propagates(self) -> None:
        exc = SystemExit(1)
        with pytest.raises(SystemExit):
            classify_webhook_exception(exc)

    def test_webhook_exception_category_disjoint_from_event_statuses(self) -> None:
        """WebhookExceptionCategory values must not overlap BillingWebhookEvent.status values."""
        event_statuses = {"pending", "processed", "failed", "quarantined"}
        category_values = {c.value for c in WebhookExceptionCategory}
        assert category_values.isdisjoint(event_statuses), (
            f"Overlap: {category_values & event_statuses}"
        )

    def test_unrecognized_sqlstate_via_classify_webhook_exception_is_unknown(self) -> None:
        """Tier-3: DB error with unrecognized SQLSTATE 99999 → UNKNOWN_EXCEPTION, requires_alert."""
        db_exc = _DummyDbError("internal error", sqlstate="99999")
        cls = classify_webhook_exception(db_exc)
        assert cls.category == WebhookExceptionCategory.UNKNOWN_EXCEPTION
        assert cls.requires_alert is True
        assert cls.is_programming_error is False
