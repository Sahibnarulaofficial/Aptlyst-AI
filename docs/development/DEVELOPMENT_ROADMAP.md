# Aptlyst AI — Platform Development Roadmap

## 1. Milestone Progression Overview

Aptlyst AI follows a staged, gate-controlled engineering roadmap. Each milestone requires comprehensive test coverage and formal architectural review before subsequent implementation phases are authorized.

```text
Phase 1: Project Foundation & Next.js Shell             [COMPLETED]
Phase 2: Multi-Tenant Database & Supabase Auth          [COMPLETED]
Phase 3: AI Chatbot Engine & RAG Knowledge Pipeline     [COMPLETED]
Phase 4: Embeddable Web Widget & Lead Capture           [COMPLETED]
Phase 5.1: Initial Stripe Billing Integration           [COMPLETED - Historical]
Phase 5.2: Provider-Neutral Generalized Billing Engine  [ACTIVE - Step 5.2D.3.3A.2]
Phase 6: Multi-Channel Messaging & Live Agent Handoff   [PLANNED]
Phase 7: Advanced Analytics & Conversion Attribution    [PLANNED]
```

---

## 2. Detailed Breakdown: Phase 5.2 Billing Engine

### Phase 5.2A – Provider Abstraction Foundation
- **Status**: Completed
- **Deliverables**: Provider-neutral database entities (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`). Migration `a1b2c3d4e5f7`.

### Phase 5.2B – Customer Provisioning & Attempt Uniqueness
- **Status**: Completed
- **Deliverables**: Partial uniqueness indexes on active checkout attempts (`organization_id`, `provider` where `status = 'open'`). Migration `b1c2d3e4f5a6` and `c1d2e3f4a5b7`.

### Phase 5.2C – Webhook Ingestion & Lease Ownership Queue
- **Status**: Completed
- **Deliverables**: HMAC signature verification endpoint (`/api/v1/billing/razorpay/webhook`), lease-based background queue worker, exponential backoff retry system, and CLI command runner. Migration `d1e2f3a4b5c6`.

### Phase 5.2D – Razorpay Subscriptions & Payment Association
- **Step 5.2D.0 to 5.2D.2.3 (Foundation & Worker Audits)**: Completed.
- **Step 5.2D.3A to 5.2D.3.1.1 (Lifecycle & Shared Handlers)**: Completed.
- **Step 5.2D.3.2.1 to 5.2D.3.2.14 (Agreement Handlers & Concurrency Hardening)**: Completed (Design & Audits). Formulated *Refusal of Resurrection*, *Conservative Billing Policy*, *Refusal to Rewrite History*, Level 0 advisory locks, and monotonic lock ordering.
- **Step 5.2D.3.3A (Schema Migration & Constraints)**: Completed (Implementation). Migration `e2f3a4b5c6d1` authored, adding composite FKs, triggers, and unified conflict gate.
- **Step 5.2D.3.3A.1 (Conflict Gate Validation)**: Completed (Implementation). Authored 14 PostgreSQL integration scenarios.
- **Step 5.2D.3.3A.2 (PostgreSQL CI Configuration)**: **ACTIVE CHECKPOINT**. GitHub Actions workflow `.github/workflows/ci.yml` authored. **Pending execution against live CI container**.

---

## 3. Upcoming Development Steps

### Step 5.2D.3.3B: Razorpay Payment Settlement Handlers
- **Status**: **NOT STARTED (Pending Step 5.2D.3.3A.2 CI execution)**.
- **Scope**:
  1. Implement `subscription.authenticated` settlement handler.
  2. Implement `subscription.charged` / `payment.captured` settlement handler with conservative period rollover.
  3. Implement delayed payment settlement for terminal agreements (invoice-only, no resurrection).
  4. Implement `payment_binding_sweeper` for expired attempt payment rediscovery.

### Step 5.2D.3.4: Checkout Session Frontend Integration
- **Status**: Planned.
- **Scope**: Connect Next.js checkout UI with backend Razorpay subscription creation, razorpay.js modal launch, and `/verify` confirmation.

### Step 5.2D.4: Live Sandbox Gateway Validation
- **Status**: Planned.
- **Scope**: End-to-end sandbox testing using real Razorpay test keys, webhook simulator, and automated test cards/UPI handles.
