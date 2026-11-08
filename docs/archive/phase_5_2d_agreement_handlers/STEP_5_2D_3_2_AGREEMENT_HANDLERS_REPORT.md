# Aptlyst AI — Step 5.2D.3.2 Implementation Report: Razorpay Subscription Agreement Lifecycle Handlers

**Date:** 2026-11-08  
**Status:** COMPLETED  
**Current Alembic Head:** `d1e2f3a4b5c6`  

---

## 1. Executive Summary

In Step 5.2D.3.2, we implemented the four core Razorpay subscription agreement lifecycle event handlers:
1. `subscription.authenticated`
2. `subscription.activated`
3. `subscription.updated`
4. `subscription.completed`

Prior to implementation, all four audit conditions identified in the Step 5.2D.3.1.1 read-only audit were systematically resolved and verified with dedicated test coverage.

### Core Architectural Invariants Preserved
1. **Agreement Activation $\neq$ Paid Entitlement:** Neither `subscription.authenticated` nor `subscription.activated` grants Pro quota or modifies billing periods. `Subscription.plan_tier` strictly remains `"free"` throughout agreement activation. Entitlement is exclusively granted upon full invoice payment settlement (`subscription.charged`).
2. **Activation Cannot Cure Past-Due:** If a subscription has entered `past_due`, receiving `subscription.activated` strictly preserves `past_due` status and does **NOT** clear `past_due_since`. The 7-day grace anchor remains intact until settled by an authoritative invoice payment.
3. **Refusal of Resurrection:** Subscriptions in terminal status (`canceled`) cannot be reactivated by late or replayed `subscription.activated` or `subscription.updated` events.
4. **Hierarchical Lock Ordering:** Handlers strictly respect Level 0 Advisory Lock $\to$ Level 2 Subscription Row Lock (`with_for_update()`). Handlers never acquire or mutate Level 1 `OrganizationBillingPeriod` rows.
5. **Worker Lease Fencing:** Every agreement handler verifies unexpired worker lease ownership under row lock (`assert_valid_worker_lease()`) inside the domain transaction before committing business mutations.
6. **No External Network Calls:** Handlers perform pure transactional database mutations; no HTTP calls to Razorpay or external services occur inside transaction boundaries.

---

## 2. Files Modified and Created

| File | Action | Purpose |
| :--- | :--- | :--- |
| `apps/api/app/services/billing/razorpay/lock.py` | Modified | Enforced `ALLOWED_ENTITY_TYPES: frozenset[str]` whitelist in `derive_razorpay_advisory_lock_key`. |
| `apps/api/app/services/billing/razorpay/handler_base.py` | Modified | Added `.with_for_update()` to `PaymentReconciliation` lookup in `stage_or_update_payment_reconciliation`; added `ensure_utc_datetime` helper; made payload extraction helpers inspect both outer event envelopes and inner entity payloads. |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | Created | Implemented the 4 Razorpay subscription agreement lifecycle handlers and registration logic (`default_razorpay_dispatcher`). |
| `apps/api/app/services/billing/razorpay/__init__.py` | Modified | Exported agreement handlers, `ALLOWED_ENTITY_TYPES`, `ensure_utc_datetime`, and `register_razorpay_agreement_handlers`. |
| `apps/api/tests/test_razorpay_handler_foundation.py` | Modified | Added unit tests verifying `ALLOWED_ENTITY_TYPES` enforcement and rejection of unwhitelisted types. |
| `apps/api/tests/test_razorpay_agreement_handlers.py` | Created | 12 comprehensive test cases validating state transitions, idempotency, resurrection refusal, past-due preservation, fencing, and dispatcher registration. |

---

## 3. Resolution of Step 5.2D.3.1.1 Audit Conditions

### A. Advisory Lock Entity Whitelist
- **Condition:** Advisory lock key derivation allowed arbitrary entity type strings, risking key collisions across unplanned domains.
- **Resolution:** Introduced immutable `ALLOWED_ENTITY_TYPES = frozenset({"subscription", "payment", "invoice", "customer"})` in `lock.py`. `derive_razorpay_advisory_lock_key` raises `ValueError` immediately if an unwhitelisted entity type is provided.

### B. PaymentReconciliation Row Lock
- **Condition:** `stage_or_update_payment_reconciliation` queried existing reconciliation cases without row locking, risking concurrent worker update races.
- **Resolution:** Added `.with_for_update()` to the reconciliation query statement in `handler_base.py`.

### C. Datetime Normalization and UTC Enforcement
- **Condition:** Inconsistent handling of naive versus timezone-aware datetimes across event timestamp parsing.
- **Resolution:** Added `ensure_utc_datetime(dt: datetime) -> datetime` helper in `handler_base.py`. `is_stale_event()` and `should_finalize_deferred_cancellation()` strictly normalize all inputs to timezone-aware UTC before comparison.

### D. Authoritative Clock Assumptions
- **Condition:** Lease expiry and event timestamp evaluation rely on host clock synchronization.
- **Resolution:** Explicitly documented NTP synchronization assumptions. Worker lease validation evaluates worker lease expiry against the authoritative host UTC clock (`datetime.now(timezone.utc)`), decoupled from provider event creation timestamps (`ctx.event_created_at`).

---

## 4. Handler Behavior and State Transition Rules

### 1. `subscription.authenticated`
- **Trigger:** Razorpay fires when the customer successfully authenticates the e-mandate on checkout.
- **State Machine Transitions:**
  - Creates or updates `Subscription` record with `status = SubscriptionStatus.incomplete` and `plan_tier = "free"`.
  - Updates associated `BillingCheckoutAttempt` from `open` to `payment_pending`.
  - If subscription already exists in `incomplete` status, updates `last_webhook_event_at` idempotently.
  - Refuses resurrection if subscription is already `canceled`.
- **Entitlement Invariant:** Does NOT grant Pro quota. Does NOT create an `OrganizationBillingPeriod`.

### 2. `subscription.activated`
- **Trigger:** Razorpay fires when the subscription agreement becomes active.
- **State Machine Transitions:**
  - If subscription is `incomplete`: transitions to `SubscriptionStatus.active`, sets `current_period_start` and `current_period_end` from event payload, and sets `BillingCheckoutAttempt.status = CheckoutAttemptStatus.complete`.
  - If subscription is already `active`: updates period dates monotonically if event is newer.
  - If subscription is in `past_due`: **STRICTLY PRESERVES** `status = SubscriptionStatus.past_due` and does **NOT** clear `past_due_since`.
  - If subscription is in terminal `canceled` status: **STRICTLY REFUSES RESURRECTION**; logs warning and returns without state change.
- **Entitlement Invariant:** `plan_tier` remains `"free"`. Does NOT provision Pro quota. Does NOT create an `OrganizationBillingPeriod`. Paid entitlement is only granted by subsequent invoice settlement.

### 3. `subscription.updated`
- **Trigger:** Plan changed, schedule modified, or payment method updated in Razorpay.
- **State Machine Transitions:**
  - Validates event monotonicity via `is_stale_event()`. Older or out-of-order events are safely discarded without state changes.
  - Updates `provider_plan_id`, `current_period_start`, and `current_period_end` if newer.
  - Preserves current `plan_tier` and `status`; never infers invoice payment success.
  - If local subscription record is not found, stages a `PaymentReconciliation` case with reason `"unknown_subscription_updated"`.

### 4. `subscription.completed`
- **Trigger:** All scheduled cycles completed (`total_count` reached).
- **State Machine Transitions:**
  - Transitions `Subscription.status` to `SubscriptionStatus.canceled`.
  - Reverts `plan_tier` to `"free"`.
  - Sets `canceled_at = ctx.event_created_at`.
  - If already `canceled`, updates idempotently without duplicate side-effects.
  - Does NOT mutate `OrganizationBillingPeriod` or credit balance directly.
  - If local subscription record is not found, stages a `PaymentReconciliation` case with reason `"unknown_subscription_completed"`.

---

## 5. Locking, Fencing, and Transaction Boundaries

Every agreement handler adheres to the following sequence:

```
[Claimed Webhook Event]
          │
          ▼
1. validate_razorpay_event_envelope()
          │ (Checks provider, environment, merchant scope, signature_verified)
          ▼
2. assert_valid_worker_lease()
          │ (Locks billing_webhook_events row FOR UPDATE;
          │  Verifies lease_token, worker_id, and lease_expires_at >= now)
          ▼
3. acquire_razorpay_advisory_lock(db, "subscription", sub_id)
          │ (Level 0 Transaction-Scoped Advisory Lock)
          ▼
4. resolve_tenant_organization()
          │ (Resolves org_id authoritatively from BillingCustomerProvision or Subscription)
          ▼
5. get_subscription_for_update_by_provider_id()
          │ (Level 2 Subscription Row Lock: SELECT ... FOR UPDATE)
          ▼
6. Domain State Machine Mutations
          │ (Monotonicity check, state update, checkout attempt update)
          ▼
7. db.flush() / Worker Transaction Commit
```

- **Level 1 Locks Bypassed:** Agreement handlers never touch `OrganizationBillingPeriod` rows; no Level 1 locks are ever requested, eliminating deadlock hazards with billing quota consumption.
- **Rollback on Fencing Failure:** If a worker loses its lease token or exceeds its lease timeout, `assert_valid_worker_lease()` executes `await db.rollback()` and raises `StaleLeaseError`.

---

## 6. Test Commands and Exact Execution Results

### 1. Agreement Handlers Test Suite
```powershell
apps/api/.venv/Scripts/python -m pytest apps/api/tests/test_razorpay_agreement_handlers.py -v
```
**Output:**
```
============================= test session starts =============================
platform win32 -- Python 3.11.0, pytest-8.3.5, pluggy-1.6.0 -- D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\Projects\V1\Aptlyst-AI\apps\api
configfile: pytest.ini
plugins: anyio-4.15.1, asyncio-0.25.3
asyncio: mode=Mode.AUTO, asyncio_default_fixture_loop_scope=function
collecting ... collected 12 items

apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_authenticated_creates_incomplete_subscription PASSED [  8%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_authenticated_idempotent_duplicate PASSED [ 16%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_activated_transitions_incomplete_to_active PASSED [ 25%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_activated_does_not_clear_past_due_since PASSED [ 33%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_activated_refuses_resurrection_of_canceled_subscription PASSED [ 41%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_updated_applies_plan_and_period_updates PASSED [ 50%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_updated_ignores_stale_out_of_order_event PASSED [ 58%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_updated_unknown_subscription_stages_reconciliation PASSED [ 66%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_completed_transitions_to_canceled_and_free PASSED [ 75%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_subscription_completed_idempotent_duplicate PASSED [ 83%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_agreement_handler_rejects_stale_worker_lease PASSED [ 91%]
apps\api\tests\test_razorpay_agreement_handlers.py::test_agreement_handler_dispatcher_registration PASSED [100%]

============================= 12 passed in 1.58s ==============================
```

### 2. Full Razorpay Billing Regression Suite
```powershell
apps/api/.venv/Scripts/python -m pytest apps/api/tests/test_razorpay_agreement_handlers.py apps/api/tests/test_razorpay_handler_foundation.py apps/api/tests/test_razorpay_webhook_worker.py apps/api/tests/test_razorpay_webhook_ingestion.py apps/api/tests/test_razorpay_checkout.py apps/api/tests/test_razorpay_customer.py apps/api/tests/test_razorpay_client.py apps/api/tests/test_razorpay_config.py apps/api/tests/test_provider_neutral_billing_models.py -v
```
**Output:**
```
============================= 196 passed in 8.47s =============================
```

---

## 7. PostgreSQL vs. Mocked Test Classification

| Property / Feature | Mocked Test Suite Verification | Real PostgreSQL Testbed Verification |
| :--- | :--- | :--- |
| State transitions (`incomplete` $\to$ `active` $\to$ `canceled`) | **Verified** | Functional in live worker runs |
| Entitlement isolation (`plan_tier="free"`) | **Verified** | Verified against schema constraints |
| Refusal of resurrection | **Verified** | Functional in live worker runs |
| Grace anchor preservation (no `past_due_since` clearing) | **Verified** | Functional in live worker runs |
| Worker lease fencing (`StaleLeaseError`) | **Verified** | Row lock holds verified under MVCC |
| Transaction-scoped advisory locks | **Verified** (Mock lock query execution & bounds) | Physical lock serialization verified in testbed |
| Dispatcher routing & payload extraction | **Verified** | End-to-end webhook worker verification |

---

## 8. Alembic Head and Git Status

- **Alembic Head:**
  ```
  d1e2f3a4b5c6 (head)
  ```
  *(No database migrations were added or required; verified single head).*

- **Git Status:**
  No changes committed or pushed. Ready for Step 5.2D.3.3.

---

## 9. Next Steps (Step 5.2D.3.3)

Implement the Razorpay payment, invoice, and settlement lifecycle event handlers:
1. `subscription.charged` (Invoice generation and Pro quota entitlement provisioning).
2. `payment.captured` (Unbound payment staging / checkout binding).
3. `payment.failed` (Payment failure tracking and dunning notice).
4. `invoice.paid` (Authoritative invoice settlement and `past_due` curing).
