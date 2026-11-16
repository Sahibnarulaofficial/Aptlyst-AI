# D-04A Stage 5 — Final Independent Engineering Review

- **Review Date:** 2026-11-15
- **Reviewer:** Independent Engineering Audit Agent
- **Target Worktree:** `D:\Projects\V1\Aptlyst-AI-d04a-ci-validation`
- **Target Branch:** `validation/d04a-stage5-pg16-ci`
- **Base Baseline Commit:** `497852e19360d85787107dbae6921519074f0773`
- **Validated CI Commit SHA:** [`190458b41e1342a0d36f91dd86bef5aa1d68947a`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/commit/190458b41e1342a0d36f91dd86bef5aa1d68947a)
- **Branch Head Commit SHA:** `023f0d31d7940ce9b1401c69117ff0056de2e997` (Documentation commit only)
- **Verified GitHub Actions Run:** [43632317523](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/43632317523)
- **Verified PostgreSQL Service:** Real PostgreSQL 16.16 Alpine container (`postgres:16-alpine`, port 5432, disposable database `aptlyst_ci_test`)
- **Overall Verdict:** **ACCEPT**

---

## 1. Executive Verdict

**Verdict: ACCEPT**

Stage 5 of the D-04A initiative has been independently audited and verified. The primary objective of Stage 5—establishing unassailable runtime evidence on real PostgreSQL 16 for all concurrency, row-locking, advisory-locking, savepoint-rollback, and lease-fencing mechanisms—has been fully accomplished.

### Key Verification Milestones:
1. **Real Database Execution:** All 14 webhook concurrency integration scenarios executed against a live PostgreSQL 16.16 container service in GitHub Actions CI (`postgres:16-alpine`). Zero tests were skipped, mocked out of existence, or bypassed.
2. **Zero-Skip CI Enforcement:** Four independent CI test suites ran to completion, generating JUnit XML artifacts verified directly from the GitHub Actions artifact archive (`test-results-ci`):
   - Provenance Suite: **14 passed, 0 skipped** (0.289s)
   - Customer Provisioning Integration: **20 passed, 0 skipped** (10.623s)
   - Webhook Concurrency Integration: **14 passed, 0 skipped** (6.584s)
   - Focused Offline Regression Suite: **375 passed, 0 skipped** (15.989s)
   - **Total CI Surface: 423 passed, 0 failed, 0 skipped.**
3. **Genuine Production Defect Identified & Resolved:** During initial CI testing, a genuine defect in `webhook_engine.py` (accessing ORM attributes on an expired instance following `await work_db.rollback()`, crashing worker loops with `MissingGreenlet`) was uncovered. A minimal, surgical scalar-caching fix was applied under explicit user authorization, and its resolution was verified under live PostgreSQL 16 concurrency.
4. **Worktree & Cryptographic Integrity:** All Stage 1–4 production and test files match the cryptographic SHA-256 digests established in prior accepted reviews, with the sole exception of the authorized 12-line fix in `webhook_engine.py`. Sibling worktrees `D:\Projects\V1\Aptlyst-AI` and `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` were preserved in strict read-only isolation.

---

## 2. Scope & Reviewed Commit History

### Commit Progression on `validation/d04a-stage5-pg16-ci`:
| Commit SHA | Author | Scope / Description | Independent Audit Finding |
|---|---|---|---|
| `0d34c03` | Sahibnarulaofficial | `test: add D-04A PostgreSQL 16 webhook concurrency validation` | Base validation commit transferring approved Stage 1–5 files. |
| `6bcde2d` | Sahibnarulaofficial | `fix: preserve webhook lease fields across rollback` | Authorized production defect correction in `webhook_engine.py` + regression test. |
| `99211a4` | Sahibnarulaofficial | `test: import record_razorpay_webhook_outcome for lease fencing regression` | Test import correction in `test_postgres_webhook_concurrency_integration.py`. |
| `798d833` | Sahibnarulaofficial | `test: correct checkout attempt schema, accounting commit semantics, and jsonb parsing in postgres concurrency suite` | Test fixture schema alignment, explicit transaction commits for usage accounting, and JSONB type guard. |
| `190458b` | Sahibnarulaofficial | `test: dispatch subscription.activated via default_razorpay_dispatcher in race test` | **Validated CI Commit SHA.** Dispatches `subscription.activated` through registered dispatcher handler. 100% green CI run `43632317523`. |
| `023f0d3` | Sahibnarulaofficial | `docs: add D-04A Stage 5 real PostgreSQL 16 CI validation report` | **Branch HEAD.** Strictly added documentation (`D2_D04A_STAGE5_CI_VALIDATION_REPORT.md`). Zero production or test code modified. |

---

## 3. Independent CI & Container Verification

Inspection of GitHub Actions run **43632317523** and direct parsing of the downloaded artifact archive `test-results-ci` (Artifact ID `14129075697`, 9,294 bytes) confirmed:

### CI Step Execution Log Analysis:
- **Runner Environment:** Ubuntu 24.04 (GitHub-hosted runner, Python 3.11.18).
- **Service Container:** `postgres:16-alpine` running PostgreSQL 16.16 on port 5432.
  - Server Log: `2026-11-14 19:29:12.768 UTC [1] LOG: starting PostgreSQL 16.16 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit`.
- **Database Initialization:** Disposable database `aptlyst_ci_test` initialized with dummy Supabase roles (`authenticated`, `anon`, `service_role`).
- **Alembic Migrations:** Successfully migrated schema to head `e2f3a4b5c6d1`.
- **Zero-Skip Gates:** Three separate verification steps executed `python -c "..."` parsing JUnit XML files and exited 0, confirming zero skipped tests.

### Parsed JUnit XML Artifact Breakdown:
```xml
<testsuites>
  <testsuite name="pytest" file="test-results-postgres.xml" tests="14" failures="0" errors="0" skipped="0" time="0.289" />
  <testsuite name="pytest" file="test-results-postgres-customer.xml" tests="20" failures="0" errors="0" skipped="0" time="10.623" />
  <testsuite name="pytest" file="test-results-postgres-webhook.xml" tests="14" failures="0" errors="0" skipped="0" time="6.584" />
  <testsuite name="pytest" file="test-results-models.xml" tests="375" failures="0" errors="0" skipped="0" time="15.989" />
</testsuites>
```

---

## 4. Production Architecture Independent Review

### A. Exception Architecture (`webhook_errors.py`, `webhook_engine.py`, `handler_base.py`, `razorpay/__init__.py`)
1. **Canonical Identities:** The root base exception `BillingWebhookError` branches into three disjoint categories:
   - `BillingWebhookPermanentError` (`is_retryable=False`, failure category `"permanent_failure"`)
   - `BillingWebhookRetryableError` (`is_retryable=True`, failure category `"retryable_error"`)
   - `BillingWebhookLeaseError` (`is_retryable=False`, failure category `"stale_lease"`)
2. **Precedence Hierarchy:** `classify_webhook_exception()` implements a strict 5-step classification order:
   - *Step 1:* Re-raises non-Exception `BaseException` (`asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`) without classification.
   - *Step 2:* Authoritative `BillingWebhookError` hierarchy (outer type wins).
   - *Step 3:* Unequivocal programming errors (`TypeError`, `AttributeError`, `NameError`, `UnboundLocalError`) quarantined immediately on attempt 1.
   - *Step 4:* Non-override guard preventing ambient `__context__` DB errors from masking Python exceptions.
   - *Step 5:* Causal-first database / transport classification via SQLSTATE and constraint buckets.
3. **Sanitization:** `sanitize_billing_error_message()` strips SQLAlchemy parameter dumps, libpq credentials, and API secrets while shielding safe identifiers (`sub_*`, `pay_*`, `cust_*`, UUIDs, SQLSTATE codes).

### B. Reconciliation Behavior (`handler_base.py`, `agreement_handlers.py`)
1. **Native PostgreSQL Upsert:** `build_payment_reconciliation_upsert_stmt()` constructs a native `INSERT ... ON CONFLICT (case_key) DO UPDATE` targeting `uq_payment_reconciliations_case_key`.
2. **Foreign-Key Coalescing:** Safely coalesces existing non-null foreign keys with newly discovered keys using `func.coalesce(PaymentReconciliation.<col>, stmt.excluded.<col>)`.
3. **Status Reopening Rule:** Transitions resolved cases (`resolved_refunded`, `resolved_ignored`) back to `pending_review` via a SQL `CASE` statement if a conflicting event occurs on the same `case_key`.
4. **JSONB History Append:** Appends timestamped conflict details to `diagnostic_payload['history']` using PostgreSQL JSONB concatenation (`||`) while updating `latest`.

### C. Atomic Webhook Execution (`webhook_engine.py`, `razorpay_webhook_worker.py`)
1. **Transaction Boundaries:**
   - Claim step uses a short-lived transaction with `SELECT ... FOR UPDATE SKIP LOCKED`.
   - Execution step runs inside a dedicated outer transaction with row lock on `BillingWebhookEvent`.
2. **Savepoint Isolation:** Handler logic runs inside `async with work_db.begin_nested():`. If domain logic fails or raises a conflict, the inner savepoint rolls back domain mutations while the outer transaction atomically persists the reconciliation case and transitions the event to `quarantined` or `failed`.
3. **No Unsafe State on Cancellation:** `asyncio.CancelledError` triggers `await work_db.rollback()` and re-raises, leaving no partially committed writes or orphaned records.

### D. Production Defect Correction Assessment
The post-rollback expired attribute defect in `webhook_engine.py` was independently examined in both locations:
- `assert_and_refresh_webhook_lease()` (lines 686–710)
- `execute_claimed_razorpay_webhook_event()` (lines 974–1015)

**Verification:**
1. All required ORM scalar values (`lease_token`, `locked_by`, `lease_expires_at`, `status`, `attempts`) are bound to local Python variables (`db_token`, `db_locked_by`, `db_expires_at`, `event_status`, `event_attempts`) **before** invoking `await work_db.rollback()`.
2. Post-rollback code references only these local variables.
3. No attribute access is performed on `event` after `rollback()`.
4. Original fencing semantics, warning logs, `StaleLeaseError` raising, and `ProcessResult` generation remain 100% preserved.

---

## 5. Detailed Review of All 14 PostgreSQL Concurrency Tests

| # | Test Name | Production Behavior Exercised | Independent Connections | Concurrency Determinism | Persisted State Assertions | Cleanup Safety | Defect Detection Capability |
|---|---|---|---|---|---|---|---|
| 1 | `test_pg16_d04a_01_checkout_verification_vs_subscription_activated_webhook_race` | Checkout verification callback racing against `subscription.activated` webhook | Separate sessions (`db` vs worker `session_factory`) | `asyncio.gather` concurrent race | Verifies attempt complete, payment bound, subscription active, event processed, zero duplicates | Reverse-FK harness cleanup | **High:** Caught missing dispatcher handler dispatch |
| 2 | `test_pg16_d04a_01_idempotent_replay_behavior` | Idempotent replay of completed checkout verification & rejection of re-claimed processed events | Separate sessions for replay & worker claim | Sequential deterministic replay | Asserts attempt complete with payment ID; worker claim returns `None` | Reverse-FK harness cleanup | **High:** Verifies idempotent replay guarantees |
| 3 | `test_pg16_d04a_02_concurrent_checkout_creation_deduplication` | Concurrent checkout creation attempts for same organization | `async with session_factory() as db1, session_factory() as db2` | Two concurrent coroutines | Asserts exactly 1 active attempt in PostgreSQL | Reverse-FK harness cleanup | **High:** Verifies partial unique index `uq_org_scoped_active_billing_checkout_attempt` |
| 4 | `test_pg16_d04a_02_expired_open_checkout_vs_verification_race` | Verification callback on expired open checkout attempt | Execution session vs verify session | Deterministic clock offset | Asserts attempt transitioned to `expired`, NOT resurrected to `complete`, subscription incomplete | Reverse-FK harness cleanup | **High:** Verifies non-resurrection of expired checkouts |
| 5 | `test_pg16_d04a_03_stale_worker_fenced_after_lease_loss` | Stale worker execution & outcome recording after lease takeover by competitor | Worker 1 session, Worker 2 session, verification session | Simulated competitor lease takeover in PostgreSQL | Asserts `lease_lost=True`, `failure_category="stale_lease"`, `StaleLeaseError` raised, DB row preserves Worker 2 | Reverse-FK harness cleanup | **Critical:** Caught and verified resolution of `MissingGreenlet` production defect |
| 6 | `test_pg16_d04a_03_savepoint_rollback_on_handler_failure_with_atomic_quarantine` | Handler savepoint rollback and outer transaction atomic reconciliation commit | Worker claim session, execution session, verify session | Simulated handler conflict within savepoint | Asserts subscription mutation rolled back (`incomplete`), event quarantined, `PaymentReconciliation` row persisted | Reverse-FK harness cleanup | **High:** Verifies savepoint rollback isolation |
| 7 | `test_pg16_d04a_03_reconciliation_replay_upsert_idempotence` | Native `ON CONFLICT DO UPDATE` on `payment_reconciliations` across multiple attempts | Two sequential upsert transactions, verify session | Sequential replay transactions | Asserts exactly 1 row, updated reason, and 2 history entries in JSONB | Reverse-FK harness cleanup | **High:** Verifies native PostgreSQL upsert idempotency |
| 8 | `test_pg16_d04a_03_cancellation_during_handler_execution_leaves_no_partial_writes` | Task cancellation during webhook execution | Claim session, worker task session, verify session | `asyncio.Event` sync, sleep, and `task.cancel()` | Asserts transaction rolled back, subscription remains `incomplete` | Reverse-FK harness cleanup | **High:** Verifies clean cancellation semantics |
| 9 | `test_pg16_d04a_04_concurrent_quota_reservation_and_settlement_stress` | 4 concurrent quota reservations and subsequent settlements on real billing period | 4 concurrent reservation sessions, concurrent settlement sessions, verify session | `asyncio.gather` concurrent reservations & settlements | Asserts non-negative token/cost constraints hold, zero deadlocks (no 40P01), quota check constraint satisfied | Reverse-FK harness cleanup | **High:** Verifies `SELECT ... FOR UPDATE` serialization on billing periods |
| 10 | `test_pg16_d04a_04_double_settlement_prevention` | Two concurrent settlements on the exact same reservation ID | Reservation session, two concurrent `try_settle` sessions | `asyncio.gather` concurrent settlement race | Asserts exactly 1 settlement succeeds, conflicting settlement rejected | Reverse-FK harness cleanup | **High:** Verifies double-settlement mutual exclusion |
| 11 | `test_pg16_d04a_05_advisory_lock_acquisition_and_transaction_rollback_release` | Transaction advisory lock acquisition, conflict detection, and rollback release | `db1` (lock holder), `db2` (probe), `db3` (post-rollback probe) | Cross-transaction lock probing | Asserts `try_acquire` returns `False` during hold, `True` after rollback | Reverse-FK harness cleanup | **High:** Verifies transaction-level advisory lock release |
| 12 | `test_pg16_d04a_05_signed_64bit_advisory_keys_and_negative_values` | Negative signed 64-bit advisory lock key execution in PostgreSQL | Dedicated asyncpg connection | Deterministic negative key generation | Asserts PostgreSQL `pg_try_advisory_xact_lock` accepts negative bigint | Reverse-FK harness cleanup | **High:** Verifies signed 64-bit integer compatibility |
| 13 | `test_pg16_d04a_05_deterministic_key_derivation_and_domain_separation` | Domain separation across entity types (`subscription`, `payment`, `invoice`, `customer`) | `db1` (subscription lock), `db2` (payment lock) | Simultaneous acquisition on distinct domains | Asserts 4 distinct derived keys and simultaneous lock acquisition | Reverse-FK harness cleanup | **High:** Verifies zero false lock contention across entity domains |
| 14 | `test_pg16_d04a_05_native_on_conflict_do_update_reconciliation_with_jsonb_history` | Native PostgreSQL JSONB history append and status reopening rule on resolved case | `db1` (initial upsert), `db_upd` (status transition), `db2` (reopening upsert), `verify_db` | Multi-transaction reconciliation lifecycle | Asserts status reopened to `pending_review`, reason updated, history length == 2, latest code == "ERR_2" | Reverse-FK harness cleanup | **High:** Verifies JSONB history append and reopening rules |

---

## 6. Git & Cryptographic Hash Comparison

### Cryptographic SHA-256 Hash Comparison:
| File Path | Accepted Stage Digest | Final Validation Branch Digest | Audit Assessment |
|---|---|---|:---:|
| `apps/api/app/services/billing/webhook_errors.py` | `9508C855AD423C2E...` | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | **EXACT MATCH (Stage 1)** |
| `apps/api/tests/test_billing_webhook_errors.py` | `7130AB8156CA175B...` | `7130AB8156CA175B043EEB1C4CFCE79C6927075953B4975EED39B6EC1338C033` | **EXACT MATCH (Stage 2)** |
| `apps/api/tests/test_razorpay_webhook_exceptions.py` | `3C6C8406BDA919B4...` | `3C6C8406BDA919B45B0C0707D58EA947AC738B54B62A1E4B5CA4B1A125956AFA` | **EXACT MATCH (Stage 2)** |
| `apps/api/app/services/billing/razorpay/handler_base.py` | `B13E29110546F69C...` | `B13E29110546F69CE642099DBE672D70F732F0F66DDCF12F801A4550A3EAF7C1` | **EXACT MATCH (Stage 3)** |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | `23E5B0EE84E694B7...` | `23E5B0EE84E694B76AF745CC49BD6AE2808206D42F400A0D4967F6CB2F212202` | **EXACT MATCH (Stage 3)** |
| `apps/api/tests/test_razorpay_handler_foundation.py` | `2B3C28D7197A6635...` | `2B3C28D7197A6635F2FA3759E960E2683660A7A85F760CCAA3DEEE2C3BFE8239` | **EXACT MATCH (Stage 3)** |
| `apps/api/tests/test_razorpay_agreement_handlers.py` | `E3CD09B38CA74ABB...` | `E3CD09B38CA74ABBE6A506D60E24C4A98CEAC03F20AD7156CBBC5DB8BAC2AB53` | **EXACT MATCH (Stage 3)** |
| `apps/api/app/workers/razorpay_webhook_worker.py` | `86BF1D1B0D1E2D63...` | `86BF1D1B0D1E2D63E0EFA832C0F5BC89018B8AEAF124BE37E3DCF1F860B1DFE5` | **EXACT MATCH (Stage 4)** |
| `apps/api/tests/test_razorpay_webhook_worker.py` | `600129FB7856452B...` | `600129FB7856452B064B64FF245355EECDBB22F83347ABF83FC336ABB6C4D5E8` | **EXACT MATCH (Stage 4)** |
| `.github/workflows/ci.yml` | `0F784DB4F4CD83A7...` | `0F784DB4F4CD83A738616A2D919DFB90CB419B22E6160687F052191DEBD6269E` | **EXACT MATCH (Stage 5)** |
| `apps/api/app/services/billing/razorpay/__init__.py` | `B4C6E0BC07E9837F...` | `B4C6E0BC07E9837FE4F8B309A737356E2CE9DA86DDB5D25B19C9E6F6F185A191` | **EXACT MATCH (Stage 3/4)** |
| `apps/api/app/workers/__init__.py` | `424D306E5DED42A8...` | `424D306E5DED42A86EDC7C7F10EEA607ED5DD9F25D3101D8B57E858CF55DB16A` | **EXACT MATCH (Stage 4)** |
| `apps/api/app/services/billing/razorpay/webhook_engine.py` | `B1837F3644360C8E...` | `F1E90D80D91C77CBB5F28BD2887ED6C0D126DD9B73190FA20943C64E0BE7951F` | **AUTHORIZED DEFECT FIX** (`6bcde2d`) |
| `apps/api/tests/test_postgres_webhook_concurrency_integration.py` | `25D73C6CBC8B635E...` | `76EDE9BF8A7D56B5EB608411C4D355A96C2074B0761889F593D85E0406AF414F` | **AUTHORIZED TEST ADJUSTMENTS** |

---

## 7. Findings Classification

### Finding S5-F1: Post-Rollback ORM Attribute Access in Webhook Engine (Resolved)
- **Severity:** HIGH (Resolved via Commit `6bcde2d`)
- **Location:** `apps/api/app/services/billing/razorpay/webhook_engine.py:686-710, 974-1015`
- **Evidence:** Evaluating `event.lease_token`, `event.locked_by`, `event.lease_expires_at`, `event.status`, and `event.attempts` after `await work_db.rollback()` triggered synchronous lazy loading (`MissingGreenlet` under asyncpg).
- **Impact:** Worker processes crashed upon encountering a stale lease or lease takeover instead of gracefully logging and quarantining/fencing.
- **Resolution:** Captured all required scalar attributes into local variables prior to invoking `rollback()`.
- **Verification:** Verified by `test_pg16_d04a_03_stale_worker_fenced_after_lease_loss` in real PostgreSQL 16 CI run `43632317523`.

### Finding S5-F2: Mocked Gateway Retrieval in Integration Tests (Informational)
- **Severity:** LOW
- **Location:** `apps/api/tests/test_postgres_webhook_concurrency_integration.py:636-641, 782-786, 849-854`
- **Evidence:** External payment gateway calls (`get_razorpay_client().get_payment`, `create_subscription`) are mocked with AsyncMocks.
- **Impact:** None. Live gateway HTTP calls are explicitly prohibited by project boundaries and security policies.
- **Assessment:** Correct and sanctioned design. All database locks, concurrency serialization, and row mutations are executed against real PostgreSQL 16.

---

## 8. Unverified Claims & Limitations

1. **Non-PostgreSQL Dialects:** This review and its associated CI runs validate behavior exclusively against PostgreSQL 16.16 (`postgres:16-alpine`). Behavior on other SQL engines (e.g. SQLite, MySQL) is out of scope and not certified.
2. **Third-Party Razorpay Gateway Latency:** Live webhook delivery network jitter and third-party gateway downtime were evaluated via simulated concurrent test doubles rather than live webhooks from Razorpay production servers.

---

## 9. Final Acceptance Recommendation

**Recommendation: ACCEPT STAGE 5 FULLY.**

All engineering acceptance criteria for D-04A Stage 5 have been satisfied:
1. Complete isolation and preservation of existing worktrees (`D:\Projects\V1\Aptlyst-AI` and `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`).
2. Successful execution against a real containerized PostgreSQL 16.16 instance.
3. 100% green CI run across all 4 gates (423 passed, 0 skipped, 0 failed).
4. Direct verification of JUnit XML artifacts confirming zero skips.
5. Successful resolution of the post-rollback expired attribute defect in `webhook_engine.py`.
6. Complete cryptographic alignment with Stage 1–4 accepted baselines.

Stage 5 is formally accepted.
