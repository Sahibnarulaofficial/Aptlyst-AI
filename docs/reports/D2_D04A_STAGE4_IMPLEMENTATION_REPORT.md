# D-04A Stage 4 — Atomic Razorpay Webhook Worker Transaction Engine & Worker Loop Hardening Implementation Report

**Date:** 2026-11-14  
**Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`  
**Branch:** `implementation/d04a-stage1-exception-foundation`  
**Baseline HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`  
**Python Runtime:** `D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe` (Python 3.11.0, pytest-8.3.5)  
**Execution Environment:** Windows Server / PowerShell with `-p no:cacheprovider` and `PYTHONPATH=".;app"`  

---

## 1. Executive Summary

This report documents the authorized implementation of **D-04A Stage 4: Atomic Razorpay Webhook Worker Transaction Engine and Worker Loop Hardening**, addressing the critical transaction split vulnerability originally identified in the D-04 Razorpay Webhook Safety Investigation:

- **Root Flaw Addressed:** In the prior implementation, `RazorpayWebhookWorker.process_one()` executed domain handler logic and webhook outcome recording in two completely disconnected database sessions (`handler_db` and `outcome_db`). If handler execution succeeded or failed partially, or if network/database faults occurred between the two sessions, domain mutations could roll back while `outcome_db` marked the event as processed, or domain mutations committed while `outcome_db` failed, resulting in data desynchronization and missed retries.
- **Architectural Solution:** Implemented `execute_claimed_razorpay_webhook_event()` in `app.services.billing.razorpay.webhook_engine`, providing a single outer PostgreSQL transaction (`work_db`) that maintains row lock on `BillingWebhookEvent` throughout execution. Domain business writes execute inside an inner PostgreSQL savepoint (`work_db.begin_nested()`).
- **Conflict & Error Resilience:** If a domain conflict (`RazorpayWebhookReconciliationConflictError`) or failure occurs, the inner savepoint rolls back in-memory domain changes (`Subscription`, etc.) cleanly. The outer `work_db` transaction remains healthy to atomically upsert the `PaymentReconciliation` audit record and commit the final event status (`quarantined` or `failed`) in the exact same commit.
- **Lease Fencing & Cancellation Integrity:** Fencing assertion (`Level -1` row lock) verifies lease token and expiry before executing and before committing. `asyncio.CancelledError` immediately rolls back `work_db` without writing outcomes, leaving the lease token intact for automatic lease expiry recovery (Option B).
- **Zero Secret Leakage:** Error messages across database persistence and logging are sanitized via `sanitize_billing_error_message()`. Full tracebacks and secret-bearing variables are excluded (`exc_info=False`).
- **Complete Test Verification:**
  - Group A (Canonical Webhook & Billing Suite): **337 tests passed** (including 10 new comprehensive `MOCK-WRK-01..10` worker transaction tests), 0 failed.
  - Group B (CI Step 13 Offline Suite): **375 tests passed**, 0 failed.
  - Total offline test surface: **712 passing tests** (100% pass rate).
  - Main repository and sibling worktrees remain completely untouched.

---

## 2. Worktree & Baseline Environment Verification

### 2.1 Workspace Isolation
All Stage 4 edits and tests were conducted strictly within the designated worktree:
- **Active Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`
- **Active Branch:** `implementation/d04a-stage1-exception-foundation`
- **Baseline HEAD:** `497852e19360d85787107dbae6921519074f0773`
- **Main Workspace Untouched:** `D:\Projects\V1\Aptlyst-AI` (clean, no uncommitted changes)
- **Validation Worktree Untouched:** `D:\Projects\V1\Aptlyst-AI-pg16-validation`

### 2.2 Execution Parameters
Commands were executed via the project venv interpreter with Pytest cache provider disabled to prevent cross-worktree lock contention on Windows:
```powershell
Set-Location "D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16\apps\api"
$env:PYTHONPATH = ".;app"
& "D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe" -B -m pytest <test_files> -p no:cacheprovider
```

---

## 3. Architecture: Single Atomic Transaction vs Two-Transaction Flaw

### 3.1 The Prior Two-Transaction Flaw (D-04 Defect)
In the baseline design:
1. `claim_one()` leased the event and committed.
2. `process_one()` opened Session 1 (`async with session_factory() as handler_db:`):
   - Executed handler domain logic (e.g. creating `Subscription`, creating customer links).
   - Committed Session 1 to PostgreSQL.
3. `process_one()` opened Session 2 (`async with session_factory() as outcome_db:`):
   - Called `record_razorpay_webhook_outcome(...)`.
   - Updated `BillingWebhookEvent` to `processed`.
   - Committed Session 2 to PostgreSQL.

**Vulnerabilities in Baseline:**
- **Crash between Session 1 & Session 2:** Domain mutations committed to DB, but event outcome never recorded. Event lease eventually expired; second worker re-processed event, causing duplicate execution or primary key collisions.
- **Rollback in Session 1 with Partial Commits:** If handler did an internal commit or flush that partially failed, or if an unexpected exception occurred after business writes, Session 2 marked the event `failed`, but Session 1 might have left orphan records.
- **Reconciliation Disconnect:** Reconciliation cases were staged in `handler_db`. If `handler_db` rolled back, the reconciliation record was also lost, preventing human operator review.

### 3.2 The Stage 4 Atomic Architecture
In Stage 4, `RazorpayWebhookWorker.process_one()` delegates directly to `execute_claimed_razorpay_webhook_event()`:

```
[Outer Transaction: work_db]
│  Acquire SELECT ... FOR UPDATE on BillingWebhookEvent
│  Assert fencing (lease_token, locked_by, lease_expires_at)
│  Validate signature & merchant scope
│
├── [Inner Savepoint: work_db.begin_nested()]
│   │  Execute domain handler (Subscription, Customer, etc.)
│   │
│   ├── Case A: Handler Succeeds
│   │   Savepoint commits into work_db
│   │   BillingWebhookEvent.status = processed, lease cleared
│   │   Outer commit -> BOTH domain mutations & event status commit together!
│   │
│   └── Case B: Handler Fails (Conflict / Internal Error / Permanent)
│       Savepoint rolls back -> Domain mutations discarded cleanly!
│       Outer work_db transaction remains valid!
│       If Conflict:
│           Execute native Stage 3 upsert_payment_reconciliation(work_db, ...)
│           BillingWebhookEvent.status = quarantined
│       If Internal Error:
│           BillingWebhookEvent.status = failed, next_retry_at = now + backoff
│       If Permanent Error:
│           BillingWebhookEvent.status = quarantined
│       Outer commit -> Reconciliation & event outcome commit together!
│
└── [Cancellation: asyncio.CancelledError]
    work_db.rollback() immediately
    Zero writes to DB; lease expires naturally for safe recovery
    re-raise CancelledError (Option B)
```

---

## 4. Phase 3 Engine Implementation (`execute_claimed_razorpay_webhook_event`)

Located in `apps/api/app/services/billing/razorpay/webhook_engine.py`:
- Signature:
  ```python
  async def execute_claimed_razorpay_webhook_event(
      session_factory: Callable[[], AsyncSession],
      claimed: ClaimedWebhookEvent,
      handler: RazorpayWebhookHandler | None,
      *,
      max_attempts: int = MAX_WEBHOOK_ATTEMPTS,
      now_fn: Callable[[], datetime] | None = None,
      expected_merchant_id: str | None = None,
      expected_environment: str | None = None,
  ) -> ProcessResult
  ```

### 4.1 Step-by-Step Execution Lifecycle
1. **Clock Resolution:** Resolves deterministic UTC timestamp via `now_fn` or `datetime.now(timezone.utc)`.
2. **Session & Lock Acquisition:** Opens `async with session_factory() as work_db:` and queries `BillingWebhookEvent` with `SELECT ... WHERE id = claimed.event_id WITH FOR UPDATE`.
3. **Fencing Assertion:** Verifies:
   - `event.lease_token == claimed.lease_token`
   - `event.locked_by == claimed.locked_by`
   - `event.lease_expires_at >= now`  
   If any condition fails, rolls back immediately and returns `ProcessResult(success=False, lease_lost=True, failure_category="stale_lease")`.
4. **Signature & Scope Verification:**
   - Asserts `event.signature_verified is True`. If unverified, immediately quarantines without retries (`failure_category="unverified_event"`).
   - Asserts merchant ID match (`event.merchant_account_id == expected_merchant_id`). If mismatched, immediately quarantines (`failure_category="merchant_mismatch"`).
5. **Handler Dispatch & Savepoint:**
   - If no handler registered for event type, marks quarantined with `quarantine_reason="Unknown event type"`.
   - If handler registered, opens `async with work_db.begin_nested():` and awaits `handler(claimed, work_db)` or `handler(claimed.payload, work_db)`.
6. **Happy Path:**
   - Validates unexpired lease prior to final commit.
   - Updates `event.status = processed`, `event.processed_at = now`, clears all lease fields (`lease_token = None`, `locked_by = None`, `lease_expires_at = None`).
   - Executes `await work_db.commit()`.
7. **Failure Classification & Outer Commit:**
   - Handler exception triggers savepoint rollback.
   - Classifies error into:
     - `StaleLeaseError`: rolls back outer tx, reports `lease_lost=True`.
     - `RazorpayWebhookReconciliationConflictError`: checks ED-02 race qualification or performs `upsert_payment_reconciliation` + quarantine.
     - `RazorpayWebhookPermanentError`: marks quarantined immediately.
     - `RazorpayWebhookRetryableError` or unexpected exception: applies exponential backoff (5s / 15s / 45s / 120s) or quarantines if `attempts >= max_attempts`.
   - Executes `await work_db.commit()`.

---

## 5. Savepoint Isolation & Conflict Rollback (`begin_nested()`)

The inner savepoint (`work_db.begin_nested()`) provides crucial ACID isolation:
- **Clean In-Memory & DB Rollback:** When an unhandled error or conflict occurs inside the handler, Python context manager exit triggers `ROLLBACK TO SAVEPOINT`. Any table rows added or modified during handler execution (`subscriptions`, `payments`, `audit_logs`) are rolled back.
- **Outer Session Usability:** Unlike a top-level rollback which invalidates the entire database transaction, a savepoint rollback leaves `work_db` in a healthy, active state.
- **Verified by Test MOCK-WRK-02 & MOCK-WRK-04:**
  - In `test_mock_wrk_02`, an active `Subscription` was added by the handler before raising `RazorpayWebhookReconciliationConflictError`. After `process_one()`, the `Subscription` was confirmed NOT committed to the repository, while the `PaymentReconciliation` record was confirmed committed in the outer transaction.
  - In `test_mock_wrk_04`, a `Subscription` was added before raising `RuntimeError`. After rollback, the `Subscription` was discarded and the event was committed with `status=failed`.

---

## 6. PaymentReconciliation Atomic Upsert Integration

In Stage 3, `upsert_payment_reconciliation()` was introduced using native PostgreSQL `INSERT ... ON CONFLICT (case_key) DO UPDATE`:
- In Stage 4, `execute_claimed_razorpay_webhook_event` imports `upsert_payment_reconciliation` from `app.services.billing.razorpay.handler_base`.
- When a `RazorpayWebhookReconciliationConflictError` occurs (e.g. metadata tenant mismatch, customer mismatch):
  1. Savepoint rolls back uncommitted domain objects.
  2. Outer transaction calls:
     ```python
     await upsert_payment_reconciliation(
         db=work_db,
         case_key=handler_exc.case_key,
         reason=handler_exc.reason,
         provider=claimed.provider,
         merchant_account_id=claimed.merchant_account_id,
         environment=claimed.environment,
         organization_id=handler_exc.organization_id,
         raw_event_id=claimed.event_id,
         diagnostic_data=diag_payload,
         status=ReconciliationStatus.pending_review,
         now=now_outcome,
     )
     ```
  3. `BillingWebhookEvent.status` is set to `quarantined`, `quarantine_reason` set to conflict summary, lease fields cleared.
  4. Single `await work_db.commit()` commits the reconciliation record and the quarantined event status simultaneously.

---

## 7. Fencing & Stale Lease Protection

Fencing is enforced at two distinct gates within `execute_claimed_razorpay_webhook_event()`:
1. **Pre-Execution Lock Gate:**
   - Under `SELECT ... FOR UPDATE`, checks `event.lease_token == claimed.lease_token`, `event.locked_by == claimed.locked_by`, and `event.lease_expires_at >= now`.
   - If a competing worker stole the lease after expiry, execution aborts immediately with `lease_lost=True`.
2. **Pre-Commit Gate:**
   - After handler execution, before `work_db.commit()`, re-checks `event.lease_expires_at >= now`.
   - If execution took longer than `lease_duration_seconds`, the transaction is rolled back (`await work_db.rollback()`) to prevent committing stale state.
3. **Verified by Test MOCK-WRK-09:**
   - A superseded event (`worker_2` holds fresh lease) rejected a stale worker with `lease_lost=True` and `failure_category="stale_lease"` without mutating any database records.

---

## 8. Cancellation Handling (Option B: `asyncio.CancelledError` Propagation)

Following approved architecture Option B:
- **Clean Worker Shutdown:** When the task runner or OS cancels the worker coroutine:
  1. `async with work_db.begin_nested():` catches `asyncio.CancelledError`.
  2. Outer `await work_db.rollback()` executes immediately.
  3. No outcome is written: `event.status` remains `pending` or `failed`, `event.attempts` is NOT incremented, and `event.lease_token` is preserved.
  4. The lease expires naturally based on `lease_expires_at`, allowing recovery workers to safely claim it without counting against the retry threshold.
  5. `CancelledError` is re-raised out of `execute_claimed_razorpay_webhook_event`, `process_one()`, and `start()`.
- **Verified by Test MOCK-WRK-07:**
  - `test_mock_wrk_07_ed05_cancelled_error_propagation` confirms that raising `asyncio.CancelledError` inside the handler re-raises out of `worker.process_one()`, leaves `event.status=pending`, leaves `attempts=0`, and rolls back all database mutations.

---

## 9. Sanitization & Zero Secret Leakage (Option B / ED-06)

To guarantee zero credential or token leakage across all logs, telemetry, and database fields:
1. **Sanitized Persistence:**
   `event.error_message = sanitize_billing_error_message(str(handler_exc))`
   Strips `key_secret`, `webhook_secret`, `api_secret`, `access_token`, and other sensitive tokens.
2. **Sanitized Logging:**
   All `logger.warning()` and `logger.error()` invocations pass strings through `sanitize_billing_error_message(str(exc))`.
3. **No Traceback Local Leakage:**
   `exc_info=True` is removed from webhook loop log sites to prevent leaking local variables (such as raw webhook payloads with secret parameters).
4. **Verified by Test MOCK-WRK-08:**
   - A handler raising `RuntimeError("API failure with key_secret=mySuperSecret999 and webhook_secret=whsec_TopSecret123")` was tested with `caplog`.
   - Persisted `event.error_message` contained `[REDACTED]` and contained neither `mySuperSecret999` nor `whsec_TopSecret123`.
   - Captured logs similarly contained neither secret.

---

## 10. ED-02 Single 2-Second Race Retry Qualification

When customer creation happens asynchronously via checkout, a webhook event might arrive before the customer provisioning record has completed committing:
- **Qualification Function:** `_check_unbound_race_qualification()`
  - Checks if `claimed.attempts == 0` and `handler_exc.reason == "unbound_provider_customer"`.
  - Queries `BillingCustomerProvision` for matching `provider_customer_id` in status `resolved` or `pending`.
  - Queries `BillingCheckoutAttempt` for matching `provider_subscription_id` or `provider_customer_id` in status `creating`, `recovery_required`, or `open`.
- **Race Retry Behavior:**
  - If qualifying record exists: sets `attempts = 1`, `status = failed`, `next_retry_at = now + 2s`.
  - Does NOT stage a reconciliation case (avoids false-alarm review tickets).
  - Returns `ProcessResult(status=failed, failure_category="concurrent_binding_race")`.
- **Non-Qualifying / Exhausted Attempts:**
  - If `attempts >= 1` or no in-flight record exists: immediately stages `upsert_payment_reconciliation` and marks `quarantined`.
- **Verified by Test MOCK-WRK-03:**
  - An event with `attempts=0` and a qualifying in-flight `BillingCustomerProvision` scheduled a 2-second retry without staging reconciliation.

---

## 11. RazorpayWebhookWorker Refactoring & Worker Loop Hardening

`apps/api/app/workers/razorpay_webhook_worker.py` was refactored:
1. **`process_one()` Delegation:**
   Replaced manual two-session execution with a direct call to `execute_claimed_razorpay_webhook_event`:
   ```python
   async def process_one(self) -> ProcessResult | None:
       claimed = await self.claim_one()
       if claimed is None:
           return None

       handler = self.dispatcher.get_handler(claimed.event_type)

       try:
           return await execute_claimed_razorpay_webhook_event(
               session_factory=self.session_factory,
               claimed=claimed,
               handler=handler,
               max_attempts=self.max_attempts,
               now_fn=self._get_now,
               expected_merchant_id=self.merchant_account_id,
               expected_environment=self.environment,
           )
       except asyncio.CancelledError:
           raise
       except Exception as exc:
           logger.error(
               "Unexpected error during execute_claimed_razorpay_webhook_event for event %s: %s",
               claimed.event_id,
               sanitize_billing_error_message(str(exc)),
           )
           return None
   ```
2. **`claim_one()` Cancellation Propagation:**
   Ensured `asyncio.CancelledError` is re-raised rather than swallowed as a None claim.
3. **`start()` Polling Loop Hardening:**
   Wrapped main polling loop in `try ... except asyncio.CancelledError: logger.info(...); raise finally: logger.info(...)`.
4. **`run_worker()` Signal Handling & Cancel Propagation:**
   Added `merchant_account_id` parameter and preserved `asyncio.CancelledError` propagation.

---

## 12. Package Exports & API Surface (`__all__`)

To maintain clean package boundaries:
1. **`app.services.billing.razorpay.webhook_engine`:**
   Exported `execute_claimed_razorpay_webhook_event`, `ProcessResult`, `ClaimedWebhookEvent`, `ProcessingOutcome`, `RazorpayWebhookDispatcher`, etc.
2. **`app.services.billing.razorpay` (`__init__.py`):**
   Imported and re-exported `execute_claimed_razorpay_webhook_event` and `RazorpayWebhookHandler` in `__all__`.
3. **`app.workers` (`__init__.py`):**
   Added `RazorpayWebhookWorker` to `__all__` and implemented lazy `__getattr__` import for fast worker loading.

---

## 13. Test Suite Architecture & Results

### 13.1 Group A: Canonical Webhook & Billing Suite (9 Files)
Executed command:
```powershell
Set-Location "D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16\apps\api"
$env:PYTHONPATH = ".;app"
& "D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe" -B -m pytest `
    tests/test_billing_webhook_errors.py `
    tests/test_razorpay_webhook_exceptions.py `
    tests/test_razorpay_handler_foundation.py `
    tests/test_razorpay_agreement_handlers.py `
    tests/test_razorpay_webhook_worker.py `
    tests/test_razorpay_webhook_ingestion.py `
    tests/test_razorpay_client.py `
    tests/test_razorpay_config.py `
    tests/test_stripe_webhook_engine.py `
    -p no:cacheprovider
```

**Results:**
- `test_billing_webhook_errors.py`: 163 passed
- `test_razorpay_webhook_exceptions.py`: 36 passed
- `test_razorpay_handler_foundation.py`: 25 passed
- `test_razorpay_agreement_handlers.py`: 18 passed
- `test_razorpay_webhook_worker.py`: **33 passed** (23 original + 10 new Stage 4 tests)
- `test_razorpay_webhook_ingestion.py`: 23 passed
- `test_razorpay_client.py`: 13 passed
- `test_razorpay_config.py`: 8 passed
- `test_stripe_webhook_engine.py`: 18 passed
- **Group A Total:** **337 passed in 9.73s (exit code 0)**

### 13.2 Group B: CI Step 13 Offline Regression Suite (7 Files)
Executed command:
```powershell
Set-Location "D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16\apps\api"
$env:PYTHONPATH = ".;app"
& "D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe" -B -m pytest `
    tests/test_postgres_test_guard.py `
    tests/test_payment_association_migration.py `
    tests/test_provider_neutral_billing_models.py `
    tests/test_provider_neutral_migration.py `
    tests/test_razorpay_customer.py `
    tests/test_razorpay_checkout.py `
    tests/test_billing_api.py `
    -p no:cacheprovider
```

**Results:**
- `test_postgres_test_guard.py`: 118 passed
- `test_payment_association_migration.py`: 17 passed
- `test_provider_neutral_billing_models.py`: 15 passed
- `test_provider_neutral_migration.py`: 9 passed
- `test_razorpay_customer.py`: 92 passed
- `test_razorpay_checkout.py`: 79 passed
- `test_billing_api.py`: 45 passed
- **Group B Total:** **375 passed in 53.50s (exit code 0)**

### 13.3 Total Combined Test Surface
- **Group A (337) + Group B (375) = 712 unique passing test items.**
- 100% pass rate; zero regressions.

---

## 14. Mock Worker Test Coverage (`MOCK-WRK-01..10`)

All 10 required mock test scenarios were authored in `apps/api/tests/test_razorpay_webhook_worker.py`:

| Test Name | Identifier | Primary Verification Target | Status |
|---|---|---|---|
| `test_mock_wrk_01_atomic_success` | MOCK-WRK-01 | Single atomic transaction commits domain changes and marks event processed | **PASSED** |
| `test_mock_wrk_02_conflict_error_savepoint_rollback_and_reconciliation_upsert` | MOCK-WRK-02 | Inner savepoint rollback, domain mutation discard, outer reconciliation upsert + quarantine | **PASSED** |
| `test_mock_wrk_03_ed02_unbound_customer_race_retry` | MOCK-WRK-03 | In-flight customer provision detection, 2s retry scheduling, zero reconciliation staging | **PASSED** |
| `test_mock_wrk_04_ed01_unexpected_internal_error_savepoint_rollback` | MOCK-WRK-04 | Savepoint rollback on unexpected error, 5s backoff delay, sanitized error logging | **PASSED** |
| `test_mock_wrk_05_permanent_error_quarantines_without_retry` | MOCK-WRK-05 | Permanent error quarantines immediately on attempt 1 without retry | **PASSED** |
| `test_mock_wrk_06_unknown_event_type_quarantines` | MOCK-WRK-06 | Unregistered event type immediately quarantines without retry | **PASSED** |
| `test_mock_wrk_07_ed05_cancelled_error_propagation` | MOCK-WRK-07 | CancelledError rolls back transaction without outcome writes and re-raises | **PASSED** |
| `test_mock_wrk_08_ed06_logging_sanitization` | MOCK-WRK-08 | Secrets stripped from database error_message and captured log output | **PASSED** |
| `test_mock_wrk_09_stale_lease_rejection` | MOCK-WRK-09 | Superseded lease rejected before execution/commit without DB mutations | **PASSED** |
| `test_mock_wrk_10_commit_failure_fallback_handling` | MOCK-WRK-10 | Outer commit failure activates fenced fallback transaction to record failure | **PASSED** |

---

## 15. Verification of Non-Interference

Strict non-interference checks were conducted:
1. **Main Worktree:** `D:\Projects\V1\Aptlyst-AI` was verified with `git status` — no files touched.
2. **Validation Worktree:** `D:\Projects\V1\Aptlyst-AI-pg16-validation` — no files touched.
3. **Stripe Codebase:** `apps/api/app/services/billing/stripe/*` and `apps/api/tests/test_stripe_webhook_engine.py` — zero modifications.
4. **CI Workflow Files:** `.github/workflows/*` — zero modifications.
5. **No Network Access / External Services:** All tests executed completely offline using test doubles and mocks.
6. **No Git Actions:** No commits, pushes, branch creations, stashes, or resets were performed.

---

## 16. Git Working-Tree Audit

### 16.1 Modified Files
```
 M apps/api/app/services/billing/razorpay/__init__.py
 M apps/api/app/services/billing/razorpay/agreement_handlers.py
 M apps/api/app/services/billing/razorpay/handler_base.py
 M apps/api/app/services/billing/razorpay/webhook_engine.py
 M apps/api/app/workers/__init__.py
 M apps/api/app/workers/razorpay_webhook_worker.py
 M apps/api/tests/test_razorpay_agreement_handlers.py
 M apps/api/tests/test_razorpay_handler_foundation.py
 M apps/api/tests/test_razorpay_webhook_worker.py
```

### 16.2 Untracked Files
```
?? STEP_5_2D_3_3B_1E_Z1_D2_STAGE1_EXCEPTION_DIAGNOSTICS_FIX_REPORT.md
?? STEP_5_2D_3_3B_1E_Z2_D2_STAGE1_CUSTOMER_PG16_TEST_REPORT.md
?? STEP_5_2D_3_3B_1E_Z3_D2_STAGE1_CI_INTEGRATION_REPORT.md
?? STEP_5_2D_3_3B_1E_Z4_D2_STAGE1_COMPLETION_READINESS_REPORT.md
?? STEP_5_2D_3_3B_1E_Z5_D2_STAGE1_INDEPENDENT_IMPLEMENTATION_AUDIT.md
?? STEP_5_2D_3_3B_1E_Z6_D2_STAGE1_IMPLEMENTATION_COMMIT_REPORT.md
?? STEP_5_2D_3_3B_1E_Z7_D2_STAGE1_PUSH_REPORT.md
?? STEP_5_2D_3_3B_1E_Z8_D2_STAGE1_MAPPER_REGISTRATION_FIX_REPORT.md
?? apps/api/app/services/billing/webhook_errors.py
?? apps/api/tests/test_billing_webhook_errors.py
?? apps/api/tests/test_razorpay_webhook_exceptions.py
?? docs/reports/
```

All existing Stage 1, Stage 2, and Stage 3 implementation reports and code artifacts remain preserved.

---

## 17. Stage 5 Concurrency Suite Readiness & Boundaries

Stage 4 implementation is complete. Per the authorization guidelines:
- **Stage 5 Deferral:** Stage 5 (PostgreSQL 16 multi-connection concurrency suite, real database integration tests, and CI gate wiring) is strictly deferred.
- **Next Step:** Awaiting independent review of the Stage 4 implementation before any further stage is initiated.
