"""Comprehensive security and durability tests for Razorpay webhook ingestion.

Endpoint: POST /api/v1/billing/razorpay/webhook

Test Scenarios (Covering All Requirements):
1. Valid signature accepted and returned HTTP 200.
2. Invalid signature rejected with HTTP 400.
3. Missing signature rejected with HTTP 400.
4. Malformed signature rejected with HTTP 400.
5. Body larger than 64 KB rejected with HTTP 413.
6. Oversized body rejected even when Content-Length is absent or misleading.
7. Malformed JSON rejected with HTTP 400.
8. Missing required envelope fields rejected with HTTP 400.
9. Valid event persisted with signature_verified=true and status='pending'.
10. Duplicate event returns HTTP 200 without duplicate persistence.
11. Different event IDs remain distinct.
12. Unexpected merchant account rejected with HTTP 400.
13. Database failure does not produce a false successful acknowledgement (HTTP 500).
14. Secrets and raw signatures are not exposed in logs or responses.
15. Fallback deterministic content-hash event ID when X-Razorpay-Event-Id is missing.
16. Razorpay disabled or unconfigured webhook secret returns HTTP 503.
17. Concurrent duplicate insertion handled safely via IntegrityError retry.
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

import hashlib
import hmac
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch
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
from app.models.payment import BillingWebhookEvent
from app.models.subscription import WebhookProcessingStatus
from app.services.billing.razorpay.webhook_ingress import (
    MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES,
    extract_razorpay_event_id,
    ingest_razorpay_webhook_event,
)

TEST_WEBHOOK_SECRET = "rzp_whsec_test_secret_for_unit_testing_12345"
TEST_MERCHANT_ACCOUNT_ID = "acc_TEST_MERCHANT_123"


# =============================================================================
# Test Doubles & Fixtures
# =============================================================================


class InMemoryRazorpayDbSession:
    """In-memory AsyncSession test double for Razorpay webhook ingestion tests."""

    def __init__(self, initial_events: list[BillingWebhookEvent] | None = None) -> None:
        self.events: dict[str, BillingWebhookEvent] = {
            e.provider_event_id: e for e in (initial_events or [])
        }
        self._pending_events: dict[str, BillingWebhookEvent] = {}
        self.added: list[Any] = []
        self.committed_count = 0
        self.rolled_back_count = 0
        self.fail_commit_with: Exception | None = None
        self.fail_commit_once_with: Exception | None = None

    async def execute(self, statement: Any) -> Any:
        class ScalarResult:
            def __init__(self, item: Any) -> None:
                self._item = item

            def scalar_one_or_none(self) -> Any:
                return self._item

            def scalars(self) -> Any:
                class Scalars:
                    def __init__(self, items: list[Any]) -> None:
                        self._items = items

                    def all(self) -> list[Any]:
                        return self._items

                return Scalars([self._item] if self._item else [])

        try:
            params = statement.compile().params
        except Exception:
            params = {}

        matching = None
        param_vals = set(params.values())

        # Scoped matching: provider, merchant_account_id, environment, provider_event_id
        for evt in list(self.events.values()):
            if (
                evt.provider in param_vals
                and evt.merchant_account_id in param_vals
                and evt.environment in param_vals
                and evt.provider_event_id in param_vals
            ):
                matching = evt
                break

        if matching is None:
            for evt in list(self._pending_events.values()):
                if (
                    evt.provider in param_vals
                    and evt.merchant_account_id in param_vals
                    and evt.environment in param_vals
                    and evt.provider_event_id in param_vals
                ):
                    matching = evt
                    break

        return ScalarResult(matching)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, BillingWebhookEvent):
            self._pending_events[obj.provider_event_id] = obj

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

    async def refresh(self, obj: Any) -> None:
        pass

    async def rollback(self) -> None:
        self._pending_events.clear()
        self.rolled_back_count += 1


def setup_db_override(db: InMemoryRazorpayDbSession) -> None:
    """Register database dependency override as an async generator for FastAPI."""
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db


def get_test_settings(**kwargs: Any) -> Settings:
    """Generate safe test settings with Razorpay enabled."""
    values: dict[str, Any] = {
        "environment": "development",
        "razorpay_enabled": True,
        "razorpay_environment": "test",
        "razorpay_merchant_account_id": TEST_MERCHANT_ACCOUNT_ID,
        "razorpay_key_id": "rzp_test_1234567890",
        "razorpay_key_secret": SecretStr("test_key_secret_12345"),
        "razorpay_webhook_secret": SecretStr(TEST_WEBHOOK_SECRET),
        "razorpay_pro_plan_id": "plan_test_pro_12345",
        "razorpay_plan_currency": "USD",
    }
    values.update(kwargs)
    return Settings(**values)


def generate_razorpay_signature(raw_bytes: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Generate an authoritative HMAC-SHA256 hex digest for Razorpay webhooks."""
    return hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()


def make_razorpay_event_dict(
    event_type: str = "subscription.charged",
    account_id: str = TEST_MERCHANT_ACCOUNT_ID,
    entity_id: str = "sub_test_12345",
) -> dict[str, Any]:
    """Build a canonical Razorpay webhook event envelope."""
    return {
        "entity": "event",
        "account_id": account_id,
        "event": event_type,
        "contains": ["subscription", "payment"],
        "payload": {
            "subscription": {
                "entity": {
                    "id": entity_id,
                    "entity": "subscription",
                    "plan_id": "plan_test_pro_1",
                    "customer_id": "cust_test_123",
                    "status": "active",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_test_99999",
                    "entity": "payment",
                    "amount": 4900,
                    "currency": "USD",
                    "status": "captured",
                    "order_id": "order_test_888",
                    "invoice_id": "inv_test_777",
                }
            },
        },
        "created_at": 1700000000,
    }


# =============================================================================
# Automated Test Suite
# =============================================================================


@pytest.mark.asyncio
async def test_1_valid_signature_accepted():
    """1. Valid signature accepted and returned HTTP 200."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    signature = generate_razorpay_signature(raw_bytes)
    event_id = "evt_rzp_valid_001"

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": signature,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 200
        res_data = response.json()
        assert res_data["received"] is True
        assert res_data["event_id"] == event_id
        assert res_data["status"] == "pending"
        assert db_session.committed_count == 1
        assert event_id in db_session.events
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2_invalid_signature_rejected():
    """2. Invalid signature rejected with HTTP 400."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    invalid_signature = "a" * 64  # valid hex format, but wrong digest

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": invalid_signature,
                    },
                )

        assert response.status_code == 400
        assert "Invalid Razorpay webhook signature" in response.json()["detail"]
        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_3_missing_signature_rejected():
    """3. Missing signature rejected with HTTP 400."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={"Content-Type": "application/json"},  # No signature header
                )

        assert response.status_code == 400
        assert "Missing or empty X-Razorpay-Signature header" in response.json()["detail"]
        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_4_malformed_signature_rejected():
    """4. Malformed signature rejected with HTTP 400."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    malformed_signatures = [
        "not-a-hex-signature",
        "12345",  # too short
        "z" * 64,  # non-hex char 'z'
        " " * 64,  # whitespace
    ]

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for malformed_sig in malformed_signatures:
                    response = await client.post(
                        "/api/v1/billing/razorpay/webhook",
                        content=raw_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-Razorpay-Signature": malformed_sig,
                        },
                    )
                    assert response.status_code == 400
                    assert "signature" in response.json()["detail"].lower()

        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_5_body_larger_than_64kb_rejected():
    """5. Body larger than 64 KB rejected with HTTP 413."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    oversized_bytes = b"x" * (MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES + 1)
    signature = generate_razorpay_signature(oversized_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=oversized_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": signature,
                    },
                )

        assert response.status_code == 413
        assert "exceeds maximum allowed size" in response.json()["detail"]
        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_6_oversized_body_streaming_enforced_without_content_length():
    """6. Oversized body rejected dynamically even when Content-Length is absent or misleading."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    # 70 KB payload
    oversized_bytes = b"{" + b'"padding":"' + b"A" * 70000 + b'"}'

    async def streaming_generator():
        chunk_size = 8192
        for i in range(0, len(oversized_bytes), chunk_size):
            yield oversized_bytes[i:i + chunk_size]

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=streaming_generator(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": "a" * 64,
                    },
                )

        assert response.status_code == 413
        assert "exceeds maximum allowed size" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_7_malformed_json_rejected():
    """7. Malformed JSON rejected with HTTP 400."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    raw_bytes = b"this is not valid json {"
    signature = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": signature,
                    },
                )

        assert response.status_code == 400
        assert "Malformed JSON in webhook payload" in response.json()["detail"]
        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_8_missing_required_envelope_fields_rejected():
    """8. Missing required envelope fields rejected with HTTP 400."""
    invalid_envelopes = [
        {"entity": "payment", "event": "payment.captured", "account_id": "acc_1", "contains": [], "payload": {}},  # entity != 'event'
        {"entity": "event", "account_id": "acc_1", "contains": [], "payload": {}},  # missing 'event'
        {"entity": "event", "event": "subscription.charged", "contains": [], "payload": {}},  # missing 'account_id'
        {"entity": "event", "event": "subscription.charged", "account_id": "acc_1", "payload": {}},  # missing 'contains'
        {"entity": "event", "event": "subscription.charged", "account_id": "acc_1", "contains": []},  # missing 'payload'
    ]

    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    test_settings = get_test_settings(razorpay_merchant_account_id="default")
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for env in invalid_envelopes:
                    raw_bytes = json.dumps(env).encode("utf-8")
                    sig = generate_razorpay_signature(raw_bytes)
                    response = await client.post(
                        "/api/v1/billing/razorpay/webhook",
                        content=raw_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-Razorpay-Signature": sig,
                        },
                    )
                    assert response.status_code == 400
                    assert "envelope" in response.json()["detail"].lower()

        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_9_valid_event_persisted_with_signature_verified():
    """9. Valid event persisted with signature_verified=true and status='pending'."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)
    event_id = "evt_rzp_persisted_001"

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 200
        assert event_id in db_session.events
        persisted = db_session.events[event_id]
        assert persisted.provider == "razorpay"
        assert persisted.merchant_account_id == TEST_MERCHANT_ACCOUNT_ID
        assert persisted.environment == "test"
        assert persisted.event_type == "subscription.charged"
        assert persisted.signature_verified is True
        assert persisted.status == WebhookProcessingStatus.pending
        assert persisted.attempts == 0
        assert persisted.payload["event"] == "subscription.charged"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_10_duplicate_event_returns_200_without_duplicate_persistence():
    """10. Duplicate event returns HTTP 200 without duplicate persistence."""
    event_id = "evt_rzp_duplicate_001"
    existing_event = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=TEST_MERCHANT_ACCOUNT_ID,
        environment="test",
        provider_event_id=event_id,
        event_type="subscription.charged",
        status=WebhookProcessingStatus.processed,
        attempts=2,
        payload={"dummy": "data"},
        signature_verified=True,
    )

    db_session = InMemoryRazorpayDbSession(initial_events=[existing_event])
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 200
        res_data = response.json()
        assert res_data["received"] is True
        assert res_data["event_id"] == event_id
        assert res_data["status"] == "duplicate"
        # Ensure existing state and attempts were preserved
        assert db_session.events[event_id].status == WebhookProcessingStatus.processed
        assert db_session.events[event_id].attempts == 2
        assert db_session.committed_count == 0  # No new commit on duplicate fast-path
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_11_different_event_ids_remain_distinct():
    """11. Different event IDs remain distinct."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for i in (1, 2):
                    payload_dict = make_razorpay_event_dict(entity_id=f"sub_distinct_{i}")
                    raw_bytes = json.dumps(payload_dict).encode("utf-8")
                    sig = generate_razorpay_signature(raw_bytes)
                    event_id = f"evt_rzp_distinct_{i}"

                    response = await client.post(
                        "/api/v1/billing/razorpay/webhook",
                        content=raw_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-Razorpay-Signature": sig,
                            "X-Razorpay-Event-Id": event_id,
                        },
                    )
                    assert response.status_code == 200
                    assert response.json()["status"] == "pending"

        assert len(db_session.events) == 2
        assert "evt_rzp_distinct_1" in db_session.events
        assert "evt_rzp_distinct_2" in db_session.events
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_12_unexpected_merchant_account_rejected():
    """12. Unexpected merchant account rejected with HTTP 400."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    # Payload has account_id = 'acc_UNKNOWN_TENANT'
    payload_dict = make_razorpay_event_dict(account_id="acc_UNKNOWN_TENANT")
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings(razorpay_merchant_account_id=TEST_MERCHANT_ACCOUNT_ID)
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": "evt_rzp_bad_merchant",
                    },
                )

        assert response.status_code == 400
        assert "does not match configured merchant account" in response.json()["detail"]
        assert db_session.committed_count == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_13_database_failure_returns_500():
    """13. Database failure does not produce a false successful acknowledgement (HTTP 500)."""
    db_session = InMemoryRazorpayDbSession()
    db_session.fail_commit_with = RuntimeError("Database disk full")
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": "evt_rzp_db_fail",
                    },
                )

        assert response.status_code == 500
        assert "Failed to durably persist webhook event" in response.json()["detail"]
        assert db_session.rolled_back_count == 1
        assert "evt_rzp_db_fail" not in db_session.events
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_14_secrets_and_raw_signatures_never_exposed(caplog):
    """14. Secrets and raw signatures are not exposed in logs or responses."""
    caplog.set_level(logging.DEBUG)
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": "evt_rzp_no_leak",
                    },
                )

        assert response.status_code == 200
        response_text = response.text
        log_text = caplog.text

        # Verify secret and signature are not present anywhere in response or logs
        assert TEST_WEBHOOK_SECRET not in response_text
        assert TEST_WEBHOOK_SECRET not in log_text
        assert sig not in response_text
        assert sig not in log_text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_15_deterministic_fallback_event_id_when_header_missing():
    """15. Fallback deterministic content-hash event ID when X-Razorpay-Event-Id is missing."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()

    expected_fallback_id = f"synthetic:razorpay:{TEST_MERCHANT_ACCOUNT_ID}:subscription.charged:{raw_digest}"

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        # No X-Razorpay-Event-Id header
                    },
                )

        assert response.status_code == 200
        res_data = response.json()
        assert res_data["received"] is True
        assert res_data["event_id"] == expected_fallback_id
        assert expected_fallback_id in db_session.events
        assert len(expected_fallback_id) <= 255
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_16_billing_disabled_or_secret_missing_returns_503():
    """16. Razorpay disabled or unconfigured webhook secret returns HTTP 503."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")

    try:
        # Case A: Billing Disabled
        disabled_settings = get_test_settings(razorpay_enabled=False)
        with patch("app.services.billing.razorpay.webhook_ingress.settings", disabled_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": "a" * 64,
                    },
                )
                assert response.status_code == 503
                assert "disabled" in response.json()["detail"].lower()

        # Case B: Secret Missing
        mock_no_secret = MagicMock(wraps=get_test_settings())
        mock_no_secret.razorpay_enabled = True
        mock_no_secret.razorpay_webhook_secret = SecretStr("")
        with patch("app.services.billing.razorpay.webhook_ingress.settings", mock_no_secret):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": "a" * 64,
                    },
                )
                assert response.status_code == 503
                assert "configuration unavailable" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_17_concurrent_duplicate_race_resolved_cleanly():
    """17. Concurrent duplicate insertion handled safely via IntegrityError rollback and recovery."""
    event_id = "evt_rzp_concurrent_race_001"
    committed_event = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id=TEST_MERCHANT_ACCOUNT_ID,
        environment="test",
        provider_event_id=event_id,
        event_type="subscription.charged",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=make_razorpay_event_dict(),
        signature_verified=True,
    )

    db_session = InMemoryRazorpayDbSession()

    # Simulate IntegrityError on first commit attempt, representing a concurrent commit
    def fail_with_integrity():
        db_session.events[event_id] = committed_event
        raise IntegrityError("duplicate key value violates unique constraint", params={}, orig=Exception())

    db_session.fail_commit_once_with = fail_with_integrity
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 200
        res_data = response.json()
        assert res_data["received"] is True
        assert res_data["event_id"] == event_id
        assert res_data["status"] == "duplicate"
        assert db_session.rolled_back_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_18_unrelated_integrity_failure_raises_and_returns_500():
    """18. Unrelated integrity failure (e.g. check constraint) does not return 200 and raises 500."""
    event_id = "evt_rzp_unrelated_integrity_fail"
    db_session = InMemoryRazorpayDbSession()

    # IntegrityError without inserting matching duplicate in db_session.events
    def fail_with_unrelated_integrity():
        raise IntegrityError("check constraint violation: non-negative amount", params={}, orig=Exception())

    db_session.fail_commit_once_with = fail_with_unrelated_integrity
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 500
        assert "Failed to durably persist webhook event" in response.json()["detail"]
        assert db_session.rolled_back_count == 1
        assert event_id not in db_session.events
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_19_duplicate_lookup_returning_no_matching_row_re_raises_integrity_error():
    """19. Direct call to ingest_razorpay_webhook_event re-raises original IntegrityError when no row found."""
    event_id = "evt_rzp_no_row_found"
    db_session = InMemoryRazorpayDbSession()

    def fail_with_integrity():
        raise IntegrityError("foreign key violation", params={}, orig=Exception())

    db_session.fail_commit_once_with = fail_with_integrity

    payload_dict = make_razorpay_event_dict()

    with pytest.raises(IntegrityError) as exc_info:
        await ingest_razorpay_webhook_event(
            db=db_session,  # type: ignore[arg-type]
            event_id=event_id,
            event_type="subscription.charged",
            payload=payload_dict,
            merchant_account_id=TEST_MERCHANT_ACCOUNT_ID,
            environment="test",
        )

    assert "foreign key violation" in str(exc_info.value)
    assert db_session.rolled_back_count == 1


@pytest.mark.asyncio
async def test_20_duplicate_race_with_different_scope_rejected():
    """20. Duplicate race with different provider/account/environment scope fails lookup and returns 500."""
    event_id = "evt_rzp_scoped_mismatch_race"
    # Event exists but with DIFFERENT merchant_account_id
    different_scope_event = BillingWebhookEvent(
        id=uuid.uuid4(),
        provider="razorpay",
        merchant_account_id="acc_OTHER_MERCHANT_999",
        environment="test",
        provider_event_id=event_id,
        event_type="subscription.charged",
        status=WebhookProcessingStatus.pending,
        attempts=0,
        payload=make_razorpay_event_dict(),
        signature_verified=True,
    )
    db_session = InMemoryRazorpayDbSession(initial_events=[different_scope_event])

    def fail_with_integrity():
        raise IntegrityError("duplicate key value violates unique constraint", params={}, orig=Exception())

    db_session.fail_commit_once_with = fail_with_integrity
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                        "X-Razorpay-Event-Id": event_id,
                    },
                )

        assert response.status_code == 500
        assert "Failed to durably persist webhook event" in response.json()["detail"]
        assert db_session.rolled_back_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_21_event_id_header_format_and_length_validation():
    """21. Event ID header validated for safe format and length <= 255; invalid falls back cleanly."""
    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    expected_fallback = f"synthetic:razorpay:{TEST_MERCHANT_ACCOUNT_ID}:subscription.charged:{raw_digest}"

    # Valid header format
    valid_id = "evt_rzp-valid.123:test"
    assert extract_razorpay_event_id(raw_bytes, payload_dict, valid_id) == valid_id

    # Invalid header format (contains space or special characters)
    invalid_format = "evt rzp invalid <script>"
    assert extract_razorpay_event_id(raw_bytes, payload_dict, invalid_format) == expected_fallback

    # Invalid header format (contains control characters or newlines)
    invalid_newlines = "evt_rzp\ninvalid\r"
    assert extract_razorpay_event_id(raw_bytes, payload_dict, invalid_newlines) == expected_fallback

    # Header exceeding 255 characters
    oversized_id = "evt_" + ("x" * 255)
    assert extract_razorpay_event_id(raw_bytes, payload_dict, oversized_id) == expected_fallback


@pytest.mark.asyncio
async def test_22_repeated_identical_payloads_without_event_id_deduplicate_consistently():
    """22. Repeated identical signed payloads without event ID header deduplicate consistently."""
    db_session = InMemoryRazorpayDbSession()
    setup_db_override(db_session)

    payload_dict = make_razorpay_event_dict()
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_razorpay_signature(raw_bytes)
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    expected_id = f"synthetic:razorpay:{TEST_MERCHANT_ACCOUNT_ID}:subscription.charged:{raw_digest}"

    test_settings = get_test_settings()
    try:
        with patch("app.services.billing.razorpay.webhook_ingress.settings", test_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # First delivery
                r1 = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                    },
                )
                assert r1.status_code == 200
                assert r1.json()["event_id"] == expected_id
                assert r1.json()["status"] == "pending"

                # Second duplicate delivery
                r2 = await client.post(
                    "/api/v1/billing/razorpay/webhook",
                    content=raw_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                    },
                )
                assert r2.status_code == 200
                assert r2.json()["event_id"] == expected_id
                assert r2.json()["status"] == "duplicate"

        assert len(db_session.events) == 1
        assert expected_id in db_session.events
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_23_different_event_identities_remain_distinct():
    """23. Different event types or bodies produce distinct fallback identities and coexist."""
    p1 = make_razorpay_event_dict(event_type="subscription.charged")
    p2 = make_razorpay_event_dict(event_type="subscription.activated")
    b1 = json.dumps(p1).encode("utf-8")
    b2 = json.dumps(p2).encode("utf-8")

    id1 = extract_razorpay_event_id(b1, p1, None)
    id2 = extract_razorpay_event_id(b2, p2, None)

    assert id1 != id2
    assert "subscription.charged" in id1
    assert "subscription.activated" in id2
    assert len(id1) <= 255
    assert len(id2) <= 255
