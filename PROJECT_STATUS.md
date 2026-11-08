# Aptlyst AI — Project Status Dashboard

**Status Date:** 2026-11-08  
**Repository State:** Development  
**Active Phase:** Phase 5.2D — Provider-Neutral Billing Foundation & Razorpay Integration  

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
| **Phase 5.2D** | Razorpay Agreement Handlers & Payment Association | **Active** | Step 5.2D.3.3A.2 in progress |

---

## 2. Active Checkpoint: Step 5.2D.3.3A.2

We are currently at **Step 5.2D.3.3A.2: PostgreSQL CI Integration Validation**.

### 2.1 Completed Within Current Checkpoint
- **Step 5.2D.3.3A.1 (Schema Migration & Hardening Authored)**:
  - Authored migration `e2f3a4b5c6d1` adding 6-column composite foreign key `fk_billing_payments_checkout_attempt_composite` targeting `(checkout_attempt_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)`.
  - Added check constraint `ck_billing_payments_checkout_attempt_complete` (eliminating PostgreSQL `MATCH SIMPLE` null bypass).
  - Added PostgreSQL procedural immutability triggers for payment and period provenance.
  - Implemented unified cross-provider provenance conflict detection query (aborting on conflicting subscription evidence).
- **Test Suite Authored**:
  - 17 unit/model tests executed and passing locally on SQLite (`test_provider_neutral_billing_models.py`, `test_payment_association_migration.py`).
  - 14 integration test scenarios authored in `tests/test_postgres_provenance_integration.py`.
- **Step 5.2D.3.3A.2 (CI Workflow Authored)**:
  - `.github/workflows/ci.yml` authored to provision an isolated PostgreSQL 16 service container and run integration tests.

### 2.2 Outstanding Validation & Blockers
- **Blocker**: The 14 PostgreSQL integration tests have **not yet been executed against a real PostgreSQL instance in CI**. They are skipped in local SQLite test runs because SQLite does not support PostgreSQL triggers, advisory locks, or composite foreign key constraints.
- **Verification Gate**: Before authorizing Step 5.2D.3.3B (implementation of settlement handlers), CI must execute with all 14 PostgreSQL tests passing green.
- **Step 5.2D.3.3B Status**: **NOT STARTED**. Implementation is strictly prohibited until CI execution passes.

---

## 3. Current Test Metrics

| Test Suite | Location | Passing | Skipped | Status |
| :--- | :--- | :---: | :---: | :--- |
| **Backend Unit & Mock Suite** | `apps/api/tests/` | **634** | 0 | All passing green |
| **PostgreSQL CI Integration** | `apps/api/tests/test_postgres_provenance_integration.py` | 0 | **14** | Authored; awaiting CI execution |
| **Frontend Test Suite** | `tests/` | **4** | 0 | All passing green |

---

## 4. Next Authorized Development Step

Upon successful completion and verification of Step 5.2D.3.3A.2 (PostgreSQL CI execution):

👉 **Step 5.2D.3.3B: Razorpay Payment Settlement Handlers**
- Implement `subscription.authenticated` settlement handler.
- Implement `subscription.charged` / `payment.captured` settlement handler with conservative period rollover.
- Implement delayed payment settlement for terminal agreements (invoice-only, no resurrection).
- Implement `payment_binding_sweeper` for expired attempt payment rediscovery.
