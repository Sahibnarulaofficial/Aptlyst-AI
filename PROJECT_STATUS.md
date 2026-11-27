# Aptlyst AI — Project Status Dashboard

- **Status Date:** 2026-11-28
- **Repository State:** Development (`main` @ `6872f471df201faeeae62decfb76552cdc28d084`)
- **Active Phase:** Stage 2L — Billing Handoff & Freeze (Active Focus: Chatbot Product Development)

---

## 1. High-Level Milestone Tracker

| Phase | Description | Status | Verification Gate |
| :--- | :--- | :---: | :--- |
| **Phase 1** | Next.js 16 Web Dashboard Shell & Billing UI Shell | **Implemented** | Dashboard shell, auth routing, legacy Stripe billing view |
| **Phase 2** | Multi-Tenancy & Supabase SSR Auth | **Implemented** | Session redirects, org CRUD, and tenant isolation verified |
| **Phase 3** | Chatbot CRUD API & Configurator | **Implemented (Core)** | Auth chatbot CRUD & config APIs implemented (`/knowledge`, `/conversations`, `/leads`, `/analytics`, `/integrations`, `/settings` UIs still placeholders) |
| **Phase 4** | Embeddable Widget Preview & Visitor Conversation API | **Partial Foundation** | `/embed/[id]` preview + visitor/conversation APIs implemented (Phase 1 `disabled`/`mock` LLM; live streaming and placeholder UIs pending) |
| **Phase 5.1** | Stripe Billing Foundation (Historical) | **Completed (Legacy)** | Preserved for backward compatibility and historical financial audit |
| **Phase 5.2A–5.2D / D-04A** | Provider-Neutral Billing Foundation & Provenance | **Completed** | PR `#2` merged (`6d11ec5`); migrations `a1b2c3d4e5f7` through `e2f3a4b5c6d1` |
| **Stage 2J – 2K** | Recovery Worker, Advisory Locks, Heartbeats, Ops & PG16 CI | **Completed (Code & CI)** | PR `#3` merged (`6872f47`); migrations `f3a4b5c6d7e2`, `a4b5c6d7e8f3`; Run `46048131091` (`1,322` tests green) |
| **Stage 2L** | **Billing Handoff & Mandatory Billing Freeze** | **ACTIVE** | See [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md); paid billing frozen (`RAZORPAY_ENABLED=false`) |
| **Final Razorpay Phase** | Paid Settlement, `BLK-04`/`06`, UI Cutover & Live Ops | **DEFERRED** | Gated behind Chatbot Product Development and 7-point release gate |

---

## 2. Active Checkpoint: Stage 2L Billing Handoff & Mandatory Freeze

Stage 2K consolidated billing safety, checkout recovery, canonical advisory locking, worker heartbeats/monitoring, and isolated PostgreSQL 16 CI validation onto `main` at commit [`6872f471df201faeeae62decfb76552cdc28d084`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/commit/6872f471df201faeeae62decfb76552cdc28d084) ([PR #3](https://github.com/Sahibnarulaofficial/Aptlyst-AI/pull/3)).

> [!IMPORTANT]
> **Paid Razorpay Billing Is Frozen & NOT Production-Ready:** Keep `RAZORPAY_ENABLED=false` and `STRIPE_ENABLED=false` during Chatbot Product Development. See [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md) for the complete freeze policy, local quota overrides (`DEFAULT_FREE_MONTHLY_TOKEN_QUOTA`), and 7-point release gate.

### 2.1 Completed Stage 2J – 2K Capabilities (Merged on `main` @ `6872f47`)
- **Stage 2J Checkout Recovery & Option A Exclusivity (`f3a4b5c6d7e2`):** Durable `razorpay_checkout_recovery_worker.py`, UUID `recovery_claim_id` fencing, bounded retry backoff, and `(organization_id, environment)` active attempt uniqueness.
- **Stage 2K-B Lock Unification (`BLK-03`):** Provider-neutral `advisory_lock.py`, backward-compatible `razorpay/lock.py` facade, ascending multi-entity sort order, transitional user lock in `organizations.py`, and 18 dedicated advisory-lock tests.
- **Stage 2K-C Operational Hardening (`a4b5c6d7e8f3`):** `billing_worker_heartbeats` table, ownership-loss fencing, heartbeat retention cleanup, `billing_health.py` alerting CLI, `billing_reconciliation.py`, redacted `GET /health/db`, staging gate (`scripts/check_staging_migration_gate.py`), and `docs/operations/RENDER_DEPLOYMENT_GUIDE.md`.
- **Stage 2K-D1 / D2 / D3 CI & Test Isolation:** Isolated `postgres_api_test_helper.py`, fail-closed `REQUIRE_POSTGRES_TESTS=1` guard, `pg_api_integration` pytest marker separation, and async `get_db` fixture restoration in `test_ai_generation.py`.

### 2.2 Explicitly Deferred Scope (Final Razorpay Phase)
- **Payment Settlement & Quota Rollover (`BLK-06`):** `subscription.charged`, `payment.captured`, `payment.failed` webhook handlers, Pro `OrganizationBillingPeriod` provisioning, and pre-activation cross-provider conflict quarantine remain unimplemented.
- **Payment Binding Sweeper (`BLK-04`):** Automated binding of late captured payments (`pending_attempt_binding`) to timed-out checkout attempts remains unimplemented.
- **Billing UI Razorpay Cutover:** `app/(app)/billing/page.tsx` and `lib/api.ts` still invoke legacy Stripe endpoints (`/billing/checkout-session`, `/billing/portal`).
- **External Staging & Live Ops Verification:** Live/sandbox Razorpay credential calls, real Render Web/Worker deployments, `SIGTERM` grace checks, staging DB migrations, and external `billing_health --notify` scheduling have not yet been executed.

---

## 3. Current Verified Baseline Metrics (`main` @ `6872f47`)

| Verification Scope | Source | Passing | Skipped | Status |
| :--- | :--- | :---: | :---: | :--- |
| **PostgreSQL 16 CI Integration** | Post-merge Run `46048131091` (`aptlyst_ci_test`) | **106** | 0 | All passing (Alembic `a4b5c6d7e8f3`) |
| **Advisory Lock Unit Suite** | `apps/api/tests/test_advisory_lock.py` (CI Run `46048131091`) | **18** | 0 | All passing |
| **Focused Non-PG Regression Suite** | CI Run `46048131091` | **1,198** | 0 | All passing |
| **Total Post-Merge CI Verified** | [GitHub Actions Run `46048131091`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/46048131091) | **1,322** | 0 | `0` failed, `0` errors |

---

## 4. Next Authorized Engineering Focus

1. **Chatbot Product Development** (under mandatory Billing Freeze): build RAG knowledge ingestion (`/knowledge`), live LLM providers + streaming widget chat (`/embed/[id]`), and the remaining placeholder UI modules (`/conversations`, `/leads`, `/analytics`, `/integrations`, `/settings`).
2. **Final Razorpay Integration & Cutover** (separate authorization after product work): complete the 7-point gate in [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md).
