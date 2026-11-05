"""Comprehensive security and durability tests for Stripe webhook ingestion.

Endpoint: POST /api/v1/billing/webhook

Covers:
1. Valid signed event is durably stored with status 'pending' (200).
2. Missing Stripe-Signature header is rejected (400).
3. Invalid Stripe signature hash is rejected (400).
4. Expired signature (timestamp > 300s old) is rejected (400).
5. Oversized payload (> 64 KB) is rejected before verification (413).
6. Malformed JSON payload is rejected (400).
7. Missing 'id' or 'type' in verified event is rejected (400).
8. Duplicate event delivery does not create another row (idempotent 200).
9. Duplicate delivery preserves existing processing state and attempt counts.
10. Concurrent duplicate delivery race condition is handled safely (200).
11. Database persistence failure triggers rollback and returns HTTP 500.
12. Event is not acknowledged before transaction commit succeeds.
13. Stripe-disabled and unconfigured webhook secret fail safely (503).
14. Existing authenticated billing endpoints remain protected (401).
15. Webhook secret, signature, and raw payload are never leaked into logs or responses.
"""

import sys

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest
from sqlalchemy.exc import IntegrityError

import app.models.billing  # noqa: F401
import app.models.chatbot  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.generation  # noqa: F401
import app.models.generation_attempt  # noqa: F401
import app.models.organization  # noqa: F401
from app.config import Settings
from app.db.session import get_db
from app.main import app
from app.models.subscription import StripeWebhookEvent, WebhookProcessingStatus
from app.services.billing.stripe_webhook import (
    MAX_WEBHOOK_PAYLOAD_BYTES,
    ingest_stripe_webhook_event,
    read_bounded_request_body,
    verify_stripe_webhook_signature,
)
import stripe

TEST_WEBHOOK_SECRET = "whsec_test_secret_for_unit_testing_12345"


# =============================================================================
# Test Doubles & Fixtures
# =============================================================================


class InMemoryWebhookDbSession:
    """In-memory AsyncSession test double for webhook ingestion tests."""

    def __init__(self, initial_events: list[StripeWebhookEvent] | None = None) -> None:
        self.events: dict[str, StripeWebhookEvent] = {
            e.id: e for e in (initial_events or [])
        }
        self._pending_events: dict[str, StripeWebhookEvent] = {}
        self.added: list[Any] = []
        self.committed_count = 0
        self.rolled_back_count = 0
        self.fail_commit_with: Exception | None = None
        self.fail_commit_once_with: Exception | None = None

    async def get(self, entity_cls: Any, pk: Any) -> Any:
        if entity_cls is StripeWebhookEvent:
            # First check pending additions in current transaction, then committed events
            return self._pending_events.get(pk) or self.events.get(pk)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, StripeWebhookEvent):
            self._pending_events[obj.id] = obj

    async def commit(self) -> None:
        if self.fail_commit_with:
            if callable(self.fail_commit_with):
                self.fail_commit_with()
            else:
                raise self.fail_commit_with
        if self.fail_commit_once_with:
            err = self.fail_commit_once_with
            self.fail_commit_once_with = None
            if callable(err):
                err()
            else:
                raise err
        self.events.update(self._pending_events)
        self._pending_events.clear()
        self.committed_count += 1

    async def rollback(self) -> None:
        self._pending_events.clear()
        self.rolled_back_count += 1

    async def refresh(self, obj: Any) -> None:
        pass


def generate_genuine_stripe_signature(
    payload: bytes | str,
    secret: str = TEST_WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> str:
    """Generate a valid Stripe-Signature header using Stripe's genuine WebhookSignature utility."""
    if timestamp is None:
        timestamp = int(time.time())
    payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    return stripe.WebhookSignature.generate_signature_header(
        payload=payload_str,
        secret=secret,
        timestamp=timestamp,
    )


def build_test_event_bytes(
    event_id: str = "evt_test_123",
    event_type: str = "invoice.paid",
    data_object: dict[str, Any] | None = None,
) -> bytes:
    """Construct canonical Stripe event JSON bytes."""
    payload = {
        "id": event_id,
        "object": "event",
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "type": event_type,
        "data": {
            "object": data_object or {"id": "in_test_123", "amount_paid": 5000}
        },
    }
    return json.dumps(payload).encode("utf-8")


def get_test_settings(**kwargs: Any) -> Settings:
    """Generate safe test settings with Stripe enabled."""
    values: dict[str, Any] = {
        "environment": "development",
        "stripe_enabled": True,
        "stripe_secret_key": SecretStr("sk_test_1234567890abcdef"),
        "stripe_webhook_secret": SecretStr(TEST_WEBHOOK_SECRET),
        "stripe_publishable_key": "pk_test_1234567890abcdef",
        "stripe_pro_price_id": "price_1234567890abcdef",
    }
    values.update(kwargs)
    return Settings(**values)


def setup_db_override(db: InMemoryWebhookDbSession) -> None:
    """Register database dependency override for test isolation."""
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db


# =============================================================================
# 1. Successful Ingestion & Durability Tests
# =============================================================================


@pytest.mark.asyncio
async def test_valid_signed_event_is_durably_stored() -> None:
    """Verify a validly signed event is durably committed to the database with status 'pending'."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes(event_id="evt_valid_1", event_type="invoice.paid")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        test_settings = get_test_settings()
        with patch("app.services.billing.stripe_webhook.settings", test_settings), \
             patch("app.services.billing.stripe_client.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["received"] is True
        assert data["event_id"] == "evt_valid_1"
        assert data["status"] == "pending"

        # Verify database durability
        assert db.committed_count == 1
        assert "evt_valid_1" in db.events
        event = db.events["evt_valid_1"]
        assert event.event_type == "invoice.paid"
        assert event.status == WebhookProcessingStatus.pending
        assert event.attempts == 1
        assert event.payload["id"] == "evt_valid_1"
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 2. Cryptographic Signature Verification Tests
# =============================================================================


@pytest.mark.asyncio
async def test_missing_signature_is_rejected() -> None:
    """Verify request without Stripe-Signature header is rejected with HTTP 400."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes()

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "Stripe-Signature" in resp.json()["detail"]
        assert db.committed_count == 0
        assert len(db.added) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected() -> None:
    """Verify request with invalid signature hash is rejected with HTTP 400."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes()
    invalid_sig = f"t={int(time.time())},v1=0000000000000000000000000000000000000000000000000000000000000000"

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": invalid_sig, "content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "Invalid or expired" in resp.json()["detail"]
        assert db.committed_count == 0
        assert len(db.added) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_expired_signature_is_rejected() -> None:
    """Verify request with expired signature timestamp (> 300s old) is rejected with HTTP 400."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes()
    # Expired 600 seconds ago (default Stripe tolerance is 300 seconds)
    expired_timestamp = int(time.time()) - 600
    expired_sig = generate_genuine_stripe_signature(event_bytes, timestamp=expired_timestamp)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": expired_sig, "content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "Invalid or expired" in resp.json()["detail"]
        assert db.committed_count == 0
        assert len(db.added) == 0
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 3. Request Payload Bounding Tests (64 KB Limit)
# =============================================================================


@pytest.mark.asyncio
async def test_oversized_payload_is_rejected_with_413() -> None:
    """Verify requests exceeding 64 KB are rejected with HTTP 413 before signature verification."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    # Create oversized payload: 65,537 bytes (> 64 KB limit of 65,536 bytes)
    oversized_bytes = b"{" + b'"padding":"' + b"A" * (65536) + b'"}'
    sig = generate_genuine_stripe_signature(oversized_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=oversized_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 413
        assert "64 KB" in resp.json()["detail"]
        assert db.committed_count == 0
        assert len(db.added) == 0
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 4. Malformed Verified Payload Tests
# =============================================================================


@pytest.mark.asyncio
async def test_malformed_json_verified_event_is_rejected() -> None:
    """Verify signed payload that is not valid JSON is rejected with HTTP 400."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    invalid_json_bytes = b"not-a-json-payload-at-all"
    sig = generate_genuine_stripe_signature(invalid_json_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=invalid_json_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "Malformed" in resp.json()["detail"]
        assert db.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_verified_event_missing_id_or_type_is_rejected() -> None:
    """Verify signed JSON payload lacking 'id' or 'type' is rejected with HTTP 400."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    # Missing 'type' field
    payload_no_type = json.dumps({"id": "evt_no_type_123", "object": "event"}).encode("utf-8")
    sig = generate_genuine_stripe_signature(payload_no_type)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=payload_no_type,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "missing or invalid" in resp.json()["detail"]
        assert db.committed_count == 0
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 5. Idempotent Deduplication & State Preservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_event_id_does_not_create_another_row() -> None:
    """Verify duplicate delivery of already-persisted event returns 200 without creating new row."""
    event_id = "evt_dup_test_100"
    existing_event = StripeWebhookEvent(
        id=event_id,
        event_type="invoice.paid",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload={"id": event_id, "type": "invoice.paid"},
    )
    db = InMemoryWebhookDbSession(initial_events=[existing_event])
    setup_db_override(db)

    event_bytes = build_test_event_bytes(event_id=event_id, event_type="invoice.paid")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["received"] is True
        assert data["event_id"] == event_id
        assert data["status"] == "duplicate"

        # Verify no additional row was inserted
        assert len(db.added) == 0
        assert len(db.events) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_overwrite_existing_processing_state() -> None:
    """Verify duplicate delivery preserves existing processing status (e.g. 'processed') and attempt count."""
    event_id = "evt_processed_test_200"
    existing_event = StripeWebhookEvent(
        id=event_id,
        event_type="customer.subscription.updated",
        status=WebhookProcessingStatus.processed,
        attempts=3,
        payload={"id": event_id, "type": "customer.subscription.updated"},
    )
    db = InMemoryWebhookDbSession(initial_events=[existing_event])
    setup_db_override(db)

    event_bytes = build_test_event_bytes(event_id=event_id, event_type="customer.subscription.updated")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"

        # Verify state was NOT overwritten to pending
        persisted = db.events[event_id]
        assert persisted.status == WebhookProcessingStatus.processed
        assert persisted.attempts == 3
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_concurrent_duplicate_receipt_is_handled_safely() -> None:
    """Verify race condition where two workers simultaneously ingest the same event ID.

    Worker B encounters an IntegrityError during commit because Worker A committed first.
    Worker B must roll back, reload the row, and return HTTP 200 without raising an error.
    """
    event_id = "evt_race_condition_300"
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    # Simulate Worker A committing between Worker B's check and commit
    committed_by_worker_a = StripeWebhookEvent(
        id=event_id,
        event_type="invoice.paid",
        status=WebhookProcessingStatus.pending,
        attempts=1,
        payload={"id": event_id, "type": "invoice.paid"},
    )

    def trigger_race_condition(*args: Any, **kwargs: Any) -> None:
        # Worker A inserts into the backing store right before Worker B commits
        db.events[event_id] = committed_by_worker_a
        raise IntegrityError("duplicate key value violates unique constraint", {}, Exception())

    db.fail_commit_once_with = trigger_race_condition

    event_bytes = build_test_event_bytes(event_id=event_id, event_type="invoice.paid")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        # Worker B safely returns 200 with duplicate status
        assert resp.status_code == 200
        data = resp.json()
        assert data["received"] is True
        assert data["event_id"] == event_id
        assert data["status"] == "duplicate"
        assert db.rolled_back_count == 1
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 6. Failure Handling & Transaction Invariant Tests
# =============================================================================


@pytest.mark.asyncio
async def test_database_persistence_failure_returns_http_500() -> None:
    """Verify database write failure triggers rollback and returns HTTP 500 for Stripe retry."""
    db = InMemoryWebhookDbSession()
    db.fail_commit_with = RuntimeError("Database connection pool exhausted")
    setup_db_override(db)

    event_bytes = build_test_event_bytes(event_id="evt_db_fail_1")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to durably persist webhook event"
        assert db.rolled_back_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_event_is_not_acknowledged_before_commit() -> None:
    """Verify event receipt is never acknowledged (200) if commit fails or is not executed."""
    db = InMemoryWebhookDbSession()
    commit_called = False

    async def tracking_commit():
        nonlocal commit_called
        commit_called = True
        # Explicitly verify insertion happened BEFORE commit was attempted
        assert len(db.added) == 1
        assert isinstance(db.added[0], StripeWebhookEvent)
        assert db.added[0].id == "evt_commit_order_1"
        assert db.added[0].status == WebhookProcessingStatus.pending
        raise RuntimeError("Disk write failed during commit")

    db.commit = tracking_commit
    setup_db_override(db)

    event_bytes = build_test_event_bytes(event_id="evt_commit_order_1")
    sig = generate_genuine_stripe_signature(event_bytes)

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert commit_called is True
        assert resp.status_code == 500
        assert resp.status_code != 200
        # Verify uncommitted addition was discarded by rollback
        assert "evt_commit_order_1" not in db.events
        assert db.rolled_back_count == 1
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 7. Fail-Safe Configuration Tests
# =============================================================================


@pytest.mark.asyncio
async def test_stripe_disabled_configuration_fails_safely() -> None:
    """Verify endpoint returns HTTP 503 when Stripe billing is disabled."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes()
    sig = generate_genuine_stripe_signature(event_bytes)

    disabled_settings = get_test_settings(stripe_enabled=False)

    try:
        with patch("app.services.billing.stripe_webhook.settings", disabled_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"].lower()
        assert db.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_missing_webhook_secret_configuration_fails_safely() -> None:
    """Verify endpoint returns HTTP 503 when webhook secret is empty."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    event_bytes = build_test_event_bytes()
    sig = generate_genuine_stripe_signature(event_bytes)

    # Simulate unconfigured webhook secret via attribute patch on valid settings
    test_settings = get_test_settings()

    try:
        with patch("app.services.billing.stripe_webhook.settings", test_settings), \
             patch.object(test_settings, "stripe_webhook_secret", SecretStr("")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/billing/webhook",
                    content=event_bytes,
                    headers={"stripe-signature": sig, "content-type": "application/json"},
                )

        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()
        assert db.committed_count == 0
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 8. Authentication Isolation & Security Non-Regression
# =============================================================================


@pytest.mark.asyncio
async def test_existing_authenticated_billing_endpoints_remain_protected() -> None:
    """Verify existing billing endpoints still strictly require authentication (401 Unauthorized).

    Ensures that adding the public webhook endpoint did not compromise or weaken authentication
    on existing routes such as checkout-session and customer.
    """
    org_id = uuid.uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Checkout session endpoint without Authorization header
        r1 = await client.post(f"/api/v1/organizations/{org_id}/billing/checkout-session")
        assert r1.status_code == 401
        assert "Not authenticated" in r1.json()["detail"]

        # Customer endpoint without Authorization header
        r2 = await client.get(f"/api/v1/organizations/{org_id}/billing/customer")
        assert r2.status_code == 401
        assert "Not authenticated" in r2.json()["detail"]

        # Invalid bearer token
        r3 = await client.post(
            f"/api/v1/organizations/{org_id}/billing/checkout-session",
            headers={"Authorization": "Bearer invalid.token.value"},
        )
        assert r3.status_code == 401


# =============================================================================
# 9. Information Leakage Prevention Tests
# =============================================================================


@pytest.mark.asyncio
async def test_webhook_secret_and_payload_never_leaked_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Verify webhook secret, signature header, and raw payload are never logged during verification failure."""
    db = InMemoryWebhookDbSession()
    setup_db_override(db)

    sensitive_content = b'{"id": "evt_sensitive", "type": "invoice.paid", "secret_bank_info": "CONFIDENTIAL"}'
    secret_value = TEST_WEBHOOK_SECRET
    bad_sig = "t=123456789,v1=abcdef0123456789"

    try:
        with patch("app.services.billing.stripe_webhook.settings", get_test_settings()):
            with caplog.at_level(logging.DEBUG):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    await client.post(
                        "/api/v1/billing/webhook",
                        content=sensitive_content,
                        headers={"stripe-signature": bad_sig, "content-type": "application/json"},
                    )

        # Assert no sensitive strings appeared in any log records
        for record in caplog.records:
            assert secret_value not in record.message
            assert bad_sig not in record.message
            assert "CONFIDENTIAL" not in record.message
    finally:
        app.dependency_overrides.clear()
