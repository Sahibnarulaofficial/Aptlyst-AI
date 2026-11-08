# Aptlyst AI — Billing Implementation Roadmap

This document outlines the authoritative sequence, architectural milestones, and verification gates for the Aptlyst AI billing engine.

---

## 1. Provider Strategy & Roadmap Summary

Aptlyst AI is transitioning its primary subscription billing provider from **Stripe** to **Razorpay Subscriptions**, anchored by a robust **provider-neutral foundation**:

```text
[Phase 5.1: Stripe Legacy] ──▶ [Phase 5.2A-C: Provider-Neutral Foundation] ──▶ [Phase 5.2D: Razorpay Subscriptions]
(Historical Audit Records)     (Neutral Tables, Webhooks, Leased Worker)      (Option A Decoupling, Monotonic Locks)
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

### Milestone 5.2D: Razorpay Agreement Handlers & Payment Association
This is the active engineering phase, progressing through distinct sub-steps:

| Step | Scope | State | Verification Status |
| :--- | :--- | :---: | :--- |
| **5.2D.0 to 5.2D.2.3** | Webhook ingestion & worker concurrency audits | **Complete** | Unit tested in pytest suite |
| **5.2D.3A to 5.2D.3.1.1** | Subscription lifecycle design & shared dispatchers | **Complete** | Refusal of Resurrection invariant formalized |
| **5.2D.3.2.1 to 5.2D.3.2.14** | Agreement handlers, Option A decoupling & concurrency audits | **Complete** | Monotonic 5-level lock hierarchy & conservative retention proven |
| **5.2D.3.3A** | Schema migration `e2f3a4b5c6d1` (Payment Association & Composite FK) | **Implemented** | DDL, composite FK with null-bypass check, and triggers authored |
| **5.2D.3.3A.1** | Unified Candidate Conflict Detection Gate | **Implemented** | 14 integration test scenarios authored |
| **5.2D.3.3A.2** | PostgreSQL CI Workflow Configuration | **Active Checkpoint** | `.github/workflows/ci.yml` authored; **pending live CI container run** |
| **5.2D.3.3B** | Razorpay Settlement Handlers & Payment Binding Sweeper | **Blocked** | Implementation NOT authorized until 5.2D.3.3A.2 passes CI |
| **5.2D.3.4** | Checkout Session Verification & Frontend Integration | **Planned** | Razorpay checkout modal & `/verify` endpoint |
| **5.2D.4** | Live Razorpay Sandbox Gateway Validation | **Planned** | Sandbox credential testing & webhook simulation |

---

## 3. Strict Boundary Rules for Step 5.2D.3.3B

When authorized, **Step 5.2D.3.3B** will implement:

1. **`subscription.authenticated` Settlement Handler**:
   - Records authentication payment in `billing_payments`.
   - Transitions subscription agreement to `authenticated`.
   - Binds originating checkout attempt if `checkout_attempt_id` is present.
2. **`subscription.charged` / `payment.captured` Settlement Handler**:
   - Acquires Level 0 organization advisory lock (`pg_advisory_xact_lock`).
   - Follows monotonic lock ordering ($L_{0A} \prec L_{0B} \prec L_1 \prec L_2 \prec L_3$).
   - Conservative rollover: provisions future period (`P2`) starting at `P1.period_end`.
   - Emits paid invoice in `billing_invoices`.
3. **Delayed Payment Settlement for Terminal Subscriptions**:
   - Settles payment and generates invoice for bookkeeping.
   - Enforces *Refusal of Resurrection*: terminal agreement status remains `canceled` or `completed`.
4. **Payment Binding Sweeper**:
   - Discovers captured payments staged with `pending_attempt_binding` whose checkout attempt timed out before webhook arrival.
   - Preserves *Refusal to Rewrite History*: links payment to expired attempt without mutating `attempt.status = 'expired'`.

> [!CAUTION]
> **Implementation Prohibition**: Under no circumstances should Step 5.2D.3.3B code be committed or merged until Step 5.2D.3.3A.2 receives verified, green execution from the PostgreSQL CI service container.
