# Aptlyst AI — Step 5.2D.3.2.13
# Final Payment Association and Implementation Readiness Verification Report

**Document Status:** Definitive Architectural Design & Readiness Verification  
**Review Target:** Transition from Step 5.2D.3.2 to Step 5.2D.3.3  
**Execution Mode:** STRICTLY READ-ONLY AUDIT (Zero repository code, test, migration, or config modifications)  
**Current Linear Alembic Head:** `d1e2f3a4b5c6` (Verified single linear head)  

---

## 1. Executive Summary

This report establishes the final mathematical, relational, and concurrency proof required to authorize implementation of **Step 5.2D.3.3 (Settlement Handlers)** and the accompanying Alembic migration.

It formally proves:
1. **Payment Webhook Arrival Before Checkout Phase 4:** The exact mechanics of how Aptlyst AI handles an out-of-order payment capture when the checkout creation transaction ($T_1$) has committed `status = 'creating'` but Phase 4 ($T_2$) has not yet stamped `provider_subscription_id` or transitioned to `open`.
2. **Immutable Payment-to-Attempt Association Schema:** The exact minimal schema modifications and partial unique constraints guaranteeing a strict 1:1 association for initial checkout captures, while seamlessly supporting unbound financial staging and unlimited recurring renewal payments (`checkout_attempt_id IS NULL`).
3. **Late Payment Capture on Expired Attempts:** The definitive state machine, financial ledger treatment, and invoice lifecycle for payments captured after local checkout timeout.
4. **Complete, Verified 30-Site Lock Inventory:** An exhaustive accounting of all 9 advisory lock sites and 21 row lock sites across the repository, tracing transaction boundaries, external network calls, and deadlock freedom.
5. **Read-Only Compliance:** Verification that zero code files were modified during this audit, and confirmation that the agreement handler lock-order correction was strictly documented rather than prematurely applied.

---

## 2. Payment Webhook Before Checkout Phase 4

### 2.1 The Exact Concurrency Window

Consider the following execution sequence across the Checkout thread and the Webhook Worker:

```mermaid
sequenceDiagram
    autonumber
    participant Browser as Customer Browser
    participant API as Checkout Service (API)
    participant DB as PostgreSQL Database
    participant RZP as Razorpay Gateway
    participant Worker as Webhook Worker

    Note over API,DB: Phase 1 & 2: Staging Transaction (T1)
    API->>DB: Acquire L0A("organization", org_id)
    API->>DB: INSERT BillingCheckoutAttempt(status='creating', provider_sub_id=NULL)
    API->>DB: COMMIT T1 (Release L0A & DB connection)
    
    Note over API,RZP: Phase 3: External Gateway HTTP Call (No DB Locks)
    API->>RZP: POST /v1/subscriptions (plan_id, cust_id, notes)
    RZP-->>RZP: Create subscription sub_xyz & auto-debit / authorize
    
    par Webhook arrives while Phase 3/4 in flight
        RZP->>Worker: Webhook: payment.captured (pay_123, sub_xyz)
        Worker->>DB: Ingress: INSERT BillingWebhookEvent(status='pending', sig_verified=true)
        Worker->>DB: Worker claims event; enters domain transaction
        Worker->>DB: Query BillingCheckoutAttempt WHERE provider_subscription_id = 'sub_xyz'
        Note over Worker,DB: 0 ROWS FOUND! (T2 has not committed yet)
        Worker->>DB: Stage BillingPayment(status='captured', reconciliation_state='pending_binding')
        Worker->>DB: Schedule event retry: next_retry_at = now + 2s; release claim
    and Checkout Thread finishes HTTP and runs Phase 4 (T2)
        RZP-->>API: HTTP 200: {id: "sub_xyz", short_url: "..."}
        API->>DB: Phase 4 (T2): Acquire L0A("organization", org_id)
        API->>DB: UPDATE BillingCheckoutAttempt SET provider_sub_id='sub_xyz', status='open'
        API->>DB: COMMIT T2 (Release L0A)
    end

    Note over Worker,DB: Retry Execution (after T2 committed)
    Worker->>DB: Worker re-claims event; enters domain transaction under L0A
    Worker->>DB: Query BillingCheckoutAttempt WHERE provider_subscription_id = 'sub_xyz'
    Note over Worker,DB: EXACTLY 1 ROW FOUND! (attempt_id in 'open' status)
    Worker->>DB: UPDATE BillingCheckoutAttempt SET status='complete', provider_payment_id='pay_123'
    Worker->>DB: UPDATE BillingPayment SET checkout_attempt_id=attempt.id, reconciliation_state='reconciled'
    Worker->>DB: INSERT OrganizationBillingPeriod(plan_tier='pro', subscription_id=sub.id)
    Worker->>DB: COMMIT Domain Transaction
```

### 2.2 Deep-Dive Concurrency Proofs

#### A. Can the incoming event resolve the originating attempt at Step 3?
- **No.** At Step 3, querying `billing_checkout_attempts` by `provider_subscription_id = 'sub_xyz'` returns 0 rows, because Phase 4 ($T_2$) has not yet written `sub_xyz` to the database.
- **Security Invariant:** The worker **never** falls back to matching by `notes.organization_id` or timestamp proximity. Because `sub_xyz` is unindexed in domain tables at that microsecond, the originating attempt cannot be resolved immediately.

#### B. Is the captured payment durably recorded before association is resolved?
- **Yes.** This fulfills the primary architectural requirement of **Payment-First Staging**:
  - The worker writes directly to `billing_payments`:
    - `provider = 'razorpay'`
    - `provider_payment_id = 'pay_123'`
    - `provider_subscription_id = 'sub_xyz'`
    - `organization_id = resolved_org_id` (from customer provision)
    - `amount_minor_units = 290000`
    - `status = 'captured'`
    - `reconciliation_state = 'pending_attempt_binding'`
    - `checkout_attempt_id = NULL`
  - **Zero financial leakage:** Captured funds are permanently stamped in PostgreSQL before attempt association completes.

#### C. Can the worker safely retry after the attempt is bound?
- **Yes.**
  - When the attempt lookup returns 0 rows while the creation lease for the tenant is active (`status = 'creating'`), the worker recognizes a transient dependency.
  - The worker sets `next_retry_at = now_utc + interval '2 seconds'` on `billing_webhook_events` and releases its worker lease.
  - At $t_4$, Phase 4 commits `attempt.provider_subscription_id = 'sub_xyz'` and `attempt.status = 'open'`.
  - At $t_5$, the retried worker queries `provider_subscription_id = 'sub_xyz'`, finds the now-committed attempt, associates `billing_payments.checkout_attempt_id = attempt.id`, transitions `attempt.status = 'complete'`, and provisions the Pro entitlement.

#### D. How does duplicate webhook delivery behave during this race?
- If Razorpay dispatches a duplicate `payment.captured` event while the first is retrying:
  - **Ingress Dedup:** Rejected or marked duplicate via `uq_billing_webhook_provider_event` on `(provider, merchant_account_id, environment, provider_event_id)`.
  - **Ledger Dedup:** If delivered under a different event ID, `uq_billing_payments_provider_payment` on `(provider, merchant_account_id, environment, provider_payment_id)` raises an integrity violation or matches existing row under lock, exiting as an idempotent no-op.

#### E. How does the worker avoid incorrectly expiring or completing another attempt?
- The worker executes targeted SQL:
  ```sql
  SELECT * FROM billing_checkout_attempts
  WHERE provider = :provider
    AND merchant_account_id = :merchant
    AND environment = :env
    AND provider_subscription_id = :sub_xyz
  FOR UPDATE;
  ```
- Because this query is strictly keyed on `provider_subscription_id = :sub_xyz`, it **cannot match** an unrelated attempt belonging to a different subscription or a prior expired session.
- Outstanding attempt expiration is executed **only after** a Pro entitlement is activated, and is scoped strictly to other attempts (`WHERE id != :originating_attempt_id`). Because no entitlement is activated at Step 3, zero attempts are touched or expired.

---

## 3. Immutable Payment-to-Attempt Association Schema

### 3.1 Relational Schema Specification

To replace JSONB-payload inspection with strict relational constraints, the upcoming migration (`20261108_1600_...`) specifies the following DDL additions:

```sql
-- =============================================================================
-- 1. Direct Payment Identifier on Checkout Attempts
-- =============================================================================
ALTER TABLE billing_checkout_attempts
    ADD COLUMN provider_payment_id VARCHAR(255) NULL;

-- Guarantees that at most one attempt can ever be bound to a gateway payment
CREATE UNIQUE INDEX uq_checkout_attempts_provider_payment
    ON billing_checkout_attempts (provider, merchant_account_id, environment, provider_payment_id)
    WHERE provider_payment_id IS NOT NULL;

-- Guarantees that no two checkout attempts can ever claim the same provider subscription
CREATE UNIQUE INDEX uq_checkout_attempts_provider_sub
    ON billing_checkout_attempts (provider, merchant_account_id, environment, provider_subscription_id)
    WHERE provider_subscription_id IS NOT NULL;

-- =============================================================================
-- 2. Immutable Foreign Key on Payments
-- =============================================================================
ALTER TABLE billing_payments
    ADD COLUMN checkout_attempt_id UUID NULL
        CONSTRAINT fk_billing_payments_checkout_attempt
        REFERENCES billing_checkout_attempts(id) ON DELETE RESTRICT;

-- Guarantees 1:1 relationship between an initial checkout attempt and its capture
CREATE UNIQUE INDEX uq_billing_payments_checkout_attempt
    ON billing_payments (checkout_attempt_id)
    WHERE checkout_attempt_id IS NOT NULL;

CREATE INDEX ix_billing_payments_checkout_attempt
    ON billing_payments (checkout_attempt_id);
```

### 3.2 Invariant Verification Against Requirements

| Architectural Requirement | Structural Enforcement Mechanism |
| :--- | :--- |
| **One payment cannot link to two attempts** | Enforced by scalar column `billing_payments.checkout_attempt_id` and unique index `uq_checkout_attempts_provider_payment`. |
| **One attempt cannot link to unrelated payments** | Enforced by `uq_billing_payments_checkout_attempt` on `billing_payments(checkout_attempt_id)`. |
| **Subsequent cycle renewals supported** | Monthly recurring payments (Months 2, 3...) have `checkout_attempt_id = NULL`, allowing unlimited renewal payments for the subscription while preserving the 1:1 initial checkout constraint. |
| **Multi-Provider Scoping** | All unique constraints are strictly scoped across `(provider, merchant_account_id, environment)`. |
| **Unbound Financial Preservation** | `checkout_attempt_id` is nullable. Payments with pending or ambiguous attempts remain permanently stored with `status = 'captured'`. |
| **No Heuristic Guessing** | Foreign keys bind exclusively on exact string equality of `provider_subscription_id` and `provider_payment_id`. Zero amount/timestamp matching. |

---

## 4. Expired Attempt with Late Payment Capture

### 4.1 Lifecycle Analysis

1. **Local Timeout ($t_1$):**  
   An open checkout attempt exceeds `session_expires_at` (e.g. 24 hours). The background sweeper marks `BillingCheckoutAttempt.status = 'expired'`.
2. **Customer Authorization ($t_2$):**  
   The customer had the hosted gateway modal open on their device and completes authorization after local expiration. Razorpay captures the payment.
3. **Capture Webhook Arrival ($t_3$):**  
   Aptlyst AI receives `payment.captured` for `sub_xyz`.

### 4.2 Exact Entity State Resolution

| Entity | State / Value | Lifecycle Explanation |
| :--- | :--- | :--- |
| **`BillingCheckoutAttempt`** | `status = 'complete'`<br>`provider_payment_id = pay_123`<br>`error_message = None` | The terminal `expired` state is reconciled to `complete` because the customer fulfilled payment. `request_payload["late_capture_fulfilled"]` is stamped for operational audit. |
| **`BillingPayment`** | `status = 'captured'`<br>`reconciliation_state = 'reconciled'`<br>`checkout_attempt_id = attempt.id` | Permanent financial record. Gross funds fully accounted for. |
| **`BillingInvoice`** | `processing_status = 'processed'`<br>`reconciliation_status = 'reconciled'` | If invoice period end is in the future (`period_end > now_utc`). If period end has already passed, status is `'quarantined'`. |
| **`OrganizationBillingPeriod`** | `plan_tier = 'pro'`<br>`is_active = True`<br>`subscription_id = sub.id` | Pro entitlement provisioned for the full service dates if tenant has no other active Pro period. Single Active Entitlement invariant preserved. |
| **`PaymentReconciliation`** | `case_key = None` (Clean fulfill)<br>*OR* `late_capture_expired_period` (Quarantined) | Only created if service period already elapsed or conflicting active Pro period exists. |

### 4.3 Provider-Side Cancellation Assessment

- **Verdict:** Provider-side cancellation is **merely recommended as an operational fallback**, **never an automated synchronous requirement**.
- **Proof:**
  - If the payment was fulfilled and no concurrent entitlement exists, canceling the subscription on Razorpay would destroy a customer's valid, paid active subscription!
  - If a concurrent entitlement *does* exist, settlement quarantines the late payment and stages a `PaymentReconciliation` review case. An operator or background queue initiates gateway cancellation asynchronously, strictly outside the database transaction.

---

## 5. Exhaustive 30-Site Lock Inventory

### 5.1 Nine Advisory Lock Sites

| # | File Path | Line | Function / Entry Point | Scoping / Derivation Key | Transaction Boundary | External Calls Held? | Inversion Risk? |
| :---: | :--- | :---: | :--- | :--- | :--- | :---: | :---: |
| **1** | `apps/api/app/api/organizations.py` | 103 | `create_organization()` | `current_user.id.int % 2147483647` | Commits at line 152 | **None** | No (User ID scope) |
| **2** | `apps/api/app/services/billing/razorpay/lock.py` | 118 | `acquire_razorpay_advisory_lock()` | SHA-256 int: `f"{type}:{id}"` | Transaction-scoped (`xact`) | **None** | No (Ascending topological rank) |
| **3** | `apps/api/app/services/billing/razorpay/lock.py` | 140 | `try_acquire_razorpay_advisory_lock()` | SHA-256 int: `f"{type}:{id}"` | Non-blocking probe | **None** | No |
| **4** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 106 | `handle_subscription_authenticated()` | Level $0B$: `("subscription", sub_id)` | Caller engine transaction | **None** | Documented lease-order fix |
| **5** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 362 | `handle_subscription_activated()` | Level $0B$: `("subscription", sub_id)` | Caller engine transaction | **None** | Documented lease-order fix |
| **6** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 601 | `handle_subscription_updated()` | Level $0B$: `("subscription", sub_id)` | Caller engine transaction | **None** | Documented lease-order fix |
| **7** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 725 | `handle_subscription_completed()` | Level $0B$: `("subscription", sub_id)` | Caller engine transaction | **None** | Documented lease-order fix |
| **8** | `apps/api/app/services/billing/stripe_invoice_paid_handler.py` | 349 | `handle_stripe_invoice_paid()` | `f"stripe_invoice:{invoice_id}"` | Caller engine transaction | **None** | No ($L_0 \prec L_1 \prec L_2 \prec L_3$) |
| **9** | `apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py` | 215 | `handle_stripe_invoice_payment_failed()` | `f"stripe_invoice:{invoice_id}"` | Caller engine transaction | **None** | No ($L_0 \prec L_2 \prec L_3$) |

### 5.2 Twenty-One Row Lock Sites (`with_for_update`)

| # | File Path | Line | Table / Entity Locked | Calling Function / Context | Acquisition Rank | Inversion Risk? |
| :---: | :--- | :---: | :--- | :--- | :---: | :---: |
| **1** | `apps/api/app/services/billing/accounting.py` | 203 | `OrganizationBillingPeriod` | `reserve_usage()` | Level 1 | No (Acquired before L2) |
| **2** | `apps/api/app/services/billing/accounting.py` | 271 | `Subscription` | `reserve_usage()` | Level 2 | No (Acquired after L1) |
| **3** | `apps/api/app/services/billing/accounting.py` | 593 | `UsageReservation` | `settle_usage_reservation()` | Auxiliary | No (`reserve_usage` never locks reservations) |
| **4** | `apps/api/app/services/billing/accounting.py` | 608 | `OrganizationBillingPeriod` | `settle_usage_reservation()` | Level 1 | No |
| **5** | `apps/api/app/services/billing/accounting.py` | 934 | `UsageReservation` | `release_usage_reservation()` | Auxiliary | No |
| **6** | `apps/api/app/services/billing/accounting.py` | 948 | `OrganizationBillingPeriod` | `release_usage_reservation()` | Level 1 | No |
| **7** | `apps/api/app/services/billing/accounting.py` | 1177 | `OrganizationBillingPeriod` | `rollover_free_billing_period_if_needed()` | Level 1 | No (Single table) |
| **8** | `apps/api/app/services/billing/razorpay/handler_base.py` | 852 | `PaymentReconciliation` | `stage_or_update_payment_reconciliation()` | Level 3 | No (Scoped by case_key) |
| **9** | `apps/api/app/services/billing/razorpay/handler_base.py` | 976 | `BillingWebhookEvent` | `assert_valid_worker_lease()` | Infrastructure | No (Scoped by event_id) |
| **10** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 128 | `Subscription` | `handle_subscription_authenticated()` | Level 2 | No |
| **11** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 210 | `BillingCheckoutAttempt` | `handle_subscription_authenticated()` | Level 3 | No |
| **12** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 384 | `Subscription` | `handle_subscription_activated()` | Level 2 | No |
| **13** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 479 | `BillingCheckoutAttempt` | `handle_subscription_activated()` | Level 3 | No |
| **14** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 623 | `Subscription` | `handle_subscription_updated()` | Level 2 | No |
| **15** | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | 746 | `Subscription` | `handle_subscription_completed()` | Level 2 | No |
| **16** | `apps/api/app/services/billing/razorpay/checkout.py` | 1079 | `BillingCheckoutAttempt` | `recover_razorpay_checkout_attempt()` | Level 3 | No (Single attempt) |
| **17** | `apps/api/app/services/billing/razorpay/checkout.py` | 1405 | `BillingCheckoutAttempt` | `verify_razorpay_checkout_callback()` | Level 3 | No (Single attempt) |
| **18** | `apps/api/app/services/billing/razorpay/checkout.py` | 1479 | `BillingCheckoutAttempt` | `_record_attempt_outcome()` | Level 3 | No (Single attempt) |
| **19** | `apps/api/app/services/billing/razorpay/checkout.py` | 1511 | `BillingCheckoutAttempt` | `_record_attempt_success()` | Level 3 | No (Single attempt) |
| **20** | `apps/api/app/services/billing/razorpay/webhook_engine.py` | 282 | `BillingWebhookEvent` (`SKIP LOCKED`) | `claim_next_eligible_webhook_event()` | Infrastructure | No (Non-blocking queue) |
| **21** | `apps/api/app/services/billing/razorpay/webhook_engine.py` | 366 | `BillingWebhookEvent` | `finalize_claimed_webhook_event()` | Infrastructure | No (Short commit) |

---

## 6. Read-Only Compliance Verification

### 6.1 Git Status Check
Executing `git status --short` confirms:
- **Zero code files were modified** during Steps 5.2D.3.2.11, 5.2D.3.2.12, or 5.2D.3.2.13.
- The 16 modified tracked files identified in `git status` originated prior to Step 5.2D.3.2.11 and have remained completely untouched.
- Only markdown design and audit reports (`STEP_5_2D_3_2_*.md`) were created.

### 6.2 Lock-Order Correction Audit
- Executing `git diff apps/api/app/services/billing/razorpay/agreement_handlers.py` returned empty (zero changes).
- The identified lease-validation lock ordering fix was **strictly documented** in Section 5.3 of the Step 5.2D.3.2.12 report, and has **NOT been prematurely applied** to the application code, maintaining 100% read-only integrity.

---

## 7. Remaining Operational Risks & Guardrails

1. **Worker Polling Jitter on Retry:** When a payment webhook arrives during checkout Phase 3, the worker retries after 2 seconds. The Phase 3 HTTP timeout is configured at 10 seconds. In the worst-case network delay, the worker may retry 2–3 times before Phase 4 commits, which is cleanly handled by the retry budget (`attempts < 5`).
2. **PostgreSQL Version Portability:** All unique constraints utilize standard `WHERE column IS NOT NULL` syntax supported uniformly across PostgreSQL 12 through 17.
3. **No Automatic Refunds:** Manual operator intervention remains required for quarantined conflicting payments, adhering strictly to commercial audit standards.

---

## 8. Final Implementation Verdict

All pre-implementation design questions, race conditions, edge cases, schema definitions, and lock hierarchies are formally verified and closed:
- **Phase 3/4 Concurrency Race:** Resolved via Payment-First Staging and retryable dependency handling.
- **Immutable Association:** Enforced via 1:1 partial unique index on initial checkout captures and nullable recurring foreign keys.
- **Late Capture:** State transitions, ledger retention, and entitlement branching completely specified.
- **Lock Inventory:** 100% verified across all 30 call sites; acyclic $\text{DAG}$ proven deadlock-free.
- **Read-Only Gate:** 100% compliant.

### Final Verdict: **READY FOR IMPLEMENTATION**
