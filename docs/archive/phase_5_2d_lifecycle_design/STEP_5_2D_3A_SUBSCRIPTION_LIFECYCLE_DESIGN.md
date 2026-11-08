# Aptlyst AI — Step 5.2D.3A: Razorpay Subscription Lifecycle Handler Design

**Repository**: `D:\Projects\V1\Aptlyst-AI`  
**Date**: November 8, 2026 (Updated: Step 5.2D.3A.1 Revision)  
**Document Status**: COMPREHENSIVE ARCHITECTURAL DESIGN SPECIFICATION (CORRECTED)  
**Target Milestone**: Step 5.2D.3 (Razorpay Webhook Handlers Implementation)  
**Current Alembic Head**: `d1e2f3a4b5c6`  

---

## 1. Executive Summary

This specification establishes the architectural, mathematical, and domain design for the Razorpay subscription lifecycle event handlers (Step 5.2D.3). It translates the platform's approved business policies into verifiable database operations, concurrency invariants, and state transition rules.

### Key Architectural Invariants
1. **Activation $\neq$ Entitlement**: `subscription.activated` updates agreement status and period boundaries but **NEVER independently grants paid Pro quota or sets `plan_tier = 'pro'`**.
2. **Full-Payment Settlement Sole Authority for Recovery**:
   - `subscription.charged` or agreement events alone **must NOT independently clear `past_due_since`**.
   - Recovery is strictly tied to successful full-payment invoice settlement.
   - The grace anchor (`past_due_since`) is cleared (`None`) **if and only if** the qualifying settlement transaction commits successfully.
   - Failed, partial, unbound, or ambiguous payments leave `past_due_since` intact.
3. **Grace Period Integrity**: The 7-day grace period is strictly anchored by `past_due_since` set upon the first renewal failure (`subscription.pending`). Repeated failures must never reset this timestamp. Recovery (`subscription.charged` full settlement) clears the anchor. `subscription.halted` preserves access for the remainder of the 7-day window.
4. **Cancellation Timestamp Semantics**:
   - `canceled_at`: Stores the **timestamp when cancellation was requested**.
   - `cancel_at_period_end`: Boolean flag indicating whether cancellation is deferred to cycle end or grace expiration.
   - `current_period_end`: Authoritative date through which access is granted for active subscriptions.
   - In Two-Phase Deferred Cancellation during grace: `Subscription.status` remains `past_due` with `cancel_at_period_end = True` and `canceled_at = event_created_at`. Access continues until the 7-day grace deadline expires, after which `accounting.py` automatically blocks usage and terminal cancellation is finalized.
5. **Deadlock-Free Lock Hierarchy**: All handlers enforce the exact global lock hierarchy established by the accounting engine:
   $$\text{Level 0: Advisory Lock} \longrightarrow \text{Level 1: OrganizationBillingPeriod} \longrightarrow \text{Level 2: Subscription} \longrightarrow \text{Level 3: BillingInvoice} \longrightarrow \text{Level 4: BillingPayment}$$
6. **Refusal of Resurrection**: Once a subscription reaches terminal status (`canceled`, `completed`), delayed or replayed events for that provider subscription ID are permanently prohibited from reactivating the agreement.

---

## 2. Phase 1: Existing Architecture & Foundation Inspection

### 2.1 The Provider-Neutral Billing Foundation
A forensic inspection of the codebase confirms the existing domain models and migrations:

```
+---------------------------------------------------------------------------------------------------+
| Provider-Neutral Billing Relational Architecture                                                 |
+---------------------------------------------------------------------------------------------------+
  organizations (id, name, created_at)
       |
       +---> billing_customer_provisions (organization_id, provider, merchant_account_id, provider_customer_id)
       |
       +---> subscriptions (organization_id, provider, provider_subscription_id, status, plan_tier,
       |                    current_period_start, current_period_end, cancel_at_period_end, past_due_since)
       |        |
       |        +---> billing_invoices (subscription_id, organization_id, provider_invoice_id,
       |        |                       gross_amount_minor_units, amount_paid_minor_units, billing_period_id)
       |        |        |
       |        |        +---> billing_payments (invoice_id, subscription_id, provider_payment_id,
       |        |                                amount_minor_units, status, reconciliation_state)
       |
       +---> organization_billing_periods (organization_id, plan_tier, monthly_token_quota,
       |                                   tokens_used, tokens_reserved, is_active, is_blocked)
       |
       +---> billing_checkout_attempts (organization_id, provider_subscription_id, status)
       |
       +---> billing_webhook_events (provider_event_id, event_type, status, attempts,
                                     lease_token, lease_expires_at, locked_by)
+---------------------------------------------------------------------------------------------------+
```

1. **`BillingWebhookEvent`** (`apps/api/app/models/payment.py#L63-L151`):
   - Ingested events default to `status = 'pending'`, `attempts = 0`.
   - Migration `d1e2f3a4b5c6` added `lease_token` (UUID), `lease_expires_at` (timestamptz), and `locked_by` (varchar(64)) with partial index `ix_billing_webhook_events_claim`.
   - Worker claims via `SELECT ... FOR UPDATE SKIP LOCKED` and commits immediately, releasing row locks prior to handler dispatch.
2. **`Subscription`** (`apps/api/app/models/subscription.py#L55-L224`):
   - Provider-neutral fields: `provider`, `provider_customer_id`, `provider_subscription_id`, `provider_plan_id`, `merchant_account_id`, `environment`, `last_webhook_event_at`.
   - Partial unique index `uq_org_active_subscription` on `organization_id` WHERE `status IN ('active', 'trialing')`.
   - Partial unique index `uq_subscriptions_provider_subscription` on `(provider, merchant_account_id, environment, provider_subscription_id)` WHERE `provider_subscription_id IS NOT NULL`.
3. **`OrganizationBillingPeriod`** (`apps/api/app/models/billing.py#L42-L130`):
   - Authoritative ledger of quota and cost headroom.
   - Partial unique index `uq_org_active_billing_period` on `organization_id` WHERE `is_active = true`.
4. **`BillingInvoice`** (`apps/api/app/models/payment.py#L280-L427`):
   - Check constraints: `gross_amount_minor_units >= 0`, `amount_paid_minor_units >= 0`, `amount_due_minor_units >= 0`, `amount_paid_minor_units <= gross_amount_minor_units`.
   - Foreign key `billing_period_id` to `organization_billing_periods.id` (non-unique index `ix_billing_invoices_billing_period_id`).
5. **`BillingPayment`** (`apps/api/app/models/payment.py#L429-L550`):
   - Supports payment-first staging (`subscription_id` and `invoice_id` are nullable).
   - `reconciliation_state` classifies payments (`reconciled`, `partial`, `conflict`).

### 2.2 Accounting Engine Entitlement Constraints
In `apps/api/app/services/billing/accounting.py` (lines 258–360), `reserve_usage()` strictly evaluates:

```python
is_paid_period = bool(period.plan_tier and period.plan_tier.strip().lower() != "free")
if is_paid_period:
    eligible_statuses = (SubscriptionStatus.active, SubscriptionStatus.past_due)
    stmt_sub = (
        select(Subscription)
        .where(
            Subscription.organization_id == org_id,
            Subscription.status.in_(eligible_statuses),
        )
        .with_for_update()
    )
    res_sub = await session.execute(stmt_sub)
    eligible_subs = res_sub.scalars().all()
```

For `SubscriptionStatus.past_due`:
- `past_due_since` must be present. If `past_due_since is None`: fails closed (`GRACE_PERIOD_EXPIRED`).
- `grace_deadline = past_due_since + timedelta(days=settings.billing_grace_period_days)`.
- If `current_time >= grace_deadline`: fails closed (`GRACE_PERIOD_EXPIRED`).
- Within the grace window: usage reservations are permitted.

---

## 3. Phase 2: Event Mapping Specifications

Every Razorpay webhook event payload follows the standard envelope validated by `webhook_ingress.py`:
- `entity == "event"`
- `account_id == settings.razorpay_merchant_account_id`
- `event == "<event_name>"`
- `contains == [...]`
- `payload == {"subscription": {"entity": {...}}, "payment": {"entity": {...}}}`
- `created_at == <positive_unix_timestamp>`

---

### 3.1 `subscription.authenticated`
- **Trigger**: Customer authenticates a recurring mandate during checkout.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `customer_id`, `plan_id`, `status == "authenticated"`, `created_at`.
  - Optional: `notes` (`organization_id`, `attempt_id`), `current_start`, `current_end`, `payload.payment.entity`.
- **Database Records Affected**:
  - `Subscription`: Queried or inserted with `status = SubscriptionStatus.incomplete`.
  - `BillingCheckoutAttempt`: Updated to `status = CheckoutAttemptStatus.payment_pending`.
- **Allowed Subscription State Transitions**:
  - `None` (new) $\longrightarrow$ `SubscriptionStatus.incomplete`
  - `SubscriptionStatus.incomplete` $\longrightarrow$ `SubscriptionStatus.incomplete` (refresh metadata)
  - `SubscriptionStatus.active` $\longrightarrow$ **NO-OP** (stale out-of-order event)
- **Organization Billing Period Mutation**: **NONE**.
- **Quota Entitlement Impact**: **NONE**.
- **Entity Idempotency Key**: `f"sub_auth:{provider_subscription_id}"`.
- **Duplicate & Out-of-Order Delivery**: Stale events (`created_at <= last_webhook_event_at`) do not regress status.
- **Inconsistent Payload Handling**: Unknown customer without existing `BillingCustomerProvision` fails closed with `RazorpayWebhookPermanentError`.

---

### 3.2 `subscription.activated`
- **Trigger**: Razorpay transitions the subscription to `active` after mandate confirmation.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `customer_id`, `plan_id`, `status == "active"`, `created_at`.
  - Optional: `current_start`, `current_end`, `charge_at`, `notes`.
- **Database Records Affected**:
  - `Subscription`: Updated to `status = SubscriptionStatus.active`. `current_period_start` and `current_period_end` populated.
  - `BillingCheckoutAttempt`: Updated to `status = CheckoutAttemptStatus.complete`.
- **Allowed Subscription State Transitions**:
  - `SubscriptionStatus.incomplete` $\longrightarrow$ `SubscriptionStatus.active`
  - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.active` (idempotent replay)
  - `SubscriptionStatus.past_due` $\longrightarrow$ **REJECTED**: `subscription.activated` cannot cure `past_due`. Only full-payment settlement (`subscription.charged`) can cure `past_due`.
  - `SubscriptionStatus.canceled` $\longrightarrow$ **REJECTED (Refusal of Resurrection)**
- **Organization Billing Period Mutation**: **NONE**.
- **Quota Entitlement Impact**: **STRICTLY NONE**.
  > [!IMPORTANT]
  > `subscription.activated` updates agreement status in `subscriptions`, but **`plan_tier` remains `'free'`** until a full-payment invoice settlement occurs. It NEVER grants Pro token quota.
- **Entity Idempotency Key**: `f"sub_act:{provider_subscription_id}:{created_at}"`.

---

### 3.3 `subscription.pending`
- **Trigger**: Recurring renewal charge on the scheduled due date fails. Razorpay initiates automated retry cycle.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `status == "pending"`, `created_at`.
  - Optional: `charge_at`, `payload.payment.entity` (failed payment details).
- **Database Records Affected**:
  - `Subscription`: Transitions to `status = SubscriptionStatus.past_due`.
- **Allowed Subscription State Transitions**:
  - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.past_due`
  - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.past_due` (**strictly preserves original `past_due_since`**)
  - `SubscriptionStatus.canceled` $\longrightarrow$ **REJECTED** (no-op on terminal state)
- **Organization Billing Period Mutation**: **NONE**. Existing period remains intact until period end.
- **Quota Entitlement Impact**: **Grace Period Retained**.
  - On the first transition to `past_due`, sets `past_due_since = event_timestamp`.
  - Pro access continues within the active billing period for 7 days (`settings.billing_grace_period_days`).
- **Entity Idempotency Key**: `f"sub_pending:{provider_subscription_id}:{created_at}"`.
- **Duplicate & Out-of-Order Delivery**:
  - Repeated `subscription.pending` events **MUST NOT overwrite or advance `past_due_since`**.
  - If delayed event arrives after subscription was already cured back to `active` via `subscription.charged`: discarded as stale failure (`event.created_at < last_webhook_event_at`).

---

### 3.4 `subscription.halted`
- **Trigger**: All automatic retry attempts are exhausted without successful payment. Razorpay halts recurring billing.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `status == "halted"`, `created_at`.
- **Database Records Affected**:
  - `Subscription`: Preserves `past_due` if within grace window; transitions to `unpaid` if grace has expired.
- **Allowed Subscription State Transitions**:
  - If `now < past_due_since + 7 days`:
    - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.past_due` (grace access retained; provider halt logged in metadata).
  - If `now >= past_due_since + 7 days`:
    - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.unpaid` (`plan_tier` demoted to `'free'`).
  - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.past_due` (sets `past_due_since = now_utc` if missing, evaluates grace).
  - `SubscriptionStatus.canceled` $\longrightarrow$ **REJECTED** (no-op).
- **Organization Billing Period Mutation**: **NONE**.
- **Quota Entitlement Impact**:
  - `subscription.halted` **does NOT immediately revoke the grace period**. If within the 7-day window, access continues until the deadline.
  - After 7 days, `accounting.py` blocks inference automatically with `GRACE_PERIOD_EXPIRED`.
- **Entity Idempotency Key**: `f"sub_halted:{provider_subscription_id}:{created_at}"`.

---

### 3.5 `subscription.charged`
- **Trigger**: A recurring renewal or initial activation charge is successfully captured by Razorpay.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `payload.payment.entity.id`, `amount`, `currency`, `status == "captured"`, `created_at`.
  - Optional: `payload.payment.entity.invoice_id`, `current_start`, `current_end`.
- **Database Records Affected**:
  - `BillingPayment`: Staged/updated as `status = 'captured'`, `reconciliation_state = 'reconciled'`.
  - `BillingInvoice`: Created or updated as `status = 'paid'`, `processing_status = 'processed'`.
  - `OrganizationBillingPeriod`: Deactivates prior period; provisions new active Pro period with full quota.
  - `Subscription`: Updated to `status = SubscriptionStatus.active`, `plan_tier = 'pro'`. Monotonic period dates advanced.
  - **Grace Anchor Clearing**: `past_due_since = None` **if and only if full settlement succeeds**.
- **Allowed Subscription State Transitions**:
  - `SubscriptionStatus.incomplete` $\longrightarrow$ `SubscriptionStatus.active` (first charge settlement)
  - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.active` (recurring renewal settlement)
  - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.active` (**Cures Past-Due**)
  - `SubscriptionStatus.unpaid` $\longrightarrow$ `SubscriptionStatus.active` (**Cures Unpaid**)
  - `SubscriptionStatus.canceled` $\longrightarrow$ **REJECTED (Refusal of Resurrection)**: Invoice and payment recorded as `ignored`; subscription remains `canceled`.
- **Organization Billing Period Mutation**: **YES (Sole Paid Authority)**.
  - Deactivates previous billing period (`is_active = False`).
  - Inserts new active Pro billing period (`is_active = True`, `monthly_token_quota = settings.default_pro_monthly_token_quota`).
- **Quota Entitlement Impact**: **GRANTS PAID PRO ENTITLEMENT**.
- **Entity Idempotency Key**: `f"sub_charged:{provider_subscription_id}:{payment_id}"`.
- **Recovery Invariant**:
  > [!IMPORTANT]
  > The grace anchor (`past_due_since`) is cleared to `None` **exclusively within the full-payment settlement transaction**. If the payment is partial, ambiguous, or failed, `past_due_since` is untouched.

---

### 3.6 `subscription.cancelled`
- **Trigger**: Subscription is canceled via Razorpay dashboard or API.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `status == "cancelled"`, `created_at`.
  - Optional: `ended_at`, `cancel_at_cycle_end`.
- **Database Records Affected**:
  - `Subscription`: Status, plan tier, cancellation fields updated according to the Two-Phase Cancellation protocol.
- **Allowed Subscription State Transitions**:
  - **Case A: Cycle-End Cancellation (`cancel_at_period_end = True`)**:
    - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.active` (`cancel_at_period_end = True`, `canceled_at = event_created_at`). Access continues until `current_period_end`.
  - **Case B: Immediate Cancellation While Active**:
    - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.canceled` (`plan_tier = 'free'`, `canceled_at = event_created_at`). Access revoked immediately.
  - **Case C: Cancellation During Grace Window (`status == past_due`)**:
    - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.past_due` (`cancel_at_period_end = True`, `canceled_at = event_created_at`, original `past_due_since` preserved). Access continues for remainder of 7-day grace period.
  - `SubscriptionStatus.canceled` $\longrightarrow$ `SubscriptionStatus.canceled` (idempotent replay).
- **Organization Billing Period Mutation**: **NONE**.
- **Entity Idempotency Key**: `f"sub_cancelled:{provider_subscription_id}:{created_at}"`.

---

### 3.7 `subscription.completed`
- **Trigger**: Subscription completes all planned cycles (`total_count` reached).
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `status == "completed"`, `created_at`.
- **Database Records Affected**:
  - `Subscription`: Transitions to `SubscriptionStatus.canceled`, `plan_tier = 'free'`, `canceled_at = event_created_at`.
- **Allowed Subscription State Transitions**:
  - `SubscriptionStatus.active` $\longrightarrow$ `SubscriptionStatus.canceled`
  - `SubscriptionStatus.past_due` $\longrightarrow$ `SubscriptionStatus.canceled`
  - `SubscriptionStatus.canceled` $\longrightarrow$ `SubscriptionStatus.canceled` (no-op)
- **Organization Billing Period Mutation**: **NONE**.
- **Entity Idempotency Key**: `f"sub_completed:{provider_subscription_id}:{created_at}"`.

---

### 3.8 `subscription.updated`
- **Trigger**: Plan changed, quantity modified, schedule updated, or payment method changed.
- **Payload Structure**:
  - Required: `payload.subscription.entity.id`, `plan_id`, `status`, `created_at`.
- **Database Records Affected**:
  - `Subscription`: Updates `provider_plan_id`, period boundaries, or metadata without regressing lifecycle state.
- **Organization Billing Period Mutation**: **NONE**.
- **Quota Entitlement Impact**: **NONE**.
- **Entity Idempotency Key**: `f"sub_updated:{provider_subscription_id}:{created_at}"`.

---

### 3.9 `payment.failed`
- **Trigger**: A payment attempt fails (card declined, insufficient funds, network timeout).
- **Payload Structure**:
  - Required: `payload.payment.entity.id`, `amount`, `currency`, `status == "failed"`, `created_at`.
  - Optional: `subscription_id`, `invoice_id`, `error_code`, `error_description`.
- **Database Records Affected**:
  - `BillingPayment`: Staged with `status = 'failed'`.
  - `BillingInvoice`: Recorded with `processing_status = 'failed'`. Attempt counter incremented.
- **Allowed Subscription State Transitions**:
  - **NONE**. `payment.failed` NEVER directly mutates `Subscription.status`.
  - Subscription status is governed exclusively by `subscription.pending` and `subscription.halted`.
- **Organization Billing Period Mutation**: **NONE**.
- **Quota Entitlement Impact**: **NONE**.
- **Entity Idempotency Key**: `f"pay_failed:{payment_id}"`.
- **Stale Failure Guard**: If the referenced invoice is already `processed` (paid), failure details are recorded under `provider_metadata["stale_failures"]` without regressing invoice status.

---

## 4. Phase 3: Mandatory Business Invariants

```
+---------------------------------------------------------------------------------------------------+
| Mandatory Business Invariants Enforcement                                                        |
+---------------------------------------------------------------------------------------------------+
  (1) subscription.activated  --> Updates agreement status (incomplete -> active)
                                  plan_tier remains 'free'
                                  Zero token quota granted
                                  NO OrganizationBillingPeriod created

  (2) Full-Payment Settlement  --> ONLY path to Pro quota & grace recovery:
                                  Captured payment amount == Invoice gross amount
                                  Currency matches exactly
                                  Invoice amount due == 0
                                  Invoice marked 'paid' & 'processed'
                                  past_due_since cleared ONLY in this transaction
                                  Deactivates prior period -> Provisions new Pro period

  (3) 7-Day Grace Window      --> First failure sets past_due_since
                                  Repeated failures preserve anchor
                                  subscription.halted preserves grace if now < anchor + 7d
                                  Partial/failed payments DO NOT clear past_due_since

  (4) Cancellation Handling   --> Active cancellation at cycle end: cancel_at_period_end = True
                                  Active immediate cancellation: status = canceled, tier = free
                                  Past-due cancellation: Two-Phase Deferred Cancellation:
                                    Phase 1: status remains past_due, canceled_at = now
                                             accounting.py permits usage for remaining days
                                    Phase 2: after 7d, accounting blocks; sweeper cancels
+---------------------------------------------------------------------------------------------------+
```

### 4.1 Agreement Status vs Paid Entitlement Decoupling
In Razorpay, a subscription agreement can be in status `"active"` even when the customer has not paid for the current period (e.g. initial setup without charge, or admin card update during retry).
- In our local architecture, `Subscription.status` tracks agreement status, while `Subscription.plan_tier` and `OrganizationBillingPeriod` track paid entitlement.
- Even if Razorpay reports `status = "active"`, `plan_tier` remains `"free"` unless a qualifying full-payment invoice settlement has committed.
- In `accounting.py` (`apps/api/app/services/billing/accounting.py#L258`), free billing periods bypass Pro quota entitlement checks entirely. Pro periods require an active Pro subscription with an unbroken payment history or active grace anchor.

### 4.2 Full-Payment Settlement Specification
In `settle_invoice()`:
1. **Exact Amount Match**:
   $$\text{BillingPayment.amount\_minor\_units} == \text{BillingInvoice.gross\_amount\_minor\_units}$$
2. **Exact Currency Match**:
   $$\text{BillingPayment.currency} == \text{BillingInvoice.currency}$$
3. **Zero Amount Due**:
   $$\text{BillingInvoice.amount\_due\_minor\_units} == 0$$
4. **Single Captured Payment**:
   $$\sum \text{qualifying payments} = 1$$
   Partial payments are never summed to fabricate a full payment. Partial payments are quarantined in `billing_payments` with `reconciliation_state = 'partial'`.
5. **Atomic Period Provisioning & Grace Recovery**:
   - In the exact same database transaction:
     - `UPDATE organization_billing_periods SET is_active = false WHERE organization_id = :org_id AND is_active = true`
     - `INSERT INTO organization_billing_periods (organization_id, plan_tier, monthly_token_quota, period_start, period_end, is_active)`
     - `UPDATE billing_invoices SET processing_status = 'processed', billing_period_id = :new_period_id`
     - `UPDATE subscriptions SET plan_tier = 'pro', status = 'active', past_due_since = NULL`

---

## 5. Phase 4: Cancellation Timestamp Semantics

### 5.1 Inspection of Cancellation Fields
The `Subscription` model (`apps/api/app/models/subscription.py#L133-L150`) exposes:
- `current_period_end`: End of current billing cycle (authoritative date).
- `cancel_at_period_end`: Boolean flag indicating whether cancellation takes effect at cycle end.
- `canceled_at`: Timezone-aware timestamp.
- `status`: `SubscriptionStatus` enum.

`accounting.py` (`apps/api/app/services/billing/accounting.py#L360`) explicitly asserts:
`cancel_at_period_end=True alone does NOT revoke entitlement while status is active.`
`accounting.py` does **not** evaluate `canceled_at` to grant or deny access; it relies strictly on `status in ('active', 'past_due')` and `past_due_since`.

The API layer (`apps/api/app/api/billing.py#L642-L643`) exposes both `cancel_at_period_end` and `canceled_at` to client frontends.

### 5.2 Exact Semantic Specifications

| Lifecycle Event | `status` | `cancel_at_period_end` | `canceled_at` | `current_period_end` | Entitlement / Usage Result |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Active Cycle-End Cancellation** | `active` | `True` | `event_created_at` (request time) | Unchanged | Retained until `current_period_end`. |
| **Active Immediate Cancellation** | `canceled` | `False` | `event_created_at` (effective time) | Unchanged | Revoked immediately (`accounting.py` fails closed). |
| **Past-Due Cancellation (Phase 1: In Grace)** | `past_due` | `True` | `event_created_at` (request time) | Unchanged | Retained until $\text{past\_due\_since} + 7\text{ days}$. |
| **Past-Due Cancellation (Phase 2: Grace Expired)** | `canceled` | `False` | Unchanged (or `now_utc`) | Unchanged | Revoked. `accounting.py` blocks access. |
| **Repeated Cancellation Event** | `canceled` | `False` | Backfilled if `None`; never overwritten | Unchanged | Idempotent no-op. |

- **Conclusion**: The existing schema fields completely and unambiguously represent both cancellation requested time and effective lifecycle state without adding redundant columns.

---

## 6. Phase 5: Event Timestamps & Stale Event Protection

### 6.1 Trustworthy vs Unreliable Timestamps
- **Trustworthy**:
  - Webhook Envelope `created_at`: The official Unix timestamp generated by Razorpay's dispatch infrastructure. Used for monotonic ordering against `Subscription.last_webhook_event_at`.
  - Entity-level `current_start` and `current_end`: Authoritative period boundaries.
  - Payment entity `created_at`: Authoritative capture time.
- **Unreliable**:
  - Webhook HTTP Arrival Order: Webhook HTTP deliveries can arrive out of order, delayed by hours due to network partition or worker retries.
  - Client-supplied metadata or browser callback timestamps.

### 6.2 Stale Event Protection Rules
1. **Delayed `subscription.pending` After Settlement**:
   - Compares `event.created_at` against `Subscription.last_webhook_event_at`.
   - If `event.created_at <= Subscription.last_webhook_event_at` and current local status is `active` with an active paid period: discarded as a stale failure.
2. **Old Failure After Successful Payment**:
   - `payment.failed` handler locks `BillingInvoice` under advisory lock.
   - If `BillingInvoice.processing_status == InvoiceProcessingStatus.processed`: the failure is recorded under `provider_metadata["stale_failures"]`; invoice status is **never** regressed.
3. **Old Activation After Cancellation**:
   - If local `Subscription.status in ('canceled', 'completed')`:
   - Any incoming `subscription.activated` or `subscription.authenticated` for that same `provider_subscription_id` is an attempted resurrection.
   - Discarded immediately; status remains `canceled`.

---

## 7. Phase 6: Lock Hierarchy & Deadlock Analysis

### 7.1 Comprehensive Transaction Path Analysis

We examine all 7 transaction paths touching billing entities across the codebase:

```
+---------------------------------------------------------------------------------------------------+
| Comprehensive Billing Transaction Paths & Lock Hierarchy Matrix                                   |
+---------------------------------------------------------------------------------------------------+
  Path 1: accounting.py (reserve_usage)
          1. SELECT ... FROM organization_billing_periods FOR UPDATE (Level 1)
          2. SELECT ... FROM subscriptions FOR UPDATE (Level 2)
          3. SELECT ... FROM usage_reservations (Level 3 - non-locking MVCC read)

  Path 2: accounting.py (settle_usage)
          1. SELECT ... FROM organization_billing_periods FOR UPDATE (Level 1)
          2. SELECT ... FROM usage_reservations FOR UPDATE (Level 3)

  Path 3: razorpay/checkout.py (create_subscription)
          - Phase 1 (Tx 1): MVCC read subscriptions -> SELECT billing_checkout_attempts FOR UPDATE -> COMMIT
          - Phase 2: Gateway HTTP call (Zero DB transactions / zero locks held)
          - Phase 3 (Tx 2): SELECT billing_checkout_attempts FOR UPDATE -> COMMIT

  Path 4: razorpay/customer.py (get_or_create_razorpay_customer)
          - Operates strictly on billing_customer_provisions. Zero locks on billing_periods/subscriptions.

  Path 5: razorpay/webhook_engine.py (claim & outcome)
          - claim: SELECT billing_webhook_events FOR UPDATE SKIP LOCKED -> COMMIT
          - outcome: SELECT billing_webhook_events FOR UPDATE -> COMMIT
          - Zero locks on billing_periods, subscriptions, or invoices.

  Path 6: Razorpay Agreement Handlers (activated, pending, cancelled, updated)
          1. SELECT subscriptions (MVCC read for tenant org_id)
          2. SELECT pg_advisory_xact_lock(hash(subscription_id)) (Level 0)
          3. SELECT subscriptions FOR UPDATE (Level 2)

  Path 7: Razorpay Settlement Handler (subscription.charged / settle_invoice)
          1. SELECT subscriptions (MVCC read for tenant org_id)
          2. SELECT pg_advisory_xact_lock(hash(invoice_or_subscription_id)) (Level 0)
          3. SELECT organization_billing_periods FOR UPDATE (Level 1 - active period)
          4. SELECT subscriptions FOR UPDATE (Level 2)
          5. Re-query organization_billing_periods under lock (In-lock re-query)
          6. SELECT billing_invoices FOR UPDATE (Level 3)
          7. SELECT billing_payments FOR UPDATE (Level 4)
+---------------------------------------------------------------------------------------------------+
```

### 7.2 Deadlock Impossibility Proof
- **Lock Ordering Consistency**:
  - `accounting.py` (Path 1) acquires: Level 1 (`OrganizationBillingPeriod`) $\to$ Level 2 (`Subscription`).
  - Razorpay Settlement Handler (Path 7) acquires: Level 0 (Advisory) $\to$ Level 1 (`OrganizationBillingPeriod`) $\to$ Level 2 (`Subscription`) $\to$ Level 3 (`BillingInvoice`) $\to$ Level 4 (`BillingPayment`).
  - Razorpay Agreement Handlers (Path 6) acquire: Level 0 (Advisory) $\to$ Level 2 (`Subscription`). They **never** acquire Level 1.
- **Deadlock Cycle Check**:
  - Can Path 1 wait for Path 7 while Path 7 waits for Path 1?
    - Path 1 requests Level 1 then Level 2.
    - Path 7 requests Level 1 then Level 2.
    - Whichever transaction acquires Level 1 first proceeds; the other blocks on Level 1. **No cycle exists.**
  - Can Path 1 wait for Path 6 while Path 6 waits for Path 1?
    - Path 1 holds Level 1, requests Level 2.
    - Path 6 holds Level 2, does **not** request Level 1. Path 6 finishes and releases Level 2. Path 1 acquires Level 2. **No cycle exists.**
- **Advisory Locks**:
  - Derived from deterministic SHA-256 hashes of provider entity IDs: `pg_advisory_xact_lock(_derive_advisory_lock_key(entity_id))`.
  - Scoped to transaction boundaries; released automatically on `commit()` or `rollback()`.
- **Unique Constraint Conflicts**:
  - If two workers race to insert an active billing period, `uq_org_active_billing_period` raises `IntegrityError`.
  - Transaction boundary rule: Handlers catch `IntegrityError`, execute `await db.rollback()`, and stage reconciliation.

---

## 8. Phase 7: Subscription Transition Matrix

| Current Local Status | Incoming Event | Guard Conditions | Resulting Local Status | Resulting Plan Tier | Side Effects & Invariants |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `incomplete` | `subscription.authenticated` | Fresh event | `incomplete` | `free` | Stages checkout attempt as `payment_pending`. |
| `incomplete` | `subscription.activated` | Fresh event | `active` | `free` | Agreement active; **Zero Pro quota granted**. |
| `incomplete` | `subscription.charged` | Full settlement verified | `active` | `pro` | **Pro Period Provisioned**; first invoice paid; `past_due_since = NULL`. |
| `incomplete` | `subscription.cancelled` | Customer abandoned | `canceled` | `free` | Terminal cancellation; `canceled_at = event_created_at`. |
| `active` | `subscription.charged` | Renewal verified | `active` | `pro` | **Renewal Period Provisioned**; advances `current_period_end`; `past_due_since = NULL`. |
| `active` | `subscription.pending` | First renewal failure | `past_due` | `pro` | Sets `past_due_since = event_timestamp`; 7-day grace starts. |
| `active` | `subscription.cancelled` | Cycle-end cancellation | `active` | `pro` | Sets `cancel_at_period_end = True`, `canceled_at = event_created_at`. |
| `active` | `subscription.cancelled` | Immediate cancellation | `canceled` | `free` | Immediate termination; `canceled_at = event_created_at`. |
| `active` | `subscription.completed` | Final cycle reached | `canceled` | `free` | Sub complete; demotes to Free. |
| `past_due` | `subscription.charged` | Full payment verified | `active` | `pro` | **Cures Past-Due**: Clears `past_due_since = NULL`; Pro extended. |
| `past_due` | `subscription.charged` | Partial/Ambiguous payment | `past_due` | `pro` | **Grace anchor intact**: `past_due_since` NOT cleared; quarantined. |
| `past_due` | `subscription.pending` | Repeated retry failure | `past_due` | `pro` | **Strictly preserves original `past_due_since`**. |
| `past_due` | `subscription.halted` | `now < past_due_since + 7d` | `past_due` | `pro` | Preserves grace period access; logs provider halt in metadata. |
| `past_due` | `subscription.halted` | `now >= past_due_since + 7d` | `unpaid` | `free` | Grace expired; blocks access in `accounting.py`. |
| `past_due` | `subscription.cancelled` | `now < past_due_since + 7d` | `past_due` | `pro` | **Two-Phase Deferred Cancellation**: `cancel_at_period_end = True`. |
| `past_due` | `subscription.cancelled` | `now >= past_due_since + 7d` | `canceled` | `free` | Grace expired; immediate terminal cancellation. |
| `unpaid` | `subscription.charged` | Portal recovery payment | `active` | `pro` | **Cures Unpaid**: Settle invoice, provisions new Pro period. |
| `canceled` | **ANY EVENT** | Delayed/replay event | `canceled` | `free` | **Refusal of Resurrection**: Safely ignored; logs diagnostic. |

---

## 9. Phase 8: Idempotency & Reconciliation Design

```
+---------------------------------------------------------------------------------------------------+
| Multi-Layer Idempotency & Conflict Resolution                                                     |
+---------------------------------------------------------------------------------------------------+
  Layer 1: Webhook Ingress Deduplication
           uq_billing_webhook_provider_event on billing_webhook_events
           (provider, merchant_account_id, environment, provider_event_id)
     |
     v
  Layer 2: Event Lease Ownership Fencing
           lease_token UUID4 + lease_expires_at comparison
           [Stale workers taking >60s rejected with StaleLeaseError]
     |
     v
  Layer 3: Domain Entity Monotonic Chronology
           last_webhook_event_at timestamp tracking on Subscription
           [Stale out-of-order events backfill missing data but never regress lifecycle status]
     |
     v
  Layer 4: Entity-Level Unique Constraints
           uq_subscriptions_provider_subscription
           uq_billing_invoices_provider_invoice
           uq_org_active_billing_period
     |
     v
  Layer 5: Reconciliation Ledger Quarantine
           PaymentReconciliation table
           [Divergent payments, unbound customers, and duplicate subscriptions quarantined for audit]
+---------------------------------------------------------------------------------------------------+
```

---

## 10. Phase 9: Test Specification Plan

### 10.1 Required Test Scenarios
1. **Activation $\neq$ Entitlement**:
   - `subscription.activated` marks `status = 'active'`, updates period dates, leaves `plan_tier = 'free'`.
   - `reserve_usage()` fails closed with `SUBSCRIPTION_INACTIVE` on activation alone.
2. **Settlement & Grace Anchor Clearing**:
   - Full settlement (`subscription.charged` with exact amount) provisions Pro period and clears `past_due_since = None`.
   - Partial payment (`amount < gross_amount`) fails settlement; `past_due_since` is **not** cleared.
   - Ambiguous or failed payment leaves `past_due_since` untouched.
3. **Grace-Period Lifecycle**:
   - First `subscription.pending` sets `past_due_since = event_timestamp`.
   - Repeated `subscription.pending` preserves original anchor.
   - At $t = \text{anchor} + 6.9\text{d}$: Pro reservation succeeds.
   - At $t = \text{anchor} + 7.1\text{d}$: Pro reservation fails (`GRACE_PERIOD_EXPIRED`).
   - `subscription.halted` within 7 days preserves `past_due` status.
   - `subscription.halted` after 7 days transitions to `unpaid`.
4. **Cancellation Timestamp Semantics**:
   - Scheduled cancellation sets `cancel_at_period_end = True`, `canceled_at = event_created_at`; access retained.
   - Immediate cancellation resets tier to `free` immediately.
   - Two-Phase Deferred Cancellation: Cancellation while `past_due` preserves `status = 'past_due'` with `cancel_at_period_end = True`. Access permitted for remaining grace days.
5. **Monotonicity & Refusal of Resurrection**:
   - Delayed `subscription.activated` arriving after cancellation is discarded.
   - Replayed `subscription.charged` arriving for canceled subscription records invoice as ignored.
6. **Deadlock Concurrency**:
   - Concurrent `subscription.charged` and `reserve_usage()` serialize on the global lock hierarchy without deadlocks.

---

## 11. Phase 10: Recommended Implementation Sequence

```
+---------------------------------------------------------------------------------------------------+
| Recommended Step 5.2D.3 Implementation Sequence                                                   |
+---------------------------------------------------------------------------------------------------+
  Step 5.2D.3.1: Shared Utilities & Advisory Lock Infrastructure
  - Implement hash-based PostgreSQL advisory locking for Razorpay entities.
  - Implement shared timestamp parsing, error classification, and tenant identity helpers.

  Step 5.2D.3.2: Agreement Lifecycle Handlers
  - Implement handle_subscription_authenticated()
  - Implement handle_subscription_activated() (plan_tier='free', zero period provisioning)
  - Implement handle_subscription_updated()
  - Implement handle_subscription_completed()

  Step 5.2D.3.3: Failure, Grace Period & Cancellation Handlers
  - Implement handle_subscription_pending() (grace anchor initialization)
  - Implement handle_subscription_halted() (grace preservation logic)
  - Implement handle_subscription_cancelled() (Two-Phase Deferred Cancellation)
  - Implement handle_payment_failed() (diagnostic failure tracking)

  Step 5.2D.3.4: Settlement & Quota Provisioning Handler
  - Implement handle_subscription_charged() (Full-Payment Settlement engine)
  - Wire atomic grace recovery (clearing past_due_since only upon qualifying settlement)
  - Wire OrganizationBillingPeriod provisioning and period rollover

  Step 5.2D.3.5: Dispatcher Wiring & Full Regression Verification
  - Register all 9 handlers in default_razorpay_dispatcher
  - Run full billing regression test suite (187+ tests)
+---------------------------------------------------------------------------------------------------+
```

---

## 12. Conclusion & Sign-Off

The design corrections specified in Step 5.2D.3A.1 resolve all recovery, timestamp, event ordering, and concurrency ambiguities. The subscription lifecycle architecture is mathematically consistent with `accounting.py` and approved for implementation.
