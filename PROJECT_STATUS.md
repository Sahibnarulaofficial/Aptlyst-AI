# Aptlyst AI — Project Status Dashboard

**Status Date:** 2026-11-17  
**Repository State:** Development  
**Active Phase:** Phase D-2 Stage 2 — Razorpay Settlement & Concurrency Unification (Preparatory Phase)  

---

## 1. High-Level Milestone Tracker

| Phase | Description | Status | Verification Gate |
| :--- | :--- | :---: | :--- |
| **Phase 1** | Next.js 16 Web Dashboard Shell | **Complete** | UI routes & components verified |
| **Phase 2** | Multi-Tenancy & Supabase SSR Auth | **Complete** | Session redirects & context verified |
| **Phase 3** | AI Chatbot Configurator & RAG Knowledge Base | **Complete** | Ingestion & retrieval endpoints verified |
| **Phase 4** | Embeddable Chat Widget & Lead Capture | **Complete** | Standalone iframe & SSE streaming verified |
| **Phase 5.1** | Stripe Billing Foundation (Historical) | **Complete** | Unit tested (legacy backward compatibility) |
| **Phase 5.2A** | Provider-Neutral Billing Foundation | **Complete** | Migration `a1b2c3d4e5f7` passed |
| **Phase 5.2B** | Customer Provisioning & Active Attempt Uniqueness | **Complete** | Migration `b1c2d3e4f5a6`, `c1d2e3f4a5b7` passed |
| **Phase 5.2C** | Webhook Ingestion & Background Worker Queue | **Complete** | Migration `d1e2f3a4b5c6` passed; CLI verified |
| **Phase 5.2D / D-04A** | Razorpay Agreement Handlers & Payment Association (Stage 5) | **Complete** | PR #2 merged (`6d11ec5`); GitHub Actions Run #17 passed on PostgreSQL 16.16 |
| **D-2 Stage 2** | Razorpay Settlement & Concurrency Unification | **Active (Prep)** | TASK-D2-S2-PREP-01 in progress (BLK-03, BLK-08 resolved) |

---

## 2. Active Checkpoint: D-2 Stage 2 Preparatory Phase (TASK-D2-S2-PREP-01)

We are currently at **D-2 Stage 2: Preparatory Phase (TASK-D2-S2-PREP-01)** in isolated worktree `D:\Projects\V1\Aptlyst-AI-d2-stage2-locks` on branch `implementation/d2-stage2-lock-unification`.

### 2.1 Completed Within Current Checkpoint
- **BLK-03 (Advisory Lock Primitive Unification)**:
  - Authored canonical provider-neutral advisory lock module `apps/api/app/services/billing/advisory_lock.py` supporting `derive_advisory_lock_key`, `acquire_advisory_lock`, `try_acquire_advisory_lock`, and `acquire_multi_entity_advisory_locks`.
  - Converted `apps/api/app/services/billing/razorpay/lock.py` into a 100% backward-compatible facade delegating to the canonical module.
  - Implemented transitional dual-locking in `apps/api/app/api/organizations.py` (legacy 31-bit modulo user lock acquired first, canonical 64-bit user advisory lock acquired second).
  - Enforced monotonic strictly ascending signed 64-bit integer sorting ($K_1 < K_2 < \dots < K_n$) for multi-entity locks with duplicate deduplication and collision logging.
  - Preserved byte-for-byte golden hash parity with all historical Razorpay and Stripe lock keys.
  - Authored 18 dedicated unit tests in `apps/api/tests/test_advisory_lock.py` (100% passing).
- **BLK-08 (Canonical Documentation Synchronization)**:
  - Synchronized `docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md` to reflect unified advisory locking and multi-entity sort order contract.
  - Updated `BILLING_ROADMAP.md` recording formal closure of D-04A Stage 5 and status of Stage 2 blockers.
  - Updated `PROJECT_STATUS.md` to active 2026-11-17 state.

### 2.2 Outstanding Stage 2 Blockers
- **BLK-04**: Payment binding sweeper schema & query design for timed-out attempts (PENDING).
- **BLK-05**: Settlement idempotency & transition state machine verification (PENDING).
- **BLK-06**: Reconciliation quarantine & out-of-order webhook isolation (PENDING).
- **BLK-07**: Live PostgreSQL multi-tenant test harness for parallel webhook/checkout concurrency (PENDING).
- **Verification Gate**: Full Stage 2 settlement handlers (`subscription.authenticated`, `subscription.charged`) remain strictly blocked until BLK-04 through BLK-07 are completed.

---

## 3. Current Test Metrics

| Test Suite | Location | Passing | Skipped | Status |
| :--- | :--- | :---: | :---: | :--- |
| **New Advisory Lock Unit Suite** | `apps/api/tests/test_advisory_lock.py` | **18** | 0 | All passing green |
| **Billing Regression Subset** | Agreement & Stripe handlers | **87** | 0 | All passing green |
| **Full Local Baseline (Merged `main`)** | `apps/api/tests/` & root `tests/` | **741** | 0 | Verified locally on `main` (`6d11ec5`) |
| **PostgreSQL 16 CI Container** | GitHub Actions Run #17 (`43898958221`) | **423** | 0 | All passing green (PR #2 merge) |
| **Frontend Production Build** | `pnpm --filter web build` | **Passed** | 0 | Webpack production bundle verified |

---

## 4. Next Authorized Development Step

Upon completion and commit of TASK-D2-S2-PREP-01:

👉 **Review and authorize design for BLK-04 through BLK-07** prior to starting settlement handler implementation.
