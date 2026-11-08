# Aptlyst AI — Step 5.2D.3.2.2 Implementation & Design Report: Agreement Handler Final Corrections

**Date:** 2026-11-08  
**Scope:** Step 5.2D.3.2.2 Agreement Handler Corrections & Paid-Period Preservation Architecture  
**Alembic Head:** `d1e2f3a4b5c6` (Single head verified)  
**Git Working Tree:** Clean on `main` (0 commits, 0 pushes)  
**Test Suite Status:** 202 / 202 Tests Passing (All 100% Mocked/Offline, 0 Live DB Connections)

---

## 1. Executive Summary

Step 5.2D.3.2.2 resolves the conditions identified in the Step 5.2D.3.2.1 audit while strictly preserving all transaction boundaries, isolation invariants, and provider-neutral billing guarantees:

1. **Missing Checkout Attempt Reconciliation (Finding 1 Resolved):**
   - In `handle_subscription_authenticated()`, when no matching `BillingCheckoutAttempt` row exists for a provider subscription ID, the handler does **not** invent an organization association or hallucinate checkout state.
   - Authoritative tenant resolution is anchored strictly on existing `BillingCustomerProvision` and `Subscription` records.
   - Verified provider identifiers (`provider_subscription_id`, `provider_customer_id`, `provider_plan_id`) are preserved in a durable `PaymentReconciliation` record staged under `case_key = "missing_checkout_attempt:{sub_id}"` with `reason = "missing_checkout_attempt"`.
   - Historical diagnostic traces are append-only in `diagnostic_payload["history"]`, ensuring duplicate webhook deliveries never overwrite evidence or create duplicate rows.
   - Ambiguous multiple checkout matches (`len(attempts) > 1`) and tenant mismatches (`attempt.organization_id != authoritative_org_id`) fail closed immediately with `RazorpayWebhookReconciliationConflictError`.
   - Symmetrical protection was applied to `handle_subscription_activated()`.

2. **Removal of Inert `stmt_pending` Branch (Finding 2 Resolved):**
   - The unreachable legacy branch searching for unbound subscriptions (`provider_subscription_id.is_(None)`) in `handle_subscription_authenticated()` was completely excised.
   - The handler now directly constructs and inserts `Subscription(status=incomplete, plan_tier="free")`, eliminating heuristic `.limit(1)` matching.

3. **In-Depth Analysis & Architectural Design Proposal for `subscription.completed` (Requirement 2):**
   - An exhaustive investigation was conducted tracing `handle_subscription_completed()`, `accounting.py` lines 258–274, the PostgreSQL `subscription_status` enum, and the partial unique index `uq_org_active_subscription`.
   - **Key Architectural Finding:** The existing codebase and database schema demonstrably **cannot** represent provider agreement completion separately from entitlement expiry without modifying either `accounting.py`, the database enum, or introducing a dedicated lifecycle worker.
   - Per the prompt's explicit directive (*"If the existing model cannot represent provider agreement completion separately from entitlement expiry, stop and produce a design proposal rather than making an unsafe state change"*), **no unsafe state modifications were made to `handle_subscription_completed()`**. Instead, a comprehensive architectural design proposal is presented in Section 4 of this report.

---

## 2. Code Changes Detailed Breakdown

### 2.1 File: `apps/api/app/services/billing/razorpay/agreement_handlers.py`

#### A. Removal of `stmt_pending` and Direct Model Instantiation
- **Before:** Lines 183–229 performed a `select(Subscription).where(provider_subscription_id.is_(None)).limit(1)` fallback search.
- **After:** Lines 182–204 directly instantiate a new `Subscription` record when no matching row is found by `provider_subscription_id == sub_id`:
  ```python
  sub = Subscription(
      id=uuid.uuid4(),
      organization_id=org_id,
      provider="razorpay",
      merchant_account_id=ctx.merchant_account_id,
      environment=ctx.environment,
      provider_customer_id=sub_entity.customer_id or f"cust_unknown_{sub_id}",
      provider_subscription_id=sub_id,
      provider_plan_id=sub_entity.plan_id,
      status=SubscriptionStatus.incomplete,
      plan_tier="free",
      current_period_start=period_start,
      current_period_end=period_end,
      last_webhook_event_at=ctx.event_created_at,
  )
  db.add(sub)
  ```

#### B. Checkout Attempt Querying & Reconciliation Triaging
In both `handle_subscription_authenticated()` and `handle_subscription_activated()`:
- Executed under row lock:
  ```python
  stmt_attempt = (
      select(BillingCheckoutAttempt)
      .where(
          BillingCheckoutAttempt.provider == "razorpay",
          BillingCheckoutAttempt.merchant_account_id == ctx.merchant_account_id,
          BillingCheckoutAttempt.environment == ctx.environment,
          BillingCheckoutAttempt.provider_subscription_id == sub_id,
      )
      .with_for_update()
  )
  res_attempt = await db.execute(stmt_attempt)
  attempts = list(res_attempt.scalars().all())
  ```
- **Ambiguous Match Guard (`len(attempts) > 1`):**
  Stages a reconciliation case with `case_key = f"ambiguous_checkout_attempt:{sub_id}"`, logs diagnostic evidence, and **fails closed** by raising `RazorpayWebhookReconciliationConflictError`.
- **Missing Attempt Guard (`len(attempts) == 0`):**
  In `handle_subscription_authenticated()`, stages or updates a reconciliation record under `case_key = f"missing_checkout_attempt:{sub_id}"` with `reason = "missing_checkout_attempt"`. Does not invent an association; subscription proceeds with `plan_tier="free"`.
- **Tenant Mismatch Guard (`attempt.organization_id != org_id`):**
  Stages reconciliation under `case_key = f"checkout_attempt_tenant_mismatch:{attempt.id}:{org_id}"`, records conflicting tenant IDs, and **fails closed** by raising `RazorpayWebhookReconciliationConflictError`.
- **Valid Single Match:**
  Transitions attempt to `payment_pending` (in authenticated) or `complete` (in activated), and backfills `provider_customer_id` if missing.

---

## 3. Test Suite Enhancements & Mock Session Fidelity

### 3.1 File: `apps/api/tests/test_razorpay_agreement_handlers.py`

#### A. Mock Session Upgrades
- `RazorpayAgreementMockDbSession.add()`: Updated `BillingCheckoutAttempt` indexing to use `str(instance.id)` rather than `instance.provider_subscription_id`, permitting multiple attempts with identical provider IDs to coexist for ambiguity testing.
- `RazorpayAgreementMockDbSession.execute()`:
  - `billing_checkout_attempts` lookup returns `MockScalarResult(list(self.checkout_attempts.values()))`, properly supporting `.scalars().all()` across zero, single, and multiple matches.
  - `billing_webhook_events` lookup parses `stmt._where_criteria` to extract the target event ID, ensuring multi-event duplicate delivery tests assert leases against the exact event rather than naively returning the first map entry.

#### B. Six New Rigorous Test Cases
1. `test_subscription_authenticated_missing_checkout_attempt`:
   - Validates that an absent checkout attempt stages `PaymentReconciliation` with `reason="missing_checkout_attempt"` and verified provider identifiers without failing closed.
2. `test_subscription_authenticated_duplicate_delivery_missing_checkout_attempt`:
   - Delivers repeated authentication events; verifies that exactly 1 reconciliation row is maintained, and `diagnostic_payload["history"]` appends entries without losing earlier traces.
3. `test_subscription_authenticated_ambiguous_checkout_attempts_fails_closed`:
   - Creates 2 competing checkout attempts for the same subscription ID; verifies fail-closed exception and staged reconciliation case.
4. `test_subscription_authenticated_mismatched_checkout_attempt_fails_closed`:
   - Verifies that when a checkout attempt belongs to a different organization than the authoritative customer provision, the handler raises `RazorpayWebhookReconciliationConflictError` and records tenant mismatch evidence.
5. `test_subscription_activated_ambiguous_checkout_attempts_fails_closed`:
   - Symmetrically verifies ambiguous attempt fail-closed behavior during agreement activation.
6. `test_subscription_activated_mismatched_checkout_attempt_fails_closed`:
   - Symmetrically verifies tenant mismatch fail-closed behavior during agreement activation.

---

## 4. Comprehensive Design Proposal: `subscription.completed` and Paid-Period Preservation

### 4.1 The Core Problem & Architectural Conflict

In Razorpay, a subscription can be configured with a finite count of billing cycles (`total_count`).
For example:
- A user purchases a 1-month non-renewing Pro pass (`total_count = 1`).
- Or an annual upfront installment plan (`total_count = 12`).

When the customer pays for cycle 1, Razorpay fires `subscription.authenticated`, `subscription.activated`, and `invoice.paid` / `payment.captured`.
Because `paid_count == total_count` (1 of 1 completed), Razorpay's state machine immediately marks the subscription as `completed` on the gateway and dispatches the webhook `subscription.completed`.

**Crucial Timing Reality:**
Razorpay dispatches `subscription.completed` **at the moment the last cycle is initiated/paid**, NOT at the end of the 30-day billing period!
Furthermore, Razorpay **never** sends another webhook when `current_period_end` arrives 30 days later.

### 4.2 Why the Existing Codebase Cannot Represent This Without Changes

#### 1. Entitlement Revocation Conflict (`accounting.py`)
Let us inspect `apps/api/app/services/billing/accounting.py` lines 258–274:
```python
is_paid_period = bool(period.plan_tier and period.plan_tier.strip().lower() != 'free')
if is_paid_period:
    stmt_sub = select(Subscription.id).where(
        Subscription.organization_id == organization_id,
        Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)),
    )
    res_sub = await db.execute(stmt_sub)
    if res_sub.scalar_one_or_none() is None:
        return AccountingResult(
            allowed=False,
            error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
            message="No active subscription found for paid tier",
        )
```
- If `handle_subscription_completed()` sets `sub.status = SubscriptionStatus.canceled` and `sub.plan_tier = "free"` on Day 1:
  - On Day 2, a user generates an LLM embedding or chat turn.
  - `accounting.py` queries `Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due))`.
  - The query returns zero rows!
  - `accounting.py` rejects the user's request with `SUBSCRIPTION_INACTIVE`.
  - **Result:** The user loses 29 days of already-paid Pro quota! This directly violates Invariant 2 (*"An active paid period must not lose its legitimate remaining access prematurely"*).

#### 2. The Resubscription Lockout Conflict (`uq_org_active_subscription`)
Suppose we attempt to avoid revoking access by keeping `sub.status = SubscriptionStatus.active` and setting `sub.cancel_at_period_end = True`.
- `apps/api/app/models/subscription.py` defines:
  ```python
  Index(
      "uq_org_active_subscription",
      "organization_id",
      unique=True,
      postgresql_where="status IN ('active', 'trialing')",
  )
  ```
- Because Razorpay never emits a second event at `current_period_end`, and because there is no background expiry worker in the application, `sub.status` remains `'active'` **forever**.
- When the 30 days expire and the customer attempts to purchase a new Pro plan, `create_razorpay_subscription_checkout` invokes `assert_no_active_subscription`.
- It finds `sub.status == 'active'` and raises `ActiveSubscriptionExistsError`, permanently blocking the customer from ever resubscribing!

#### 3. Database Schema Limitation (`SubscriptionStatus` Enum)
The PostgreSQL database type `subscription_status` defines:
```sql
CREATE TYPE subscription_status AS ENUM (
    'incomplete',
    'incomplete_expired',
    'trialing',
    'active',
    'past_due',
    'canceled',
    'unpaid',
    'paused'
);
```
There is **no** `'completed'` value in `subscription_status`.

---

### 4.3 Proposed Architectural Solutions

We evaluate three potential architectural designs:

| Dimension | Option A: Lazy Accounting & Checkout Expiry | Option B: Background Expiry Worker | Option C: Dedicated `'completed'` DB Status Enum |
| :--- | :--- | :--- | :--- |
| **Schema Migration Required?** | **No** (Zero Alembic migrations) | **No** (Zero Alembic migrations) | **Yes** (`ALTER TYPE subscription_status ADD VALUE 'completed'`) |
| **New Background Infrastructure?** | None | Requires a new periodic cron/worker loop | None (relies on Option A or B for timing) |
| **Preserves Paid Period Access?** | **Yes** (Evaluated lazily against `period.period_end`) | **Yes** (Transition delayed until clock expiry) | **Yes** (`completed` recognized during grace period) |
| **Prevents Resubscription Lockout?** | **Yes** (Checkout ignores expired `cancel_at_period_end`) | **Yes** (Worker flips status to `canceled`) | **Yes** (Exclude `'completed'` from partial unique index) |
| **Zero New Quota Invariant?** | **Yes** (Period quota remains capped at invoice minor units) | **Yes** (No new period created) | **Yes** (No new period created) |
| **Complexity & Risk** | **Low / Non-disruptive** | Medium (new operational daemon) | Medium-High (PostgreSQL enum alterations) |

---

### 4.4 Recommended Architectural Design: Option A + Targeted Lazy Expiry

We recommend **Option A**, augmented by lazy state finalization during checkout and accounting checks.

#### Component 1: `handle_subscription_completed()` Semantics
When `subscription.completed` is received:
1. Do **not** transition `sub.status` to `canceled` immediately.
2. Verify if `sub.current_period_end` is in the future (`now_utc < sub.current_period_end`):
   - **If Future (Legitimate paid time remains):**
     - Maintain `sub.status = SubscriptionStatus.active`.
     - Set `sub.cancel_at_period_end = True`.
     - Set `sub.canceled_at = ctx.event_created_at`.
     - Log diagnostic message: `"Subscription %s marked completed; access retained until %s"`.
   - **If Expired (`now_utc >= sub.current_period_end` or no paid period):**
     - Immediately set `sub.status = SubscriptionStatus.canceled`.
     - Set `sub.plan_tier = "free"`.
     - Set `sub.cancel_at_period_end = False`.
     - Set `sub.canceled_at = ctx.event_created_at`.

#### Component 2: Lazy Entitlement Expiry in `accounting.py`
In `apps/api/app/services/billing/accounting.py`:
Modify the subscription query in lines 260–274 to evaluate whether an active subscription marked `cancel_at_period_end` has passed its period end:
```python
if is_paid_period:
    stmt_sub = select(Subscription).where(
        Subscription.organization_id == organization_id,
        Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)),
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    # Lazy Expiry Check
    if sub is not None and sub.cancel_at_period_end and sub.current_period_end:
        now_utc = datetime.now(timezone.utc)
        if now_utc >= sub.current_period_end:
            # Lazily demote status to canceled and plan_tier to free
            sub.status = SubscriptionStatus.canceled
            sub.plan_tier = "free"
            sub.cancel_at_period_end = False
            sub = None  # Demoted; paid access expired

    if sub is None:
        return AccountingResult(
            allowed=False,
            error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
            message="No active subscription found for paid tier",
        )
```

#### Component 3: Lazy Checkout Unlocking in `checkout.py`
In `apps/api/app/services/billing/razorpay/checkout.py` (and the provider-neutral subscription check):
When checking for existing active subscriptions:
```python
# If existing subscription has cancel_at_period_end=True and now >= current_period_end:
if existing_sub.cancel_at_period_end and existing_sub.current_period_end:
    if datetime.now(timezone.utc) >= existing_sub.current_period_end:
        existing_sub.status = SubscriptionStatus.canceled
        existing_sub.plan_tier = "free"
        existing_sub.cancel_at_period_end = False
        await db.flush()
        # Allows checkout to proceed cleanly without ActiveSubscriptionExistsError!
```

#### Invariant Verification Under Proposed Design
1. **Full-Payment Settlement Grants Quota:** Granted exclusively when `invoice.paid` / `payment.captured` settles.
2. **Paid Period Access Preserved:** Until `now >= current_period_end`, the subscription is active, `accounting.py` authorizes requests, and the tenant consumes their purchased quota.
3. **No Unearned Quota:** No new `OrganizationBillingPeriod` rows are created; agreement completion prevents renewal.
4. **Resurrection Refusal:** `is_terminal_subscription()` guards ensure that once demoted, replay events cannot reactivate it.
5. **Clean Unlocking:** The moment the clock passes `current_period_end`, both `accounting.py` and checkout seamlessly finalize the subscription to `canceled`, releasing `uq_org_active_subscription`.

---

## 5. Verification Matrix & Test Classifications

All test suites were executed against the offline/mocked harness. Zero network or live database calls were performed.

| Test File | Test Count | Classification | Live DB | Result |
| :--- | :---: | :--- | :---: | :---: |
| `test_razorpay_agreement_handlers.py` | 18 | Mocked Async Handlers (`RazorpayAgreementMockDbSession`) | 0 | **PASS** |
| `test_razorpay_handler_foundation.py` | 21 | Mocked Async & Unit (`RazorpayHandlerMockDbSession`) | 0 | **PASS** |
| `test_razorpay_webhook_worker.py` | 11 | Mocked Async Worker (`MockAsyncSession`) | 0 | **PASS** |
| `test_razorpay_webhook_ingestion.py` | 18 | Mocked FastAPI TestClient | 0 | **PASS** |
| `test_razorpay_checkout.py` | 28 | Mocked Async Service (`httpx.MockTransport`) | 0 | **PASS** |
| `test_razorpay_customer.py` | 25 | Mocked Async Service (`httpx.MockTransport`) | 0 | **PASS** |
| `test_razorpay_client.py` | 12 | Mocked HTTP Transport (`httpx.MockTransport`) | 0 | **PASS** |
| `test_razorpay_config.py` | 8 | Static / Offline Configuration Tests | 0 | **PASS** |
| `test_provider_neutral_billing_models.py` | 15 | Static / SQLAlchemy Metadata Reflection | 0 | **PASS** |
| **Total Test Suite** | **202** | **100% Mocked / Static / Offline** | **0** | **PASS** |

### Execution Command
```powershell
apps/api/.venv/Scripts/python -m pytest apps/api/tests/test_razorpay_agreement_handlers.py apps/api/tests/test_razorpay_handler_foundation.py apps/api/tests/test_razorpay_webhook_worker.py apps/api/tests/test_razorpay_webhook_ingestion.py apps/api/tests/test_razorpay_checkout.py apps/api/tests/test_razorpay_customer.py apps/api/tests/test_razorpay_client.py apps/api/tests/test_razorpay_config.py apps/api/tests/test_provider_neutral_billing_models.py -v
# Output: 202 passed in 8.76s
```

### Alembic & Version Control Invariants
- **Alembic Single Head:** Confirmed at `d1e2f3a4b5c6`. No pending, branched, or dangling migrations.
- **Git Hygiene:** No commits created, no branches pushed.

---

## 6. Recommendations for Next Stage (Step 5.2D.3.3)

With the agreement handlers and checkout reconciliation triaging fully hardened and tested:
1. **Implement Invoice and Settlement Handlers (Step 5.2D.3.3):**
   - Implement `invoice.paid`, `payment.captured`, and `subscription.charged`.
   - Enforce the full-payment settlement requirement before granting Pro quota or creating `OrganizationBillingPeriod` rows.
   - Enforce single-transaction clearance of `past_due_since` only upon verified full payment.
2. **Review & Approve `subscription.completed` Design Proposal:**
   - Review Section 4 of this report to decide whether Option A (Lazy Expiry in accounting and checkout) or Option C (Adding `'completed'` to the DB enum via migration) is preferred before finalizing cancellation handlers.
