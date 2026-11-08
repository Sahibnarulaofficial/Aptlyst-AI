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

### 4.1 Currently Implemented (Verified in Code)
- **Entity Advisory Locking**: `acquire_razorpay_advisory_lock` in `apps/api/app/services/billing/razorpay/lock.py` acquires transaction-scoped advisory locks on `{"subscription", "payment", "invoice", "customer"}` entities using deterministic SHA-256 derivation.
- **Agreement Handler Row Locking**: Agreement handlers in `agreement_handlers.py` acquire row-level locks on `organization_billing_periods` before `subscriptions`.
- **Network Isolation**: All external network HTTP requests (Razorpay/Stripe API) execute strictly **outside** database transactions.

### 4.2 Approved Architectural Specification (Scheduled for Step 5.2D.3.3B)
- **Level 0A Organization Advisory Lock**:
  ```sql
  SELECT pg_advisory_xact_lock(hashtext('org:' || :organization_id));
  ```
  This transaction-scoped lock serializes all billing state modifications for the tenant and closes the concurrency void when an organization has zero existing billing periods.
- **Monotonic 5-Level Acquisition Sequence**:
  Transactions must acquire locks in strictly ascending order:
  1. **Level 0A**: `pg_advisory_xact_lock(hashtext('org:' || org_id))`
  2. **Level 0B**: `pg_advisory_xact_lock(hashtext('subscription:' || sub_id))`
  3. **Level 1**: `organization_billing_periods` (`SELECT ... FOR UPDATE`)
  4. **Level 2**: `subscriptions` (`SELECT ... FOR UPDATE`)
  5. **Level 3**: Auxiliary tables (`billing_invoices`, `billing_payments`, `billing_checkout_attempts`)

---

## 5. Current Subsystem Implementation Status

To maintain engineering accuracy, the billing architecture is classified into exact implementation states:

| Subsystem / Capability | Current Status | Notes & Blockers |
| :--- | :---: | :--- |
| **Historical Stripe Invoicing & Subscriptions** | **Implemented & Verified** | Legacy implementation preserved; unit tested in `test_stripe_*.py`. |
| **Provider-Neutral Database Models** | **Implemented & Verified** | Models in `app/models/billing.py` tested with 17 SQLite unit tests. |
| **Webhook Ingestion Endpoint (`/razorpay/webhook`)** | **Implemented & Verified** | HMAC signature verification and payload persistence unit tested. |
| **Webhook Background Worker Daemon & CLI** | **Implemented & Verified** | Lease claiming, backoff, and DLQ tested in unit test suite. |
| **Migration `e2f3a4b5c6d1` (Payment Association)** | **Implemented, Not Fully Validated** | DDL and conflict gate authored; **blocked by outstanding PostgreSQL CI execution**. |
| **PostgreSQL Integration Test Suite (14 tests)** | **Authored, Not Executed in CI** | `test_postgres_provenance_integration.py` requires live PostgreSQL service container. |
| **Razorpay Payment Settlement Handlers** | **Designed, Not Implemented** | Full mathematical design completed (Step 5.2D.3.2); Step 5.2D.3.3B pending. |
| **Payment Binding Background Sweeper** | **Designed, Not Implemented** | Sweeper for expired checkout payment rediscovery designed; Step 5.2D.3.3B pending. |
| **Live Razorpay Gateway Sandbox Verification** | **Pending Future Milestone** | No live API credentials or sandbox network tests executed to date. |
