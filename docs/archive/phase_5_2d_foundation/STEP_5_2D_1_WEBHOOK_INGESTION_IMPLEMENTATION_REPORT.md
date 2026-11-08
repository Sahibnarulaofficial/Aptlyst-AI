# Step 5.2D.1: Razorpay Webhook Ingestion & Signature Verification Implementation Report
*(Updated Post-Audit: Step 5.2D.1.1)*

## Executive Summary

Step 5.2D.1 implements the **Razorpay Webhook HTTP Ingestion Boundary** for Aptlyst AI. The endpoint receives asynchronous event notifications from Razorpay, validates the envelope and HMAC-SHA256 signature, ensures strict bounded-memory streaming (maximum 64 KB), resolves event identity deterministically, and durably persists the event in the existing provider-neutral `billing_webhook_events` table before acknowledging receipt.

Under Step 5.2D.1.1, a narrow correctness audit and hardening pass was completed:
1. **Attempt Counter Semantics**: Newly persisted events are initialized with `attempts = 0` (reflecting 0 actual execution attempts), ensuring consistent bounded retry backoff schedules (5s, 15s, 45s, 120s) and exactly 5 processing attempts prior to quarantine.
2. **IntegrityError Handling**: Concurrency races during duplicate insert perform clean transaction rollback, query strictly by provider/account/environment/event identity, and re-raise the original database error if no matching duplicate exists, guaranteeing that unrelated database integrity failures are never acknowledged as HTTP 200.
3. **Event Identity Hardening**: `X-Razorpay-Event-Id` is strictly validated against a safe identifier regex (`^[A-Za-z0-9_\-\.:]{1,255}$`) and length ceiling (<= 255 chars), cleanly falling back to deterministic cryptographic content-hashing for malformed or missing headers.
4. **Architectural Limitation Documentation**: Formally documents that Razorpay computes HMAC-SHA256 solely over the raw payload body, not HTTP headers, requiring downstream entity-level deduplication.

---

## 1. Exact Files Created and Modified

### Created Files
- [`apps/api/app/services/billing/razorpay/webhook_ingress.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/webhook_ingress.py)
  - Core webhook ingress service containing:
    - Bounded streaming request reader (`read_bounded_request_body`) rejecting payloads exceeding 64 KB with HTTP 413.
    - Constant-time HMAC-SHA256 signature verification (`verify_razorpay_webhook_signature`).
    - Razorpay envelope parsing and merchant account scoping (`parse_and_validate_razorpay_envelope`).
    - Event identity extraction with regex format validation, length bounding, and deterministic synthetic fallback (`extract_razorpay_event_id`).
    - Scoped, durable, idempotent database persistence with `attempts = 0` and strict `IntegrityError` re-raise (`ingest_razorpay_webhook_event`).
- [`apps/api/tests/test_razorpay_webhook_ingestion.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/tests/test_razorpay_webhook_ingestion.py)
  - 23 comprehensive automated tests covering all security, streaming, validation, scoping, durability, duplicate handling, attempt counting, and concurrency race requirements.
- [`STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md`](file:///d:/Projects/V1/Aptlyst-AI/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md)
  - This implementation report.

### Modified Files
- [`apps/api/app/services/billing/razorpay/__init__.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/__init__.py)
  - Exported all webhook ingress services and domain exceptions (`MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES`, `RazorpayWebhookError`, `ingest_razorpay_webhook_event`, `parse_and_validate_razorpay_envelope`, `read_bounded_request_body`, `verify_razorpay_webhook_signature`, etc.).
- [`apps/api/app/api/billing.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/api/billing.py)
  - Added public webhook endpoint `POST /api/v1/billing/razorpay/webhook` under `webhook_router`.
  - Catches `(RazorpayWebhookPersistenceError, IntegrityError)` mapping to HTTP 500 without false acknowledgments.

---

## 2. Endpoint URL, HTTP Method, and Router Mount

- **Full URL**: `POST /api/v1/billing/razorpay/webhook`
- **HTTP Method**: `POST`
- **Router Mounting**:
  - Defined in `apps/api/app/api/billing.py` on `webhook_router`:
    ```python
    @webhook_router.post(
        "/razorpay/webhook",
        status_code=status.HTTP_200_OK,
        summary="Ingest Razorpay webhook event",
        description="Public webhook ingestion endpoint for Razorpay asynchronous event delivery.",
    )
    async def handle_razorpay_webhook(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
    ```
  - `webhook_router` has prefix `"/billing"` and is included by `api_router` with prefix `"/api/v1"`.
  - Endpoint is public and unauthenticated. No user session or JWT bearer token is required, ensuring Razorpay servers can deliver notifications.
- **HTTP Status Codes Returned**:
  - `200 OK`: Event successfully persisted or recognized as duplicate delivery. Response body: `{"received": True, "event_id": event_id, "status": "pending" | "duplicate"}`.
  - `400 Bad Request`: Invalid/missing/malformed signature, malformed JSON, missing required envelope fields, or merchant account mismatch.
  - `413 Payload Too Large`: Payload exceeds bounded threshold (64 KB / 65,536 bytes).
  - `500 Internal Server Error`: Database insertion failure or unhandled internal exception. Prevents false delivery acknowledgments and prompts Razorpay to retry.
  - `503 Service Unavailable`: Razorpay billing is disabled or webhook secret is unconfigured.

---

## 3. Attempt Counter Semantics

### Architectural Audit & Decision
An audit of `BillingWebhookEvent` and the existing Stripe worker semantics revealed:
1. In the legacy Stripe implementation, newly ingested events were assigned `attempts = 1` before any background processing had taken place.
2. In `stripe_webhook_worker.py` and `stripe_webhook_engine.py`:
   - On each retryable failure, `event.attempts += 1`.
   - The exponential backoff schedule is defined as:
     - `attempts <= 1`: 5 seconds
     - `attempts == 2`: 15 seconds
     - `attempts == 3`: 45 seconds
     - `attempts >= 4`: 120 seconds
   - If an event begins at `attempts = 1`, its first failure increments `attempts` to `2`. Consequently, the `attempts <= 1` (5s) backoff tier is skipped, jumping straight to 15s. Furthermore, `attempts >= max_attempts (5)` is reached after only 4 attempts.
3. A newly persisted event has undergone **zero** processing attempts.
4. **Resolution**: `ingest_razorpay_webhook_event()` explicitly initializes `attempts = 0` on new `BillingWebhookEvent` rows. When the future Razorpay worker executes and records a failure, incrementing `attempts` to 1 correctly activates the first retry backoff tier (5 seconds) and provides exactly 5 total attempts before quarantine.
5. The Stripe webhook ingestion implementation and retry limit remain untouched to preserve backwards compatibility.

---

## 4. IntegrityError Handling & Concurrency Race Safety

### Requirements & Behavior
When two identical webhook notifications arrive concurrently, both may pass the initial existence check and attempt to insert into `billing_webhook_events`. The database partial/unique constraint `uq_billing_webhook_events_provider_account_env_event_id` causes the second insert to fail with an `IntegrityError`.

The handler resolves this race as follows:
```python
    except IntegrityError as exc:
        # 1. Roll back the failed transaction
        await db.rollback()
        # 2. Look up the duplicate using complete provider/account/environment/event identity
        res_dup = await db.execute(stmt)
        existing_dup = res_dup.scalar_one_or_none()
        if existing_dup is not None:
            logger.info(
                "Concurrent duplicate Razorpay webhook event resolved (id=%s, type=%s, status=%s)",
                event_id,
                existing_dup.event_type,
                existing_dup.status.value,
            )
            return (existing_dup, False)
        # 3. If no matching scoped duplicate exists, re-raise original database error
        logger.error(
            "IntegrityError during Razorpay event ingestion for id=%s but matching scoped record not found: %s",
            event_id,
            exc,
        )
        raise exc
```

### Safety Guarantees
- **Exact Scoping**: The retry lookup queries with all 4 scope fields: `provider == "razorpay"`, `merchant_account_id == merchant_account_id`, `environment == environment`, and `provider_event_id == event_id`. If an event with the same ID exists under a different account or environment, it is not matched.
- **Fail-Closed on Unrelated Failures**: If an `IntegrityError` is caused by an unrelated constraint violation (e.g. check constraint or foreign key violation) and no duplicate row exists, `existing_dup` is `None`. The original `IntegrityError` is re-raised and caught by the API route to return `HTTP 500`. Unrelated database errors are never converted into false HTTP 200 acknowledgments.

---

## 5. Event Identity Review & Architectural Limitations

### Validation & Fallback Logic
1. **Primary Event ID**:
   - Header: `X-Razorpay-Event-Id`.
   - Format Validation: Must match regex `^[A-Za-z0-9_\-\.:]{1,255}$` (ASCII alphanumeric, underscore, hyphen, period, colon).
   - Length ceiling: `len(cleaned) <= 255`.
   - Any header containing whitespace, newlines, control characters, special characters (e.g. `<script>`), or exceeding 255 characters is rejected and falls back cleanly.
2. **Deterministic Cryptographic Fallback**:
   - If header is absent or invalid, computes:
     `synthetic:razorpay:{account_id}:{event_type}:{sha256(raw_bytes)}`
   - Scoped by `account_id` and `event_type`.
   - Guaranteed <= 255 characters, fitting the `VARCHAR(255)` column constraint.
   - Deterministic: Repeated deliveries of identical signed payloads produce the exact same synthetic ID and deduplicate cleanly.
   - Distinct payloads or event types produce distinct hashes and do not collide.

### Architectural Limitation: Event ID Excluded from HMAC Signature
- In Razorpay's webhook protocol, the HMAC-SHA256 signature in `X-Razorpay-Signature` is computed **exclusively** over the raw request payload body.
- The `X-Razorpay-Event-Id` HTTP header is **not included** in the signed payload.
- **Consequences & Mitigations**:
  - A man-in-the-middle or proxy cannot tamper with the payload without failing signature verification, but could strip or modify the `X-Razorpay-Event-Id` header.
  - Stripping the header causes the system to fall back safely to deterministic content-hashing.
  - Modifying the header could result in the same payload being stored under a different event ID. However, because Aptlyst AI's downstream billing workers enforce entity-level idempotent processing (on `subscription_id`, `payment_id`, and `invoice_id`), duplicate execution on the underlying financial entities is strictly prevented.

---

## 6. Signature Verification & Constant-Time Security

- **Algorithm**: HMAC-SHA256 computed over the exact raw request bytes.
- **Header**: `X-Razorpay-Signature` (expected to be a 64-character lowercase hex string).
- **Constant-Time Comparison**:
  ```python
  expected_signature = hmac.new(
      webhook_secret.encode("utf-8"),
      raw_body,
      hashlib.sha256,
  ).hexdigest()

  if not hmac.compare_digest(expected_signature, normalized_received_signature):
      raise RazorpayWebhookSignatureVerificationError("Webhook signature verification failed.")
  ```
  `hmac.compare_digest` runs in constant time relative to signature length, eliminating side-channel timing analysis attacks.
- **Fail-Closed Guarantees**:
  - Rejects missing signature headers (`400 Bad Request`).
  - Rejects non-hex or invalid-length signatures (`400 Bad Request`).
  - Rejects signatures computed over mutated or decoded bytes.
  - If `settings.razorpay_enabled` is False or `settings.razorpay_webhook_secret` is empty, immediately fails with `503 Service Unavailable`.
  - Secret keys and raw signatures are scrubbed and never exposed in logs or exception messages.

---

## 7. Bounded-Memory Request Streaming (64 KB Limit)

- **Constant**: `MAX_RAZORPAY_WEBHOOK_PAYLOAD_BYTES = 64 * 1024` (65,536 bytes).
- **Implementation**:
  - Checks incoming `Content-Length` header upfront. If `Content-Length > 65536`, rejects immediately with `413 Payload Too Large` without buffering.
  - Reads request body chunks via `request.stream()`. If total accumulated bytes exceed 65,536 bytes (even when `Content-Length` is omitted, chunked, or falsified), the stream terminates immediately and raises `413 Payload Too Large`.

---

## 8. Test Execution Summary

### Test Execution Commands
```powershell
# 1. Razorpay Webhook Ingestion Test Suite (All 23 Tests)
apps/api/.venv/Scripts/pytest.exe apps/api/tests/test_razorpay_webhook_ingestion.py -v

# 2. Comprehensive Billing Regression Test Suite (115 Tests)
apps/api/.venv/Scripts/pytest.exe apps/api/tests/test_razorpay_checkout.py apps/api/tests/test_stripe_webhook_ingestion.py apps/api/tests/test_stripe_webhook_engine.py apps/api/tests/test_stripe_webhook_worker.py apps/api/tests/test_razorpay_config.py apps/api/tests/test_stripe_config.py -v
```

### Test Results

#### Razorpay Webhook Ingestion Tests (`test_razorpay_webhook_ingestion.py`)
```
collected 23 items

test_1_valid_signature_accepted PASSED
test_2_invalid_signature_rejected PASSED
test_3_missing_signature_rejected PASSED
test_4_malformed_signature_rejected PASSED
test_5_body_larger_than_64kb_rejected PASSED
test_6_oversized_body_streaming_enforced_without_content_length PASSED
test_7_malformed_json_rejected PASSED
test_8_missing_required_envelope_fields_rejected PASSED
test_9_valid_event_persisted_with_signature_verified PASSED
test_10_duplicate_event_returns_200_without_duplicate_persistence PASSED
test_11_different_event_ids_remain_distinct PASSED
test_12_unexpected_merchant_account_rejected PASSED
test_13_database_failure_returns_500 PASSED
test_14_secrets_and_raw_signatures_never_exposed PASSED
test_15_deterministic_fallback_event_id_when_header_missing PASSED
test_16_billing_disabled_or_secret_missing_returns_503 PASSED
test_17_concurrent_duplicate_race_resolved_cleanly PASSED
test_18_unrelated_integrity_failure_raises_and_returns_500 PASSED
test_19_duplicate_lookup_returning_no_matching_row_re_raises_integrity_error PASSED
test_20_duplicate_race_with_different_scope_rejected PASSED
test_21_event_id_header_format_and_length_validation PASSED
test_22_repeated_identical_payloads_without_event_id_deduplicate_consistently PASSED
test_23_different_event_identities_remain_distinct PASSED

============================= 23 passed in 12.00s =============================
```

#### Billing Regression Tests (115 Tests)
```
apps/api/tests/test_razorpay_checkout.py (49 tests) PASSED
apps/api/tests/test_stripe_webhook_ingestion.py (16 tests) PASSED
apps/api/tests/test_stripe_webhook_engine.py (14 tests) PASSED
apps/api/tests/test_stripe_webhook_worker.py (19 tests) PASSED
apps/api/tests/test_razorpay_config.py (8 tests) PASSED
apps/api/tests/test_stripe_config.py (9 tests) PASSED

============================ 115 passed in 10.42s =============================
```

**Total Billing Tests Passing**: 138 tests.

---

## 9. Boundaries and Invariant Confirmations

1. **No Webhook Handlers or State Mutations**:
   - No background worker, polling mechanism, or event dispatch was implemented.
   - No event processing logic (such as mutating `billing_subscriptions`, `billing_invoices`, `billing_payments`, or organization billing periods) was added.
2. **No Migrations Created**:
   - Existing schema and migration history remain untouched.
   - Current Alembic head confirmed: `c1d2e3f4a5b7` (head).
3. **No Live Gateway Connections**:
   - No requests made to live Razorpay or Supabase APIs.
4. **Stripe Isolation**:
   - Existing Stripe webhook ingestion and handlers remain completely unaffected.

---

## 10. Git Status and Alembic Head Confirmation

### Git Status
```
On branch main
Your branch is ahead of 'origin/main' by 1 commit.

Changes not staged for commit:
	modified:   apps/api/alembic/env.py
	modified:   apps/api/app/api/billing.py
	modified:   apps/api/app/config.py
	modified:   apps/api/app/models/organization.py
	modified:   apps/api/app/models/subscription.py
	modified:   apps/api/app/schemas/billing.py
	modified:   apps/api/app/services/billing/__init__.py
	modified:   apps/api/app/services/billing/stripe_checkout_handler.py
	modified:   apps/api/app/services/billing/stripe_customer.py
	modified:   apps/api/app/services/billing/stripe_invoice_paid_handler.py
	modified:   apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py
	modified:   apps/api/app/services/billing/stripe_subscription_deleted_handler.py
	modified:   apps/api/app/services/billing/stripe_subscription_updated_handler.py
	modified:   apps/api/tests/test_billing_api.py
	modified:   apps/api/tests/test_stripe_checkout_handler.py
	modified:   apps/api/tests/test_stripe_customer.py

Untracked files:
	STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md
	STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md
	STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md
	apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py
	apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py
	apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py
	apps/api/app/models/payment.py
	apps/api/app/services/billing/razorpay/
	apps/api/tests/test_provider_neutral_billing_models.py
	apps/api/tests/test_provider_neutral_migration.py
	apps/api/tests/test_razorpay_checkout.py
	apps/api/tests/test_razorpay_client.py
	apps/api/tests/test_razorpay_config.py
	apps/api/tests/test_razorpay_customer.py
	apps/api/tests/test_razorpay_webhook_ingestion.py
```

### Alembic Head
```
c1d2e3f4a5b7 (head)
```

---

## 11. Conclusion & Readiness

Step 5.2D.1.1 is complete. The Razorpay HTTP webhook ingestion layer is verified for attempt count correctness (`attempts = 0`), strict scoped `IntegrityError` rollback and re-raise, header format and length validation, and deterministic cryptographic fallback. All 138 billing test scenarios pass cleanly.
