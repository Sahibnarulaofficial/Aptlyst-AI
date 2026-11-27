# Aptlyst AI — Stage 2L Billing Handoff & Freeze Specification

- **Document Status:** Canonical Operational Reference (Stage 2L)
- **Effective Date:** 2026-11-28
- **Canonical Baseline (`main`):** [`6872f471df201faeeae62decfb76552cdc28d084`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/commit/6872f471df201faeeae62decfb76552cdc28d084) ([PR #3](https://github.com/Sahibnarulaofficial/Aptlyst-AI/pull/3))
- **Post-Merge CI Reference:** [GitHub Actions Run `46048131091`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/46048131091)
- **Purpose:** Formal engineering handoff from completed Stage 2K billing infrastructure into **Chatbot Product Development**, establishing a mandatory **Billing Freeze** and defining the **Final Razorpay Release Gate**.

> [!IMPORTANT]
> **Paid Razorpay Billing Is NOT Production-Ready.**
> Stage 2K completed and validated the core billing safety, locking, recovery, webhook-queue, monitoring, and PostgreSQL integration infrastructure in code and CI. Full paid-subscription settlement (`subscription.charged`, `payment.captured`, `payment.failed`), Pro quota provisioning, the payment-binding sweeper (`BLK-04`), frontend Razorpay checkout cutover, and live staging/production operational verification remain deferred to the **Final Razorpay Integration & Cutover Phase**.

---

## 1. Baseline & Evidence Distinction

### 1.1 Verified Merge & CI Evidence (Code-Level)
- **Merged Commit:** `6872f471df201faeeae62decfb76552cdc28d084` on `main` (PR `#3`).
- **Database Engine & Schema:** `PostgreSQL 16 (160016)` on CI database `'aptlyst_ci_test'`, Alembic linear migration chain verified at head **`a4b5c6d7e8f3`** (`billing_worker_heartbeats` on top of `f3a4b5c6d7e2`).
- **Post-Merge CI Suite Breakdown ([Run `46048131091`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/46048131091)):**
  - **PostgreSQL 16 Integration (`REQUIRE_POSTGRES_TESTS=1`):** **`106` passed**, `0` skipped, `0` failed, `0` errors across all 9 live PostgreSQL test modules (`14 + 20 + 14 + 16 + 11 + 4 + 3 + 3 + 21`).
  - **Advisory Lock Unit Suite:** **`18` passed**, `0` skipped, `0` failed.
  - **Focused Offline / Non-PG Regression Suite:** **`1,198` passed**, `0` skipped, `0` failed.
  - **Total Post-Merge CI Verified Tests:** **`1,322` passed**, `0` skipped, `0` failed, `0` errors.

### 1.2 Boundary: Code-Level CI vs External Operational Validation
To prevent premature production claims, Aptlyst AI distinguishes two verification tiers:

1. **Completed & Proven (Code & PostgreSQL 16 CI):**
   - SQLAlchemy models, Alembic migrations (`3ba61d54e4d9` through `a4b5c6d7e8f3`), PostgreSQL immutability triggers, partial unique indexes, composite foreign key `fk_billing_payments_checkout_attempt_composite` paired with check constraint `ck_billing_payments_checkout_attempt_complete` (closing PostgreSQL's `MATCH SIMPLE` null-bypass behavior), transactional advisory-lock ordering, fail-closed config/environment validation, mocked worker lease-fencing logic, and isolated CI test harnesses.
2. **Not Yet Performed (External Staging / Live Infrastructure):**
   - Zero live or sandbox Razorpay API calls or real dashboard webhook deliveries.
   - Zero staging or production Render Web/Worker deployments or Render Dashboard `SIGTERM` grace-period checks.
   - Zero staging database migrations or live cron/alerting runs via `billing_health --notify`.

---

## 2. Completed Stage 1 – 2K Billing Infrastructure

All of the following subsystems are implemented and present on `main` (`6872f47`):

1. **Provider-Neutral Foundation & Provenance (Phases 5.2A–5.2D / D-04A):**
   - Provider-neutral models (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`, `OrganizationBillingPeriod`, `PaymentReconciliation`) and Alembic migrations through `e2f3a4b5c6d1` (payment association, composite FK, immutability triggers).
   - Preserved read-only/legacy Stripe records and routes without adding Stripe functionality.
2. **Stage 1–2G Exception, Agreement & Exclusivity Hardening:**
   - Explicit exception hierarchy for webhook and checkout failure classification.
   - Razorpay webhook ingress (`/api/v1/billing/razorpay/webhook`) with constant-time HMAC signature verification and `billing_webhook_events` queue persistence.
   - Registered Razorpay agreement handlers in `apps/api/app/services/billing/razorpay/agreement_handlers.py` (`subscription.authenticated`, `subscription.activated`, `subscription.updated`, `subscription.completed`) enforcing terminal state preservation (*Refusal of Resurrection*).
   - Migration `f3a4b5c6d7e2` introducing `recovery_claim_id` / recovery lease columns and environment-scoped active checkout uniqueness, enforcing **Option A** from [ADR-006](../decisions/ADR-006-CHECKOUT-EXCLUSIVITY-AND-RECOVERY.md).
3. **Stage 2J Checkout Recovery & Lease Fencing:**
   - Durable `razorpay_checkout_recovery_worker.py` claiming `recovery_required` checkout attempts via `SELECT ... FOR UPDATE SKIP LOCKED`, UUID fencing tokens (`recovery_claim_id`), bounded backoff `[60s, 300s, 900s, 1800s, 3600s]` with attempt caps, and zero DB locks held during external Razorpay HTTP inquiries.
4. **Stage 2K-B & Stage 2K-C Operational Hardening, Heartbeats & Runbook:**
   - Canonical advisory-lock unification (`app/services/billing/advisory_lock.py`, backward-compatible `razorpay/lock.py` facade, transitional user lock in `organizations.py`), unifying advisory-lock primitives, ascending multi-entity sort order, and existing `L0A`/`L0B` calls (full settlement-handler `L0A -> L0B -> L1 -> L2 -> L3` enforcement remains deferred to Final Razorpay Release Gate 1).
   - Migration `a4b5c6d7e8f3` adding `billing_worker_heartbeats`, worker ownership-loss fencing (`instance_token`), retention pruning, `billing_health.py` inspection/alerting CLI, `billing_reconciliation.py` operator tooling, `GET /health/db` redacted diagnostics, `scripts/check_staging_migration_gate.py`, and [`RENDER_DEPLOYMENT_GUIDE.md`](RENDER_DEPLOYMENT_GUIDE.md).
5. **Stage 2K-D1 / D2 / D3 Test Isolation & CI Integrity:**
   - Dedicated PostgreSQL 16 integration helper (`apps/api/tests/postgres_api_test_helper.py`), fail-closed `REQUIRE_POSTGRES_TESTS=1` guard (`test_postgres_test_guard.py`), `pg_api_integration` pytest marker separating live-PG API tests from offline tests, async `get_db` dependency-override restoration in `test_ai_generation.py`, and dedicated PostgreSQL 16 CI job validation.
6. **Stage 2K-E Readiness & Merge Gate:**
   - Operational readiness and merge-readiness audits verified prior to PR `#3` merge (`6872f47`).

---

## 3. Explicitly Deferred Paid-Billing Scope (Final Razorpay Phase)

The following items are **NOT implemented or NOT production-verified** and remain reserved exclusively for the future **Final Razorpay Integration & Cutover Phase**:

1. **Payment Settlement Webhook Handlers:**
   - `subscription.charged`, `payment.captured`, and `payment.failed` are not registered on `default_razorpay_dispatcher` and remain unimplemented.
2. **Pro Tier Entitlement & Quota Provisioning:**
   - `subscription.activated` currently transitions `Subscription.status` to `'active'` without promoting `plan_tier` to `'pro'` or provisioning `OrganizationBillingPeriod` monthly token quotas; Pro quota rollover/provisioning awaits settlement handlers and non-zero commercial `DEFAULT_PRO_MONTHLY_TOKEN_QUOTA` configuration.
3. **Payment Binding Sweeper & Remaining Stage 2 Blockers (`BLK-04` / `BLK-06`):**
   - Automated sweeper linking late payments staged with `pending_attempt_binding` to expired checkout attempts without mutating `expired` attempt history (`BLK-04`), plus pre-activation cross-provider conflict reconciliation / `pending` quarantine handling (`BLK-06`) and remaining multi-tenant parallel concurrency soak coverage (`BLK-07`).
4. **Frontend Billing UI Cutover:**
   - `app/(app)/billing/page.tsx` and `lib/api.ts` currently call legacy endpoints (`/api/v1/organizations/{org_id}/billing/checkout-session` and `/portal`); they have not yet been migrated to `/api/v1/organizations/{org_id}/billing/razorpay/subscription` and `/verify` with Razorpay Checkout.js UX.
5. **Staging & Live Operational Validation:**
   - Deploying all three Render services (`aptlyst-api`, `aptlyst-razorpay-webhook-worker`, `aptlyst-razorpay-recovery-worker`), verifying Render's `SIGTERM` grace period (`>= 60s` recommended for recovery worker), running `scripts/check_staging_migration_gate.py` against real staging, testing real Razorpay test-mode webhook delivery end-to-end, and wiring a persistent `billing_health --notify` scheduler.

---

## 4. Mandatory Billing Freeze Policy

Effective immediately until the Final Razorpay Integration & Cutover Phase is explicitly authorized:

1. **Payment Providers Disabled by Default:**
   - Keep `RAZORPAY_ENABLED=false` and `STRIPE_ENABLED=false` across local development, product testing, and previews.
2. **Strict Environment Isolation:**
   - Keep `RAZORPAY_ENVIRONMENT=test` in all non-production configs (`ENVIRONMENT` in `development`, `staging`, `test`, `ci`). Never place `live` credentials (`rzp_live_*`), live webhook secrets, or production `DATABASE_URL` strings in development environments, source control, or logs.
3. **Aptlyst AI Canonical Merchant Invariant:**
   - Aptlyst AI operates under a single canonical merchant account identity (`RAZORPAY_MERCHANT_ACCOUNT_ID`). Do not introduce multi-merchant or customer-connected gateway logic, and do not re-enable or add Stripe billing features.
4. **No Production Operations or Schema Drift:**
   - No live Razorpay API calls, no live charges/subscriptions, no staging/production database migrations, and no edits to existing billing domain contracts or historical Alembic revisions during chatbot product work.
   - Any bug discovered in frozen billing modules must be reported rather than bypassed or weakened.

---

## 5. Chatbot Product Development Handoff

With the billing freeze active, engineering focus moves safely to **Chatbot Product Development**.

### 5.1 Current Product Baseline vs Planned Scope
- **Implemented Today on `main` (`6872f47`):**
  - Multi-tenant organization management (`apps/api/app/api/organizations.py`) and Supabase SSR JWT auth (`/(auth)/*`, `lib/supabase/*`).
  - Authenticated dashboard shell and implemented pages (`/(app)/dashboard`, `/chatbots`, `/chatbots/new`, `/chatbots/[id]`, and legacy `/billing`).
  - Chatbot CRUD API (`apps/api/app/api/chatbots.py`) and visitor conversation/message API foundation (`apps/api/app/api/public_chatbots.py`) backed by HMAC visitor tokens, rate limiting, and `reserve_tokens()` accounting.
  - Phase 1 LLM configuration (`LLM_PROVIDER="disabled"` by default, `"mock"` for deterministic tests) and preview-only embed route (`app/embed/[id]/page.tsx`).
- **Not Yet Implemented (Safe Product Focus Areas & `6` Milestone Placeholders):**
  - **RAG Knowledge Base Pipeline:** Document ingestion, chunking, embeddings, retrieval, and `app/(app)/knowledge/page.tsx` (placeholder page).
  - **Live AI Provider & Streaming Widget:** Live LLM provider integration (extending beyond Phase 1 `disabled`/`mock`), SSE streaming responses, and enabling live interactive messaging in `app/embed/[id]/page.tsx` (currently preview-only with disabled composer).
  - **Conversations, Leads, Analytics, Integrations & Settings UIs:** Currently milestone placeholder pages in `app/(app)/conversations/page.tsx`, `leads/page.tsx`, `analytics/page.tsx`, `integrations/page.tsx`, and `settings/page.tsx`.
  - **Public Marketing / Landing Copy:** Keep public claims strictly aligned with actually implemented features at all times.

### 5.2 Local Chatbot Testing Under Billing Freeze
- By design (`apps/api/app/config.py`), `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA` defaults to `0` (fail-closed), and `LLM_PROVIDER` defaults to `"disabled"`.
- Developers testing chatbot conversations or token-quota reservation locally can set in their **local untracked `.env`**:
  - `RAZORPAY_ENABLED=false` and `STRIPE_ENABLED=false` (freeze preserved)
  - `LLM_PROVIDER=mock` (or an authorized product LLM provider once implemented)
  - A positive integer for `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA` (e.g., `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA=100000`) to test Free-tier token accounting without activating any payment gateway.
- Note: Do **not** weaken fail-closed defaults (`0` / `false`) in `apps/api/app/config.py` or `.env.example`.

---

## 6. Final Razorpay Release Gate (Pre-Production Checklist)

Paid Razorpay billing **MUST NOT** be enabled in production until all **7 gates** are completed and verified:

1. **Gate 1 — Settlement & Quota Handlers Implemented:** `subscription.charged`, `payment.captured`, and `payment.failed` implemented with level-0 monotonic locking and idempotent `OrganizationBillingPeriod` Pro quota rollover provisioning.
2. **Gate 2 — Sweeper & Reconciliation Clearance:** `BLK-04` (`pending_attempt_binding` sweeper) and `BLK-06` (pre-activation cross-provider conflict guard & quarantine rules) implemented and tested against live PostgreSQL 16.
3. **Gate 3 — Billing UI Cutover:** Next.js `app/(app)/billing/page.tsx` and `lib/api.ts` migrated cleanly from legacy Stripe routes to Razorpay subscription checkout, callback verification, and status rendering while keeping historical Stripe records readable where authorized.
4. **Gate 4 — PostgreSQL 16 CI Suite Green:** Full CI test suite (including live PostgreSQL concurrency, webhook replay, recovery lease fencing, and settlement regression tests) passes with zero skipped billing gates.
5. **Gate 5 — Staging Render & Sandbox Verification:** `aptlyst-api`, `aptlyst-razorpay-webhook-worker`, and `aptlyst-razorpay-recovery-worker` deployed to staging following [`RENDER_DEPLOYMENT_GUIDE.md`](RENDER_DEPLOYMENT_GUIDE.md), passing `check_staging_migration_gate.py`, graceful `SIGTERM` checks, `billing_health --environment test`, and real Razorpay test-mode webhook lifecycle verification.
6. **Gate 6 — Alerting & Configuration Audit:** External `billing_health --notify` alerting configured with durable state/runbook ownership, `DEFAULT_PRO_MONTHLY_TOKEN_QUOTA` set to an approved positive integer, `RAZORPAY_ENVIRONMENT=live`, `rzp_live_*` credentials, and canonical non-placeholder `RAZORPAY_MERCHANT_ACCOUNT_ID` verified exclusively in production.
7. **Gate 7 — Explicit Go-Live Authorization:** Formal sign-off confirming tenant isolation, Option A live-only database hygiene, and rollback readiness prior to toggling `RAZORPAY_ENABLED=true` in production.
