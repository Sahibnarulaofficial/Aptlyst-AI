# Aptlyst AI — Billing Architecture

## 1. Executive Summary & Provider Strategy

Aptlyst AI utilizes a **provider-neutral billing architecture** designed to decouple customer entitlements from payment gateway mechanics.

### 1.1 Provider Transition
- **Stripe (Historical)**: The initial billing engine was tightly coupled to Stripe. Stripe models (`stripe_customer_id`, `stripe_invoices`, `stripe_subscriptions`) remain in the database for backward compatibility and historical audit records.
- **Razorpay (Active Replacement)**: Selected as the primary payment gateway for domestic Indian Rupee (INR) transactions, Reserve Bank of India (RBI) recurring e-mandates, and domestic payment rails (UPI, Netbanking, Cards).
- **Provider-Neutral Abstraction**: Business logic interacts exclusively with generalized billing tables (`billing_customers`, `billing_checkout_attempts`, `billing_invoices`, `billing_payments`, and `organization_billing_periods`).

---

## 2. Decoupled Entitlement Model (Option A)

In Aptlyst AI, **agreements do not equal entitlements**. 

```
+----------------------------------------------------------------------------------+
|                            Payment Gateway Domain                                |
|                                                                                  |
|  +-------------------------------------+  +-----------------------------------+  |
|  |     BillingCheckoutAttempt          |  |           Subscription            |  |
|  | (Ephemeral intent, timeout states)  |  |  (Recurring mandate agreement)    |  |
|  +------------------+------------------+  +-----------------+-----------------+  |
+---------------------|---------------------------------------|--------------------+
                      |                                       |
                      | Provisions upon payment               | References (FK)
                      v                                       v
+----------------------------------------------------------------------------------+
|                             Entitlement Domain                                   |
|                                                                                  |
|  +-----------------------------------------------------------------------------+  |
|  |                       OrganizationBillingPeriod                             |  |
|  |  - tier: 'free' | 'pro' | 'enterprise'                                      |  |
|  |  - tokens_limit: e.g. 5,000,000                                             |  |
|  |  - period_start / period_end: Authoritative quota window                     |  |
|  |  - subscription_id: Historical provenance link                              |  |
|  +-----------------------------------------------------------------------------+  |
+----------------------------------------------------------------------------------+
```

### Key Lifecycle Principles:
1. **Conservative Retention (No Immediate Reset)**:
   When a customer resubscribes or changes plans, active billing periods are **never truncated**. The customer retains access to all days and tokens paid for until `period_end`. Future periods are queued to begin after `period_end`.
2. **Refusal of Resurrection**:
   When a subscription agreement is `canceled` or `completed`, it can never transition back to `active`. Any late payments settling against it generate invoices and update ledgers without reviving the canceled agreement.
3. **Refusal to Rewrite History**:
   A checkout attempt that times out (`expired`) remains permanently marked as `'expired'`. If late payment capture occurs at the gateway, bidirectional links and audit metadata associate the payment without mutating the expired attempt status.

---

## 3. Database Schema & Integrity Guarantees

### 3.1 Composite Foreign Key & PostgreSQL Null-Bypass Check
PostgreSQL defaults to `MATCH SIMPLE` on composite foreign keys, which silently bypasses constraint validation if any column is `NULL`. To prevent orphaned payment records:
- `billing_payments` references `billing_checkout_attempts` via a 6-column composite foreign key:
  `fk_billing_payments_checkout_attempt_composite` targeting `(checkout_attempt_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)`.
- A check constraint guarantees tuple completeness:
  ```sql
  CONSTRAINT ck_billing_payments_checkout_attempt_complete CHECK (
      (checkout_attempt_id IS NULL) OR (
          provider IS NOT NULL AND
          merchant_account_id IS NOT NULL AND
          environment IS NOT NULL AND
          provider_subscription_id IS NOT NULL AND
          provider_payment_id IS NOT NULL
      )
  )
  ```

### 3.2 Immutability Database Triggers
PostgreSQL procedural triggers prevent post-settlement tampering:
- Once an `organization_billing_periods.subscription_id` is set, updating it to a different non-null value triggers a database exception.
- Once a `billing_payments.provider_payment_id` is set, updating it triggers an exception.

### 3.3 Unified Provenance Conflict Migration Gate
In migration `e2f3a4b5c6d1`, candidate subscription-to-period links from both Stripe and provider-neutral invoices are aggregated into a single candidate set:
- If any billing period maps to more than one distinct subscription ID, the migration aborts.
- No heuristic tie-breaking (`MIN()`, `ORDER BY created_at`) is allowed.

---

## 4. Concurrency & Monotonic Lock Hierarchy

To prevent deadlocks between concurrent webhooks, checkout verification, and automated renewal workers, the billing engine defines two concurrency states:

### 4.1 Implemented Through Stage 2K (Verified in Code & PostgreSQL 16 CI)
- **Canonical Advisory Lock Module (`apps/api/app/services/billing/advisory_lock.py`)**: Provider-neutral lock derivation and acquisition, strictly ascending multi-entity sort order (`acquire_multi_entity_advisory_locks`), backward-compatible `apps/api/app/services/billing/razorpay/lock.py` facade, and transitional user lock ordering in `apps/api/app/api/organizations.py`.
- **Agreement Handler Row Locking & Triggers**: `agreement_handlers.py` acquires entity/row locks consistently, and migration `e2f3a4b5c6d1` enforces PostgreSQL immutability triggers on provenance columns.
- **Recovery & Worker Lease Fencing**: Migrations `f3a4b5c6d7e2` and `a4b5c6d7e8f3` enforce UUID `recovery_claim_id` fencing, `(organization_id, environment)` attempt uniqueness (ADR-006 Option A), `billing_worker_heartbeats`, and ownership-loss halts.
- **Network Isolation**: All external Razorpay/Stripe HTTP calls execute strictly **outside** database transactions and locks.

### 4.2 Deferred Settlement Concurrency & Release Gate (Final Razorpay Phase)
- Full Level 0A organization-to-auxiliary monotonic locking inside `subscription.charged` / `payment.captured` / `payment.failed` settlement handlers (`L0A -> L0B -> L1 -> L2 -> L3`), Pro `OrganizationBillingPeriod` provisioning, and the `BLK-04` payment-binding sweeper remain **not implemented** under the [Stage 2L Billing Freeze](../operations/BILLING_HANDOFF_AND_FREEZE.md).

---

## 5. Current Subsystem Implementation Status (`main` @ `6872f47`)

| Subsystem / Capability | Current Status | Notes & Gate |
| :--- | :---: | :--- |
| **Historical Stripe Invoicing & Subscriptions** | **Implemented (Legacy)** | Read-only compatibility preserved; no new Stripe functionality added. |
| **Provider-Neutral Models & Provenance (`a1b2c3d4e5f7`–`e2f3a4b5c6d1`)** | **Implemented & PG16 CI-Verified** | Immutability triggers, composite FKs, conflict gates verified on PostgreSQL 16 (Run `46048131091`). |
| **Webhook Ingestion Endpoint & Queue Worker** | **Implemented & CI-Verified** | HMAC verification, lease claiming, backoff, quarantine, heartbeats (`a4b5c6d7e8f3`). |
| **Checkout Recovery Worker & Exclusivity (`f3a4b5c6d7e2`)** | **Implemented & CI-Verified** | `razorpay_checkout_recovery_worker.py` + Option A `(organization_id, environment)` uniqueness. |
| **Advisory Lock Unification (`BLK-03`) & Ops Runbook** | **Implemented & CI-Verified** | `advisory_lock.py`, 18 tests, `billing_health.py`, `billing_reconciliation.py`, `RENDER_DEPLOYMENT_GUIDE.md`. |
| **Razorpay Payment Settlement & Pro Quota Rollover** | **Deferred (Not Implemented)** | `subscription.charged`, `payment.captured`, `payment.failed` & Pro `OrganizationBillingPeriod` provisioning await Final Razorpay Phase. |
| **Payment Binding Sweeper (`BLK-04`) & Conflict Guard (`BLK-06`)** | **Deferred (Not Implemented)** | Timed-out `pending_attempt_binding` sweeper & pre-activation conflict quarantine deferred. |
| **Next.js Billing UI Cutover & Live Render / Sandbox Validation** | **Deferred (Not Implemented)** | `app/(app)/billing/page.tsx` still calls legacy Stripe routes; zero real staging/live Razorpay runs yet. |

> [!IMPORTANT]
> Paid Razorpay billing is **frozen (`RAZORPAY_ENABLED=false`, `STRIPE_ENABLED=false`) and NOT production-ready**. See [`BILLING_HANDOFF_AND_FREEZE.md`](../operations/BILLING_HANDOFF_AND_FREEZE.md).
