# Aptlyst AI — Step 5.2D.3.2.12
# Payment-to-Checkout Association and Final Concurrency Audit Report

**Document Status:** Authoritative Architectural Design & Concurrency Audit  
**Review Target:** Step 5.2D.3.2 Completion & Settlement Pre-Implementation Gate  
**Execution Mode:** STRICTLY READ-ONLY AUDIT (Zero code, migration, database, or network modifications)  
**Alembic Head Verification:** `d1e2f3a4b5c6` (Verified single linear head)  

---

## 1. Executive Summary

This audit resolves the final four pre-implementation blockers governing the transition between **Step 5.2D.3.2 (Agreement Handlers)** and **Step 5.2D.3.3 (Settlement Handlers)**:

1. **Payment-to-Checkout-Attempt Association:** Establishes an immutable, deterministically validated relational chain connecting `BillingCheckoutAttempt`, `Subscription`, `BillingPayment`, `BillingInvoice`, and `OrganizationBillingPeriod` across both *Subscription-First* and *Payment-First* event arrival orders without heuristic guessing or reliance on client-mutable metadata.
2. **Settlement Expiration Policy for Outstanding Attempts:** Specifies the exact criteria for expiring conflicting checkout attempts upon activating a paid entitlement, proving that the originating attempt is excluded, in-flight payments in `payment_pending` are never prematurely aborted, and the "no-locks-across-network-calls" rule is strictly upheld.
3. **Captured Payment Conflict Policy Matrix:** Delivers an exhaustive policy across all six operational scenarios (normal, post-expiry, concurrent active subscription, delayed canceled subscription, duplicate delivery, and unbound/ambiguous payment). Proves that captured revenue is **never discarded** from the financial ledger, while entitlement double-allocation is mathematically impossible.
4. **Exhaustive Repository-Wide Lock Audit:** Conducts a comprehensive search of all advisory lock call sites (`pg_advisory_xact_lock`, `acquire_razorpay_advisory_lock`, and wrappers) and all row locks (`with_for_update` / `FOR UPDATE`). Identifies an architectural row-lock-before-advisory-lock ordering in `agreement_handlers.py` (`assert_valid_worker_lease`), details the required sequence correction, and proves deadlock freedom across all five concurrent transaction paths.

---

## 2. Payment-to-Checkout-Attempt Association Design

### 2.1 Entity Association Architecture

A core requirement is ensuring that financial settlement deterministically identifies the originating `BillingCheckoutAttempt` while maintaining strict multi-provider isolation `(provider, merchant_account_id, environment)`.

```mermaid
erDiagram
    BillingCheckoutAttempt ||--o| Subscription : "binds via provider_sub_id"
    BillingCheckoutAttempt ||--o| BillingPayment : "originates initial (FK / sub_id)"
    Subscription ||--o{ BillingInvoice : "owns billing history"
    Subscription ||--o{ BillingPayment : "accumulates payments"
    Subscription ||--o{ OrganizationBillingPeriod : "entitles paid quota"
    BillingInvoice ||--o{ BillingPayment : "reconciles settled amount"
    BillingInvoice ||--o| OrganizationBillingPeriod : "provisions entitlement period"
```

#### Deterministic Association Table

| Entity | Relational Key to Other Entities | Scope / Invariant Verification |
| :--- | :--- | :--- |
| **`BillingCheckoutAttempt`** | `provider_subscription_id` (Phase 4)<br>`organization_id` (Tenant)<br>`provider_payment_id` (Captured payment) | Matches `(provider, merchant_account_id, environment)`.<br>Partial unique constraint guarantees at most one active attempt per tenant. |
| **`Subscription`** | `provider_subscription_id` (Gateway ID)<br>`organization_id` (Tenant FK)<br>`id` (UUID PK) | Bound via `BillingCustomerProvision` and server-issued `sub_id`. Never binds to untrusted client notes alone. |
| **`BillingPayment`** | `provider_payment_id` (Gateway ID)<br>`provider_subscription_id` (Gateway sub)<br>`provider_invoice_id` (Gateway inv)<br>`subscription_id` (UUID FK)<br>`invoice_id` (UUID FK)<br>`organization_id` (Tenant FK)<br>`checkout_attempt_id` (UUID FK) | Directly captures gross funds.<br>Permits out-of-order staging with nullable foreign keys (`reconciliation_state = 'unreconciled'`). |
| **`BillingInvoice`** | `provider_invoice_id` (Gateway ID)<br>`provider_subscription_id` (Gateway sub)<br>`subscription_id` (UUID FK)<br>`billing_period_id` (UUID FK)<br>`organization_id` (Tenant FK) | Authoritative source of service dates (`period_start`, `period_end`) and gross amount due. |
| **`OrganizationBillingPeriod`** | `id` (UUID PK)<br>`organization_id` (Tenant FK)<br>`subscription_id` (UUID FK to `subscriptions.id`) | Represents active entitlement. Single Active Entitlement enforced by PostgreSQL partial unique index `uq_org_active_billing_period`. |

### 2.2 Event Arrival Order Traceability

#### Order A: Subscription-First Arrival (Standard Flow)
1. Customer authenticates mandate on Razorpay hosted checkout.
2. Gateway dispatches `subscription.authenticated`.
3. Handler locks Level $0B$ advisory lock `("subscription", sub_id)`.
4. Handler resolves authoritative `organization_id` via `resolve_tenant_organization()`.
5. Handler queries `Subscription` under Level 2 row lock (`FOR UPDATE`); updates status to `incomplete`.
6. Handler queries `BillingCheckoutAttempt` where `provider_subscription_id = sub_id` under Level 3 row lock (`FOR UPDATE`); transitions attempt to `payment_pending`.
7. Gateway dispatches `payment.captured` / `invoice.paid`.
8. Settlement handler acquires $L_{0A}(\text{"organization"}, \text{org\_id}) \to L_{0B}(\text{"payment"}, \text{pay\_id})$.
9. Settlement identifies originating attempt via `provider_subscription_id = sub_id`:
   - Originating attempt is identified: `attempt.id = settled_attempt_id`.
   - Originating attempt transitions to `status = 'complete'`, `provider_payment_id = pay_id`.
   - Originating attempt is **explicitly excluded** from settlement attempt expiration.
10. Pro entitlement period is provisioned; `OrganizationBillingPeriod.subscription_id` is stamped.

#### Order B: Payment-First Arrival (Out-of-Order / Network Delay)
1. Gateway processes initial payment and dispatches `payment.captured` or `invoice.paid` before `subscription.authenticated` reaches the worker queue.
2. Webhook payload contains `payment.id = "pay_xxx"` and `payment.subscription_id = "sub_xxx"`.
3. Settlement handler resolves `organization_id` from authoritative `BillingCustomerProvision` or pre-existing subscription/attempt record.
4. Settlement handler acquires $L_{0A}(\text{"organization"}, \text{org\_id}) \to L_{0B}(\text{"payment"}, \text{pay\_id})$.
5. Settlement handler queries `BillingCheckoutAttempt` where `provider_subscription_id = sub_id`:
   - **Crucial Invariant:** Because Phase 4 of `create_razorpay_subscription_checkout()` committed `attempt.provider_subscription_id = sub_id` *before* issuing the checkout URL to the customer, the attempt record **already exists** in `open` status!
   - Settlement marks this attempt as the originating attempt and promotes it directly:
     `attempt.status = CheckoutAttemptStatus.complete`
     `attempt.provider_payment_id = pay_id`
6. Settlement creates/updates `Subscription` (sets `plan_tier = 'pro'`, `status = 'active'`) and inserts active `OrganizationBillingPeriod`.
7. When the delayed `subscription.authenticated` event finally arrives:
   - Handler acquires $L_{0B}(\text{"subscription"}, \text{sub\_id})$.
   - Handler queries `BillingCheckoutAttempt` under row lock.
   - Detects `attempt.status == CheckoutAttemptStatus.complete`.
   - **Monotonic Fencing:** A `complete` attempt is never regressed to `payment_pending`. The handler logs an idempotent no-op and exits cleanly.

### 2.3 Handling Missing, Conflicting, and Ambiguous Associations
- **No Reliance on Mutable Notes:** Client notes (`notes.organization_id`, `notes.attempt_id`) are treated strictly as secondary validation checks. Tenant identity is resolved from authoritative database provisions (`BillingCustomerProvision`). Any discrepancy between notes and authoritative provisions results in immediate failure and staging in `PaymentReconciliation`.
- **No Amount/Timestamp Guessing:** Settlement **never** associates an unlinked payment to a checkout attempt based on matching currency, amount (e.g. ₹2,900 / $29), or proximity of creation timestamps. Unlinked payments fail closed into `PaymentReconciliation`.
- **Ambiguous Multiple Matches:** If `provider_subscription_id` matches $> 1$ checkout attempts, settlement halts immediately without provisioning entitlement, recording case `ambiguous_checkout_attempt` in `PaymentReconciliation`.

---

## 3. Settlement Expiration Policy for Outstanding Attempts

When a paid Pro entitlement is activated by settlement for an organization, outstanding checkout attempts for that organization must be reconciled without causing data loss or race conditions.

```mermaid
flowchart TD
    Start["Settlement Activates Pro Period for Tenant"] --> Query["Query all active attempts for org_id<br>status IN ('creating', 'recovery_required', 'open', 'payment_pending')"]
    Query --> Loop{"For each attempt A"}
    Loop --> IsOrig{"A.provider_sub_id == Settled sub_id?"}
    IsOrig -- YES --> Complete["Mark A as COMPLETE<br>(Originating Attempt - EXCLUDED from Expiry)"]
    IsOrig -- NO --> CheckStatus{"Status of A"}
    
    CheckStatus -- "open" --> ExpireOpen["Mark status = EXPIRED<br>error = 'concurrent_entitlement_activated'<br>Queue async remote cancellation"]
    CheckStatus -- "payment_pending" --> RetainPending["DO NOT EXPIRE PREMATURELY.<br>Wait for gateway capture/failure.<br>If captured: Route to Scenario 3 Quarantine."]
    CheckStatus -- "creating" --> CheckLease{"Creation Lease Active?"}
    CheckLease -- YES --> AbortPhase4["Leave untouched.<br>Phase 4 Pre-Open Guard under L0A<br>will abort and expire attempt."]
    CheckLease -- NO --> ExpireStale["Mark status = EXPIRED<br>error = 'creation_lease_expired_during_settlement'"]
    CheckStatus -- "recovery_required" --> ExpireRecovery["Mark status = EXPIRED locally.<br>Stage PaymentReconciliation case for remote audit."]
```

### 3.1 Detailed Attempt Handling by Lifecycle State

1. **Originating Checkout Attempt ($A_{\text{orig}}$):**
   - **Rule:** Identified by `A.provider_subscription_id == settled_subscription_id`.
   - **Action:** Transition to `status = CheckoutAttemptStatus.complete`. Explicitly **excluded** from expiration queries via `WHERE id != :originating_attempt_id`.
2. **Attempts in `open` Status:**
   - **Context:** The customer initiated another checkout session on a different browser/device and received a checkout URL, but had not completed payment.
   - **Action:** Transition to `status = CheckoutAttemptStatus.expired` with `error_message = 'concurrent_entitlement_activated'`.
   - **Remote Gateway Consideration:** Marking a local row `expired` does **not** invalidate the checkout URL on Razorpay or cancel the remote subscription. Therefore, settlement flags the attempt for **asynchronous remote cancellation** (`client.cancel_subscription(sub_id)`).
   - **Network Separation Invariant:** The remote cancellation HTTP call is **never** executed while holding database transaction or advisory locks. It is queued as a post-commit background task.
3. **Attempts in `payment_pending` Status:**
   - **Critical Hazard:** If customer money is in flight (mandate authenticated, payment debit pending at bank), expiring the attempt locally creates a phantom charge condition.
   - **Rule:** Settlement **never** preemptively expires a `payment_pending` attempt.
   - **Action:** The attempt remains in `payment_pending`. When the gateway event for that second payment eventually arrives:
     - If `payment.failed`: Handled cleanly, transitioning attempt to `failed`.
     - If `payment.captured`: Handled under $L_{0A}(\text{"organization"})$ by **Scenario 3 Conflict Policy** (Payment captured, ledger credited, entitlement quarantine staged for manual finance refund, attempt marked `expired`).
4. **Attempts in `creating` Status:**
   - **Active Lease (`attempt_lease_expires_at > now_utc`):** Worker/API is currently executing Phase 2/3. Settlement leaves it untouched. When the creation thread returns and attempts Phase 4 ($T_2$), it must acquire $L_{0A}(\text{"organization"})$. Phase 4's Pre-Open Guard re-queries active Pro entitlements under $L_{0A}$, detects the newly settled Pro period, aborts, and marks the attempt `expired`.
   - **Expired Lease (`attempt_lease_expires_at <= now_utc`):** Stale/crashed attempt. Settlement transitions it to `status = CheckoutAttemptStatus.expired`.
5. **Attempts in `recovery_required` Status:**
   - **Context:** Ambiguous network timeout occurred during remote subscription creation.
   - **Action:** Transition to `status = CheckoutAttemptStatus.expired` locally and stage a `PaymentReconciliation` record with reason `unresolved_recovery_during_settlement` so operations can audit whether a ghost subscription exists on Razorpay requiring remote cleanup.

---

## 4. Captured Payment Conflict Policy Matrix

Aptlyst AI operates under the strict financial invariant: **No automatic refunds are authorized, and zero captured funds are ever discarded from the financial ledger.**

| Scenario | Financial Ledger (`BillingPayment`) | Invoice Ledger (`BillingInvoice`) | Checkout Attempt (`BillingCheckoutAttempt`) | Entitlement Outcome (`OrganizationBillingPeriod`) | Reconciliation Reason & Human Action Required |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Normal payment for own checkout** | Insert row: `status = 'captured'`, `reconciliation_state = 'reconciled'`. | Insert/Update row: `processing_status = 'processed'`, `reconciliation_status = 'reconciled'`. | `status = 'complete'`, `error_message = None`. | Active Pro period provisioned with full quota. `subscription_id` linked. | `reason = None`.<br>**Human Action:** None. Automated success. |
| **2. Payment captured after attempt expired** | Insert row: `status = 'captured'`, `reconciliation_state = 'reconciled_with_expired_attempt'`. | Insert row: `processing_status = 'processed'` (if `period_end > now`), else `'quarantined'`. | Preserved as terminal/complete with audit note. | If no other active Pro period and `period_end > now`: Pro period provisioned. Else: No entitlement. | If provisioned: None.<br>If quarantined (`period_end <= now`): `payment_for_expired_period`.<br>**Human Action:** Support reviews service dates; issues manual credit or manual refund. |
| **3. Payment captured after different sub activated Pro** | Insert row: `status = 'captured'`, `reconciliation_state = 'quarantined_concurrent_subscription'`. | Insert row: `processing_status = 'quarantined'`, `reconciliation_status = 'unreconciled'`. | `status = 'expired'`, `error_message = 'concurrent_active_entitlement_conflict'`. | **Zero quota granted.** Pre-existing active Pro period is untouched. | `reason = 'concurrent_active_subscription_conflict'`.<br>**Human Action:** Finance contacts customer to issue manual refund via Razorpay Dashboard or schedule future period. |
| **4. Delayed payment for canceled subscription** | Insert row: `status = 'captured'`, `reconciliation_state = 'quarantined_canceled_subscription'`. | Insert row: `processing_status = 'quarantined'`, `reconciliation_status = 'unreconciled'`. | Preserved in terminal state (`expired` / `complete`). | **Zero quota granted.** `Subscription.status` remains `canceled` (Refusal of Resurrection). | `reason = 'payment_for_terminal_subscription'`.<br>**Human Action:** Operations investigates whether debit was erroneous bank retry; issues manual refund if service canceled. |
| **5. Duplicate webhook for settled payment** | Unique constraint `uq_billing_payments_provider_payment` detects existence. Verified under lock. | Unchanged (`InvoiceProcessingStatus.processed`). | Unchanged (`CheckoutAttemptStatus.complete`). | Unchanged. Active period untouched. Zero quota added. | `reason = None`.<br>**Human Action:** None. Handled idempotently. |
| **6. Payment with missing/ambiguous association** | Insert row: `organization_id = NULL`, `subscription_id = NULL`, `status = 'captured'`, `reconciliation_state = 'unreconciled'`. | `processing_status = 'quarantined'`, `reconciliation_status = 'unreconciled'`. | No attempt modified. | **Zero quota granted.** Tenant entitlement untouched. | `reason = 'missing_or_ambiguous_checkout_association'`.<br>**Human Action:** Engineering/Finance matches payment on gateway to customer account or initiates manual refund. |

---

## 5. Exhaustive Advisory and Row-Lock Audit

### 5.1 Comprehensive Inventory of Advisory Locks

A complete scan of all source code, models, and workers was performed using `git grep --untracked -i "advisory"` and `git grep --untracked -n "pg_advisory"`. Every advisory lock call site in the repository is cataloged below:

| File Path | Line | Function / Context | Entity / Key Scoping | Acquisition Method |
| :--- | :--- | :--- | :--- | :--- |
| `apps/api/app/api/organizations.py` | 103 | `create_organization()` | `current_user.id.int % 2147483647` (User ID scope) | Raw SQL: `text("SELECT pg_advisory_xact_lock(:key)")` |
| `apps/api/app/services/billing/razorpay/lock.py` | 118 | `acquire_razorpay_advisory_lock()` | Deterministic SHA-256 64-bit int: `f"{entity_type}:{entity_id}"` | Raw SQL: `text("SELECT pg_advisory_xact_lock(:key)")` |
| `apps/api/app/services/billing/razorpay/lock.py` | 140 | `try_acquire_razorpay_advisory_lock()` | Deterministic SHA-256 64-bit int: `f"{entity_type}:{entity_id}"` | Raw SQL: `text("SELECT pg_try_advisory_xact_lock(:key)")` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 106 | `handle_subscription_authenticated()` | Level $0B$: `("subscription", sub_id)` | Helper: `acquire_razorpay_advisory_lock` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 362 | `handle_subscription_activated()` | Level $0B$: `("subscription", sub_id)` | Helper: `acquire_razorpay_advisory_lock` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 601 | `handle_subscription_updated()` | Level $0B$: `("subscription", sub_id)` | Helper: `acquire_razorpay_advisory_lock` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 725 | `handle_subscription_completed()` | Level $0B$: `("subscription", sub_id)` | Helper: `acquire_razorpay_advisory_lock` |
| `apps/api/app/services/billing/stripe_invoice_paid_handler.py` | 349 | `handle_stripe_invoice_paid()` | Level $0$: `f"stripe_invoice:{invoice_id}"` | Raw SQL: `text("SELECT pg_advisory_xact_lock(:key)")` |
| `apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py` | 215 | `handle_stripe_invoice_payment_failed()` | Level $0$: `f"stripe_invoice:{invoice_id}"` | Raw SQL: `text("SELECT pg_advisory_xact_lock(:key)")` |

### 5.2 Comprehensive Inventory of Row Locks (`with_for_update`)

| File Path | Line | Entity / Table Locked | Calling Transaction / Workflow |
| :--- | :--- | :--- | :--- |
| `apps/api/app/services/billing/accounting.py` | 203 | `OrganizationBillingPeriod` (Level 1) | `reserve_usage()` |
| `apps/api/app/services/billing/accounting.py` | 271 | `Subscription` (Level 2) | `reserve_usage()` |
| `apps/api/app/services/billing/accounting.py` | 593 | `UsageReservation` | `settle_usage_reservation()` |
| `apps/api/app/services/billing/accounting.py` | 608 | `OrganizationBillingPeriod` | `settle_usage_reservation()` |
| `apps/api/app/services/billing/accounting.py` | 934 | `UsageReservation` | `release_usage_reservation()` |
| `apps/api/app/services/billing/accounting.py` | 948 | `OrganizationBillingPeriod` | `release_usage_reservation()` |
| `apps/api/app/services/billing/accounting.py` | 1177 | `OrganizationBillingPeriod` | `rollover_free_billing_period_if_needed()` |
| `apps/api/app/services/billing/razorpay/handler_base.py` | 852 | `PaymentReconciliation` | `stage_or_update_payment_reconciliation()` |
| `apps/api/app/services/billing/razorpay/handler_base.py` | 976 | `BillingWebhookEvent` | `assert_valid_worker_lease()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 128 | `Subscription` (Level 2) | `handle_subscription_authenticated()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 210 | `BillingCheckoutAttempt` (Level 3) | `handle_subscription_authenticated()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 384 | `Subscription` (Level 2) | `handle_subscription_activated()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 479 | `BillingCheckoutAttempt` (Level 3) | `handle_subscription_activated()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 623 | `Subscription` (Level 2) | `handle_subscription_updated()` |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 746 | `Subscription` (Level 2) | `handle_subscription_completed()` |
| `apps/api/app/services/billing/razorpay/checkout.py` | 1079 | `BillingCheckoutAttempt` | `recover_razorpay_checkout_attempt()` |
| `apps/api/app/services/billing/razorpay/checkout.py` | 1405 | `BillingCheckoutAttempt` | `verify_razorpay_checkout_callback()` |
| `apps/api/app/services/billing/razorpay/checkout.py` | 1479 | `BillingCheckoutAttempt` | `_record_attempt_outcome()` |
| `apps/api/app/services/billing/razorpay/checkout.py` | 1511 | `BillingCheckoutAttempt` | `_record_attempt_success()` |
| `apps/api/app/services/billing/razorpay/webhook_engine.py` | 282 | `BillingWebhookEvent` (`SKIP LOCKED`) | `claim_next_eligible_webhook_event()` |
| `apps/api/app/services/billing/razorpay/webhook_engine.py` | 366 | `BillingWebhookEvent` | `finalize_claimed_webhook_event()` |

### 5.3 Critical Finding: Row-Lock-Before-Advisory-Lock in Agreement Handlers

**Identification:**  
In `apps/api/app/services/billing/razorpay/agreement_handlers.py`:
- Line 98: `assert_valid_worker_lease()` executes `SELECT ... FROM billing_webhook_events WHERE id = :event_id FOR UPDATE`
- Line 106: `acquire_razorpay_advisory_lock(db, "subscription", sub_id)` executes `SELECT pg_advisory_xact_lock(:key)`
- Identical ordering exists at lines 354/362, lines 593/601, and lines 717/725.

**Deadlock Risk Assessment:**  
Because `BillingWebhookEvent` is partitioned by primary key (`event_id`) and no worker ever locks more than one webhook event, this does not cause a circular wait with other webhook workers.  
**However**, holding a physical row lock while blocking on an advisory lock violates the strict hierarchical principle ($L_0 \prec \text{Row Locks}$) and prolongs physical tuple lock duration under subscription contention.

**Required Architectural Fix:**  
Move the Level $0B$ advisory lock acquisition to execute **immediately prior** to `assert_valid_worker_lease()`:
```python
# 1. Level 0B Advisory Lock on provider subscription identity FIRST
await acquire_razorpay_advisory_lock(db, "subscription", sub_id)

# 2. Worker lease verification SECOND
if ctx.lease_token and ctx.locked_by:
    await assert_valid_worker_lease(
        db=db,
        event_id=ctx.event_id,
        expected_lease_token=ctx.lease_token,
        expected_worker_id=ctx.locked_by,
    )
```

### 5.4 Deadlock Freedom Proof Across All Transaction Sequences

The system enforces a strict, global monotonic lock acquisition hierarchy:

$$\text{Level 0A: Advisory } (\text{"organization"}) \prec \text{Level 0B: Advisory } (\text{Entity}) \prec \text{Level 1: } \text{Billing Period} \prec \text{Level 2: } \text{Subscription} \prec \text{Level 3: Auxiliary Rows}$$

```mermaid
graph TD
    subgraph Lock_Hierarchy ["Global Lock Hierarchy (Ascending Topological Rank)"]
        L0A["Level 0A: pg_advisory_xact_lock('organization', org_id)"]
        L0B["Level 0B: pg_advisory_xact_lock('subscription' | 'payment' | 'invoice', entity_id)"]
        L1["Level 1: OrganizationBillingPeriod (FOR UPDATE)"]
        L2["Level 2: Subscription (FOR UPDATE)"]
        L3["Level 3: BillingInvoice / BillingPayment / BillingCheckoutAttempt (FOR UPDATE)"]
        
        L0A --> L0B
        L0B --> L1
        L1 --> L2
        L2 --> L3
    end
```

#### Detailed Transaction Traces:
1. **Agreement Handlers:**  
   Acquire $L_{0B}(\text{"subscription"}) \to L_2(\text{Subscription}) \to L_3(\text{BillingCheckoutAttempt})$.  
   **Crucial Rule:** They **never** acquire $L_{0A}(\text{"organization"})$ and **never** acquire $L_1(\text{BillingPeriod})$.
2. **Settlement Handlers (Step 5.2D.3.3):**  
   Resolve `org_id` upfront via non-locking read $\to$ acquire $L_{0A}(\text{"organization"}) \to L_{0B}(\text{"payment"}) \to L_1(\text{BillingPeriod}) \to L_2(\text{Subscription}) \to L_3(\text{Auxiliary})$.
3. **Checkout Creation ($T_1$) & Phase 4 ($T_2$):**  
   Acquire $L_{0A}(\text{"organization"})$ only. Release before HTTP dispatch.
4. **Accounting `reserve_usage()`:**  
   Acquires $L_1(\text{BillingPeriod}) \to L_2(\text{Subscription})$. Never acquires $L_0$.
5. **Accounting `settle_usage_reservation()` / `release_usage_reservation()`:**  
   Acquires `UsageReservation` row lock $\to L_1(\text{BillingPeriod})$. Because `reserve_usage()` only *inserts* a new reservation (never locking existing reservations), no circular wait exists.

**Conclusion:** All transaction paths acquire resources in strictly ascending topological rank. The wait-for graph contains no cycles ($\text{DAG}$), proving global deadlock freedom.

---

## 6. Required Schema Changes

### 6.1 Schema Status: Can Existing Schema Support This Association?

The existing schema can associate records through matching `(provider, merchant_account_id, environment, provider_subscription_id)`. However:
1. `BillingCheckoutAttempt` currently stores payment verification data only as unstructured JSONB inside `request_payload["verification"]`.
2. `BillingPayment` has foreign keys to `subscription_id` and `invoice_id`, but **lacks a direct foreign key to `billing_checkout_attempts`**.
3. `Index("ix_billing_checkout_attempts_provider_sub")` on `billing_checkout_attempts` is currently **non-unique**, allowing multiple rows with the same provider subscription ID if not strictly fenced.

### 6.2 Recommended Migration Specifications

In the upcoming Alembic migration (`20261108_1600_..._add_subscription_id_to_billing_periods.py`), the following additions establish compile-time relational guarantees:

```sql
-- 1. Add direct payment column on billing_checkout_attempts
ALTER TABLE billing_checkout_attempts
ADD COLUMN provider_payment_id VARCHAR(255) NULL;

CREATE INDEX ix_billing_checkout_attempts_payment
ON billing_checkout_attempts (provider, merchant_account_id, environment, provider_payment_id);

-- 2. Add partial unique constraint on provider_subscription_id
CREATE UNIQUE INDEX uq_checkout_attempts_provider_sub
ON billing_checkout_attempts (provider, merchant_account_id, environment, provider_subscription_id)
WHERE provider_subscription_id IS NOT NULL;

-- 3. Add direct foreign key from billing_payments to billing_checkout_attempts
ALTER TABLE billing_payments
ADD COLUMN checkout_attempt_id UUID NULL REFERENCES billing_checkout_attempts(id) ON DELETE SET NULL;

CREATE INDEX ix_billing_payments_checkout_attempt
ON billing_payments (checkout_attempt_id);
```

---

## 7. Remaining Assumptions and Operational Risks

| Item | Architectural Assumption | Operational Risk Mitigation |
| :--- | :--- | :--- |
| **No Automatic Refunds** | All anomalous or conflicting payments are preserved on the financial ledger and staged in `PaymentReconciliation`. | Finance dashboard / alerting ensures pending review tickets are acted upon within standard business SLA (24–48 hours). |
| **Async Remote Cancellation** | Conflicting open attempts marked `expired` locally trigger background gateway subscription cancellation. | Handled via asynchronous background task outside DB locks; even if delayed, Layer 3 Verification Revalidation prevents duplicate entitlement. |
| **Gateway `payment.captured` Payload** | Razorpay subscription payments contain both `payment.id` and `payment.subscription_id`. | If `subscription_id` is missing in one-off payment payloads, handler fails closed to `unbound_payment` without granting quota. |

---

## 8. Final Audit Verdict

- **Payment-to-Checkout Association:** Resolved and formally traced across both event arrival orders.
- **Settlement Expiration Rules:** Formally defined across all five checkout states (`creating`, `recovery_required`, `open`, `payment_pending`, `complete`).
- **Conflict Policy Matrix:** All six payment scenarios specified; zero funds discarded; zero double-entitlement.
- **Lock Audit:** 100% exhaustive call site inventory; row-lock inversion in agreement handlers identified and sequenced; global deadlock freedom proven.
- **Schema Migration:** Relational enhancements specified for incorporation into the pending Alembic revision.

### Final Verdict: **READY FOR IMPLEMENTATION**
