"""Unit and CLI tests for Stage 2K-B Operator Reconciliation Tooling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import (
    BillingReconciliation,
    ReconciliationStatus,
    WebhookProcessingStatus,
)
from app.services.billing.reconciliation_ops import (
    ConcurrentReconciliationConflictError,
    EnvironmentIsolationError,
    InvalidOperatorInputError,
    InvalidReconciliationTransitionError,
    ReconciliationNotFoundError,
    ReconciliationScopeMismatchError,
    ReconciliationTenantMismatchError,
    inspect_exhausted_recovery_attempt,
    inspect_payment_reconciliation,
    inspect_quarantined_webhook,
    list_exhausted_recovery_attempts,
    list_legacy_stripe_reconciliations,
    list_payment_reconciliations,
    list_quarantined_webhooks,
    resolve_payment_reconciliation,
    validate_operator_environment,
    validate_operator_uuid,
    validate_pagination,
    validate_resolution_notes,
)
from app.workers.billing_reconciliation import build_parser, execute_reconciliation_cli


class _FakeScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalar_one(self) -> object:
        return self._rows[0]

    def scalar_one_or_none(self) -> object | None:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> object | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> "_FakeScalarResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class _SessionFactory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _make_payment_reconciliation(
    *,
    case_id: uuid.UUID | None = None,
    environment: str = "test",
    merchant_account_id: str = "acc_test_1",
    organization_id: uuid.UUID | None = None,
    status: ReconciliationStatus = ReconciliationStatus.pending_review,
    reason: str = "unbound_provider_customer",
    resolved_by: uuid.UUID | None = None,
    resolved_at: datetime | None = None,
    resolution_notes: str | None = None,
    diagnostic_payload: dict | None = None,
) -> PaymentReconciliation:
    now = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
    return PaymentReconciliation(
        id=case_id or uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=merchant_account_id,
        environment=environment,
        case_key=f"case:{uuid.uuid4().hex[:8]}",
        organization_id=organization_id,
        subscription_id=None,
        invoice_id=None,
        payment_id=None,
        raw_event_id=None,
        reason=reason,
        status=status,
        diagnostic_payload=diagnostic_payload
        if diagnostic_payload is not None
        else {
            "customer_id": "cust_123",
            "customer_email": "secret-pii@example.com",
            "razorpay_key_secret": "should-never-appear",
            "nested": {"raw_dsn": "postgresql://postgres:supersecret@db.internal:5432/app"},
            "history": [{"reason": reason, "timestamp": now.isoformat()}],
        },
        resolved_by=resolved_by,
        resolved_at=resolved_at,
        resolution_notes=resolution_notes,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
    )


# =============================================================================
# 1. Input & Environment Isolation Validation
# =============================================================================


def test_environment_isolation_fails_closed() -> None:
    assert validate_operator_environment("test", deployment_environment="staging") == "test"
    assert validate_operator_environment("live", deployment_environment="production") == "live"

    with pytest.raises(EnvironmentIsolationError, match="Explicit environment"):
        validate_operator_environment(None, deployment_environment="staging", require_explicit=True)

    with pytest.raises(EnvironmentIsolationError, match="non-production"):
        validate_operator_environment("live", deployment_environment="staging")

    with pytest.raises(EnvironmentIsolationError, match="production"):
        validate_operator_environment("test", deployment_environment="production")

    with pytest.raises(EnvironmentIsolationError, match="Unsupported"):
        validate_operator_environment("sandbox", deployment_environment="staging")

    with pytest.raises(EnvironmentIsolationError, match="ENVIRONMENT"):
        validate_operator_environment("test", deployment_environment="")


def test_operator_uuid_and_notes_and_pagination_validation() -> None:
    op_uuid = uuid.uuid4()
    assert validate_operator_uuid(str(op_uuid)) == op_uuid
    assert validate_operator_uuid(op_uuid) == op_uuid

    with pytest.raises(InvalidOperatorInputError):
        validate_operator_uuid("")
    with pytest.raises(InvalidOperatorInputError):
        validate_operator_uuid("alice@aptlyst.ai")
    with pytest.raises(InvalidOperatorInputError):
        validate_operator_uuid("00000000-0000-0000-0000-000000000000")

    with pytest.raises(InvalidOperatorInputError):
        validate_resolution_notes("")
    with pytest.raises(InvalidOperatorInputError):
        validate_resolution_notes("   ok ")
    sanitized_notes = validate_resolution_notes(
        "Verified payment rzp_live_abc123def456 settlement mismatch offline"
    )
    assert "rzp_live_abc123def456" not in sanitized_notes
    assert "[REDACTED_KEY]" in sanitized_notes

    assert validate_pagination(10, 0) == (10, 0)
    with pytest.raises(InvalidOperatorInputError):
        validate_pagination(0, 0)
    with pytest.raises(InvalidOperatorInputError):
        validate_pagination(101, 0)
    with pytest.raises(InvalidOperatorInputError):
        validate_pagination(10, -1)


# =============================================================================
# 2. Read-Only Listing, Inspection & Diagnostic Sanitization
# =============================================================================


@pytest.mark.asyncio
async def test_list_payment_reconciliations_pagination_and_filters() -> None:
    org_id = uuid.uuid4()
    rec1 = _make_payment_reconciliation(organization_id=org_id, environment="test")
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([3]),
            _FakeScalarResult([rec1]),
        ]
    )

    page = await list_payment_reconciliations(
        db,
        environment="test",
        deployment_environment="staging",
        status=ReconciliationStatus.pending_review,
        reason="unbound_provider_customer",
        min_age_seconds=300,
        organization_id=org_id,
        limit=1,
        offset=2,
    )

    assert page.total_count == 3
    assert page.limit == 1
    assert page.offset == 2
    assert len(page.items) == 1
    assert page.items[0].id == rec1.id
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_inspect_payment_reconciliation_sanitizes_diagnostics_and_enforces_scope() -> None:
    org_id = uuid.uuid4()
    other_org = uuid.uuid4()
    rec = _make_payment_reconciliation(organization_id=org_id, environment="test")

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([rec]),
            _FakeScalarResult(["Acme Org"]),
        ]
    )

    detail = await inspect_payment_reconciliation(
        db,
        case_id=rec.id,
        environment="test",
        deployment_environment="staging",
        organization_id=org_id,
    )
    assert detail.organization_name == "Acme Org"
    assert "razorpay_key_secret" not in detail.diagnostic_payload
    assert "customer_email" not in detail.diagnostic_payload
    assert "secret-pii@example.com" not in json.dumps(detail.diagnostic_payload)
    assert "supersecret" not in json.dumps(detail.diagnostic_payload)
    assert detail.diagnostic_payload["customer_id"] == "cust_123"

    # Cross-organization access blocked
    db_mismatch = AsyncMock()
    db_mismatch.execute = AsyncMock(return_value=_FakeScalarResult([rec]))
    with pytest.raises(ReconciliationTenantMismatchError):
        await inspect_payment_reconciliation(
            db_mismatch,
            case_id=rec.id,
            environment="test",
            deployment_environment="staging",
            organization_id=other_org,
        )

    # Cross-environment record mismatch blocked (non-prod cannot inspect live row even if present)
    live_rec = _make_payment_reconciliation(environment="live")
    db_env = AsyncMock()
    db_env.execute = AsyncMock(return_value=_FakeScalarResult([live_rec]))
    with pytest.raises(EnvironmentIsolationError):
        await inspect_payment_reconciliation(
            db_env,
            case_id=live_rec.id,
            environment="test",
            deployment_environment="staging",
        )


# =============================================================================
# 3. Audited Resolution, Idempotency, Conflicts & Invalid Transitions
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_payment_reconciliation_success_idempotency_and_conflicts() -> None:
    org_id = uuid.uuid4()
    op1 = uuid.uuid4()
    op2 = uuid.uuid4()
    rec = _make_payment_reconciliation(organization_id=org_id, environment="test")
    fixed_now = datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([(rec.id, org_id)]),
            _FakeScalarResult([rec]),
        ]
    )

    with patch(
        "app.services.billing.reconciliation_ops.acquire_advisory_lock",
        new=AsyncMock(),
    ) as mock_lock:
        result = await resolve_payment_reconciliation(
            db,
            case_id=rec.id,
            environment="test",
            deployment_environment="staging",
            target_status=ReconciliationStatus.resolved_refunded,
            operator_id=op1,
            resolution_notes="Verified external refund completed on gateway",
            organization_id=org_id,
            now=fixed_now,
        )

    mock_lock.assert_awaited_once_with(db, "organization", org_id)
    assert result.changed is True
    assert result.idempotent_replay is False
    assert result.case.status == ReconciliationStatus.resolved_refunded
    assert rec.resolved_by == op1
    assert rec.resolved_at == fixed_now
    assert "no Razorpay refund was initiated or independently verified" in result.notice
    assert rec.diagnostic_payload["history"][-1]["action"] == "operator_resolution"
    db.commit.assert_awaited_once()

    # Exact replay by same operator -> idempotent no-op
    db_replay = AsyncMock()
    db_replay.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([(rec.id, org_id)]),
            _FakeScalarResult([rec]),
        ]
    )
    with patch("app.services.billing.reconciliation_ops.acquire_advisory_lock", new=AsyncMock()):
        replay = await resolve_payment_reconciliation(
            db_replay,
            case_id=rec.id,
            environment="test",
            deployment_environment="staging",
            target_status=ReconciliationStatus.resolved_refunded,
            operator_id=op1,
            resolution_notes="Verified external refund completed on gateway",
            now=fixed_now + timedelta(minutes=5),
        )
    assert replay.changed is False
    assert replay.idempotent_replay is True
    assert rec.resolved_at == fixed_now
    db_replay.commit.assert_not_awaited()
    db_replay.rollback.assert_awaited()

    # Different operator on already-resolved status -> concurrent conflict
    db_competing = AsyncMock()
    db_competing.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([(rec.id, org_id)]),
            _FakeScalarResult([rec]),
        ]
    )
    with patch("app.services.billing.reconciliation_ops.acquire_advisory_lock", new=AsyncMock()):
        with pytest.raises(ConcurrentReconciliationConflictError):
            await resolve_payment_reconciliation(
                db_competing,
                case_id=rec.id,
                environment="test",
                deployment_environment="staging",
                target_status=ReconciliationStatus.resolved_refunded,
                operator_id=op2,
                resolution_notes="Competing resolution attempt",
            )
    db_competing.rollback.assert_awaited()

    # Conflicting terminal transition resolved_refunded -> resolved_ignored rejected
    db_flip = AsyncMock()
    db_flip.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([(rec.id, org_id)]),
            _FakeScalarResult([rec]),
        ]
    )
    with patch("app.services.billing.reconciliation_ops.acquire_advisory_lock", new=AsyncMock()):
        with pytest.raises(InvalidReconciliationTransitionError):
            await resolve_payment_reconciliation(
                db_flip,
                case_id=rec.id,
                environment="test",
                deployment_environment="staging",
                target_status=ReconciliationStatus.resolved_ignored,
                operator_id=op1,
                resolution_notes="Attempting invalid terminal flip",
            )

    # Disallow resolving back to pending_review
    db_pending = AsyncMock()
    with pytest.raises(InvalidReconciliationTransitionError):
        await resolve_payment_reconciliation(
            db_pending,
            case_id=rec.id,
            environment="test",
            deployment_environment="staging",
            target_status=ReconciliationStatus.pending_review,
            operator_id=op1,
            resolution_notes="Reopening not permitted via operator resolve",
        )


# =============================================================================
# 4. Read-Only Quarantined Webhooks, Exhausted Recoveries & Legacy Inspection
# =============================================================================


@pytest.mark.asyncio
async def test_quarantined_webhooks_and_exhausted_recoveries_are_read_only_and_sanitized() -> None:
    now = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
    wh = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_test_1",
        environment="test",
        provider_event_id="evt_q_1",
        event_type="subscription.activated",
        status=WebhookProcessingStatus.quarantined,
        attempts=5,
        payload={
            "entity": "event",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_q_1",
                        "customer_id": "cust_q_1",
                        "notes": {"customer_email": "hidden@example.com"},
                    }
                }
            },
        },
        signature_verified=True,
        error_message="Connection failed postgresql://user:secret@localhost:5432/db",
        received_at=now - timedelta(hours=1),
        processed_at=now - timedelta(minutes=30),
        quarantined_at=now - timedelta(minutes=30),
        quarantine_reason="Exceeded max attempts (5/5)",
    )

    db_wh = AsyncMock()
    db_wh.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([1]),
            _FakeScalarResult([wh]),
        ]
    )
    wh_page = await list_quarantined_webhooks(
        db_wh, environment="test", deployment_environment="staging"
    )
    assert wh_page.redrive_supported is False
    assert wh_page.total_count == 1
    assert wh_page.items[0].entity_refs == {
        "subscription_id": "sub_q_1",
        "customer_id": "cust_q_1",
    }
    assert "hidden@example.com" not in json.dumps(wh_page.to_dict())
    assert "secret" not in (wh_page.items[0].error_message or "")

    org_id = uuid.uuid4()
    att = BillingCheckoutAttempt(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="razorpay",
        merchant_account_id="acc_test_1",
        environment="test",
        provider_customer_id="cust_rec_1",
        provider_subscription_id=None,
        idempotency_key="idem_1",
        status=CheckoutAttemptStatus.recovery_required,
        amount_minor_units=2900,
        currency="USD",
        request_payload={
            "transient_failures": 10,
            "expired_lease_count": 2,
            "razorpay_key_secret": "do-not-leak",
        },
        attempt_lease_expires_at=now - timedelta(hours=1),
        next_recovery_retry_at=None,
        recovery_attempts=5,
        error_message="recovery_budget_exhausted: rzp_test_secret123456",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=10),
    )
    linked_rec = _make_payment_reconciliation(
        organization_id=org_id,
        environment="test",
        reason="recovery_budget_exhausted",
    )
    linked_rec.case_key = f"recovery_budget_exhausted:{att.id}"

    db_rec = AsyncMock()
    db_rec.execute = AsyncMock(
        side_effect=[
            _FakeScalarResult([1]),
            _FakeScalarResult([att]),
            _FakeScalarResult([linked_rec]),
        ]
    )
    rec_page = await list_exhausted_recovery_attempts(
        db_rec, environment="test", deployment_environment="staging"
    )
    assert rec_page.retry_supported is False
    assert rec_page.total_count == 1
    assert rec_page.items[0].linked_reconciliation_id == linked_rec.id
    assert rec_page.items[0].transient_failures == 10
    assert rec_page.items[0].expired_lease_count == 2
    assert "do-not-leak" not in json.dumps(rec_page.to_dict())
    assert "rzp_test_secret123456" not in (rec_page.items[0].error_message or "")


@pytest.mark.asyncio
async def test_legacy_stripe_reconciliation_list_is_read_only() -> None:
    row = BillingReconciliation(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        event_id="evt_stripe_1",
        existing_subscription_id="sub_old",
        incoming_subscription_id="sub_new",
        reason="duplicate_active_subscription",
        status=ReconciliationStatus.pending_review,
        diagnostic_payload={"customer_email": "stripe@example.com", "note": "contact stripe@example.com"},
        created_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_FakeScalarResult([1]), _FakeScalarResult([row])])
    res = await list_legacy_stripe_reconciliations(db)
    assert res.total_count == 1
    assert "customer_email" not in res.items[0].diagnostic_payload
    assert "stripe@example.com" not in json.dumps(res.items[0].diagnostic_payload)
    assert res.items[0].diagnostic_payload["note"] == "contact [REDACTED_EMAIL]"
    assert res.items[0].to_dict()["environment"] is None


# =============================================================================
# 5. CLI Interface & Minimum Credential Scoping
# =============================================================================


@pytest.mark.asyncio
async def test_cli_requires_confirm_and_works_without_razorpay_api_secrets() -> None:
    parser = build_parser()
    case_id = str(uuid.uuid4())
    op_id = str(uuid.uuid4())

    # Without --confirm, CLI fails closed (exit code 2) without touching DB
    args_no_confirm = parser.parse_args(
        [
            "resolve",
            "--case-id",
            case_id,
            "--environment",
            "test",
            "--status",
            "resolved_ignored",
            "--operator-id",
            op_id,
            "--notes",
            "Verified duplicate conflict",
            "--json",
        ]
    )
    db_unused = AsyncMock()
    exit_code, rendered = await execute_reconciliation_cli(
        args_no_confirm,
        session_factory=_SessionFactory(db_unused),  # type: ignore[arg-type]
    )
    assert exit_code == 2
    assert "--confirm" in rendered
    db_unused.execute.assert_not_awaited()

    # Valid list command executes without needing Razorpay API/webhook secrets
    args_list = parser.parse_args(["list", "--environment", "test", "--json"])
    db_list = AsyncMock()
    db_list.execute = AsyncMock(side_effect=[_FakeScalarResult([0]), _FakeScalarResult([])])
    exit_code_ok, rendered_ok = await execute_reconciliation_cli(
        args_list,
        session_factory=_SessionFactory(db_list),  # type: ignore[arg-type]
    )
    assert exit_code_ok == 0
    parsed = json.loads(rendered_ok)
    assert parsed["environment"] == "test"
    assert parsed["total_count"] == 0
