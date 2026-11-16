# D-04A Stage 5 — Real PostgreSQL 16 CI Validation Report

- **Date:** 2026-11-15
- **Branch:** `validation/d04a-stage5-pg16-ci`
- **Validation Worktree:** `D:\Projects\V1\Aptlyst-AI-d04a-ci-validation`
- **Primary Implementation Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` (Preserved Untouched)
- **Main Worktree:** `D:\Projects\V1\Aptlyst-AI` (Preserved Untouched)
- **Base Parent Commit:** `497852e19360d85787107dbae6921519074f0773`
- **Final Validated Head SHA:** `190458b41e1342a0d36f91dd86bef5aa1d68947a`
- **Final Green GitHub Actions Run ID:** [43632317523](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/43632317523)
- **Status:** **STAGE 5 COMPLETE — 100% GREEN (All 4 CI Test Gates Passed with Zero Skips Against Real PostgreSQL 16)**

---

## 1. Executive Summary

Stage 5 of the D-04A initiative successfully established end-to-end runtime verification against a real containerized PostgreSQL 16 instance (`postgres:16-alpine`, PostgreSQL 16.16) in GitHub Actions CI.

All concurrency, transaction isolation, row-level locking, advisory locking, savepoint rollback, idempotency, and lease-fencing mechanisms developed across Stages 1–4 were subjected to rigorous concurrent stress and race scenarios on real PostgreSQL 16.

### Final CI Verification Summary (Run 43632317523)

| CI Gate / Test Suite | Executed Against | Required Pass | Result | Skips Enforced |
|---|---|:---:|:---:|:---:|
| **PostgreSQL 16 Readiness & Isolation** | `postgres:16-alpine` (Port 5432) | Port Open & DB Healthy | **VERIFIED** | N/A |
| **Supabase Dummy Roles & Alembic Migrations** | `aptlyst_ci_test` (PostgreSQL 16) | Schema Head `e2f3a4b5c6d1` | **APPLIED** | N/A |
| **Suite 1: PostgreSQL Integration (Provenance)** | Real PostgreSQL 16 Container | 14 / 14 scenarios | **14 PASSED** (0.28s) | **0 SKIPPED (Enforced)** |
| **Suite 2: Customer Provisioning Integration** | Real PostgreSQL 16 Container | 20 / 20 scenarios | **20 PASSED** (10.62s) | **0 SKIPPED (Enforced)** |
| **Suite 3: Webhook Concurrency Integration** | Real PostgreSQL 16 Container | 14 / 14 scenarios | **14 PASSED** (6.58s) | **0 SKIPPED (Enforced)** |
| **Suite 4: Migration, Billing & Model Regression** | Python 3.11 Offline Test Suite | 375 / 375 items | **375 PASSED** (15.99s) | **0 SKIPPED** |
| **Total Test Execution Surface** | CI Container & Runner | **423 / 423 items** | **423 PASSED** | **ZERO SKIPS** |
| **Artifact Preservation** | `test-results-ci` | XML Reports Uploaded | **PRESERVED** | `if: always()` |

---

## 2. Phase 1 — Main Worktree Integrity Audit

Before initiating any Git branching or transfer, an exhaustive audit of `D:\Projects\V1\Aptlyst-AI` was performed:
1. **Branch & HEAD:** `main` at `027ab7ad3ee8a02fb20823a4f743e29b03c0f248` tracking `origin/main`.
2. **Tracked Files:** All 13 modified tracked files (`13 files changed, 13773 insertions(+), 712 deletions(-)`) were confirmed 100% intact.
3. **Untracked Files:** Exactly the 33 root markdown reports (`STEP_5_2D_3_3A_3_...` through `STEP_5_2D_3_3B_1...`), `LOCAL_COMMIT_REPORT.md`, and test guard files were intact.
4. **Conclusion:** Zero work was discarded or altered. The main worktree was maintained in strict read-only preservation.

---

## 3. Phase 2 — Isolated Validation Worktree Setup

To prevent any cross-contamination with in-progress work:
- **Worktree Path:** `D:\Projects\V1\Aptlyst-AI-d04a-ci-validation`
- **Validation Branch:** `validation/d04a-stage5-pg16-ci`
- **Base Commit:** `497852e19360d85787107dbae6921519074f0773`
- Both `D:\Projects\V1\Aptlyst-AI` and `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` remained untouched.

---

## 4. Phase 3 — Selective Transfer & Cryptographic Integrity

Strictly the 14 approved D-04A files and 17 D-04A reports were transferred from `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`. All pre-existing D-2 customer files (`STEP_5_2D_3_3B_1E_Z1_*.md` through `Z8`, `D2_STAGE1_FORMAL_CLOSURE_REPORT.md`, `D2_STAGE2_READINESS_AUDIT.md`) were strictly excluded.

### SHA-256 Digest Verification Against Accepted Stage Reports:
- `apps/api/app/services/billing/webhook_errors.py`: `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` (Exact match Stage 1)
- `apps/api/tests/test_billing_webhook_errors.py`: `7130AB8156CA175B043EEB1C4CFCE79C6927075953B4975EED39B6EC1338C033` (Exact match Stage 2)
- `apps/api/tests/test_razorpay_webhook_exceptions.py`: `3C6C8406BDA919B45B0C0707D58EA947AC738B54B62A1E4B5CA4B1A125956AFA` (Exact match Stage 2)
- `apps/api/app/services/billing/razorpay/handler_base.py`: `B13E29110546F69CE642099DBE672D70F732F0F66DDCF12F801A4550A3EAF7C1` (Exact match Stage 3)
- `apps/api/app/services/billing/razorpay/agreement_handlers.py`: `23E5B0EE84E694B76AF745CC49BD6AE2808206D42F400A0D4967F6CB2F212202` (Exact match Stage 3)
- `apps/api/tests/test_razorpay_handler_foundation.py`: `2B3C28D7197A6635F2FA3759E960E2683660A7A85F760CCAA3DEEE2C3BFE8239` (Exact match Stage 3)
- `apps/api/app/workers/razorpay_webhook_worker.py`: `86BF1D1B0D1E2D634DE7BE31862FDC56DC80E6EF2D6EBDF419F4F648D4D6BFF2` (Exact match Stage 4)

---

## 5. Iterative CI Runs & Defect Investigations

### 5.1 Initial CI Run 43628310303 (Commit `0d34c037ee05450b6f62be7cc8bdcc4969cc2277`)
- **Outcome:** 5 passed, 9 failed.
- **Failures Identified:**
  1. Five tests failed due to PostgreSQL schema NOT NULL constraint on `subscriptions.provider_customer_id` (test fixture omitted it).
  2. One test (`test_pg16_d04a_03_stale_worker_fenced_after_lease_loss`) exposed a **genuine production defect**: evaluating ORM attributes (`event.lease_token`, `event.locked_by`, `event.lease_expires_at`, `event.status`, `event.attempts`) immediately after `await work_db.rollback()` triggered `sqlalchemy.exc.MissingGreenlet`.
  3. Two tests failed on accessing `res.reservation_id` instead of `res.reservation.id`.
  4. One test failed on JSONB string deserialization.

### 5.2 Authorized Production Fix & Regression (Commit `6bcde2d7d509a56f15462dd8492d46f3033308c4`)
- Following explicit user authorization, corrected `apps/api/app/services/billing/razorpay/webhook_engine.py`:
  - In `assert_and_refresh_webhook_lease()` and `execute_claimed_razorpay_webhook_event()`, scalar attributes are captured into local variables **prior** to `rollback()`.
  - Stale lease logging and `ProcessResult` generation now strictly reference cached scalar values.
  - Added regression assertions in `test_postgres_webhook_concurrency_integration.py`.

### 5.3 Schema & Transaction Alignment (Commits `798d833` & `190458b`)
- In `test_postgres_webhook_concurrency_integration.py`:
  - `BillingCheckoutAttempt` test fixture aligned with PostgreSQL schema (`idempotency_key`, `currency`, `amount_minor_units`, `attempt_lease_expires_at`, `session_expires_at`).
  - Added transaction commit (`await db.commit()`) in `reserve_task`, `settle_task`, and `try_settle` so real PostgreSQL transactions persist usage reservations before settlement.
  - Added JSONB string deserialization check for `diagnostic_payload['latest']`.
  - Dispatched webhook events through `default_razorpay_dispatcher.get_handler(claimed.event_type)` in `test_pg16_d04a_01_checkout_verification_vs_subscription_activated_webhook_race`.

---

## 6. Final CI Verification Evidence (Run 43632317523)

### GitHub Actions Execution Metadata:
- **Run ID:** `43632317523`
- **Run URL:** [https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/43632317523](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/43632317523)
- **Commit SHA:** `190458b41e1342a0d36f91dd86bef5aa1d68947a`
- **Target Ref:** `refs/heads/validation/d04a-stage5-pg16-ci`
- **Job Conclusion:** `success`
- **Workflow Status:** `completed`

### Suite 3: Webhook Concurrency Scenarios on Real PostgreSQL 16 (14/14 Passed)
1. `test_pg16_d04a_01_checkout_verification_vs_subscription_activated_webhook_race`: **PASSED**
2. `test_pg16_d04a_01_idempotent_replay_behavior`: **PASSED**
3. `test_pg16_d04a_02_concurrent_checkout_creation_deduplication`: **PASSED**
4. `test_pg16_d04a_02_expired_open_checkout_vs_verification_race`: **PASSED**
5. `test_pg16_d04a_03_stale_worker_fenced_after_lease_loss`: **PASSED** (Production fix verified)
6. `test_pg16_d04a_03_savepoint_rollback_on_handler_failure_with_atomic_quarantine`: **PASSED**
7. `test_pg16_d04a_03_reconciliation_replay_upsert_idempotence`: **PASSED**
8. `test_pg16_d04a_03_cancellation_during_handler_execution_leaves_no_partial_writes`: **PASSED**
9. `test_pg16_d04a_04_concurrent_quota_reservation_and_settlement_stress`: **PASSED**
10. `test_pg16_d04a_04_double_settlement_prevention`: **PASSED**
11. `test_pg16_d04a_05_advisory_lock_acquisition_and_transaction_rollback_release`: **PASSED**
12. `test_pg16_d04a_05_signed_64bit_advisory_keys_and_negative_values`: **PASSED**
13. `test_pg16_d04a_05_deterministic_key_derivation_and_domain_separation`: **PASSED**
14. `test_pg16_d04a_05_native_on_conflict_do_update_reconciliation_with_jsonb_history`: **PASSED**

---

## 7. Stage 5 Compliance & Sign-Off

- [x] Work performed exclusively in isolated validation worktree `D:\Projects\V1\Aptlyst-AI-d04a-ci-validation`.
- [x] Primary implementation worktree `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` and main worktree `D:\Projects\V1\Aptlyst-AI` preserved untouched.
- [x] Tested against real containerized PostgreSQL 16 (`postgres:16-alpine`).
- [x] Zero skipped tests across all mandatory integration suites.
- [x] All 4 test gates green in CI (Provenance: 14/14, Customer: 20/20, Webhook Concurrency: 14/14, Regression: 375/375).
- [x] Genuine production defect in `webhook_engine.py` (post-rollback attribute access) resolved and verified under real PostgreSQL runtime concurrency.
- [x] Test XML artifacts securely archived (`test-results-ci`).
