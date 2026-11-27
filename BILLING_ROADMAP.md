# Aptlyst AI — Billing Implementation Roadmap

This document outlines the authoritative sequence, architectural milestones, and verification gates for the Aptlyst AI billing engine.

---

## 1. Provider Strategy & Roadmap Summary

Aptlyst AI is transitioning its primary subscription billing provider from **Stripe** to **Razorpay Subscriptions**, anchored by a robust **provider-neutral foundation**:

```text
[Phase 5.1: Stripe Legacy] ──▶ [Phase 5.2A-C: Provider-Neutral Foundation] ──▶ [Phase 5.2D: Agreement Handlers & D-04A] ──▶ [Phase D-2 Stage 2: Settlement & Sweeper]
(Historical Audit Records)     (Neutral Tables, Webhooks, Leased Worker)      (Closed via PR #2 Merge: 6d11ec5)            (Active: BLK-03/08 Resolved; BLK-04..07 Pending)
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

## 3. Milestone D-2 Stage 2: Razorpay Settlement & Concurrency Unification (Active)

We are currently executing **D-2 Stage 2: Preparatory Phase (TASK-D2-S2-PREP-01)**.

> [!IMPORTANT]
> **Completion Status**: **IN PROGRESS — NOT COMPLETE**.
> TASK-D2-S2-PREP-01 resolves blockers BLK-03 and BLK-08 only. Implementation of settlement handlers, sweeper background tasks, and schema modifications remains gated until remaining blockers BLK-04 through BLK-07 are formally cleared.

### 3.1 Stage 2 Blocker Matrix

| Blocker ID | Domain | Scope & Objective | Resolution Status |
| :--- | :--- | :--- | :---: |
| **BLK-03** | Concurrency / Locks | Advisory lock primitive unification (`advisory_lock.py`, facade in `razorpay/lock.py`, transitional dual-lock in `organizations.py`, strictly ascending multi-entity sort order) | **RESOLVED** (PREP-01) |
| **BLK-08** | Documentation | Canonical roadmap and ADR-005 synchronization reflecting D-04A closure and D-2 Stage 2 status | **RESOLVED** (PREP-01) |
| **BLK-04** | Data Integrity | Payment binding sweeper schema & query design for timed-out attempts | **PENDING** |
| **BLK-05** | State Machine | Checkout exclusivity, recovery lifecycle & settlement state machine policy | **DESIGN FINALIZED (ADR-006 Option A Authorized)** — *Implementation Pending* |
| **BLK-06** | Resilience | Reconciliation quarantine & out-of-order webhook isolation | **PENDING** |
| **BLK-07** | Test Harness | Live PostgreSQL multi-tenant fixture harness for parallel webhook/checkout concurrency | **PENDING** |

> [!NOTE]
> **BLK-05 Status Note**: The Product Owner has authorized **Option A** (production billing databases are live-only; test-mode billing runs in isolated development, staging, and CI databases), and **ADR-006 documentation is finalized** (supporting rationale preserved in `docs/decisions/ADR-006-TEST-LIVE-SUBSCRIPTION-DECISION-BRIEF.md`). Decisions include defensive `(organization_id, environment)` attempt exclusivity, preserved global active-subscription uniqueness, live-only production entitlements, read-only Stripe archive, NULL-safe recovery worker leases with `recovery_claim_id` fencing, and platform-staff authorization with two-person approval. **Implementation remains strictly pending and NOT complete**: production safety safeguards (Stripe `livemode` enforcement, `accounting.py` test-environment exclusion, Razorpay pre-activation conflict reconciliation), schema migrations, recovery worker, staff auth boundaries, and settlement reconciliation are **not yet implemented** and remain gated behind separate implementation authorization and BLK-04, BLK-06, and BLK-07.

---

## 4. Strict Boundary Rules for Stage 2 Settlement Implementation

When authorization is granted following resolution of BLK-04 through BLK-07, full Stage 2 settlement implementation will enact:

1. **`subscription.authenticated` Settlement Handler**:
   - Records authentication payment in `billing_payments`.
   - Transitions subscription agreement to `authenticated`.
   - Binds originating checkout attempt if `checkout_attempt_id` is present.
2. **`subscription.charged` / `payment.captured` Settlement Handler**:
   - Acquires Level 0 organization advisory lock via canonical `derive_advisory_lock_key("organization", org_id)`.
   - Follows monotonic lock ordering ($L_{0A} \prec L_{0B} \prec L_1 \prec L_2 \prec L_3$).
   - Multi-entity entity locks acquired in ascending signed 64-bit integer order.
   - Conservative rollover: provisions future period (`P2`) starting at `P1.period_end`.
   - Emits paid invoice in `billing_invoices`.
3. **Delayed Payment Settlement for Terminal Subscriptions**:
   - Settles payment and generates invoice for bookkeeping.
   - Enforces *Refusal of Resurrection*: terminal agreement status remains `canceled` or `completed`.
4. **Payment Binding Sweeper**:
   - Discovers captured payments staged with `pending_attempt_binding` whose checkout attempt timed out before webhook arrival.
   - Preserves *Refusal to Rewrite History*: links payment to expired attempt without mutating `attempt.status = 'expired'`.

> [!CAUTION]
> **Implementation Prohibition**: Under no circumstances should settlement handler business logic or database schema changes be committed or merged until all Stage 2 blockers (BLK-04 through BLK-07) are formally designed, reviewed, and approved.
