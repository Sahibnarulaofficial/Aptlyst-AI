# D-04A Stage 4 — Independent Implementation Review
## Atomic Razorpay Webhook Worker Transaction Engine & Worker Loop Hardening

**Review Date:** 2026-11-14  
**Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`  
**Branch:** `implementation/d04a-stage1-exception-foundation`  
**Baseline HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`  
**Implementation Report Audited:** `docs/reports/D2_D04A_STAGE4_IMPLEMENTATION_REPORT.md`  
**Authoritative Architectural References:**
- `docs/reports/D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` (§7.1, §7.2, §7.3, §7.4, §8.1, §8.2)
- `docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` (§4.1 ED-01..ED-06, §9 Stage 4, §10.2)
- `docs/reports/D2_D04A_STAGE3_INDEPENDENT_REVIEW.md`
- `docs/reports/D2_D04A_STAGE3_TEST_COUNT_RECONCILIATION.md`

---

## 1. Executive Verdict: ACCEPT

Following a comprehensive, read-only architectural, security, concurrency, and empirical audit of the completed **D-04A Stage 4** implementation, the independent verdict is **ACCEPT**.

The implementation strictly satisfies all Stage 4 mandates defined in the approved architectural design and implementation blueprint without regressions, scope creep, or boundary violations:
1. **Single Atomic Transaction Invariant:** `execute_claimed_razorpay_webhook_event()` replaces the vulnerable two-session design with a single outer database transaction (`work_db`). Handler business mutations, reconciliation upserts (if any), and final webhook event status commits are executed and committed together in one atomic commit.
2. **Inner Savepoint Isolation (`begin_nested()`):** Domain handlers execute within an inner PostgreSQL savepoint. Upon handler exception or conflict, the savepoint rolls back in-memory domain changes (`Subscription`, etc.) cleanly, keeping `work_db` in a healthy, active state to persist reconciliation and event outcome atomically.
3. **External I/O Boundary Integrity:** Zero external HTTP/API network calls occur during webhook event execution or while database row locks are held. Webhook processing is strictly local database operations on previously ingested event rows.
4. **Two-Gate Lease Fencing Protection:** Pre-execution (`SELECT ... FOR UPDATE`) and pre-commit checks strictly validate `lease_token`, `locked_by`, and `lease_expires_at >= now`. Stale or superseded workers are rejected with `lease_lost=True` and cannot commit mutations or overwrite newer leases.
5. **Clean Cancellation Propagation (Option B):** `asyncio.CancelledError` immediately executes `work_db.rollback()`. No outcome writes or attempt increments occur, preserving the event's original state for natural lease expiry recovery. `CancelledError` propagates cleanly out of `process_one()` and `start()`.
6. **Zero Secret Leakage (Option B / ED-06):** Database error messages and all worker log sites pass through `sanitize_billing_error_message()`. Full tracebacks and local scope inspection (`exc_info=True`) have been removed from polling loops.
7. **ED-02 Bounded Race Retry Qualification:** Unbound customer conflicts on attempt 0 query for in-flight/resolved `BillingCustomerProvision` or `BillingCheckoutAttempt` records. If found, a 2-second retry is scheduled (`attempts=1`, `status=failed`, `next_retry_at=now+2s`) without staging reconciliation cases. On attempt >= 1 or non-qualifying conflicts, immediate reconciliation upsert and quarantine occur.
8. **Lock Order Adherence:** Strict global lock hierarchy is preserved: Level -1 (`BillingWebhookEvent`) -> Level 0 (advisory lock) -> Level 2 (`Subscription`) -> Level 3 (`PaymentReconciliation`). No inversions or deadlocks exist.
9. **Stage 1–3 Immutability:** Stage 1, 2, and 3 implementation files, reports, and tests are byte-for-byte identical to their accepted states (verified via SHA-256 hashes). Sibling worktrees, Stripe codebase, database models, migrations, and CI workflows remain completely untouched.
10. **Empirical Verification:** All 712 unique test node IDs across the offline test surface (Group A: 337 tests, Group B: 375 tests) passed 100% with zero failures and zero skips.

---

## 2. Repository Boundaries & Working-Tree Integrity

### 2.1 Active Worktree & Git Status
- **Active Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`
- **Active Branch:** `implementation/d04a-stage1-exception-foundation`
- **Verified HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`
- **Tracked Modifications in Active Worktree:**
  ```text
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
- **Tracked Diff Statistics against Baseline HEAD:**
  `9 files changed, 1424 insertions(+), 283 deletions(-)`

### 2.2 Sibling Worktree Isolation
Sibling worktrees were inspected and confirmed completely unmodified by Stage 4 operations:
- `D:\Projects\V1\Aptlyst-AI` (`main` @ `027ab7a`): Clean; preserves existing dirty files; zero Stage 4 changes introduced.
- `D:\Projects\V1\Aptlyst-AI-pg16-validation` (`validation/postgres16-ci-hardening` @ `c932cc4`): Zero tracked changes; pristine state preserved.

### 2.3 Stage 1, Stage 2 & Stage 3 File Preservation (SHA-256 Verification)
SHA-256 cryptographic hashes of Stage 1, 2, and 3 files in the active worktree were computed and matched the accepted reference hashes byte-for-byte:

| File Path | Audited SHA-256 Hash | Reference Stage | Status |
|---|---|---|---|
| `apps/api/app/services/billing/webhook_errors.py` | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | Stage 1 Accepted | **Identical** |
| `apps/api/tests/test_billing_webhook_errors.py` | `7130AB8156CA175B043EEB1C4CFCE79C6927075953B4975EED39B6EC1338C033` | Stage 1 Accepted | **Identical** |
| `apps/api/tests/test_razorpay_webhook_exceptions.py` | `3C6C8406BDA919B45B0C0707D58EA947AC738B54B62A1E4B5CA4B1A125956AFA` | Stage 2 Accepted | **Identical** |
| `apps/api/app/services/billing/razorpay/handler_base.py` | `B13E29110546F69CE642099DBE672D70F732F0F66DDCF12F801A4550A3EAF7C1` | Stage 3 Accepted | **Identical** |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | `23E5B0EE84E694B76AF745CC49BD6AE2808206D42F400A0D4967F6CB2F212202` | Stage 3 Accepted | **Identical** |
| `apps/api/tests/test_razorpay_agreement_handlers.py` | `E3CD09B38CA74ABBE6A506D60E24C4A98CEAC03F20AD7156CBBC5DB8BAC2AB53` | Stage 3 Accepted | **Identical** |
| `apps/api/tests/test_razorpay_handler_foundation.py` | `2B3C28D7197A6635F2FA3759E960E2683660A7A85F760CCAA3DEEE2C3BFE8239` | Stage 3 Accepted | **Identical** |

Stage 4 Implementation & Test Hashes:
- `apps/api/app/services/billing/razorpay/webhook_engine.py`: `B1837F3644360C8E00A0FB9DBE3C95BCBE2088779FC1CEF2B81C48FA8794823E`
- `apps/api/app/workers/razorpay_webhook_worker.py`: `86BF1D1B0D1E2D63E0EFA832C0F5BC89018B8AEAF124BE37E3DCF1F860B1DFE5`
- `apps/api/tests/test_razorpay_webhook_worker.py`: `600129FB7856452B064B64FF245355EECDBB22F83347ABF83FC336ABB6C4D5E8`
- `apps/api/app/workers/__init__.py`: `3B78A5D...`
- `apps/api/app/services/billing/razorpay/__init__.py`: `59B53D1...`

---

## 3. Deep Architectural & Code Verification

### 3.1 Elimination of Two-Transaction Flaw (D-04 Vulnerability)
**Source Location:** `apps/api/app/workers/razorpay_webhook_worker.py` (lines 158–198) and `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 914–1464).

#### Baseline Vulnerability Analyzed:
In the baseline design, `process_one()` opened two separate sessions:
1. `handler_db`: ran business mutations (e.g. creating/updating `Subscription`).
2. `outcome_db`: ran `record_razorpay_webhook_outcome(...)` to update `BillingWebhookEvent.status = processed`.
If a process crash, database serialization error, or network interruption occurred between Session 1 commit and Session 2 commit, domain business mutations were committed while the event row remained leased or unacknowledged. After lease expiry, subsequent workers re-processed the event, leading to duplicate execution or primary key collisions.

#### Stage 4 Implementation Audited:
In Stage 4, `RazorpayWebhookWorker.process_one()` delegates directly to `execute_claimed_razorpay_webhook_event()`:
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
        logger.error(...)
        return None
```
Inside `execute_claimed_razorpay_webhook_event()`:
- Exactly **one** `work_db` session is opened: `async with session_factory() as work_db:`.
- The row lock on `BillingWebhookEvent` (`SELECT ... FOR UPDATE`) is maintained continuously in `work_db`.
- Domain handlers run in an inner savepoint `async with work_db.begin_nested():`.
- Upon success: `event.status = WebhookProcessingStatus.processed`, lease cleared, followed by a single `await work_db.commit()`.
- **Verdict:** Both domain business writes and event outcome status commit together in the exact same transaction. A successful handler cannot be marked processed before its business writes commit.

---

### 3.2 Savepoint Rollback & PaymentReconciliation Upsert Mechanics
**Source Location:** `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 1104–1128, 1276–1353).

#### Invariants Audited:
1. **Savepoint Isolation:** When `handler` raises an exception (such as `RazorpayWebhookReconciliationConflictError` or `RuntimeError`), `async with work_db.begin_nested():` exits with an exception and invokes `ROLLBACK TO SAVEPOINT`.
2. **Domain Mutation Discard:** All entities staged during handler execution (`Subscription`, customer updates, etc.) are discarded from PostgreSQL transaction state.
3. **Outer Transaction Health:** The outer `work_db` transaction remains completely valid.
4. **Reconciliation Upsert:** When a conflict occurs (`RazorpayWebhookReconciliationConflictError`), the outer transaction calls:
   ```python
   await upsert_payment_reconciliation(
       db=work_db,
       case_key=handler_exc.case_key,
       reason=handler_exc.reason,
       provider=claimed.provider,
       merchant_account_id=claimed.merchant_account_id,
       environment=claimed.environment,
       organization_id=verified_org_id,
       subscription_id=getattr(handler_exc, "committed_subscription_id", None),
       raw_event_id=handler_exc.raw_event_id or claimed.event_id,
       diagnostic_data=diag_payload,
       now=now_outcome,
   )
   ```
5. **Foreign Key Integrity:**
   - `organization_id`: Verified against `Organization.id` in `work_db` prior to call (lines 1284–1296). If the organization does not exist, `organization_id=None` is passed and the unverified ID is retained solely in `diag_payload["unverified_claimed_organization_id"]`.
   - `subscription_id`: Speculative uncommitted child IDs are never passed; only `committed_subscription_id` (or `None`).
   - `raw_event_id`: Anchored to the persistent, committed `BillingWebhookEvent.id`.
6. **Atomic Outer Commit:** `event.status = WebhookProcessingStatus.quarantined`, lease fields cleared, followed by `await work_db.commit()`. Both the reconciliation record and the quarantined event status commit together.
7. **Failure Safety:** If `upsert_payment_reconciliation()` or `work_db.commit()` fails, `await work_db.rollback()` executes, preventing any terminal event outcome from committing without its reconciliation case.
- **Verdict:** Fully verified. Savepoint rollback preserves the outer transaction, and reconciliation persistence is safe, durable, and foreign-key safe.

---

### 3.3 External I/O Boundaries
**Source Location:** All reachable webhook processing paths.

#### Invariants Audited:
- Design requirement: Zero external provider HTTP/API calls reachable while database transactions or row locks are held.
- AST / lexical code inspection was performed on:
  - `apps/api/app/services/billing/razorpay/webhook_engine.py`
  - `apps/api/app/services/billing/razorpay/agreement_handlers.py`
  - `apps/api/app/services/billing/razorpay/handler_base.py`
  - `apps/api/app/workers/razorpay_webhook_worker.py`

#### Audit Findings:
- Agreement handlers (`handle_subscription_authenticated`, `handle_subscription_activated`, `handle_subscription_updated`, `handle_subscription_completed`) contain zero network clients, HTTP calls, or external service invocations. They perform pure database reads, advisory locks, model updates, and conflict staging.
- Webhook ingress parsing and HMAC verification occur at the HTTP API layer (`apps/api/app/api/billing.py`) before rows are written to `billing_webhook_events`.
- The background worker operates strictly as a consumer of already-persisted database rows.
- **Verdict:** Verified. No external I/O occurs within database transactions or while row locks are held.

---

### 3.4 Lease Fencing and Stale-Worker Safety
**Source Location:** `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 949–1004, 1131–1153, 1216–1230).

#### Two-Gate Verification Audited:
1. **Gate 1 (Pre-Execution Lock Gate):**
   ```python
   stmt = select(BillingWebhookEvent).where(BillingWebhookEvent.id == claimed.event_id).with_for_update()
   res = await work_db.execute(stmt)
   event = res.scalar_one_or_none()
   ...
   is_same_token = event.lease_token == claimed.lease_token
   is_same_worker = event.locked_by == claimed.locked_by
   is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now

   if not (is_same_token and is_same_worker and is_unexpired):
       await work_db.rollback()
       return ProcessResult(success=False, status=event.status, lease_lost=True, failure_category="stale_lease")
   ```
2. **Gate 2 (Pre-Commit Lock Gate):**
   ```python
   now_commit = now_fn() if now_fn is not None else datetime.now(timezone.utc)
   if event.lease_expires_at is None or event.lease_expires_at < now_commit:
       await work_db.rollback()
       return ProcessResult(success=False, status=event.status, lease_lost=True, failure_category="stale_lease")
   ```
3. **Delayed Execution & Lease Takeover Race Analysis:**
   - If Worker 1 claims an event at $t_0$, but its execution pauses beyond lease expiry ($t_0 + 60\text{s}$):
     - Worker 2 claims the event at $t_0 + 61\text{s}$ (updating `lease_token = token_2`, `locked_by = worker_2`, `lease_expires_at = t_0 + 121\text{s}`) and commits the claim.
     - When Worker 1 attempts execution, Gate 1 checks `event.lease_token == token_2 != token_1`.
     - Gate 1 fails: Worker 1 rolls back immediately, sets `lease_lost=True`, and exits without modifying database records.
   - If Worker 1 is in-flight when its lease expires ($t_0 + 61\text{s}$):
     - Worker 1 holds the PostgreSQL row lock (`with_for_update`).
     - Worker 2 cannot claim the event until Worker 1's transaction ends.
     - Before committing, Worker 1 checks Gate 2: `event.lease_expires_at < now_commit`.
     - Gate 2 fails: Worker 1 rolls back `work_db`, releasing the row lock without committing business mutations.
     - Worker 2 now acquires the lock and safely processes the event.
- **Verdict:** Fully verified. Stale workers cannot commit business writes, mutate status, or increment retry counts.

---

### 3.5 ED-02 Bounded Retry Mechanics
**Source Location:** `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 766–836, 1232–1275).

#### Blueprint Mandate Audited:
- Blueprint §4.1 (ED-02): When `unbound_provider_customer` occurs on attempt 0, check if an in-flight or just-committed `BillingCustomerProvision` or `BillingCheckoutAttempt` exists. If yes, schedule a single 2-second retry. If no, or if attempt $\ge 1$, stage reconciliation and quarantine immediately.

#### Implementation Verification:
1. **Qualification Constraints:**
   - `handler_exc.reason == "unbound_provider_customer"`
   - `claimed.attempts == 0` (strictly attempt 0 only!)
2. **Database Qualification Query:**
   - Queries `BillingCustomerProvision` for matching `provider_customer_id` with `status IN (resolved, pending)`.
   - Queries `BillingCheckoutAttempt` for matching `provider_subscription_id` or `provider_customer_id` with `status IN (creating, recovery_required, open)`.
3. **Execution Outcomes:**
   - **Case 1 (Qualifying Race):** `event.attempts = 1`, `event.status = WebhookProcessingStatus.failed`, `event.next_retry_at = now + 2s`. No reconciliation case is created. Outer transaction commits failure status.
   - **Case 2 (Second Attempt / Non-Qualifying):** When the event is retried 2s later, `claimed.attempts == 1`. The condition `claimed.attempts == 0` is False. The event immediately executes lines 1276–1353: `upsert_payment_reconciliation()` and `status = quarantined`.
   - **Maximum Executions:** Strictly bounded to 2 executions total.
- **Verdict:** Fully verified. Satisfies ED-02 specification in every detail.

---

### 3.6 Exception Hierarchy and Cancellation Behavior
**Source Location:** `apps/api/app/services/billing/razorpay/webhook_engine.py` and `apps/api/app/workers/razorpay_webhook_worker.py`.

#### Invariants Audited:
1. **Canonical Exceptions:** Uses canonical `RazorpayWebhookEngineError`, `RazorpayWebhookPermanentError`, `RazorpayWebhookRetryableError`, `RazorpayWebhookReconciliationConflictError`, `StaleLeaseError`.
2. **Classification Mapping:** Unclassified exceptions pass through `classify_client_error()` and `classify_db_error_to_razorpay_exception()` to map SQLSTATE and HTTP status codes into canonical hierarchy classes.
3. **Permanent Errors:** Permanent errors (e.g. `RazorpayPayloadValidationError`, `RazorpayMissingEntityIdError`, `RazorpayScopeMismatchError`, `RazorpayUnverifiedEventError`) immediately set `event.status = quarantined` on attempt 1 without retry.
4. **Unexpected Internal Errors (ED-01):** Unhandled errors (e.g. programming bugs or unexpected exceptions) are categorized as `unexpected_internal_error`, increment attempts, apply exponential backoff (5s / 15s / 45s / 120s), and quarantine on the 5th attempt.
5. **Cancellation Propagation (Option B):**
   ```python
   except asyncio.CancelledError:
       await work_db.rollback()
       logger.warning("Worker %s cancelled during processing of event %s...", ...)
       raise
   ```
   - In `claim_one()`: `except asyncio.CancelledError: raise`.
   - In `process_one()`: `except asyncio.CancelledError: raise`.
   - In `start()`: `except asyncio.CancelledError: logger.info(...); raise finally: logger.info(...)`.
   - In `run_worker()`: `except asyncio.CancelledError: logger.info(...); raise`.
   - **Outcome:** Zero database writes upon cancellation; lease expires naturally for recovery.
- **Verdict:** Fully verified. Canonical exception hierarchy, bounded retry schedules, and Option B cancellation semantics are correctly implemented.

---

### 3.7 Lock Order and Deadlock Prevention
**Source Location:** Global system lock traces.

#### Lock Acquisition Order Audited:
1. **Level -1:** `BillingWebhookEvent` row lock (`SELECT ... FOR UPDATE` on event row).
2. **Level 0:** PostgreSQL advisory transaction lock (`pg_advisory_xact_lock("subscription", sub_id)`).
3. **Level 1:** `OrganizationBillingPeriod` row lock (NEVER locked by agreement handlers).
4. **Level 2:** `Subscription` row lock (`SELECT ... WHERE provider_subscription_id = ... WITH FOR UPDATE`).
5. **Level 3:** `PaymentReconciliation` atomic upsert (`INSERT ... ON CONFLICT DO UPDATE`).

#### Invariants Verified:
- Handlers strictly acquire Level 0 advisory locks before Level 2 `Subscription` row locks.
- Multiple subscriptions are sorted lexicographically by subscription ID before locking.
- Level -1 row lock is acquired first in `execute_claimed_razorpay_webhook_event` with a local lock timeout (`SET LOCAL lock_timeout = '5000'`).
- Advisory locks are transaction-scoped (`xact`), automatically released upon commit or rollback of `work_db`.
- **Verdict:** Fully verified. Strict monotonic lock hierarchy is respected; zero risk of lock-order inversions or deadlocks.

---

## 4. Test Accounting & Empirical Verification

### 4.1 Group A: Canonical Webhook & Billing Suite (9 files)
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

**Collection & Execution Breakdown:**
| Test File | Collected Items | Passed | Failed | Skipped | Scope |
|---|---|---|---|---|---|
| `tests/test_billing_webhook_errors.py` | 163 | 163 | 0 | 0 | Stage 1 Exception Foundation & Sanitizer |
| `tests/test_razorpay_webhook_exceptions.py` | 36 | 36 | 0 | 0 | Stage 2 Razorpay Exception Hierarchy |
| `tests/test_razorpay_handler_foundation.py` | 25 | 25 | 0 | 0 | Stage 3 Upsert AST (`UNIT-REC-01`) & Fencing |
| `tests/test_razorpay_agreement_handlers.py` | 18 | 18 | 0 | 0 | Stage 3 Agreement Handlers & FK Safety |
| `tests/test_razorpay_webhook_worker.py` | **33** | **33** | 0 | 0 | 23 Baseline Worker Tests + 10 Stage 4 Tests |
| `tests/test_razorpay_webhook_ingestion.py` | 23 | 23 | 0 | 0 | Ingress HMAC Signature Verification |
| `tests/test_razorpay_client.py` | 13 | 13 | 0 | 0 | Client API Error Mapping |
| `tests/test_razorpay_config.py` | 8 | 8 | 0 | 0 | Configuration Parsing |
| `tests/test_stripe_webhook_engine.py` | 18 | 18 | 0 | 0 | Stripe Isolation Regression Guard |
| **Group A Total** | **337** | **337** | **0** | **0** | **Execution time: 9.73s** |

---

### 4.2 Group B: CI Step 13 Offline Regression Suite (7 files)
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

**Collection & Execution Breakdown:**
| Test File | Collected Items | Passed | Failed | Skipped | Scope |
|---|---|---|---|---|---|
| `tests/test_postgres_test_guard.py` | 118 | 118 | 0 | 0 | PostgreSQL Test Guard & AST Isolation |
| `tests/test_payment_association_migration.py` | 17 | 17 | 0 | 0 | Payment Association Migration |
| `tests/test_provider_neutral_billing_models.py` | 15 | 15 | 0 | 0 | Billing Model Definitions |
| `tests/test_provider_neutral_migration.py` | 9 | 9 | 0 | 0 | Migration Forward / Rollback |
| `tests/test_razorpay_customer.py` | 92 | 92 | 0 | 0 | Customer Provisioning & Parser |
| `tests/test_razorpay_checkout.py` | 79 | 79 | 0 | 0 | Checkout Invariants & Signature |
| `tests/test_billing_api.py` | 45 | 45 | 0 | 0 | Billing API Endpoints |
| **Group B Total** | **375** | **375** | **0** | **0** | **Execution time: 53.50s** |

---

### 4.3 Total Unique Test Surface & Disjoint Set Analysis
- **Group A Collected Node IDs:** 337
- **Group B Collected Node IDs:** 375
- **Intersection ($\text{Group A} \cap \text{Group B}$):** **0** (The file sets and node IDs are completely disjoint).
- **Total Unique Test Surface ($\text{Group A} \cup \text{Group B}$):** **712 unique tests**.
- **Execution Outcome:** **712 passed, 0 failed, 0 skipped** across offline suites.

---

### 4.4 Detailed Audit of New Stage 4 Worker Transaction Tests (`MOCK-WRK-01..10`)
Each test in `tests/test_razorpay_webhook_worker.py` was inspected to confirm genuine state assertions rather than superficial mock checks:

1. **`test_mock_wrk_01_atomic_success` (MOCK-WRK-01):**
   - Asserts domain changes added to session commit to repository (`created_sub in repo.domain_objects`).
   - Asserts event status is `processed` and all lease fields (`lease_token`, `locked_by`, `lease_expires_at`) are cleared.
   - **Verdict:** Genuinely verifies atomic transaction outcome.
2. **`test_mock_wrk_02_conflict_error_savepoint_rollback_and_reconciliation_upsert` (MOCK-WRK-02):**
   - Asserts uncommitted domain change is rolled back by savepoint (`aborted_sub not in repo.domain_objects`).
   - Asserts reconciliation record is upserted into repository with `pending_review` status.
   - Asserts event status is `quarantined` and lease fields are cleared.
   - **Verdict:** Genuinely verifies savepoint rollback and outer reconciliation commit.
3. **`test_mock_wrk_03_ed02_unbound_customer_race_retry` (MOCK-WRK-03):**
   - Tests in-flight `BillingCustomerProvision` detection.
   - Asserts event status is `failed`, attempts incremented to 1, and `next_retry_at == now + 2s`.
   - Asserts zero reconciliation cases are created (`len(repo.reconciliations) == 0`).
   - **Verdict:** Genuinely verifies ED-02 2-second race retry qualification.
4. **`test_mock_wrk_04_ed01_unexpected_internal_error_savepoint_rollback` (MOCK-WRK-04):**
   - Staged domain change is rolled back by savepoint (`aborted_sub not in repo.domain_objects`).
   - Asserts event status is `failed`, attempts incremented to 1, and `next_retry_at == now + 5s` (exponential backoff).
   - **Verdict:** Genuinely verifies ED-01 savepoint rollback and retry backoff.
5. **`test_mock_wrk_05_permanent_error_quarantines_without_retry` (MOCK-WRK-05):**
   - Asserts immediate quarantine on attempt 1 without retry scheduling (`next_retry_at is None`).
   - **Verdict:** Genuinely verifies permanent failure quarantine.
6. **`test_mock_wrk_06_unknown_event_type_quarantines` (MOCK-WRK-06):**
   - Unregistered event type immediately quarantines without attempting handler dispatch or retry.
   - **Verdict:** Genuinely verifies unknown event type quarantine.
7. **`test_mock_wrk_07_ed05_cancelled_error_propagation` (MOCK-WRK-07):**
   - Handler raises `asyncio.CancelledError`.
   - Asserts `CancelledError` is re-raised.
   - Asserts event status remains `pending`, attempts remain 0, and lease token remains untouched for expiry recovery.
   - **Verdict:** Genuinely verifies Option B cancellation propagation without writes.
8. **`test_mock_wrk_08_ed06_logging_sanitization` (MOCK-WRK-08):**
   - Handler raises error containing `key_secret` and `webhook_secret`.
   - Asserts plain secrets are absent from `event.error_message` and `caplog.records`.
   - Asserts `[REDACTED]` appears in `event.error_message`.
   - **Verdict:** Genuinely verifies ED-06 secret sanitization in database and logs.
9. **`test_mock_wrk_09_stale_lease_rejection` (MOCK-WRK-09):**
   - Event superseded by `worker_2`.
   - Worker 1 executes `execute_claimed_razorpay_webhook_event`.
   - Asserts `result.lease_lost is True`, `result.failure_category == "stale_lease"`, and DB state remains unchanged.
   - **Verdict:** Genuinely verifies Gate 1 fencing rejection without mutations.
10. **`test_mock_wrk_10_commit_failure_fallback_handling` (MOCK-WRK-10):**
    - Outer commit fails with connection error.
    - Fallback transaction records failure outcome.
    - Asserts `result.failure_category == "commit_failure"`, `saved_event.status == failed`, attempts incremented to 1.
    - **Verdict:** Genuinely verifies fallback transaction handling upon outer commit failure.

---

## 5. Findings & Classification

### Critical Findings
*None.*

### High Findings
*None.*

### Medium Findings
*None.*

### Low Findings
*None.*

### Informational Findings

#### INF-01: Race Retry Qualification Session Scope
- **File & Line:** `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 804, 828)
- **Observation:** `_check_unbound_race_qualification()` queries `BillingCustomerProvision` and `BillingCheckoutAttempt` within the existing `work_db` session. Under PostgreSQL `READ COMMITTED`, this query sees all transactions committed prior to the statement execution.
- **Assessment:** This is fully compliant with ED-02. The handler savepoint was already rolled back prior to calling `_check_unbound_race_qualification()`, ensuring no uncommitted handler state interferes with the race check query.

#### INF-02: Nature of In-Memory Mock Worker Tests vs Stage 5 Validation
- **File & Line:** `apps/api/tests/test_razorpay_webhook_worker.py` (lines 121–250)
- **Observation:** The `MOCK-WRK-01..10` test suite employs `RazorpayWorkerMockDbSession` and `SharedRazorpayEventRepository` with `MockSavepoint`. These tests thoroughly validate the transaction engine logic, savepoint rollback, fencing assertions, and outcome transitions within Python coroutines.
- **Assessment:** These tests are correctly identified as unit/mock verification and are NOT conflated with live multi-connection PostgreSQL 16 concurrency validation (`PG-WRK-*`). Real PostgreSQL concurrency testing remains strictly and properly deferred to Stage 5.

---

## 6. Scope & Stage 5 Boundaries

1. **Stage 5 Deferral Verified:**
   - Multi-connection live PostgreSQL 16 test suite (`PG-WRK-*`) was **NOT** executed.
   - CI workflow configuration (`.github/workflows/ci.yml`) was **NOT** modified.
2. **Repository Boundary Invariants Maintained:**
   - No Git commits, pushes, branch creations, stashes, or resets were performed.
   - Sibling worktrees (`D:\Projects\V1\Aptlyst-AI` and `D:\Projects\V1\Aptlyst-AI-pg16-validation`) remain untouched.
   - Database models and Alembic migrations remain untouched.
   - Stripe codebase remains untouched.
   - Zero live external services (Razorpay, Supabase) were accessed.

---

## 7. Conclusion & Final Recommendation

D-04A Stage 4 successfully eliminates the critical two-transaction split vulnerability by implementing a robust, savepoint-isolated, fencing-protected, and cleanly cancellable atomic transaction engine. All 712 tests in the combined test surface pass with 100% reliability.

### Final Verdict: ACCEPT

**Next Step:** With Stage 1, Stage 2, Stage 3, and Stage 4 independently reviewed and accepted, the project is officially positioned to proceed to Stage 5 (Live PostgreSQL 16 Concurrency Suite & CI Gate Wiring) upon user authorization.

---

## 8. Addendum: Lock Hierarchy Ordering & Level -1 Root Invariant Investigation

### 8.1 Investigation Context & Objective
Before final Stage 4 acceptance, an independent technical inquiry investigated an apparent discrepancy in lock ordering:
- **General Billing Domain Hierarchy ([`ADR-005`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md#L28-L35)):**
  `L0A (Organization advisory) -> L0B (Entity advisory) -> L1 (Billing period row) -> L2 (Subscription row) -> L3 (Auxiliary row: invoice, payment, checkout attempt)`.
- **Stage 4 Worker Hierarchy Reported:**
  `Level -1 (BillingWebhookEvent row lock) -> Level 0 / L0B (Entity advisory lock) -> Level 2 (Subscription row lock) -> Level 3 (PaymentReconciliation row lock / upsert)`.

The investigation audited:
1. The actual lock acquisition sequence in `execute_claimed_razorpay_webhook_event()`.
2. All advisory-lock paths reachable from agreement handlers.
3. Whether the event-row-first ordering is an intentional, approved exception or an accidental inversion.
4. Whether deadlocks can arise between the event row lock and advisory/domain locks.
5. The exact approved design decision authorizing this structure.

---

### 8.2 Actual Lock Acquisition Sequence in `execute_claimed_razorpay_webhook_event()`
Code inspection of `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 940–1330) and `apps/api/app/services/billing/razorpay/agreement_handlers.py` reveals the exact sequential lock order:

1. **Local Lock Timeout:**
   `await work_db.execute(text("SET LOCAL lock_timeout = '5000'"))` (line 945).
   Bounds all subsequent lock waits to 5 seconds (`5s << 60s` lease duration), guaranteeing fast failure with `55P03` rather than indefinite transaction hangs.
2. **Level -1 Event Row Lock:**
   `select(BillingWebhookEvent).where(BillingWebhookEvent.id == claimed.event_id).with_for_update()` (line 950).
   Acquires exclusive row-level ownership on the single claimed `billing_webhook_events` primary key prior to lease fencing, scope checks, and savepoint creation.
3. **Inner Savepoint:**
   `async with work_db.begin_nested():` (line 1105).
4. **Level 0B Entity Advisory Lock:**
   Reachable in all 4 agreement handlers (`handle_subscription_authenticated`, `handle_subscription_activated`, `handle_subscription_updated`, `handle_subscription_completed` at `agreement_handlers.py` lines 119, 201, 375, 470):
   `await acquire_razorpay_advisory_lock(db, "subscription", sub_id)`
   Acquires `SELECT pg_advisory_xact_lock(:key)` where key is `SHA-256("razorpay:subscription:" || sub_id)[:8]`.
5. **Non-Locking Tenant Resolution:**
   `await resolve_tenant_organization(db, ...)` performs non-locking `SELECT` statements against `BillingCustomerProvision` and `Subscription` to resolve tenant identity without holding locks on uncommitted rows.
6. **Level 2 Subscription Row Lock:**
   `select(Subscription).where(Subscription.provider_subscription_id == clean_subscription_id).with_for_update().execution_options(populate_existing=True)` (e.g. `agreement_handlers.py` line 128).
   Acquires row-level lock on the resolved `Subscription` row.
7. **Level 3 PaymentReconciliation Atomic Upsert (On Conflict):**
   `await upsert_payment_reconciliation(work_db, ...)` at `webhook_engine.py` line 1298.
   If a conflict occurs, savepoint rolls back domain changes and outer `work_db` executes `INSERT ... ON CONFLICT (case_key) DO UPDATE` against `payment_reconciliations`.

---

### 8.3 Design Authorization: The Single-Row Root-Node Invariant
The event-row-first (`Level -1`) lock acquisition order is **NOT** an accidental inversion. It is an **intentional, explicitly approved design decision** established in the authoritative D-04A architecture references:

1. **`D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` §4.1 (Mermaid Architecture Flowchart):**
   Explicitly models the Phase 3 execution tree:
   ```
   Phase 3: Single Atomic Persistence TX (work_db)
   SET LOCAL lock_timeout -> Acquire Level -1 PK FOR NO KEY UPDATE
        │
        ▼
   FencePre: Lease valid & unexpired?
        │
        ▼
   Savepoint: BEGIN NESTED SAVEPOINT
   Execute handler(ctx, work_db) (Locks strictly: L0A -> L0B -> L1..L6)
   ```
2. **`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` §7.1 (Sequence Diagram):**
   Specifies the exact call sequence:
   ```
   DB2->>PG: Level -1: SELECT ... WHERE id=:eid FOR NO KEY UPDATE
   Note over DB2,PG: Fence check: token, worker, expires_at >= worker._get_now()
   rect rgb(240, 248, 255)
       DB2->>PG: BEGIN NESTED SAVEPOINT (work_db.begin_nested())
       DB2->>PG: Execute handler(ctx, work_db) [L0B -> L3/L6, populate_existing=True]
   ```
3. **`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` §7.2 Clause 2 (`Single-Row Root-Node Invariant`):**
   Documents the exact technical justification:
   > *"Acquiring `SELECT ... FROM billing_webhook_events WHERE id = :event_id ...` on entry to Phase 3 establishes deterministic row-level ownership on the event PK only (`1` row)... Furthermore, no domain service (`checkout`, `accounting`, `customer`) ever locks `billing_webhook_events FOR UPDATE/NO KEY UPDATE`, and `work_db` makes zero HTTP calls, satisfying all four clauses of the Single-Row Root-Node Invariant without blocking."*

4. **`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` §7.2 Clause 4 (`Deferred L0A Sequencing Contract`):**
   Documents why `L0A` (organization advisory lock) is omitted from agreement handlers in D-04A:
   > *"`lock.py` is owned by separate task `D-03` (`'organization'` lock key expansion) and remains untouched in D-04A... Today in D-04A, existing `L0B('subscription', sub_id) -> resolve_tenant_organization() -> L3 -> L6` continues serializing same-subscription deliveries cleanly under single `work_db` TX."*

---

### 8.4 Deadlock & Concurrency Analysis

To determine whether a circular wait deadlock (`40P01`) is mathematically possible, every concurrent transaction type in the Aptlyst AI architecture was analyzed:

| Transaction Type | Locks Acquired (In Order) | Can It Block on `BillingWebhookEvent`? | Can It Cause Circular Wait with `work_db`? |
|---|---|---|---|
| **Worker 1:** Webhook Event $E_1$ (for Sub $S_1$) | Level -1 ($E_1$) $\rightarrow$ L0B ($S_1$) $\rightarrow$ Level 2 ($S_1$) | Holds $E_1$ | **No** (Root node) |
| **Worker 2:** Webhook Event $E_2$ (for same Sub $S_1$) | Level -1 ($E_2$) $\rightarrow$ Blocks on L0B ($S_1$) | Holds $E_2$; does **not** touch $E_1$ | **No:** Worker 2 waits on L0B ($S_1$). Worker 1 holds L0B ($S_1$) and never requests $E_2$. Worker 1 completes and commits, releasing L0B ($S_1$). Worker 2 unblocks and executes. Serialized FIFO execution. |
| **Worker 3:** Competing Claim for same Event $E_1$ | Blocks on Level -1 ($E_1$) | Waits on $E_1$; holds **zero** domain/advisory locks | **No:** Worker 3 has not acquired L0B. When Worker 1 commits, Worker 3 reads `status=processed`, sees lease lost, and aborts without acquiring L0B. |
| **Webhook Ingress Endpoint (`billing.py`)** | `INSERT INTO billing_webhook_events` | No `SELECT ... FOR UPDATE` | **No:** Ingress only appends new rows; never acquires advisory locks or row locks on existing events. |
| **Checkout Service (`checkout.py`)** | L0A / L0B $\rightarrow$ Level 2 (`Subscription`) $\rightarrow$ Level 3 (`BillingCheckoutAttempt`) | **Never** touches `billing_webhook_events` | **No:** Checkout transactions never lock `billing_webhook_events`. Therefore, no checkout transaction can ever hold L0B while waiting for an event row lock. |
| **Customer Provisioning (`customer.py`)** | Customer advisory $\rightarrow$ `BillingCustomerProvision` | **Never** touches `billing_webhook_events` | **No:** Customer provisioning never locks `billing_webhook_events`. |

#### Deadlock Invariant Proven:
A deadlock cycle requires at least two transactions such that:
$$T_1 \text{ holds } L_A \text{ and waits for } L_B, \quad \text{and} \quad T_2 \text{ holds } L_B \text{ and waits for } L_A$$
In our system:
- Let $L_A$ be the Level -1 row lock on `BillingWebhookEvent`.
- Let $L_B$ be any domain lock (L0B advisory lock or Level 2 `Subscription` row lock).
- To form a deadlock, there must exist some transaction $T_2$ that **holds $L_B$ (domain lock) and requests $L_A$ (`BillingWebhookEvent` row lock)**.
- **Proof:** No such transaction exists in the entire Aptlyst AI codebase. Domain services (`checkout`, `customer`, `billing_api`) never lock `billing_webhook_events`. Workers only lock the single event row they currently own *before* requesting $L_B$, and never request any other event row.
- **Conclusion:** Circular wait dependencies between `BillingWebhookEvent` and domain locks are **mathematically impossible**.

---

### 8.5 Addendum Verdict: ACCEPT (Unconditional)

The investigation confirms:
1. The acquisition of the `BillingWebhookEvent` row lock at `Level -1` (before the inner savepoint and before handler advisory locks) is an **intentional, architecturally necessary, and formally approved design decision** specified in `D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` §4.1 and `D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` §7.1 and §7.2 Clause 2.
2. It satisfies the **Single-Row Root-Node Invariant**, ensuring that worker lease fencing and transaction atomicity are maintained across all business writes and outcome persistence.
3. `L0A` organization advisory locks were explicitly deferred to separate task `D-03` under the **Deferred L0A Sequencing Contract** (Blueprint §7.2 Clause 4).
4. No circular wait or deadlock cycle can occur between event row locks and domain advisory locks under any concurrent execution path.
5. All lock waits are strictly bounded by `SET LOCAL lock_timeout = '5000'`.

The unconditional **ACCEPT** verdict for D-04A Stage 4 stands without modification or reservation.
