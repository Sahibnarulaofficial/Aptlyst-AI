# D-04A Stage 2 — Independent Implementation Review

**Review Date:** 2026-11-14  
**Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`  
**Branch:** `implementation/d04a-stage1-exception-foundation`  
**Baseline HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`  
**Review Type:** Independent Read-Only Review  
**Authoritative Design References:**
- `docs/reports/D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` (§1.2.1, §2.1, §3.1, §3.2, §12.1)
- `docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` (§4.1, §4.2, §9 Stage 2, §10.1)
- `docs/reports/D2_D04A_STAGE1_CORRECTION_PLAN.md` (Revision 1.2)
- `docs/reports/D2_D04A_STAGE2_IMPLEMENTATION_REPORT.md`

---

## 1. Executive Verdict

### Verdict: **ACCEPT**

The D-04A Stage 2 implementation (Razorpay Webhook Exception Hierarchy Unification & Export Cleanup) is **accepted** without required corrections.

The implementation successfully resolves architectural flaw **`D04-02`** by establishing a single canonical Razorpay exception hierarchy rooted in `RazorpayWebhookEngineError(BillingWebhookError)`, eliminating duplicate class definitions from `handler_base.py`, providing backward-compatible aliases, implementing all approved concrete retryable and permanent subclasses, ensuring `StaleLeaseError` disjointness, providing client and database exception classifiers, delegating sanitization to the Stage 1 sanitizer, and cleanly exporting symbols in `app.services.billing.razorpay.__init__.__all__`.

All 701 tests executed across the test suites pass with zero failures and zero skips. Sibling worktrees remain untouched, and scoping boundaries for Stages 3, 4, and 5 have been strictly preserved.

---

## 2. Repository Integrity & Baseline Verification

### 2.1 Worktree & Branch Status
- **Worktree Path:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`
- **Branch:** `implementation/d04a-stage1-exception-foundation`
- **HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`
- **Tracked Modified Files (4):**
  - `apps/api/app/services/billing/razorpay/__init__.py`
  - `apps/api/app/services/billing/razorpay/handler_base.py`
  - `apps/api/app/services/billing/razorpay/webhook_engine.py`
  - `apps/api/tests/test_razorpay_handler_foundation.py`
- **Untracked Implementation & Test Deliverables (3):**
  - `apps/api/app/services/billing/webhook_errors.py` (Stage 1 provider-neutral foundation)
  - `apps/api/tests/test_billing_webhook_errors.py` (Stage 1 test suite, updated for Stage 2 reparenting)
  - `apps/api/tests/test_razorpay_webhook_exceptions.py` (New Stage 2 dedicated test suite)
- **Untracked Documentation Deliverables:**
  - `docs/reports/D2_D04A_STAGE2_IMPLEMENTATION_REPORT.md`
  - `docs/reports/D2_D04A_STAGE2_INDEPENDENT_REVIEW.md` (this report)
  - Pre-existing Stage 1 audit and completion reports

### 2.2 Hash Verification & Discrepancy Analysis

| File Path | Baseline / Expected SHA-256 | Current SHA-256 | Status | Notes |
|---|---|---|---|---|
| `apps/api/app/services/billing/webhook_errors.py` | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | **MATCH (Byte-for-byte)** | Stage 1 provider-neutral foundation preserved untouched. |
| `apps/api/app/services/billing/razorpay/webhook_engine.py` | *(Pre-Stage 2 tracked baseline)* | `7098575BBBD68DDC9B144E6185F96A4B01D57AEF9CA20FDB940AA5869AAE912D` | **MATCH** | Matches reported Stage 2 implementation hash. |
| `apps/api/app/services/billing/razorpay/handler_base.py` | *(Pre-Stage 2 tracked baseline)* | `A4173F777DB651D8928522C49A1098E7243CA84F321D35AE97D346764DC3C68B` | **MATCH** | Matches reported Stage 2 implementation hash. |
| `apps/api/app/services/billing/razorpay/__init__.py` | *(Pre-Stage 2 tracked baseline)* | `74022B82A88F4132ED01749B0E579A35C3E05300C1A0F0300A19411946D5679E` | **MATCH** | Matches reported Stage 2 implementation hash. |
| `apps/api/tests/test_razorpay_handler_foundation.py` | *(Pre-Stage 2 tracked baseline)* | `E3C3FDFEB0249C415D54D2DA6F39B23664C963F529895E0734C423E16B0B545D` | **MATCH** | Matches reported Stage 2 implementation hash. |
| `apps/api/tests/test_razorpay_webhook_exceptions.py` | *(New in Stage 2)* | `3C6C8406BDA919B45B0C0707D58EA947AC738B54B62A1E4B5CA4B1A125956AFA` | **MATCH** | Matches reported Stage 2 implementation hash. |
| `apps/api/tests/test_billing_webhook_errors.py` | `592DABDA406903540569F8D05E2E9767CC01D4BA3D9BE2B00BF9B02624FF3645` | `7130AB8156CA175B043EEB1C4CFCE79C6927075953B4975EED39B6EC1338C033` | **EXPLAINED** | See Discrepancy Note 1 below. |
| `docs/reports/D2_D04A_STAGE2_IMPLEMENTATION_REPORT.md` | *(Report deliverable)* | `F52988E978ABD923D09D72DC4CC0653AE22CE636D106A236EE5BE5500B320F0E` | **MATCH** | Matches reported deliverable hash. |

**Discrepancy Note 1 (`test_billing_webhook_errors.py` hash update):**  
In Stage 1, `test_billing_webhook_errors.py` contained `test_existing_provider_hierarchies_unmodified_in_stage1()`, which asserted `assert not issubclass(rzp_webhook_engine.RazorpayWebhookEngineError, BillingWebhookError)`. With Stage 2 authorized to reparent `RazorpayWebhookEngineError` onto `BillingWebhookError`, this assertion was updated to:
```python
assert issubclass(rzp_webhook_engine.RazorpayWebhookEngineError, BillingWebhookError)
assert issubclass(rzp_handler_base.RazorpayWebhookHandlerError, BillingWebhookError)
```
While keeping negative assertions for HTTP ingress (`RazorpayWebhookError`), Stripe (`WebhookEngineError`), client (`RazorpayClientError`), and customer (`RazorpayCustomerError`) intact. This accounted for the hash shift from `592DAB...` to `7130AB...`.

### 2.3 Sibling Worktrees Verification
- `D:\Projects\V1\Aptlyst-AI` on branch `main` @ `027ab7a`: Verified untouched (13 pre-existing dirty files preserved).
- `D:\Projects\V1\Aptlyst-AI-pg16-validation` on branch `validation/postgres16-ci-hardening` @ `c932cc4`: Verified untouched (zero tracked changes).
- No git commits, adds, pushes, or branch checkouts were performed.

---

## 3. Canonical Exception Hierarchy Assessment

### 3.1 Flaw D04-02 Elimination
The fundamental defect identified in `D04-02` was that `apps/api/app/services/billing/razorpay/webhook_engine.py` and `apps/api/app/services/billing/razorpay/handler_base.py` independently defined classes with identical names:
- `RazorpayWebhookRetryableError`
- `RazorpayWebhookPermanentError`

Handlers raised subclasses inheriting from `handler_base.py`'s classes, while `razorpay_webhook_worker.py` imported and caught classes from `webhook_engine.py`. Consequently:
```python
issubclass(handler_base.RazorpayPayloadValidationError, webhook_engine.RazorpayWebhookPermanentError) == False
```
This caused worker permanent error handling to be unreachable, erroneously treating permanent validation errors as retryable and consuming all 5 retry attempts.

**Independent Verification:**
In the reviewed code:
1. `apps/api/app/services/billing/razorpay/handler_base.py` lines 60–86 deleted the duplicate classes.
2. `handler_base.py` imports the canonical classes directly from `webhook_engine.py`:
   ```python
   from app.services.billing.razorpay.webhook_engine import (
       ClaimedWebhookEvent,
       RazorpayBusinessRejectionError,
       RazorpayDatabasePermanentError,
       RazorpayDatabaseRetryableError,
       RazorpayMissingEntityIdError,
       RazorpayPayloadValidationError,
       RazorpayProviderPermanentError,
       RazorpayProviderTransientError,
       RazorpayScopeMismatchError,
       RazorpayTransientRaceRetryError,
       RazorpayUnknownEventError,
       RazorpayUnverifiedEventError,
       RazorpayWebhookEngineError,
       RazorpayWebhookPermanentError,
       RazorpayWebhookReconciliationConflictError,
       RazorpayWebhookRetryableError,
       StaleLeaseError,
   )

   RazorpayWebhookHandlerError = RazorpayWebhookEngineError
   ```
3. Independent programmatic evaluation confirms:
   - `rzp_hb.RazorpayWebhookHandlerError is rzp_we.RazorpayWebhookEngineError` -> **`True`**
   - `issubclass(rzp_hb.RazorpayPayloadValidationError, rzp_we.RazorpayWebhookPermanentError)` -> **`True`**
   - `issubclass(rzp_hb.RazorpayWebhookReconciliationConflictError, rzp_we.RazorpayWebhookPermanentError)` -> **`True`**
   - `issubclass(rp.RazorpayPayloadValidationError, rp.RazorpayWebhookPermanentError)` -> **`True`**
   - `issubclass(rp.RazorpayWebhookEngineError, BillingWebhookError)` -> **`True`**

### 3.2 Subclass Inheritance Alignment
All concrete subclasses adhere to the canonical dual-hierarchy blueprint:

| Subclass | Immediate Parent | Secondary Provider-Neutral Parent | Category Slug | `is_retryable` |
|---|---|---|---|---|
| `RazorpayWebhookEngineError` | `BillingWebhookError` | — | `"webhook_error"` | `False` |
| `StaleLeaseError` | `RazorpayWebhookEngineError` | `BillingWebhookLeaseError` | `"stale_lease"` | `False` |
| `RazorpayWebhookRetryableError` | `RazorpayWebhookEngineError` | `BillingWebhookRetryableError` | `"retryable_failure"` | `True` |
| `RazorpayDatabaseRetryableError` | `RazorpayWebhookRetryableError` | `BillingWebhookRetryableError` | `"database_retryable"` | `True` |
| `RazorpayProviderTransientError` | `RazorpayWebhookRetryableError` | `BillingWebhookRetryableError` | `"provider_transient"` | `True` |
| `RazorpayTransientRaceRetryError` | `RazorpayWebhookRetryableError` | `BillingWebhookRetryableError` | `"transient_race"` | `True` |
| `RazorpayWebhookPermanentError` | `RazorpayWebhookEngineError` | `BillingWebhookPermanentError` | `"permanent_failure"` | `False` |
| `RazorpayPayloadValidationError` | `RazorpayWebhookPermanentError` | `BillingWebhookValidationError` | `"validation"` | `False` |
| `RazorpayMissingEntityIdError` | `RazorpayPayloadValidationError` | `BillingWebhookValidationError` | `"missing_entity_id"` | `False` |
| `RazorpayScopeMismatchError` | `RazorpayWebhookPermanentError` | `BillingWebhookScopeMismatchError` | `"scope_mismatch"` | `False` |
| `RazorpayUnverifiedEventError` | `RazorpayWebhookPermanentError` | `BillingWebhookPermanentError` | `"unverified_event"` | `False` |
| `RazorpayUnknownEventError` | `RazorpayWebhookPermanentError` | `BillingWebhookUnknownEventError` | `"unknown_event"` | `False` |
| `RazorpayBusinessRejectionError` | `RazorpayWebhookPermanentError` | `BillingWebhookPermanentError` | `"business_rejection"` | `False` |
| `RazorpayDatabasePermanentError` | `RazorpayWebhookPermanentError` | `BillingWebhookPermanentError` | `"database_permanent"` | `False` |
| `RazorpayProviderPermanentError` | `RazorpayWebhookPermanentError` | `BillingWebhookPermanentError` | `"provider_permanent"` | `False` |
| `RazorpayWebhookReconciliationConflictError` | `RazorpayWebhookPermanentError` | `BillingWebhookConflictError` | `"reconciliation_conflict"` | `False` |

### 3.3 StaleLeaseError Disjointness
`StaleLeaseError` inherits from `(RazorpayWebhookEngineError, BillingWebhookLeaseError)`.  
Independent assertions confirm:
- `issubclass(StaleLeaseError, RazorpayWebhookRetryableError)` -> **`False`**
- `issubclass(StaleLeaseError, BillingWebhookRetryableError)` -> **`False`**
- `issubclass(StaleLeaseError, RazorpayWebhookPermanentError)` -> **`False`**
- `issubclass(StaleLeaseError, BillingWebhookPermanentError)` -> **`False`**
- `StaleLeaseError.is_retryable` -> **`False`**

This ensures worker lease-loss handling cannot be misclassified into either retry backoff or event quarantine.

### 3.4 Constructor Backward Compatibility & FK Safety
The `RazorpayWebhookReconciliationConflictError` constructor in `webhook_engine.py` lines 270–315 was independently reviewed:
```python
def __init__(
    self,
    message: str = "",
    *,
    case_key: str,
    reason: str,
    organization_id: uuid.UUID | None = None,
    committed_subscription_id: uuid.UUID | None = None,
    subscription_id: uuid.UUID | None = None,
    committed_invoice_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    committed_payment_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    raw_event_id: uuid.UUID | None = None,
    diagnostic_payload: dict[str, Any] | None = None,
    failure_category: str | None = None,
    error_code: str | None = None,
    constraint_name: str | None = None,
) -> None:
```

**Constructor Contract Verification:**
1. **Positional Compatibility:** `message: str = ""` preserves legacy callers passing a positional message string.
2. **Mandatory Keywords:** `case_key: str` and `reason: str` remain keyword-only.
3. **Legacy Keyword Compatibility:** `subscription_id`, `invoice_id`, `payment_id`, `organization_id`, `raw_event_id`, and `diagnostic_payload` continue to be accepted.
4. **Committed FK Precedence:** If both `committed_subscription_id` and `subscription_id` are provided, `committed_subscription_id` takes precedence. Both `self.committed_subscription_id` and `self.subscription_id` are assigned identically.
5. **Automatic Diagnostic Sanitization:** `diagnostic_payload` is processed through `sanitize_conflict_diagnostic_payload()`, ensuring raw emails, phone numbers, and API secrets cannot be leaked through reconciliation cases.

---

## 4. Classifier Assessment

### 4.1 `classify_client_error()` Review
The classifier in `webhook_engine.py` lines 331–432 inspects exceptions and maps them as follows:

| Input Exception Type / State | Mapped Classification | Error Code | Retryable? | Assessment |
|---|---|---|---|---|
| `RazorpayRateLimitError` | `RazorpayProviderTransientError` | `"429"` | `True` | **CORRECT:** HTTP 429 is transient throttling. |
| `RazorpayServer5xxError` | `RazorpayProviderTransientError` | Status code (e.g. `"500"`, `"502"`) | `True` | **CORRECT:** Gateway/server 5xx errors on idempotent webhook verification/reads are transient. |
| `RazorpayNetworkError(is_pre_dispatch=True)` | `RazorpayProviderTransientError` | `"PRE_DISPATCH_NETWORK_ERROR"` | `True` | **CORRECT:** Connect timeouts / pool timeouts occurred prior to remote transmission; safe to retry. |
| `RazorpayNetworkError(is_pre_dispatch=False)` | `RazorpayProviderPermanentError` | `"POST_DISPATCH_NETWORK_ERROR"` | `False` | **CORRECT:** Post-dispatch network failures (read timeouts) represent remote outcome uncertainty. Blind retry risks double mutation. Quarantines/fails closed. |
| `asyncio.TimeoutError` | `RazorpayProviderTransientError` | `"TIMEOUT"` | `True` | **CORRECT:** Standard asyncio timeout on async operations. |
| `RazorpayAuthError` | `RazorpayProviderPermanentError` | `"401"` | `False` | **CORRECT:** HTTP 401 is invalid API key; cannot succeed on retry. |
| `RazorpayBillingDisabledError` | `RazorpayProviderPermanentError` | `"RazorpayBillingDisabledError"` | `False` | **CORRECT:** Feature disabled; cannot succeed on retry. |
| `RazorpayConfigurationError` | `RazorpayProviderPermanentError` | `"RazorpayConfigurationError"` | `False` | **CORRECT:** Invalid credentials/keys; cannot succeed on retry. |
| `RazorpayAPIError(429)` | `RazorpayProviderTransientError` | `"429"` | `True` | **CORRECT:** Generic APIError with 429 is rate limit. |
| `RazorpayAPIError(>= 500)` | `RazorpayProviderTransientError` | Status code | `True` | **CORRECT:** Generic APIError with 5xx is server transient. |
| `RazorpayAPIError(400, 401, 403, 404, 422)` | `RazorpayProviderPermanentError` | Status code | `False` | **CORRECT:** Client 4xx error is deterministic bad request. |
| `RazorpayWebhookEngineError` | Returns `exc` unmodified | Pre-existing | Inherited | **CORRECT:** Pre-classified exceptions are passed through. |
| `StaleLeaseError` | Returns `exc` unmodified | Pre-existing | `False` | **CORRECT:** Stale lease passes through without misclassification. |
| Unknown / Non-Client Error (e.g. `KeyError`) | Returns `None` | `None` | — | **CORRECT:** Does not guess; allows caller generic classification. |

**Uncertainty vs. Retryability Check:**  
The review specifically verified that post-dispatch network errors (`is_pre_dispatch=False`) are **NOT** classified as transient retryable. They are mapped to `RazorpayProviderPermanentError`, ensuring that network uncertainty on remote mutations is never conflated with safe retryability.

### 4.2 Database Exception Classifiers Review
1. **`classify_db_exception(exc, *, max_nodes=32) -> DbErrorClassification`:**  
   Re-exports `_classify_db_exception` from `app.services.billing.webhook_errors`, preserving full SQLSTATE and unique constraint classification capability.
2. **`classify_db_error_to_razorpay_exception(exc, *, max_nodes=32) -> RazorpayDatabasePermanentError | RazorpayDatabaseRetryableError | None`:**
   - `DbRetryDisposition.PERMANENT` -> `RazorpayDatabasePermanentError` with `error_code=sqlstate` and `constraint_name`.
   - `DbRetryDisposition.RETRYABLE` / `RETRYABLE_WITH_ALERT` -> `RazorpayDatabaseRetryableError` with `error_code=sqlstate`.
   - `DbRetryDisposition.UNKNOWN` -> `None`. Does not convert unknown exceptions into database errors.

### 4.3 Sanitizer Delegation Review
`sanitize_error_message(err, max_chars=1024)` in `webhook_engine.py` delegates directly to `sanitize_billing_error_message(err, max_chars=max_chars)`.  
Independent tests confirm:
- Razorpay keys (`rzp_live_*`, `rzp_test_*`), Stripe keys, webhook secrets, passwords, Bearer tokens, emails, phones, and PANs are redacted.
- UUIDs, Unix epoch timestamps, prefixed entity IDs (`sub_*`, `pay_*`), SQLSTATEs, and constraint names are preserved byte-for-byte.

---

## 5. Package Export & Compatibility Assessment

### 5.1 Razorpay Package `__init__.py` Review
The review inspected lines 60–120 and lines 135–240 of `apps/api/app/services/billing/razorpay/__init__.py`.

1. **`__all__` Completeness:**
   - Every exported class and function exists in the package namespace.
   - Independent script confirmed `__all__` has **zero duplicate symbols**.
   - `__all__` is strictly sorted in alphabetical order.
2. **Canonical Re-exports:**
   - `rp.RazorpayWebhookEngineError is rzp_we.RazorpayWebhookEngineError` -> `True`
   - `rp.RazorpayWebhookHandlerError is rzp_we.RazorpayWebhookEngineError` -> `True`
   - `rp.RazorpayWebhookPermanentError is rzp_we.RazorpayWebhookPermanentError` -> `True`
   - `rp.RazorpayWebhookRetryableError is rzp_we.RazorpayWebhookRetryableError` -> `True`
   - `rp.StaleLeaseError is rzp_we.StaleLeaseError` -> `True`
   - `rp.RazorpayPayloadValidationError is rzp_we.RazorpayPayloadValidationError` -> `True`
   - `rp.RazorpayWebhookReconciliationConflictError is rzp_we.RazorpayWebhookReconciliationConflictError` -> `True`
3. **HTTP Ingress Isolation:**
   - `RazorpayWebhookError` is imported from `webhook_ingress.py`.
   - `issubclass(rp.RazorpayWebhookError, BillingWebhookError)` -> **`False`**
   - `issubclass(rp.RazorpayWebhookError, rp.RazorpayWebhookEngineError)` -> **`False`**
   - HTTP ingress exception hierarchy remains completely independent of worker/handler exceptions.
4. **Circular Imports:**
   - `client.py` imports neither `webhook_engine` nor `handler_base`.
   - `webhook_engine.py` performs a lazy function-scoped import of `client.py` inside `classify_client_error()`.
   - Importing `app.services.billing.razorpay`, `app.services.billing.razorpay.webhook_engine`, and `app.services.billing.razorpay.handler_base` in any order succeeds cleanly without cycles.

---

## 6. Stage 1 Compatibility & Preservation

1. **Byte-for-Byte Preservation:**
   `apps/api/app/services/billing/webhook_errors.py` SHA-256 hash was independently computed:
   `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033`
   Matches the baseline accepted Stage 1 hash byte-for-byte.
2. **Stage 1 Test Suite Execution:**
   Running `pytest test_billing_webhook_errors.py` executed **163 tests, 100% passing** in 1.87s.
3. **F-01 Compound Secret Sanitization:**
   Verified directly on:
   - `razorpay_key_secret=mySuperSecret999` -> `razorpay_key_secret=[REDACTED]`
   - `my_webhook_secret=secret123` -> `my_webhook_secret=[REDACTED]`
   - `webhook_signature=signature123` -> `webhook_signature=[REDACTED]`
   - `api_secret=secret123` -> `api_secret=[REDACTED]`
   - `client_secret=secret123` -> `client_secret=[REDACTED]`
   - `access_token=token123` -> `access_token=[REDACTED]`
   - `refresh_token=token123` -> `refresh_token=[REDACTED]`
4. **SQLSTATE & 23505 Buckets:**
   - `40001`, `40P01`, `55P03` -> `RETRYABLE`
   - `53300` -> `RETRYABLE_WITH_ALERT`
   - `P0001`, `23502`, `23503`, `23514` -> `PERMANENT`
   - `uq_billing_payments_provider_payment` -> `IDEMPOTENT_CONVERGENCE`
   - `uq_subscriptions_provider_subscription` -> `CONCURRENCY_RACE`
   - `uq_org_active_subscription` -> `PERMANENT_INTEGRITY_CONFLICT`
   - `unknown_constraint` -> `UNMAPPED_UNIQUE_VIOLATION`

---

## 7. Regression & Scope Review

### 7.1 Independent Test Evidence

All test runs were executed independently using Python virtualenv `D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe`:

```powershell
# 1. Dedicated Stage 2 Suite
pytest tests/test_razorpay_webhook_exceptions.py
# Result: 36 passed in 1.60s

# 2. Handler Foundation Suite
pytest tests/test_razorpay_handler_foundation.py
# Result: 24 passed in 1.69s

# 3. Stage 1 Exception Foundation Suite
pytest tests/test_billing_webhook_errors.py
# Result: 163 passed in 1.87s

# 4. Combined Webhook & Billing Suite (9 files)
pytest tests/test_billing_webhook_errors.py tests/test_razorpay_webhook_exceptions.py tests/test_razorpay_handler_foundation.py tests/test_razorpay_agreement_handlers.py tests/test_razorpay_webhook_worker.py tests/test_razorpay_webhook_ingestion.py tests/test_razorpay_client.py tests/test_razorpay_config.py tests/test_stripe_webhook_engine.py
# Result: 326 passed in 6.25s

# 5. CI Step 13 Offline Regression Suite (7 files)
pytest tests/test_postgres_test_guard.py tests/test_payment_association_migration.py tests/test_provider_neutral_billing_models.py tests/test_provider_neutral_migration.py tests/test_razorpay_customer.py tests/test_razorpay_checkout.py tests/test_billing_api.py
# Result: 375 passed in 36.78s
```

### 7.2 Non-Overlapping Test Count Accounting
To avoid conflating re-executions with unique tests:
- **Suite A (Webhook & Billing Test Files):**
  - `test_billing_webhook_errors.py`: 163 tests
  - `test_razorpay_webhook_exceptions.py`: 36 tests
  - `test_razorpay_handler_foundation.py`: 24 tests
  - `test_razorpay_agreement_handlers.py`: 18 tests
  - `test_razorpay_webhook_worker.py`: 23 tests
  - `test_razorpay_webhook_ingestion.py`: 23 tests
  - `test_razorpay_client.py`: 13 tests
  - `test_razorpay_config.py`: 8 tests
  - `test_stripe_webhook_engine.py`: 18 tests
  - **Subtotal Suite A:** **326 unique tests**
- **Suite B (CI Step 13 Offline Files):**
  - `test_postgres_test_guard.py`: 117 tests
  - `test_payment_association_migration.py`: 18 tests
  - `test_provider_neutral_billing_models.py`: 16 tests
  - `test_provider_neutral_migration.py`: 8 tests
  - `test_razorpay_customer.py`: 90 tests
  - `test_razorpay_checkout.py`: 81 tests
  - `test_billing_api.py`: 45 tests
  - **Subtotal Suite B:** **375 unique tests**
- **Grand Total Unique Tests:** 326 + 375 = **701 unique tests, 100% passing**.

### 7.3 Scope Discipline Confirmation
- **No PostgreSQL 16 Claims:** All tests are mock/offline unit tests. Zero live PostgreSQL 16 claims are made.
- **Worker Logic Untouched:** `razorpay_webhook_worker.py` transaction engine and process loop are completely unmodified (deferred to Stage 4).
- **Handler Conflict Staging Untouched:** `agreement_handlers.py` and `stage_or_update_payment_reconciliation()` are completely unmodified (deferred to Stage 3).
- **Stripe Hierarchy Untouched:** `stripe_webhook_engine.py` and Stripe handlers are unmodified.
- **Migrations & CI Untouched:** `alembic/versions/` and `.github/workflows/ci.yml` are unmodified.

---

## 8. Findings by Severity

| ID | Severity | Category | Description | Disposition |
|---|---|---|---|---|
| **INF-01** | Informational | Test Accounting | `test_billing_webhook_errors.py` hash changed from `592DAB...` to `7130AB...` because `test_existing_provider_hierarchies_unmodified_in_stage1` was updated to assert Stage 2 reparenting of Razorpay webhook engine errors onto `BillingWebhookError`. | Verified and documented; expected change. |
| **INF-02** | Informational | Test Accounting | `test_razorpay_handler_foundation.py` added 3 tests (`UNIT-EXC-01`, `UNIT-EXC-02`, `UNIT-API-01`) per blueprint §9 Stage 2, bringing its test count from 21 to 24. | Verified and documented; expected change. |

**Critical Findings:** 0  
**High Severity Findings:** 0  
**Medium Severity Findings:** 0  
**Low Severity Findings:** 0  

---

## 9. Stage 3 Readiness Recommendation

D-04A Stage 2 satisfies all architectural requirements, invariants, and safety boundaries specified in `D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` and `D2_D04A1_IMPLEMENTATION_BLUEPRINT.md`.

### Explicit Recommendation:
**PROCEED TO STAGE 3 AUTHORIZATION**

Stage 3 scope will cover:
1. Implementation of native PostgreSQL `build_payment_reconciliation_upsert_stmt()` and `upsert_payment_reconciliation()` with `ON CONFLICT ON CONSTRAINT uq_payment_reconciliations_case_key DO UPDATE`.
2. Updating `agreement_handlers.py` conflict paths to raise `RazorpayWebhookReconciliationConflictError` cleanly with sanitized diagnostics and `raw_event_id=ctx.event_id`, without uncommitted `sub.id` FKs.
3. Updating observational reconciliation cases to use `upsert_payment_reconciliation()`.
4. Adding `UNIT-REC-01` PostgreSQL AST dialect compilation tests.
