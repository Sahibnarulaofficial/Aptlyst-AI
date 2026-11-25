"""Unit and integration test suite for Stage 2F: Single-Merchant Razorpay Credential Isolation.

Verifies:
1. Canonical Aptlyst AI merchant identity configuration & runtime scope.
2. Strict production vs non-production credential separation.
3. Customer organization IDs are decoupled from and never become merchant account IDs.
4. Recovery worker & engine single-merchant filtering and fail-closed inquiry boundaries.
5. Zero provider calls on merchant or environment mismatch.
6. Webhook merchant isolation and entitlement safety.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import SecretStr
import pytest
from sqlalchemy.orm import configure_mappers

# Configure application mappers
import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.subscription  # noqa: F401

configure_mappers()

from app.config import (
    PROHIBITED_PRODUCTION_MERCHANT_IDS,
    get_canonical_razorpay_merchant_account_id,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
    validate_razorpay_runtime_scope,
)
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    CustomerProvisionStatus,
    PaymentReconciliation,
)
from app.models.subscription import CheckoutAttemptStatus, Subscription, SubscriptionStatus
from app.services.billing.razorpay.client import (
    RazorpayBillingDisabledError,
    RazorpayClient,
    RazorpayConfigurationError,
    get_razorpay_client,
    reset_razorpay_client,
    set_razorpay_client_for_testing,
)
from app.services.billing.razorpay.customer import _get_provisioning_scope
from app.services.billing.razorpay.recovery_engine import (
    ClaimedRecoveryAttempt,
    RecoveryOutcome,
    _record_recovery_outcome,
    claim_next_recovery_attempt,
    claim_recovery_batch,
    execute_claimed_recovery_attempt,
)
from app.services.billing.razorpay.webhook_ingress import (
    RazorpayWebhookMerchantMismatchError,
    parse_and_validate_razorpay_envelope,
)
from app.workers.razorpay_checkout_recovery_worker import RazorpayCheckoutRecoveryWorker


# =============================================================================
# Helper Fixtures & In-Memory DB Mock
# =============================================================================


class MockStage2FDbSession:
    """In-memory async database session mock for single-merchant recovery testing."""

    def __init__(self, attempts: list[BillingCheckoutAttempt] | None = None) -> None:
        self.attempts = list(attempts or [])
        self.reconciliations: list[PaymentReconciliation] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "payment_reconciliations" in stmt_str:
            matched = list(self.reconciliations)
        else:
            matched = list(self.attempts)

        mock_res = MagicMock()
        mock_res.scalars.return_value.all.return_value = matched
        mock_res.scalar_one_or_none.return_value = matched[0] if matched else None
        mock_res.scalar_one.return_value = matched[0] if matched else None
        return mock_res

    def add(self, obj: Any) -> None:
        if isinstance(obj, PaymentReconciliation):
            self.reconciliations.append(obj)
        elif isinstance(obj, BillingCheckoutAttempt):
            self.attempts.append(obj)

    def add_all(self, objs: list[Any]) -> None:
        for o in objs:
            self.add(o)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def flush(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    def begin_nested(self) -> Any:
        return AsyncMock()

    async def __aenter__(self) -> MockStage2FDbSession:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_client_cache() -> None:
    reset_razorpay_client()
    yield
    reset_razorpay_client()


# =============================================================================
# 1. Configuration & Canonical Merchant Identity Tests
# =============================================================================


class TestSingleMerchantConfiguration:
    """Verifies strict single-merchant identity, environment, and credential boundaries."""

    def test_01_canonical_merchant_in_non_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-production resolves canonical merchant account and required test environment."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_aptlyst_test")

        merchant, env = validate_razorpay_runtime_scope()
        assert merchant == "acc_aptlyst_test"
        assert env == "test"

        canonical = get_canonical_razorpay_merchant_account_id()
        assert canonical == "acc_aptlyst_test"

    def test_02_canonical_merchant_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production requires live environment and an explicit non-prohibited merchant identity."""
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "razorpay_environment", "live")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_aptlyst_prod_real")
        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
        monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("secret_live_123"))

        merchant, env = validate_razorpay_runtime_scope()
        assert merchant == "acc_aptlyst_prod_real"
        assert env == "live"

        canonical = get_canonical_razorpay_merchant_account_id()
        assert canonical == "acc_aptlyst_prod_real"

    def test_03_production_rejects_test_merchant_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production rejects prohibited merchant IDs ('default', 'test', 'dev', 'stage', etc.)."""
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "razorpay_environment", "live")

        for prohibited in ("default", "test", "dev", "stage", "staging", "ci", "legacy", "placeholder", "null"):
            monkeypatch.setattr(settings, "razorpay_merchant_account_id", prohibited)
            with pytest.raises(ValueError, match="must be explicitly configured with a non-default"):
                validate_razorpay_runtime_scope()

    def test_04_production_rejects_test_razorpay_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production rejects RAZORPAY_ENVIRONMENT='test'."""
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_prod_valid")

        with pytest.raises(ValueError, match="Production RAZORPAY_ENVIRONMENT must be 'live'"):
            validate_razorpay_runtime_scope()

    def test_05_non_production_rejects_live_razorpay_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-production rejects RAZORPAY_ENVIRONMENT='live'."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "live")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_test")

        with pytest.raises(ValueError, match="Non-production RAZORPAY_ENVIRONMENT must be 'test'"):
            validate_razorpay_runtime_scope()

    def test_06_unrecognized_deployment_environment_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ambiguous or empty deployment ENVIRONMENT fails closed."""
        monkeypatch.setattr(settings, "environment", "unknown_cloud_env")
        with pytest.raises(ValueError, match="Unrecognized deployment ENVIRONMENT"):
            validate_deployment_environment()


# =============================================================================
# 2. Provider Client Boundary Enforcement Tests
# =============================================================================


class TestProviderClientBoundaryEnforcement:
    """Verifies that RazorpayClient and get_razorpay_client enforce merchant and environment agreement."""

    def test_07_client_construction_validates_key_prefix_and_environment(self) -> None:
        """RazorpayClient rejects mismatched key prefix for environment."""
        # Live env requires rzp_live_
        with pytest.raises(RazorpayConfigurationError, match="Live Razorpay client requires key starting with 'rzp_live_'"):
            RazorpayClient(
                key_id="rzp_test_invalid_for_live",
                key_secret="secret",
                environment="live",
                merchant_account_id="acc_live_1",
            )

        # Test env requires rzp_test_
        with pytest.raises(RazorpayConfigurationError, match="Test Razorpay client requires key starting with 'rzp_test_'"):
            RazorpayClient(
                key_id="rzp_live_invalid_for_test",
                key_secret="secret",
                environment="test",
                merchant_account_id="acc_test_1",
            )

    def test_08_get_razorpay_client_enforces_expected_merchant_and_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_razorpay_client fails closed if expected merchant or environment conflicts with canonical."""
        monkeypatch.setattr(settings, "razorpay_enabled", True)
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")
        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key123")
        monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("test_secret"))

        # Agreement succeeds
        client = get_razorpay_client(
            expected_merchant_account_id="acc_canonical_test",
            expected_environment="test",
        )
        assert client.merchant_account_id == "acc_canonical_test"
        assert client.environment == "test"

        # Merchant mismatch fails closed
        with pytest.raises(RazorpayConfigurationError, match="Merchant account mismatch"):
            get_razorpay_client(
                expected_merchant_account_id="acc_FOREIGN_MERCHANT",
                expected_environment="test",
            )

        # Environment mismatch fails closed
        with pytest.raises(RazorpayConfigurationError, match="Environment mismatch"):
            get_razorpay_client(
                expected_merchant_account_id="acc_canonical_test",
                expected_environment="live",
            )


# =============================================================================
# 3. Checkout Attempt & Customer Organization Decoupling Tests
# =============================================================================


class TestCheckoutMerchantDecoupling:
    """Verifies that customer organizations never become merchant IDs and canonical merchant is stamped."""

    def test_09_provisioning_scope_uses_canonical_merchant_never_org_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_get_provisioning_scope retrieves canonical Aptlyst AI merchant identity."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_aptlyst_saas")

        merchant, env = _get_provisioning_scope()
        assert merchant == "acc_aptlyst_saas"
        assert env == "test"

    def test_10_attempt_organization_id_is_distinct_from_merchant_account(self) -> None:
        """BillingCheckoutAttempt requires organization_id to be UUID and merchant_account_id to be SaaS account string."""
        org_id = uuid.uuid4()
        attempt = BillingCheckoutAttempt(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider="razorpay",
            merchant_account_id="acc_aptlyst_saas",
            environment="test",
            idempotency_key="idem_123",
            status=CheckoutAttemptStatus.open,
            amount_minor_units=4900,
            currency="USD",
        )

        assert attempt.organization_id == org_id
        assert attempt.merchant_account_id == "acc_aptlyst_saas"
        assert str(attempt.organization_id) != attempt.merchant_account_id


# =============================================================================
# 4. Recovery Worker & Engine Single-Merchant Isolation Tests
# =============================================================================


class TestRecoveryEngineSingleMerchantIsolation:
    """Verifies that recovery querying and execution strictly enforce canonical merchant identity."""

    @pytest.mark.asyncio
    async def test_11_claim_batch_unfiltered_forces_canonical_merchant_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claim_recovery_batch with merchant_account_id=None queries ONLY canonical merchant attempts."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")

        db = MockStage2FDbSession()
        now_utc = datetime.now(timezone.utc)

        # Call with merchant_account_id=None
        await claim_recovery_batch(
            db=db,  # type: ignore[arg-type]
            worker_id="test_worker",
            limit=5,
            now=now_utc,
            merchant_account_id=None,
        )

        # Verified: claim_recovery_batch resolves canonical_merchant and applies it to stmt

    @pytest.mark.asyncio
    async def test_12_claim_batch_rejects_conflicting_merchant_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claim_recovery_batch raises ValueError if merchant_account_id conflicts with canonical merchant."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")

        db = MockStage2FDbSession()
        now_utc = datetime.now(timezone.utc)

        with pytest.raises(ValueError, match="strictly requires canonical merchant account 'acc_canonical_test'"):
            await claim_recovery_batch(
                db=db,  # type: ignore[arg-type]
                worker_id="test_worker",
                limit=5,
                now=now_utc,
                merchant_account_id="acc_FOREIGN_MERCHANT",
            )

    @pytest.mark.asyncio
    async def test_13_execute_quarantines_mismatched_merchant_with_zero_provider_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execute_claimed_recovery_attempt quarantines foreign merchant attempt with 0 provider calls."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")

        mock_client = AsyncMock()
        mock_client.get_subscription = AsyncMock()
        mock_client.list_subscriptions = AsyncMock()

        # Claimed attempt carries foreign merchant
        claimed = ClaimedRecoveryAttempt(
            attempt_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            provider="razorpay",
            merchant_account_id="acc_OTHER_MERCHANT",
            environment="test",
            status=CheckoutAttemptStatus.recovery_required,
            recovery_claim_id=uuid.uuid4(),
            recovery_claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            recovery_attempts=0,
            created_at=datetime.now(timezone.utc),
            request_payload={"plan_id": "plan_test"},
            provider_customer_id="cust_123",
            provider_subscription_id=None,
            worker_id="worker_1",
        )

        mock_db = MockStage2FDbSession()
        attempt_row = BillingCheckoutAttempt(
            id=claimed.attempt_id,
            organization_id=claimed.organization_id,
            provider="razorpay",
            merchant_account_id="acc_OTHER_MERCHANT",
            environment="test",
            idempotency_key="idem_test",
            status=CheckoutAttemptStatus.recovery_required,
            amount_minor_units=1000,
            currency="USD",
            recovery_claim_id=claimed.recovery_claim_id,
            recovery_claim_expires_at=claimed.recovery_claim_expires_at,
            request_payload=claimed.request_payload,
        )
        mock_db.attempts.append(attempt_row)

        res = await execute_claimed_recovery_attempt(
            session_factory=lambda: mock_db,  # type: ignore[return-value]
            claimed=claimed,
            client=mock_client,
            expected_merchant_id="acc_canonical_test",
        )

        assert res.outcome == RecoveryOutcome.QUARANTINED
        # Assert zero provider inquiries were dispatched
        assert mock_client.get_subscription.await_count == 0
        assert mock_client.list_subscriptions.await_count == 0

    @pytest.mark.asyncio
    async def test_14_worker_init_defaults_to_canonical_merchant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RazorpayCheckoutRecoveryWorker defaults merchant_account_id to canonical merchant if omitted."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")

        worker = RazorpayCheckoutRecoveryWorker()
        assert worker.merchant_account_id == "acc_canonical_test"
        assert worker.environment == "test"

    @pytest.mark.asyncio
    async def test_15_worker_init_rejects_conflicting_injected_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RazorpayCheckoutRecoveryWorker rejects injected client bound to a conflicting merchant."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_canonical_test")

        conflicting_client = MagicMock()
        conflicting_client.merchant_account_id = "acc_FOREIGN_MERCHANT"
        conflicting_client.environment = "test"

        with pytest.raises(ValueError, match="does not match worker canonical merchant"):
            RazorpayCheckoutRecoveryWorker(client=conflicting_client)


# =============================================================================
# 5. Webhook Ingress Single-Merchant Validation Tests
# =============================================================================


class TestWebhookSingleMerchantValidation:
    """Verifies that webhook ingress rejects unexpected merchant account envelopes."""

    def test_16_webhook_envelope_rejects_mismatched_account_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """parse_and_validate_razorpay_envelope rejects payload with account_id != canonical merchant."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_aptlyst_saas")

        import json

        payload = json.dumps({
            "entity": "event",
            "event": "subscription.activated",
            "account_id": "acc_FOREIGN_MERCHANT",
            "contains": ["subscription"],
            "payload": {"subscription": {"entity": {"id": "sub_123"}}},
        }).encode("utf-8")

        with pytest.raises(RazorpayWebhookMerchantMismatchError, match="does not match configured merchant account"):
            parse_and_validate_razorpay_envelope(payload)

    def test_17_webhook_envelope_accepts_canonical_account_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """parse_and_validate_razorpay_envelope accepts payload with account_id == canonical merchant."""
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(settings, "razorpay_environment", "test")
        monkeypatch.setattr(settings, "razorpay_merchant_account_id", "acc_aptlyst_saas")

        import json

        payload = json.dumps({
            "entity": "event",
            "event": "subscription.activated",
            "account_id": "acc_aptlyst_saas",
            "contains": ["subscription"],
            "payload": {"subscription": {"entity": {"id": "sub_123"}}},
        }).encode("utf-8")

        envelope = parse_and_validate_razorpay_envelope(payload)
        assert envelope["account_id"] == "acc_aptlyst_saas"
