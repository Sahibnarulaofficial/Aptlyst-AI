"""PostgreSQL 16 integration tests for Stage 2K-B operator reconciliation tooling.

Validates against real PostgreSQL 16 (never SQLite):
- Paginated listing & age/reason/status filtering on PaymentReconciliation.
- Audited resolution transitions under ADR-005 advisory lock + SELECT ... FOR UPDATE.
- Concurrent racing resolution attempts (one succeeds; competing attempt fails closed).
- Idempotent replay by the same operator with preserved resolved_at & notes.
- Tenant and environment isolation enforcement.
- Read-only inspection of quarantined webhooks and exhausted checkout recovery attempts.

Safety:
- Uses tests.postgres_test_guard (never reads `.env` or production DATABASE_URL).
- Skips locally only when REQUIRE_POSTGRES_TESTS is unset and POSTGRES_TEST_URL is unavailable.
- Fails closed when REQUIRE_POSTGRES_TESTS=1.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import uuid

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import register_all_models
from app.models.organization import Organization
from app.models.payment import (
    BillingCheckoutAttempt,
    BillingWebhookEvent,
    CheckoutAttemptStatus,
    PaymentReconciliation,
)
from app.models.subscription import ReconciliationStatus, WebhookProcessingStatus
from app.services.billing.reconciliation_ops import (
    ConcurrentReconciliationConflictError,
    EnvironmentIsolationError,
    InvalidReconciliationTransitionError,
    ReconciliationTenantMismatchError,
    inspect_payment_reconciliation,
    list_exhausted_recovery_attempts,
    list_payment_reconciliations,
    list_quarantined_webhooks,
    resolve_payment_reconciliation,
)
from tests.postgres_test_guard import (
    PostgresTestConfigError,
    _OptionalPostgresUnreachableError,
    is_postgres_tests_required,
    resolve_verified_pg_engine,
)

register_all_models()

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
REQUIRE_POSTGRES_TESTS = is_postgres_tests_required()
_SKIP_IF_OPTIONAL_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_URL and not REQUIRE_POSTGRES_TESTS,
    reason="Disposable PostgreSQL 16 environment required",
)


def _to_asyncpg_url(raw_url: str) -> str:
    trimmed = raw_url.strip()
    scheme, rest = trimmed.split("://", 1)
    if scheme.lower() in ("postgresql", "postgres", "postgresql+psycopg2", "postgresql+psycopg"):
        return f"postgresql+asyncpg://{rest}"
    return trimmed


@pytest.fixture(scope="module")
def pg16_urls():
    """Verify PostgreSQL 16 isolation and return (sync_engine, async_url)."""
    try:
        engine, _summary, _required = resolve_verified_pg_engine()
    except _OptionalPostgresUnreachableError as exc:
        pytest.skip(str(exc))
    except PostgresTestConfigError as exc:
        pytest.fail(str(exc), pytrace=False)

    if engine is None or not POSTGRES_TEST_URL:
        pytest.skip("Disposable PostgreSQL 16 database not available.")

    async_url = _to_asyncpg_url(POSTGRES_TEST_URL)
    try:
        yield engine, async_url
    finally:
        engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2kb_01_list_inspect_and_isolation(pg16_urls) -> None:
    """Verify real PG16 pagination, filtering, sanitized inspection, and tenant/env isolation."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    rec_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as db:
            db.add(Organization(id=org_id, name="PG16 Ops Org", slug=f"pg16-ops-{uuid.uuid4().hex[:8]}"))
            db.add(
                PaymentReconciliation(
                    id=rec_ids[0],
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    case_key=f"pg16:case0:{rec_ids[0].hex[:8]}",
                    organization_id=org_id,
                    reason="unbound_provider_customer",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={
                        "customer_email": "leak@example.com",
                        "summary": "contact leak@example.com",
                        "api_secret": "rzp_test_topsecret123",
                        "subscription_id": "sub_pg16_1",
                    },
                    created_at=now - timedelta(hours=3),
                    updated_at=now - timedelta(hours=3),
                )
            )
            db.add(
                PaymentReconciliation(
                    id=rec_ids[1],
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    case_key=f"pg16:case1:{rec_ids[1].hex[:8]}",
                    organization_id=org_id,
                    reason="metadata_tenant_mismatch",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={"customer_id": "cust_2"},
                    created_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=10),
                )
            )
            db.add(
                PaymentReconciliation(
                    id=rec_ids[2],
                    provider="razorpay",
                    merchant_account_id="acc_pg16_live",
                    environment="live",
                    case_key=f"pg16:case2:{rec_ids[2].hex[:8]}",
                    organization_id=org_id,
                    reason="unbound_provider_customer",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={"customer_id": "cust_live"},
                    created_at=now - timedelta(hours=4),
                    updated_at=now - timedelta(hours=4),
                )
            )
            await db.commit()

        async with session_factory() as db:
            page = await list_payment_reconciliations(
                db,
                environment="test",
                deployment_environment="staging",
                organization_id=org_id,
                min_age_seconds=3600,
                limit=10,
                offset=0,
                now=now,
            )
            assert page.total_count == 1
            assert page.items[0].id == rec_ids[0]

            detail = await inspect_payment_reconciliation(
                db,
                case_id=rec_ids[0],
                environment="test",
                deployment_environment="staging",
                organization_id=org_id,
            )
            assert detail.organization_name == "PG16 Ops Org"
            assert "customer_email" not in detail.diagnostic_payload
            assert detail.diagnostic_payload.get("summary") == "contact [REDACTED_EMAIL]"
            assert "api_secret" not in detail.diagnostic_payload

            with pytest.raises(ReconciliationTenantMismatchError):
                await inspect_payment_reconciliation(
                    db,
                    case_id=rec_ids[0],
                    environment="test",
                    deployment_environment="staging",
                    organization_id=other_org_id,
                )

            with pytest.raises(EnvironmentIsolationError):
                await inspect_payment_reconciliation(
                    db,
                    case_id=rec_ids[2],
                    environment="test",
                    deployment_environment="staging",
                )
    finally:
        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM payment_reconciliations WHERE id = ANY(:ids)"),
                {"ids": rec_ids},
            )
            await db.execute(text("DELETE FROM organizations WHERE id = :oid"), {"oid": org_id})
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2kb_02_concurrent_resolution_and_idempotency(pg16_urls) -> None:
    """Verify real PG16 row lock / advisory lock prevents concurrent conflicting resolutions."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    org_id = uuid.uuid4()
    case_id = uuid.uuid4()
    op1 = uuid.uuid4()
    op2 = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as db:
            db.add(Organization(id=org_id, name="PG16 Lock Org", slug=f"pg16-lck-{uuid.uuid4().hex[:8]}"))
            db.add(
                PaymentReconciliation(
                    id=case_id,
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    case_key=f"pg16:concurrent:{case_id.hex[:8]}",
                    organization_id=org_id,
                    reason="recovery_budget_exhausted",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={"attempt_id": str(uuid.uuid4())},
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()

        async def _attempt_resolve(
            target_status: ReconciliationStatus,
            operator_id: uuid.UUID,
            notes: str,
        ) -> str:
            async with session_factory() as db:
                try:
                    res = await resolve_payment_reconciliation(
                        db,
                        case_id=case_id,
                        environment="test",
                        deployment_environment="staging",
                        target_status=target_status,
                        operator_id=operator_id,
                        resolution_notes=notes,
                        organization_id=org_id,
                        now=now,
                    )
                    return f"ok:{res.case.status.value}"
                except (ConcurrentReconciliationConflictError, InvalidReconciliationTransitionError) as exc:
                    return f"rejected:{type(exc).__name__}"

        outcomes = await asyncio.gather(
            _attempt_resolve(
                ReconciliationStatus.resolved_ignored,
                op1,
                "Confirmed checkout never charged customer",
            ),
            _attempt_resolve(
                ReconciliationStatus.resolved_refunded,
                op2,
                "Conflicting concurrent refund determination",
            ),
        )
        ok_count = sum(1 for item in outcomes if item.startswith("ok:"))
        rejected_count = sum(1 for item in outcomes if item.startswith("rejected:"))
        assert ok_count == 1
        assert rejected_count == 1

        # Verify winning row and test idempotent replay
        async with session_factory() as db:
            persisted = await db.get(PaymentReconciliation, case_id)
            assert persisted is not None
            winner_status = persisted.status
            winner_op = persisted.resolved_by
            winner_notes = persisted.resolution_notes
            winner_at = persisted.resolved_at
            assert winner_op in (op1, op2)
            assert winner_notes is not None

            replay = await resolve_payment_reconciliation(
                db,
                case_id=case_id,
                environment="test",
                deployment_environment="staging",
                target_status=winner_status,
                operator_id=winner_op,  # type: ignore[arg-type]
                resolution_notes=winner_notes,
                now=now + timedelta(minutes=10),
            )
            assert replay.changed is False
            assert replay.idempotent_replay is True
            assert replay.case.resolved_at == winner_at
    finally:
        async with session_factory() as db:
            await db.execute(text("DELETE FROM payment_reconciliations WHERE id = :cid"), {"cid": case_id})
            await db.execute(text("DELETE FROM organizations WHERE id = :oid"), {"oid": org_id})
            await db.commit()
        await async_engine.dispose()


@_SKIP_IF_OPTIONAL_POSTGRES
@pytest.mark.asyncio
async def test_pg16_stage2kb_03_read_only_quarantined_webhooks_and_exhausted_recoveries(pg16_urls) -> None:
    """Verify PG16 read-only inspection of quarantined webhooks and exhausted recovery attempts."""
    _, async_url = pg16_urls
    async_engine = create_async_engine(async_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    org_id = uuid.uuid4()
    wh_id = uuid.uuid4()
    att_id = uuid.uuid4()
    rec_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as db:
            db.add(Organization(id=org_id, name="PG16 Inspect Org", slug=f"pg16-ins-{uuid.uuid4().hex[:8]}"))
            db.add(
                BillingWebhookEvent(
                    id=wh_id,
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    provider_event_id=f"evt_pg16_{wh_id.hex[:8]}",
                    event_type="subscription.activated",
                    status=WebhookProcessingStatus.quarantined,
                    attempts=5,
                    payload={"entity": "event", "payload": {"subscription": {"entity": {"id": "sub_pg16_wh"}}}},
                    signature_verified=True,
                    error_message="Simulated transient exhaustion",
                    received_at=now - timedelta(minutes=30),
                    quarantined_at=now - timedelta(minutes=5),
                    quarantine_reason="Exceeded max attempts",
                )
            )
            db.add(
                BillingCheckoutAttempt(
                    id=att_id,
                    organization_id=org_id,
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    idempotency_key=f"idem_{att_id.hex[:8]}",
                    status=CheckoutAttemptStatus.recovery_required,
                    amount_minor_units=2900,
                    currency="USD",
                    request_payload={"transient_failures": 10, "expired_lease_count": 1},
                    attempt_lease_expires_at=now - timedelta(hours=1),
                    next_recovery_retry_at=None,
                    recovery_attempts=5,
                    error_message="recovery_budget_exhausted",
                    created_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(minutes=15),
                )
            )
            db.add(
                PaymentReconciliation(
                    id=rec_id,
                    provider="razorpay",
                    merchant_account_id="acc_pg16_test",
                    environment="test",
                    case_key=f"recovery_budget_exhausted:{att_id}",
                    organization_id=org_id,
                    reason="recovery_budget_exhausted",
                    status=ReconciliationStatus.pending_review,
                    diagnostic_payload={"attempt_id": str(att_id)},
                    created_at=now - timedelta(minutes=15),
                    updated_at=now - timedelta(minutes=15),
                )
            )
            await db.commit()

        async with session_factory() as db:
            wh_page = await list_quarantined_webhooks(
                db,
                environment="test",
                deployment_environment="staging",
                merchant_account_id="acc_pg16_test",
            )
            assert wh_page.redrive_supported is False
            assert any(item.id == wh_id for item in wh_page.items)

            rec_page = await list_exhausted_recovery_attempts(
                db,
                environment="test",
                deployment_environment="staging",
                organization_id=org_id,
            )
            assert rec_page.retry_supported is False
            assert len(rec_page.items) == 1
            assert rec_page.items[0].id == att_id
            assert rec_page.items[0].linked_reconciliation_id == rec_id
    finally:
        async with session_factory() as db:
            await db.execute(text("DELETE FROM payment_reconciliations WHERE id = :rid"), {"rid": rec_id})
            await db.execute(text("DELETE FROM billing_checkout_attempts WHERE id = :aid"), {"aid": att_id})
            await db.execute(text("DELETE FROM billing_webhook_events WHERE id = :wid"), {"wid": wh_id})
            await db.execute(text("DELETE FROM organizations WHERE id = :oid"), {"oid": org_id})
            await db.commit()
        await async_engine.dispose()
