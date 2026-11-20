# ADR-006 Supporting Decision Brief: Test and Live Subscription Lifecycle

> **Document Type:** Standalone Architectural Decision Brief (Preserved Rationale & Decision Record)
> **Related ADR:** `docs/decisions/ADR-006-CHECKOUT-EXCLUSIVITY-AND-RECOVERY.md` (**Accepted — Option A Authorized; Implementation Pending**)
> **Baseline Evidence:** Repository `D:\Projects\V1\Aptlyst-AI-d2-stage2-locks`, branch `implementation/d2-stage2-lock-unification`, commit `72e108431385f6587fb6b0d1c576f69fa236f963`
> **Decision Status:** `AUTHORIZED BY PRODUCT OWNER (Option A Selected — Implementation Pending)`

---

## A. Executive Summary

This decision brief documents the architectural analysis, failure scenarios, option comparison, and final Product Owner decision for the test/live subscription lifecycle boundary associated with **ADR-006 (Organization-Wide Checkout Exclusivity, Recovery Lifecycle, and Webhook Reconciliation Policy)**.

During the final read-only review of ADR-006, source-level verification established that combining `(organization_id, environment)` checkout attempt scoping with global `uq_org_active_subscription` uniqueness and environment-blind accounting (`organization_billing_periods`, `accounting.py`) is **not internally safe if a test subscription can ever transition to `active` or `trialing` in the same database as live billing**:
- An active `test` subscription occupies the global `uq_org_active_subscription` slot and can cause a subsequent paying customer's `live` subscription activation webhook to fail with SQLSTATE `23505` and **quarantine immediately on attempt 1 with zero retries and no reconciliation case**.
- When a live Pro subscription is `past_due` (within its grace period), `uq_org_active_subscription` does not block a `test` subscription from becoming `active`; once both rows exist, `accounting.py` finds two eligible subscriptions and **fails closed (`RECONCILIATION_REQUIRED`), blocking production LLM inference for a paying customer**.
- Legacy Stripe handlers do not inspect `livemode` or `environment` at all and can directly provision real Pro `OrganizationBillingPeriod` rows from test events.

**Recorded Product Owner Decision:**
The Product Owner has explicitly authorized **Option A**: **Production billing databases are live-only. Test-mode billing and test subscription lifecycles must run in isolated development, staging, or CI databases.** Customer-facing subscription testing inside production is not required.

**Current Governance Status:**
- **Option A is authorized** by the Product Owner as the architectural direction, and **ADR-006 has been revised and finalized** accordingly.
- **Option B remains rejected** due to verified uniqueness and accounting failure modes.
- **Option C remains preserved as a future alternative** only if a genuine product requirement for customer-facing production sandbox billing emerges later.
- **Application implementation remains strictly pending/blocked** awaiting separate implementation authorization (none of the proposed Option A safeguards, recovery worker changes, or schema migrations have been implemented).

---

## B. Current Architecture

This section distinguishes **verified existing implementation**, **identified defects**, and **proposed future safeguards** (none of which are implemented at commit `72e1084`).

### 1. Razorpay Subscription Environment Handling (Existing Implementation)
- **Provisioning & checkout scope:** `_get_provisioning_scope()` (`apps/api/app/services/billing/razorpay/customer.py:141-178`) validates `settings.razorpay_environment` (`"test"` or `"live"`) and `settings.razorpay_merchant_account_id`. When `settings.environment == "production"`, it fails closed unless `RAZORPAY_ENVIRONMENT == "live"` (`customer.py:162-171`); in non-production environments, it fails closed unless `RAZORPAY_ENVIRONMENT == "test"` (`customer.py:172-176`).
- **Webhook ingress provenance:** `get_webhook_scope()` (`apps/api/app/services/billing/razorpay/webhook_ingress.py:87-100`) reads `settings.razorpay_environment` and `settings.razorpay_merchant_account_id`. After HMAC-SHA256 verification (`webhook_ingress.py:140-190`), `ingest_razorpay_webhook_event()` (`webhook_ingress.py:296-303`) persists `BillingWebhookEvent.environment` using the deployment's configured environment. Razorpay webhook JSON envelopes do not carry a signed `environment` attribute; separation relies on distinct test/live webhook secrets and deployment configuration.
- **Worker scope validation:** Before handler dispatch, `webhook_engine.py:1062-1082` checks `event.environment == expected_environment` and quarantines mismatches immediately (`failure_category="scope_mismatch"`). Inside handlers, `validate_razorpay_event_envelope()` (`apps/api/app/services/billing/razorpay/handler_base.py:496-550`) enforces the same check and raises `RazorpayScopeMismatchError` on mismatch.
- **Subscription creation and activation:** Razorpay checkout creation (`apps/api/app/services/billing/razorpay/checkout.py:230-755`) does **not** create `Subscription` rows. `Subscription` rows for Razorpay are created and mutated solely by webhook agreement handlers:
  - `handle_subscription_authenticated` (`apps/api/app/services/billing/razorpay/agreement_handlers.py:80-299`, instantiation at `187-200`) persists `Subscription(provider="razorpay", environment=ctx.environment, status=SubscriptionStatus.incomplete, plan_tier="free")`.
  - `handle_subscription_activated` (`agreement_handlers.py:305-510`, status update at `411-416`, instantiation at `427-442`) sets `status=SubscriptionStatus.active` while keeping `plan_tier="free"`.
  - `handle_subscription_updated` (`agreement_handlers.py:517-645`, lines `635-637`) can also transition `incomplete` or `paused` rows to `active`.
  - `handle_subscription_completed` (`agreement_handlers.py:650-755`, lines `745-748`) sets `status=SubscriptionStatus.canceled` and `plan_tier="free"`.
  - No Razorpay handler assigns `SubscriptionStatus.trialing`.
- **Checkout Phase B guard (`checkout.py:370-388`):** Checks for an existing `Subscription` matching `organization_id == organization.id`, `status IN ('active', 'trialing')`, and `plan_tier == "pro"`. This query has **no `environment` filter** and **ignores `status='active'` rows with `plan_tier='free'`**.
- **Identified defect in `handle_subscription_activated`:** Unlike Stripe handlers, `handle_subscription_activated` performs **no pre-activation conflict query** for another active subscription on the same organization (`agreement_handlers.py:349-361` filters strictly to the incoming `provider_subscription_id` and `ctx.environment`) and does not catch `IntegrityError`.

### 2. Legacy Stripe Environment Assignment & Missing `livemode` Enforcement (Existing Implementation & Defect)
- **Hardcoded `legacy` environment:** `Subscription.__init__()` (`apps/api/app/models/subscription.py:167-182`) defaults any Stripe-instantiated subscription (`stripe_customer_id` present) to `provider="stripe"`, `merchant_account_id="default"`, and `environment="legacy"` (`subscription.py:171-175`).
- **Missing `livemode` validation (Identified Defect):** Neither `livemode` nor `environment` is checked anywhere in `apps/api/app/services/billing/stripe_*.py`.
  - `get_or_create_stripe_customer` (`apps/api/app/services/billing/stripe_customer.py:176-185`) creates a shell `Subscription(status=incomplete, plan_tier="free", environment="legacy")`.
  - `create_stripe_checkout_session` (`apps/api/app/services/billing/stripe_checkout.py:688-702`) blocks checkout if **any** `Subscription` for the organization has `status IN ('active', 'trialing')` (regardless of provider, environment, or `plan_tier`).
  - `handle_checkout_session_completed` (`apps/api/app/services/billing/stripe_checkout_handler.py:285-314, 366, 466-485`), `stripe_subscription_handler.py:266-294, 350-352`, and `stripe_subscription_updated_handler.py:290-318, 415-429` can set `active` or `trialing` without checking `livemode`. They include an explicit global `Active Subscription Conflict Guard` (`stripe_checkout_handler.py:285-314`) that raises `WebhookReconciliationConflictError` if another subscription ID is already `active`/`trialing`.
  - `handle_invoice_paid` (`apps/api/app/services/billing/stripe_invoice_paid_handler.py:530-553, 674, 702, 865, 890`) locks `Subscription` by `(stripe_subscription_id, provider="stripe")` with **zero `livemode`/`environment` checks** and directly creates active Pro `OrganizationBillingPeriod` rows (`plan_tier="pro"`, `is_active=True`).

### 3. Global Active-Subscription Uniqueness (Existing Database Constraint)
- `subscriptions` enforces a global partial unique index (`apps/api/app/models/subscription.py:204-208`; migration `apps/api/alembic/versions/20261026_1945_b2c3d4e5f6a1_add_phase2a_billing_and_generation.py:230`):
  - **Index:** `uq_org_active_subscription` on `(organization_id) WHERE status IN ('active', 'trialing')`.
  - **Scope:** Global across all providers (`razorpay`, `stripe`) and all environments (`live`, `test`, `legacy`).
  - **Omission:** Does **not** include `past_due` in its predicate.
- By contrast, provider identity indexes `uq_subscriptions_provider_customer` (`subscription.py:210-216`) and `uq_subscriptions_provider_subscription` (`subscription.py:218-224`) include `(provider, merchant_account_id, environment, ...)`, so `test` and `live` rows with distinct provider IDs can coexist in `subscriptions` as long as at most one row has `status IN ('active', 'trialing')`.

### 4. Single-Authority `OrganizationBillingPeriod` (Existing Implementation)
- `OrganizationBillingPeriod` (`apps/api/app/models/billing.py:43-196`) has **no `environment` column** and enforces `uq_org_active_billing_period` on `(organization_id) WHERE is_active = true` (`billing.py:192-196`; migration `20261026_1945_b2c3d4e5f6a1_add_phase2a_billing_and_generation.py:60`).
- Active billing periods are created only at organization signup (`apps/api/app/api/organizations.py:146`), free-period rollover (`apps/api/app/services/billing/accounting.py:1170-1219`), and Stripe `invoice.paid` settlement (`stripe_invoice_paid_handler.py:674, 702, 865, 890`).
- Razorpay payment settlement handlers (`subscription.charged`, `payment.captured`, `invoice.paid`) are **not yet implemented**; any such webhook delivered to the Razorpay worker currently quarantines as `unknown_event` (`webhook_engine.py:1084-1107`). Razorpay agreement handlers never mutate `OrganizationBillingPeriod`.

### 5. Environment-Blind Accounting Queries (Existing Implementation & Defect)
- For any paid `OrganizationBillingPeriod` (`plan_tier != "free"`), `reserve_tokens()` (`apps/api/app/services/billing/accounting.py:258-336`) locks `Subscription` rows matching:
  - `Subscription.organization_id == org_id` AND `Subscription.status IN ('active', 'past_due')` (`accounting.py:264-274`).
- **Identified Defect:** This query applies **no filter on `Subscription.environment`, `Subscription.plan_tier`, or `period.subscription_id`**.
  - If `len(eligible_subs) > 1` (`accounting.py:276-290`), `reserve_tokens()` **fails closed with `AccountingErrorCode.RECONCILIATION_REQUIRED`**, denying LLM inference.
  - If `len(eligible_subs) == 1` and that row is a `test` subscription (`status='active', plan_tier='free', environment='test'`), `accounting.py:317-336` treats it as an entitled `active` subscription and authorizes Pro usage.
- Note also an existing divergence from ADR-003: `docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md:28-31` specifies that a canceled subscription should not revoke an already-paid active billing period, whereas `accounting.py:258-336` still enforces legacy Stripe coupled gating (`canceled` → `SUBSCRIPTION_INACTIVE`).

### 6. Webhook Quarantine Behavior on `uq_org_active_subscription` Violation (Existing Implementation)
- Razorpay handlers execute inside an inner savepoint (`work_db.begin_nested()`, `apps/api/app/services/billing/razorpay/webhook_engine.py:1112-1135`).
- If `handle_subscription_activated` violates `uq_org_active_subscription` (PostgreSQL SQLSTATE `23505`), the savepoint rolls back and `webhook_engine.py:1368` invokes `classify_db_error_to_razorpay_exception()` (`webhook_engine.py:451-479`).
- `apps/api/app/services/billing/webhook_errors.py:609-619` explicitly classifies `"uq_org_active_subscription"` in `UNIQUE_PERMANENT_CONSTRAINTS` (`UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT` → `DbRetryDisposition.PERMANENT`, `webhook_errors.py:919-920, 1019-1025`).
- Consequently, `webhook_engine.py:1373-1411` **immediately quarantines the webhook on attempt 1** (`status = quarantined`, `failure_category = "permanent_failure"`, **no retry backoff**, and **no `PaymentReconciliation` record created**).

### 7. Summary of Existing Behavior vs. Proposed Future Safeguards

| Capability | Existing Implementation (Commit `72e1084`) | Proposed / Not Yet Implemented |
| :--- | :--- | :--- |
| Razorpay deployment environment guard | Enforced (`customer.py:162-176`, `webhook_ingress.py:87-100`, `webhook_engine.py:1062-1082`, `handler_base.py:544-549`) | — |
| Cross-provider checkout attempt exclusivity per `(org_id, env)` | Not implemented (split tables; scoped per `(org, provider, merchant, env)` for Razorpay) | Proposed in ADR-006 Decision A & Phase 1–3 |
| Stripe `livemode` / `environment` enforcement | **Missing** (hardcoded `"legacy"`; `livemode` unchecked) | Authorized under Option A; **not yet implemented** |
| Accounting `Subscription.environment` filtering | **Missing** (`accounting.py:264-290` is environment-blind) | Authorized under Option A; **not yet implemented** |
| Razorpay pre-activation conflict guard + reconciliation staging | **Missing** (crashes directly into SQLSTATE `23505` quarantine with no reconciliation row) | Proposed safeguard (BLK-06) |
| Razorpay settlement handlers (`subscription.charged`, `payment.captured`) | **Not implemented** (`unknown_event` quarantine) | Blocked pending BLK-04 through BLK-07 |

---

## C. Five Failure Scenarios

The following five concrete scenarios were traced end-to-end against actual transaction, locking, constraint, accounting, and exception-handling code. Scenarios 1–5 assume test and live activity can reach the same database (for example, shared staging/production DB, pre-cutover test data carried into production, or a future same-DB test-mode feature).

### Scenario 1 — Test Subscription Activates Before a Live Checkout
- **Database behavior:**
  - A test checkout completes and test `subscription.activated` persists `Subscription(organization_id=org_id, provider="razorpay", environment="test", status="active", plan_tier="free")` (`agreement_handlers.py:427-442`).
  - Later, the organization initiates a live Razorpay checkout. Phase B (`checkout.py:370-388`) queries `Subscription` WHERE `status IN ('active', 'trialing') AND plan_tier == "pro"`. Because the test row has `plan_tier="free"`, **Phase B does not block the checkout**.
  - Live checkout creates a `BillingCheckoutAttempt(environment="live")`, creates a live Razorpay subscription, and the customer completes real payment. Live `subscription.authenticated` inserts `Subscription(environment="live", status="incomplete")` (`agreement_handlers.py:187-200`).
  - When live `subscription.activated` attempts to set the live subscription to `status="active"` (`agreement_handlers.py:416`), PostgreSQL rejects the flush and rolls back the savepoint.
  - *(Variant 1B: If a test subscription ever has `plan_tier="pro"`, or if the customer uses Stripe checkout where `stripe_checkout.py:688-702` omits `plan_tier == "pro"`, checkout creation is blocked upfront with HTTP 409 `ActiveSubscriptionExistsError`.)*
- **Unique constraint implications:** **Triggers `uq_org_active_subscription`** (SQLSTATE `23505`) on live `subscription.activated`.
- **Webhook outcome:** **Immediate quarantine on attempt 1** (`status="quarantined"`, `failure_category="permanent_failure"`, zero retries, no `PaymentReconciliation` case; `webhook_errors.py:611`, `webhook_engine.py:1373-1411`).
- **Production entitlement risk:** The test subscription itself does not create a Pro `OrganizationBillingPeriod`, **but** the live subscription cannot activate, preventing clean progression to live Pro settlement.
- **Customer impact:** **Severely stuck.** The customer paid real money, their live `BillingCheckoutAttempt` remains permanently stuck in `payment_pending` (blocking retries), and their live `Subscription` remains `incomplete` until manual database/operational intervention.

### Scenario 2 — Live Subscription Exists Before a Test Checkout (Including `past_due` Outage)
- **Database behavior:**
  - *Sub-case 2A (Live active Pro subscription, `plan_tier="pro"`):* Test Razorpay checkout hits Phase B (`checkout.py:370-388`, no `environment` filter), matches the live active Pro row, rolls back, and raises `ActiveSubscriptionExistsError` (HTTP 409). Database unchanged.
  - *Sub-case 2B (Live active pre-settlement subscription, `plan_tier="free"`):* Phase B does not block. Test checkout creates a test attempt (`environment="test"`), test `subscription.authenticated` inserts `Subscription(environment="test", status="incomplete")`, and test `subscription.activated` rolls back on flush.
  - *Sub-case 2C (Live Pro subscription is `past_due` within grace period):* `past_due` is **not** covered by `uq_org_active_subscription` (`WHERE status IN ('active', 'trialing')`). A test `subscription.activated` webhook **succeeds** in setting the test row to `status="active"`. The database now contains **two** rows matching `accounting.py:264`'s `eligible_statuses = ('active', 'past_due')`.
- **Unique constraint implications:**
  - 2A: Not triggered (rejected in application layer).
  - 2B: **Triggers `uq_org_active_subscription`** on test `subscription.activated`.
  - 2C (`past_due`): **Not triggered** during test activation; **will trigger later** when the live `past_due` subscription recovers to `active`!
- **Webhook outcome:** 2A: No webhook. 2B: Test activation **quarantines immediately on attempt 1** (`permanent_failure`). 2C: Test activation **succeeds**; subsequent live recovery webhook **quarantines immediately on attempt 1**.
- **Production entitlement risk:**
  - 2A & 2B: Live entitlement remains intact while live subscription stays `active`.
  - **2C (`past_due` accounting outage):** On the very next production LLM request, `accounting.py:264-290` queries `Subscription` WHERE `organization_id == org_id AND status IN ('active', 'past_due')` without filtering by `environment`, finds `len(eligible_subs) == 2`, and **fails closed with `AccountingErrorCode.RECONCILIATION_REQUIRED`**, causing an immediate production outage for a paying Pro customer inside their grace window.
- **Customer impact:** 2A: Live customer unaffected (test blocked). 2B: Live customer unaffected while `active`; test attempt stuck in `payment_pending`. **2C: Active paying Pro customer experiences an immediate production LLM outage and cannot auto-recover from `past_due`.**

### Scenario 3 — Test Subscription Is Authenticated (`incomplete`) Before Live Checkout
- **Database behavior:**
  - Test `subscription.authenticated` persists `Subscription(environment="test", status="incomplete", plan_tier="free")` and moves test `BillingCheckoutAttempt` to `payment_pending` (`agreement_handlers.py:187-200, 292`).
  - Live checkout initiates (`environment="live"`). Phase B passes (no `active`/`trialing` Pro sub; no live active attempt). Live `subscription.authenticated` inserts `Subscription(environment="live", status="incomplete")` (allowed because `uq_subscriptions_provider_subscription` includes `environment`). Live `subscription.activated` sets live `Subscription.status = "active"` and moves live attempt to `complete`.
- **Unique constraint implications:** **Not triggered** during live activation (`incomplete` is excluded from `uq_org_active_subscription`). If a delayed test `subscription.activated` arrives later while live is `active`, `uq_org_active_subscription` **triggers** on the test event.
- **Webhook outcome:** Live activation **succeeds**. Any delayed test activation later **quarantines immediately on attempt 1** (`permanent_failure`) — *unless* the live subscription has transitioned to `past_due`, in which case delayed test activation succeeds and triggers the Scenario 2C `accounting.py` outage.
- **Production entitlement risk:** None while live stays `active`; **high (`RECONCILIATION_REQUIRED` outage)** if delayed test activation lands during a live `past_due` window, or **false Pro authorization** if delayed test activation lands after live cancellation while a paid billing period is still active (`accounting.py:264-336`).
- **Customer impact:** None under normal flow; outage risk if delayed test webhook coincides with live `past_due`.

### Scenario 4 — Test and Live Webhook Events Arrive Concurrently
- **Database behavior:**
  - In a single deployment configured with `RAZORPAY_ENVIRONMENT="live"`, test events are quarantined before handler execution (`webhook_engine.py:1062-1082`, `failure_category="scope_mismatch"`).
  - If two worker deployments (test and live) share the same database, or if a future multi-environment worker processes both concurrently: `handle_subscription_activated` acquires Level 0 advisory locks on `("subscription", sub_id)` (`agreement_handlers.py:335`). Because `test_sub_id != live_sub_id`, the two transactions acquire **different advisory locks and do not serialize**. Both execute in parallel and race to insert/update `status="active"`.
- **Unique constraint implications:** **Triggers `uq_org_active_subscription`** for whichever transaction flushes/commits second.
- **Webhook outcome:** First committer **succeeds**; second committer **immediately quarantines on attempt 1** (`permanent_failure`, zero retries, no `PaymentReconciliation` record).
- **Production entitlement risk:** Non-deterministic race. If the `test` event commits first, the `live` activation is permanently quarantined, breaking the live entitlement lifecycle.
- **Customer impact:** **Non-deterministic lockout.** If the test webhook wins the race, a paying live customer is permanently stuck in `payment_pending` / `incomplete`.

### Scenario 5 — Test Subscription Is Cancelled or Completed Before Live Activation
- **Database behavior:**
  - Test `subscription.completed` (`agreement_handlers.py:745-748`) sets `Subscription.status = "canceled"` and `plan_tier = "free"`.
  - Subsequent live checkout and live `subscription.activated` insert/update `Subscription(environment="live", status="active")`.
- **Unique constraint implications:** **Not triggered** (`canceled` is excluded from `WHERE status IN ('active', 'trialing')`, vacating the slot).
- **Webhook outcome:** **Succeeds** cleanly.
- **Production entitlement risk:** None.
- **Customer impact:** None; live subscription activates normally.

### Additional Verified Cross-Provider Hazard — Stripe Test Invoice Settlement
- **Code path:** `stripe_checkout_handler.py:466-485`, `stripe_subscription_handler.py:350-352`, `stripe_subscription_updated_handler.py:415-429`, and `stripe_invoice_paid_handler.py:530-553, 674, 702, 865, 890`.
- **Behavior:** Legacy Stripe webhook ingestion and handlers perform **zero `livemode` or `environment` checks**, store all subscriptions with `environment="legacy"`, and on `invoice.paid` directly deactivate the current billing period and insert a new active Pro `OrganizationBillingPeriod(plan_tier="pro", is_active=True)`.
- **Impact:** Any Stripe test-mode event (`livemode=false`) signed by the configured webhook secret will **activate a `Subscription` in `uq_org_active_subscription` AND grant real production Pro token quotas** on the organization's single active `OrganizationBillingPeriod`.

### Scenario Summary Table

| Scenario | `uq_org_active_subscription` Triggered? | Webhook Outcome | Production Entitlement Risk | Customer Impact |
| :--- | :--- | :--- | :--- | :--- |
| **1. Test active → live checkout** | Yes (on live activation) | Live event immediately quarantined (attempt 1, no reconciliation row) | Live activation blocked before settlement | **Stuck** after paying real money (`payment_pending` / `incomplete`) |
| **2. Live exists → test checkout** | 2A: No; 2B: Yes (on test); **2C (`past_due`): No on test, Yes on live recovery** | 2A: None; 2B: Test quarantined; **2C: Test succeeds, live recovery quarantined** | **2C: Production LLM outage (`RECONCILIATION_REQUIRED` in `accounting.py:276`)** | 2A/2B: Live unaffected; **2C: Paying Pro customer blocked** |
| **3. Test `incomplete` → live checkout** | No on live; Yes if test activates later while live `active` | Live succeeds; delayed test quarantines (unless live is `past_due`) | Outage if delayed test hits live `past_due`; false Pro auth if live `canceled` | None normally; outage risk during `past_due` |
| **4. Concurrent test & live webhooks** | Yes (for losing transaction in shared DB) | Winner succeeds; loser immediately quarantined (attempt 1) | Race can quarantine live activation | **Non-deterministic lockout** if test wins |
| **5. Test canceled/completed → live** | No | Live succeeds | None | None |
| **Stripe test `invoice.paid` (bonus hazard)** | Only if another active sub exists | Succeeds | **Directly creates live Pro `OrganizationBillingPeriod`** | Unauthorized Pro quota granted from test payment |

---

## D. Architecture Options

### Option A — Production Databases Are Live-Only
- **Definition:**
  - Production databases strictly process `live` billing traffic only (`RAZORPAY_ENVIRONMENT="live"`, Stripe `livemode=true`).
  - Test billing runs exclusively in isolated development, staging, or CI databases (never sharing a database with production and never migrating test rows into production).
  - Test subscriptions never become active production `Subscription` rows; production entitlements are driven exclusively by verified live events.
  - Existing global `uq_org_active_subscription` on `subscriptions(organization_id)` and single-authority `OrganizationBillingPeriod` are preserved unchanged.
  - Defense-in-depth safeguards added during implementation: `Subscription.environment != "test"` filter in `accounting.py:264-274`, Stripe `livemode` verification on webhook ingress/handlers, and an explicit pre-activation conflict guard with `PaymentReconciliation` staging in `handle_subscription_activated`.
  - Checkout attempt uniqueness on `(organization_id, environment)` remains a safe hygiene boundary for non-production/CI databases without implying production test/live coexistence.

### Option B — Test Subscriptions May Become Active But Cannot Grant Entitlements
- **Definition:**
  - Allows `environment="test"` subscriptions to enter `status="active"` or `"trialing"` inside the same database under the existing global `uq_org_active_subscription` index, relying on settlement handlers not to create Pro `OrganizationBillingPeriod` rows for test events.
- **Why this option is unsafe (Not Recommended):**
  - Directly reproduces **Scenario 1** (active test subscription blocks live subscription activation and permanently quarantines live webhooks after real payment).
  - Directly reproduces **Scenario 2C** (test subscription activating while a live subscription is `past_due` causes `accounting.py:276-290` to fail closed with `RECONCILIATION_REQUIRED`, causing a production LLM outage and blocking live recovery).
  - Directly reproduces **Scenario 4** (concurrent test/live race can quarantine live payment).
  - Allows false Pro authorization in `accounting.py:264-336` if a live subscription is canceled while a paid period remains active.
  - Provides no defensible implementation path without first dismantling the global index and rewriting accounting — at which point it effectively becomes Option C with worse isolation.

### Option C — Separate Test Subscription Records (Same-Database Test & Live Coexistence)
- **Definition:**
  - Supports end-to-end test and live subscriptions coexisting for the same organization inside the production database.
  - Production `subscriptions` and `organization_billing_periods` remain strictly live-only (plus legacy Stripe), while test subscriptions are stored in an isolated test table (e.g., `test_subscriptions`) or behind an environment-partitioned subscription + entitlement architecture.
- **Required changes across subsystems:**
  - **Data model & migrations:** New isolated test subscription model/table (or migrating `uq_org_active_subscription` to `(organization_id, environment)` plus strict environment check constraints and foreign-key separation so `organization_billing_periods` can only reference live subscriptions).
  - **Webhook ingress & workers:** Decouple `webhook_ingress.py:87-100`, `webhook_engine.py:1062-1082`, and `handler_base.py:544-549` from single-environment deployment enforcement; maintain separate test and live webhook endpoints, secrets, and routing/workers; add Stripe `livemode` routing.
  - **Agreement & settlement handlers:** Route test events to isolated test persistence without touching production `subscriptions` or `organization_billing_periods`.
  - **Accounting & entitlement checks:** Enforce strict `environment="live"` (or `!= "test"`) filtering and `period.subscription_id` alignment in `accounting.py:258-336` so test records can never trigger `len(eligible_subs) > 1` or authorize Pro usage.
  - **Checkout services:** Scope Phase B active-subscription checks (`checkout.py:370-388`, `stripe_checkout.py:688-702`) strictly by environment and expose explicit environment selection/authorization at API boundaries.

### Comparative Evaluation

| Evaluation Criterion | Option A (Live-Only Production DB) | Option B (Active Test Sub, No Entitlements) | Option C (Separate Test Subscription Records) |
| :--- | :--- | :--- | :--- |
| **Correctness** | **High.** Eliminates Scenarios 1, 2C, and 4 in production without weakening global uniqueness. | **Defective.** Fails Scenarios 1, 2C, and 4; contradicts ADR-006's safety goals. | **High once fully built**, provided every query, index, and handler enforces environment separation. |
| **Production Entitlement Safety** | **Very High** (single authority preserved + `accounting.py` and Stripe `livemode` defense-in-depth). | **Low / Unsafe** (`past_due` `RECONCILIATION_REQUIRED` outage; false Pro auth; Stripe `livemode` gap). | **High after full refactor**, but higher regression surface during ongoing feature development. |
| **Customer Experience** | **Best for live billing:** Paying customers are never blocked or quarantined by test state. No production sandbox billing. | **Unacceptable:** Paying live customers can be wedged in `payment_pending` or hit LLM outages. | Supports customer-facing production sandbox billing if desired; live customers remain isolated from test state. |
| **Operational Complexity** | **Low.** Aligns with existing single-env deployment guards (`customer.py:162-176`, `webhook_engine.py:1062-1082`). Requires strict DB/secret hygiene between staging and prod. | **High incident burden:** Requires manual DB un-wedging and quarantined webhook replay whenever test rows collide with live billing. | **High.** Dual webhook endpoints/secrets, dual workers or multi-env dispatch, dual tables/routing, and extra observability. |
| **Migration Scope** | **Minimal:** Zero schema change to `subscriptions` or `organization_billing_periods`; targeted code guards only. | Zero schema change, but unsafe. | **Large:** New Alembic schema/tables or unique index redesign + multi-module refactor across checkout, webhooks, workers, and accounting. |
| **Future Extensibility** | Cleanly defers customer sandbox billing to a dedicated future milestone if product ever requests it. | Dead end; must be replaced before live launch or sandbox support. | Directly enables customer-facing sandbox billing in production. |
| **Main Risks & Trade-Offs** | Cannot run live-and-test subscriptions side-by-side inside the same production DB; requires enforcing that staging DB snapshots/data are never promoted into production. | Live customer lockout, production LLM outage during `past_due`, silent attempt-1 quarantine without reconciliation records. | Significant engineering delay to D-2 Stage 2 settlement; increased maintenance and cross-environment bug surface if no customer sandbox product exists. |

---

## E. Product Questions Considered

The following product and operational questions framed the Product Owner decision between Option A and Option C (resolved in favor of **Option A**; customer-facing subscription testing inside production is not required):

1. **Must customers be able to run end-to-end test subscriptions inside the production database?**
2. **Is Razorpay test mode intended only for internal development, staging, and CI?**
3. **Will Aptlyst AI expose a customer-facing sandbox billing feature?**
4. **Should test and live billing data ever share an organization record in the production database?**
5. **What operational separation is expected between test and live webhook endpoints, credentials, and workers?**

---

## F. Technical Recommendation (Historical Pre-Decision Basis)

> **Notice:** The engineering recommendation below was prepared prior to Product Owner authorization and is preserved here as historical rationale supporting the final decision in Section G.

- **If production databases are live-only (no same-DB production test subscriptions):** **Option A is strongly recommended.** It aligns with the existing deployment-level environment guards (`customer.py:162-176`, `webhook_engine.py:1062-1082`), preserves `uq_org_active_subscription` and single-authority `OrganizationBillingPeriod` with zero subscription schema migration, and closes all identified failure modes once paired with `accounting.py` environment filtering, Stripe `livemode` enforcement, and a Razorpay pre-activation conflict guard.
- **If end-to-end test subscriptions inside the production database ever become a genuine product requirement:** **Option C is required** as a dedicated future architectural milestone. Option A cannot represent active test subscriptions in a shared production database without violating live isolation.
- **Option B is rejected** under all product directions due to verified live-checkout wedging (Scenario 1), `past_due` production LLM outages (Scenario 2C), and non-deterministic activation races (Scenario 4).

---

## G. Decision Record

`Decision status: AUTHORIZED BY PRODUCT OWNER`

| Field | Entry |
| :--- | :--- |
| **Selected option** | **Option A — Production billing databases are live-only. Test-mode billing and test subscription lifecycles must run in isolated development, staging, or CI databases.** |
| **Product rationale** | Customer-facing subscription testing inside production is not required. Production billing databases remain strictly live-only, while test billing is confined to isolated non-production databases. Option C remains preserved only as a future alternative if a genuine product requirement emerges; Option B is rejected due to identified accounting and uniqueness failure modes. |
| **Approved constraints** | 1. Production databases must not activate test-mode subscriptions.<br>2. Production entitlements must be derived exclusively from verified live billing events.<br>3. Keep the global active-subscription uniqueness constraint (`uq_org_active_subscription`) on `organization_id`.<br>4. Test-mode Razorpay credentials, webhook endpoints, workers, and databases must be isolated from production.<br>5. Stripe webhook processing must validate `livemode` and trusted environment provenance before any subscription or entitlement mutation.<br>6. Accounting must explicitly exclude test-environment subscriptions from production entitlement decisions.<br>7. Test-mode billing must never grant production Pro quotas or interfere with production usage accounting.<br>8. Production checkout and webhook handling must fail safely when environment provenance cannot be established.<br>*(Note: Proposed `(organization_id, environment)` checkout-attempt exclusivity on `billing_checkout_attempts` is retained as defensive isolation.)* |
| **Decision date** | *(Unfilled)* |
| **Approver** | *(Unfilled)* |
| **Follow-up ADR revision required** | **Documentation finalized:** `docs/decisions/ADR-006-CHECKOUT-EXCLUSIVITY-AND-RECOVERY.md` revised to encode Option A, `recovery_claim_id`, and the NULL-safe recovery eligibility predicate (`BILLING_ROADMAP.md` and `PROJECT_STATUS.md` synchronized). **Application implementation remains pending separate authorization.** |

---

## H. Dependencies and Downstream Work

Architectural authorization of Option A finalizes the design baseline only. **All implementation items below remain PENDING / NOT YET IMPLEMENTED** and require separate implementation authorization:

1. **ADR-006 architectural documentation finalization (Documentation Complete; Implementation Pending):**
   - Option A policy, `recovery_claim_id` schema addition, and NULL-safe claim predicate incorporated into `docs/decisions/ADR-006-CHECKOUT-EXCLUSIVITY-AND-RECOVERY.md`.
   - `PROJECT_STATUS.md:48` corrected so already-implemented `subscription.authenticated` is not described as blocked.
2. **Environment isolation and Stripe `livemode` enforcement (PENDING — Not Yet Implemented):**
   - Enforce `livemode` validation and trusted environment provenance on legacy Stripe webhook ingress and handlers (`stripe_checkout_handler.py`, `stripe_subscription_handler.py`, `stripe_subscription_updated_handler.py`, `stripe_invoice_paid_handler.py`) and resolve explicit environment tagging for new unified Stripe writes.
   - Codify operational deployment guardrails for isolated test vs. live credentials, webhook secrets, endpoints, workers, and databases.
3. **Accounting environment safeguards (PENDING — Not Yet Implemented):**
   - Add explicit `Subscription.environment` exclusion (`environment != 'test'`) and ADR-003 alignment review in `apps/api/app/services/billing/accounting.py:258-336` so non-live rows can never trigger `RECONCILIATION_REQUIRED` or falsely authorize paid usage.
4. **Webhook conflict handling and reconciliation — BLK-06 (PENDING — Not Yet Implemented):**
   - Add an explicit pre-activation active-subscription conflict guard in `handle_subscription_activated` (`agreement_handlers.py:305-442`) that stages a durable `PaymentReconciliation` case (matching Stripe's conflict guard pattern) rather than failing silently into attempt-1 `permanent_failure` quarantine without a reconciliation record.
   - Implement reconciliation processing for `missing_checkout_attempt` cases (`agreement_handlers.py:239-264`).
5. **Recovery worker, schema migration, and platform-staff security — BLK-04 & BLK-05 Implementation (PENDING — Not Yet Implemented):**
   - Alembic migration for `(organization_id, environment)` active attempt uniqueness and recovery worker columns (`next_recovery_retry_at`, `recovery_claim_expires_at`, `recovery_claim_id`, `recovery_attempts`).
   - Background recovery worker invoking `recover_razorpay_checkout_attempt()` (`checkout.py:762-1260`, currently zero callers) with NULL-safe eligibility predicate and `recovery_claim_id` fencing, plus payment binding sweeper (BLK-04).
   - Security design and implementation for platform-staff JWT boundary, two-person approval, and tamper-resistant audit logging (ADR-006 Decision D).
6. **PostgreSQL concurrency validation — BLK-07 (PENDING — Not Yet Implemented):**
   - Live PostgreSQL 16 integration tests covering Option A isolation guards, cross-provider checkout exclusivity, recovery worker `FOR UPDATE SKIP LOCKED` claiming, stale-worker `recovery_claim_id` fencing, and Stage 2 settlement handlers (`subscription.charged`, `payment.captured`, `payment.failed`).
