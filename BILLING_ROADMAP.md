# Aptlyst AI — Billing Implementation Roadmap

This document outlines the authoritative sequence, architectural milestones, and verification gates for the Aptlyst AI billing engine.

---

## 1. Provider Strategy & Roadmap Summary

Aptlyst AI is transitioning its primary subscription billing provider from **Stripe** to **Razorpay Subscriptions**, anchored by a robust **provider-neutral foundation**:

```text
[Phase 5.1: Stripe Legacy] ──▶ [Phase 5.2A-D / D-04A] ──▶ [Stage 2J–2K: Recovery, Ops & CI] ──▶ [Stage 2L: Billing Freeze] ──▶ [Final Razorpay Cutover]
(Historical Audit Records)     (Neutral Foundation)       (Merged via PR #3: 6872f47)            (Active: Chatbot Dev)         (Settlement, BLK-04, UI, Live)
```

---

## 2. Milestone Execution Status

### Milestone 5.1: Initial Stripe Billing (Historical)
- **Status**: **Completed & Preserved**
- **Artifacts**: `apps/api/app/services/billing/stripe_*.py`, `stripe_invoices`, `stripe_subscriptions`.
- **Purpose**: Maintained for backward compatibility and historical financial auditing.

### Milestone 5.2A: Provider-Neutral Database Foundation
- **Status**: **Completed & Verified**
- **Artifacts**: Migration `20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py`.
- **Entities**: `billing_customers`, `billing_checkout_attempts`, `billing_invoices`, `billing_payments`, `billing_webhook_events`.

### Milestone 5.2B: Customer Provisioning & Scoped Uniqueness
- **Status**: **Completed & Verified**
- **Artifacts**: Migrations `b1c2d3e4f5a6` and `c1d2e3f4a5b7`.
- **Features**: Partial unique index on active checkout attempts per organization and provider.

### Milestone 5.2C: Webhook Ingestion & Background Queue Worker
- **Status**: **Completed & Verified**
- **Artifacts**: Migration `d1e2f3a4b5c6`, `/api/v1/billing/razorpay/webhook`, `app/workers/razorpay_webhook_worker.py`.
- **Features**: Fast constant-time HMAC signature verification, leased event claiming, exponential backoff, dead-letter queue, CLI runner.

### Milestone 5.2D / D-04A: Razorpay Agreement Handlers & Payment Association
- **Status**: **Formally Closed & Verified**
- **Verification Gate**: PR #2 merged into `main` at commit `6d11ec53dab47c4159f56c9b119762ced146033a`.
- **CI Run**: Post-merge GitHub Actions Run #17 (`43898958221`) green on PostgreSQL 16.16 (423 tests passed, 0 skipped, 0 failed).
- **Scope Completed**:
  - DDL migration `e2f3a4b5c6d1` (Payment Association & Composite FK).
  - PostgreSQL procedural triggers for payment and period provenance immutability.
  - Option A decoupling, refusal of resurrection, and conservative rollover invariants proven.
  - 14 PostgreSQL live provenance integration tests passed in CI.

---

## 3. Stages 2J – 2K Completion & Stage 2L Billing Freeze

**Stage 2K Billing Infrastructure (`PR #3` / `6872f471df201faeeae62decfb76552cdc28d084`) is merged into `main` and CI-verified on PostgreSQL 16 (Run `46048131091`: `1,322` passed, `0` skipped, `0` failed, `0` errors).**
Platform work is now in **Stage 2L: Billing Handoff & Freeze** ([`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md)). Paid Razorpay billing remains **NOT production-ready** until the Final Razorpay Integration & Cutover Phase completes.

### 3.1 Completed Since D-04A (Merged on `main` @ `6872f47`)
- **Stage 2J (Recovery Worker & Migration `f3a4b5c6d7e2`):** Leased `razorpay_checkout_recovery_worker.py`, UUID `recovery_claim_id` fencing, bounded retry backoff, and `(organization_id, environment)` active attempt exclusivity per ADR-006 Option A.
- **Stage 2K-B & 2K-C (Unification, Heartbeats & Ops Hardening):** Canonical `advisory_lock.py`, migration `a4b5c6d7e8f3` (`billing_worker_heartbeats`), ownership-loss fencing, `billing_health.py` inspection/alerting CLI, `billing_reconciliation.py`, redacted `GET /health/db`, staging gate (`scripts/check_staging_migration_gate.py`), and [`docs/operations/RENDER_DEPLOYMENT_GUIDE.md`](docs/operations/RENDER_DEPLOYMENT_GUIDE.md).
- **Stage 2K-D1 / D2 / D3 (PostgreSQL 16 & Offline Test Isolation):** Dedicated `postgres_api_test_helper.py`, fail-closed `REQUIRE_POSTGRES_TESTS=1` guard, `pg_api_integration` marker separation, async `get_db` fixture restoration in `test_ai_generation.py`, and dedicated PostgreSQL 16 CI job (`106` PG16 integration + `18` advisory-lock + `1,198` regression = `1,322` passed).

### 3.2 Deferred Scope & Blocker Matrix (Final Razorpay Cutover Phase)

| Blocker / Gate ID | Domain | Scope & Objective | Status (Stage 2L) |
| :--- | :--- | :--- | :---: |
| **BLK-03** | Concurrency / Locks | Advisory lock primitive unification (`advisory_lock.py`, `razorpay/lock.py` facade, multi-entity sort order) | **COMPLETED** (`6872f47`) |
| **BLK-08** | Documentation | Canonical roadmap, ADR-005/006, and Stage 2L handoff/freeze synchronization | **COMPLETED** |
| **BLK-05** | Checkout & Option A | Live-only production isolation, `(organization_id, environment)` attempt exclusivity, and `razorpay_checkout_recovery_worker.py` | **CORE MERGED** (`6872f47`) *(settlement transitions deferred)* |
| **BLK-07** | Test Harness | Dedicated PostgreSQL 16 CI integration harness (`106` PG16 tests green in Run `46048131091`) | **CORE MERGED** (`6872f47`) *(settlement soak tests deferred)* |
| **BLK-04** | Data Integrity | Payment binding sweeper for `pending_attempt_binding` & timed-out attempts | **DEFERRED** (Final Razorpay Phase) |
| **BLK-06** | Reconciliation & Settlement | `subscription.charged` / `payment.captured` / `payment.failed` settlement, Pro quota rollover provisioning, pre-activation conflict guard | **DEFERRED** (Final Razorpay Phase) |
| **UI & Live Ops** | Frontend & Render | Migrate Next.js billing UI from legacy Stripe routes; validate real Render services, staging Razorpay webhooks, and persistent `billing_health --notify` | **DEFERRED** (Final Razorpay Phase) |

> [!NOTE]
> See [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md) for the mandatory **Billing Freeze** (`RAZORPAY_ENABLED=false`, `STRIPE_ENABLED=false`, no live payments or staging/production DB migrations during product dev) and the **7-Point Final Razorpay Release Gate**.

---

## 4. Strict Boundary Rules for the Final Razorpay Integration Phase

When explicit authorization is granted following chatbot product development, the Final Razorpay Integration Phase must still enact:

1. **`subscription.charged` / `payment.captured` / `payment.failed` Settlement Handlers:**
   - Record settled or failed payments in `billing_payments`, emit invoices in `billing_invoices`, and handle dunning/grace transitions cleanly.
   - Acquire Level 0 organization advisory lock via canonical `derive_advisory_lock_key("organization", org_id)` and follow monotonic lock ordering (`L0A < L0B < L1 < L2 < L3`).
   - Provision future Pro `OrganizationBillingPeriod` quota (`DEFAULT_PRO_MONTHLY_TOKEN_QUOTA > 0`) via conservative rollover (`P2` starts at `P1.period_end`).
2. **Delayed Payment Settlement for Terminal Subscriptions & Reconciliation:**
   - Settle payment and generate bookkeeping invoice while enforcing *Refusal of Resurrection* (terminal agreement status remains `canceled` or `completed`).
3. **Payment Binding Sweeper (`BLK-04`) & Conflict Guard (`BLK-06`):**
   - Discover captured payments staged with `pending_attempt_binding` whose checkout attempt timed out before webhook arrival without mutating `attempt.status = 'expired'` (*Refusal to Rewrite History*), and enforce pre-activation conflict quarantine.
4. **Frontend Razorpay Cutover & Live Staging Validation:**
   - Migrate `app/(app)/billing/page.tsx` and `lib/api.ts` away from legacy Stripe routes and pass all 7 gates in [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md).

> [!CAUTION]
> **Freeze Enforcement:** Under **Stage 2L Billing Freeze**, do not implement paid settlement handlers, alter frozen billing migrations/contracts, or enable `RAZORPAY_ENABLED=true` / `STRIPE_ENABLED=true` until Chatbot Product Development hands back cleanly and the Final Razorpay Phase is explicitly authorized.
