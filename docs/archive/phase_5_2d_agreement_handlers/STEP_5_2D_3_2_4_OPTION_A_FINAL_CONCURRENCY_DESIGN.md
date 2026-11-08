# Aptlyst AI — Step 5.2D.3.2.4 Final Concurrency & Lifecycle Design: Option A (Lazy Accounting & Checkout Expiry)

**Date:** 2026-11-08  
**Scope:** Resolution of Accounting Expiry Sequencing, Period-End Authority Drift, and Zero-Billing-Period Concurrency  
**Alembic Head:** `d1e2f3a4b5c6` (Verified single head)  
**Task Type:** Strictly Design, Concurrency, and Correctness Validation (Zero code/model/test modifications)  
**Target File Delivered:** `STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md`  

---

## 1. Executive Summary & Verdict

This document delivers the final, hardened architectural design for **Option A: Lazy Accounting & Checkout Expiry**, resolving the three critical design challenges identified in the Step 5.2D.3.2.3 audit:

1. **Issue 1 (Accounting Expiry Sequencing):**
   The premature `BILLING_PERIOD_EXPIRED` return in [`accounting.py` line 240](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L240) bypassed subscription locking, preventing lazy finalization of completed subscriptions. We restructure Step 4 into a **Unified Period & Entitlement Evaluation** that strictly preserves the global lock hierarchy ($L_1 \to L_2 \to L_3$), finalizes completed subscriptions upon expiration, and denies new usage without invalidating in-flight reservations or mutating quota counters.

2. **Issue 2 (Period-End Authority & Timestamp Drift):**
   The arbitrary `max()` timestamp rule proposed in Step 5.2D.3.2.3 is explicitly revoked. We establish that **`OrganizationBillingPeriod.period_end` is the sole, non-negotiable authority for paid quota consumption**. Gateway schedule projections (`Subscription.current_period_end`) can **never** extend paid token access beyond settled billing periods. Exhaustive policies are established for earlier provider dates, later provider dates, missing timestamps, and reconciliation diagnostics.

3. **Issue 3 (Concurrency When No Active Billing Period Exists):**
   When an organization has zero active billing periods, the system serializes simultaneous operations without inventing artificial lock tiers:
   - **Reservations:** Fail closed immediately at the orchestrator layer (`NO_ACTIVE_BILLING_PERIOD`).
   - **Checkout & Lazy Expiry:** Serialize on the Level 2 `Subscription` row lock (`FOR UPDATE`) and database partial unique constraint `uq_active_checkout_attempt`.
   - **Full-Payment Settlement:** Serializes on Level 0 transaction advisory locks (`pg_advisory_xact_lock`), Level 2 `Subscription` row lock, and database partial unique constraint `uq_org_active_billing_period`.
   - The lock graph remains a strict Directed Acyclic Graph (DAG) with guaranteed deadlock freedom.

### Final Verdict: **READY FOR IMPLEMENTATION**

---

## 2. Issue 1 Resolution: Accounting Expiry Transaction Sequence

### 2.1 The Architectural Sequencing Defect in Prior Proposal
In [`apps/api/app/services/billing/accounting.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py):
- Lines 200–206 lock `OrganizationBillingPeriod` under `SELECT ... FOR UPDATE` ($L_1$).
- Lines 238–249 currently evaluate:
  ```python
  current_time = now or datetime.now(timezone.utc)
  if current_time < period.period_start or current_time >= period.period_end:
      return ReservationResult(
          success=False,
          error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
          message="Current time is outside the active billing period date range",
      )
  ```
- Lines 258–275 subsequently lock `Subscription` ($L_2$).

**The Defect:** If a generation request arrives at or after `period.period_end`, line 240 returns immediately. `Subscription` is never queried or locked ($L_2$ never acquired). Consequently, a completed subscription (`cancel_at_period_end = True`) is **never lazily finalized to `canceled`** during an expired reservation attempt! The subscription remains `'active'` indefinitely until a subsequent checkout or manual intervention occurs.

### 2.2 The Unified Step 4 Architecture in `reserve_usage()`

To guarantee lazy finalization while strictly preserving global lock order ($L_1 \to L_2$), we restructure Step 4 in [`accounting.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L214-L362) as follows:

```mermaid
sequenceDiagram
    autonumber
    participant App as Orchestrator / Task
    participant DB_BP as OrganizationBillingPeriod (L1)
    participant DB_Sub as Subscription (L2)

    App->>DB_BP: 1. SELECT ... FOR UPDATE (Lock L1)
    Note over DB_BP: Validate period ownership, is_active, is_blocked

    alt Plan Tier is Free
        Note over DB_BP: Free periods do not require Subscription lock
        alt now >= period.period_end
            App-->>DB_BP: Return BILLING_PERIOD_EXPIRED
        else now < period.period_end
            Note over DB_BP: Proceed to Quota & Headroom Checks (Step 5-7)
        end
    else Plan Tier is Paid (Pro)
        App->>DB_Sub: 2. SELECT ... FOR UPDATE (Lock L2)
        Note over DB_Sub: Both L1 and L2 held in strict hierarchy L1 -> L2

        alt now >= period.period_end (Period Expired)
            alt sub.cancel_at_period_end is True
                Note over DB_Sub,DB_BP: Finalize Agreement & Period
                App->>DB_Sub: sub.status = canceled, sub.plan_tier = free, sub.cancel_at_period_end = false
                App->>DB_BP: period.is_active = false
                App->>DB_Sub: session.flush()
                App-->>DB_Sub: Return SUBSCRIPTION_INACTIVE (finalized=True)
            else sub.cancel_at_period_end is False (Auto-Renewing, Pending Ingestion)
                Note over DB_BP: Do NOT cancel subscription; awaiting renewal webhook
                App-->>DB_BP: Return BILLING_PERIOD_EXPIRED
            end
        else now < period.period_end (Period Active)
            alt sub.cancel_at_period_end is True
                Note over DB_Sub: Active paid time remains! Authorize usage!
                Note over DB_BP: Proceed to Quota & Headroom Checks (Step 5-7)
            else sub.status == past_due
                Note over DB_Sub: Evaluate 7-day grace period
            else sub.status == active
                Note over DB_BP: Proceed to Quota & Headroom Checks (Step 5-7)
            end
        end
    end
```

### 2.3 Exact Code-Level Implementation Blueprint for `reserve_usage()`

```python
    # 4. Verify Billing Period Eligibility & Entitlement Policy
    if period.organization_id != org_id:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Billing period does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "period_org_id": str(period.organization_id)},
        )

    if not period.is_active:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_INACTIVE,
            message="Billing period is not active",
        )

    if period.is_blocked:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_BLOCKED,
            message=f"Billing period is blocked: {period.blocked_reason or 'No reason provided'}",
            details={"blocked_reason": period.blocked_reason},
        )

    current_time = now or datetime.now(timezone.utc)
    if current_time < period.period_start:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
            message="Current time is prior to the active billing period start date",
            details={"period_start": period.period_start.isoformat(), "now": current_time.isoformat()},
        )

    is_paid_period = bool(period.plan_tier and period.plan_tier.strip().lower() != "free")
    is_period_expired = (current_time >= period.period_end)

    if not is_paid_period:
        # Free billing periods proceed without Subscription locking
        if is_period_expired:
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
                message="Current time is outside the active billing period date range",
                details={
                    "period_start": period.period_start.isoformat(),
                    "period_end": period.period_end.isoformat(),
                    "now": current_time.isoformat(),
                },
            )
    else:
        # Paid Pro billing periods: Lock Subscription SECOND in strict hierarchy (L1 -> L2)
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

        if len(eligible_subs) > 1:
            logger.warning("Organization %s has multiple (%d) eligible subscriptions. Failing closed.", org_id, len(eligible_subs))
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.RECONCILIATION_REQUIRED,
                message=f"Multiple eligible subscriptions ({len(eligible_subs)}) found for organization '{org_id}'; reconciliation required",
                details={"eligible_subscription_count": len(eligible_subs), "subscription_ids": [str(s.id) for s in eligible_subs]},
            )

        if len(eligible_subs) == 0:
            # Query non-entitled row for diagnostic reporting
            stmt_any = select(Subscription).where(Subscription.organization_id == org_id).order_by(Subscription.created_at.desc()).limit(1)
            latest_sub = (await session.execute(stmt_any)).scalar_one_or_none()
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE if latest_sub else AccountingErrorCode.SUBSCRIPTION_NOT_FOUND,
                message="Subscription is not in an entitled state for Pro access" if latest_sub else "Pro billing period requires an active subscription record, but none was found",
                details={"subscription_status": latest_sub.status.value} if latest_sub else {},
            )

        subscription = eligible_subs[0]

        # Branch A: Period has chronologically expired (now >= period.period_end)
        if is_period_expired:
            if subscription.cancel_at_period_end:
                # Atomically finalize completed subscription and deactivate expired period
                subscription.status = SubscriptionStatus.canceled
                subscription.plan_tier = "free"
                subscription.cancel_at_period_end = False
                subscription.canceled_at = subscription.canceled_at or current_time
                period.is_active = False
                await session.flush()
                logger.info("Lazily finalized completed subscription %s for org %s to canceled (period expired at %s)", subscription.id, org_id, period.period_end)
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
                    message="Subscription agreement completed its final period and is now canceled",
                    details={"subscription_status": SubscriptionStatus.canceled.value, "period_end": period.period_end.isoformat()},
                )

            # Auto-renewing subscription awaiting renewal invoice/webhook: deny usage without canceling subscription
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
                message="Current time is outside the active billing period date range",
                details={"period_start": period.period_start.isoformat(), "period_end": period.period_end.isoformat(), "now": current_time.isoformat()},
            )

        # Branch B: Period is still active (now < period.period_end)
        # cancel_at_period_end=True permits access until period_end!
        if subscription.status == SubscriptionStatus.past_due:
            past_due_since = subscription.past_due_since
            if past_due_since is None:
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                    message="Subscription is past_due but past_due_since anchor is missing; failing closed",
                )
            if past_due_since.tzinfo is None:
                past_due_since = past_due_since.replace(tzinfo=timezone.utc)
            grace_deadline = past_due_since + timedelta(days=settings.billing_grace_period_days)
            if current_time >= grace_deadline:
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                    message="Subscription past-due grace period has expired; Pro access denied",
                )
```

### 2.4 Invariant Guarantees of the Unified Sequence
1. **Global Lock Hierarchy Preserved:** `OrganizationBillingPeriod` ($L_1$) is locked first; `Subscription` ($L_2$) is locked second. Zero lock-order inversions.
2. **Denies Usage At or After Period End:** If `now >= period.period_end`, the function **unconditionally returns an error** (`SUBSCRIPTION_INACTIVE` or `BILLING_PERIOD_EXPIRED`). Zero tokens can be reserved outside the paid window.
3. **In-Flight Reservation Immunity:** [`settle_usage()`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L508) locks `OrganizationBillingPeriod` ($L_1$) and `UsageReservation` ($L_3$); it does not inspect `Subscription`. An in-flight turn reserved 1 second before expiry settles normally after expiry without counter corruption.
4. **Zero Quota/Spending Mutation:** Neither `monthly_token_quota` nor `tokens_used` is modified.

---

## 3. Issue 2 Resolution: Period-End Authority and Timestamp Drift

### 3.1 Revocation of the `max()` Rule
Step 5.2D.3.2.3 suggested using `cutoff = max(period.period_end, sub.current_period_end)`.
**This rule is explicitly rejected.**
- *Reasoning:* In a consumption-metered LLM platform, token quota is provisioned in exchange for verified monetary settlement. `OrganizationBillingPeriod.period_end` reflects the exact temporal span purchased by the customer.
- If a payment gateway emits a projected cycle end 3 days past the settled period, granting 3 extra days of LLM token inference based on a gateway projection constitutes unauthorized, unbilled quota inflation.
- Conversely, if a gateway cycle ends earlier due to billing retry schedule alignment, cutting off access prematurely breaches the service agreement established by the settled invoice.

### 3.2 Authoritative Expiry Cutoff Definition
$$\text{Authoritative Expiry Cutoff } \equiv \mathbf{OrganizationBillingPeriod.period\_end}$$

The internal verified billing period is the **sole source of truth for entitlement duration**. Gateway subscription dates govern external subscription billing mechanics, but internal accounting strictly enforces `OrganizationBillingPeriod.period_end`.

### 3.3 Exhaustive Timestamp Drift Policy

| Scenario | Condition | System Action | Entitlement Effect | Reconciliation Staged? |
| :--- | :--- | :--- | :--- | :---: |
| **Aligned Timestamps** | `abs(period.period_end - sub.current_period_end) <= 60s` | Standard lifecycle. When `now >= period.period_end`, lazy expiry finalizes both. | Entitled until `period.period_end`. | No |
| **Provider Earlier** | `sub.current_period_end < period.period_end` | Gateway marked cycle complete earlier than internal period end. **Honor `period.period_end`**. Do **not** demote subscription to canceled while `now < period.period_end`. Demote only when `now >= period.period_end`. | Retains paid access through verified `period.period_end`. | If drift $> 24\text{h}$: **YES** (`reason="provider_period_earlier_than_billing_period"`) |
| **Provider Later** | `sub.current_period_end > period.period_end` | Gateway schedule extends beyond settled period. **Strictly enforce `period.period_end`**. Deny token usage once `now >= period.period_end`. Lazy expiry demotes subscription and deactivates period. | Cut off strictly at `period.period_end`. Zero free unbilled access. | If drift $> 24\text{h}$: **YES** (`reason="provider_period_ahead_of_billing_period"`) |
| **Missing Provider End** | `sub.current_period_end is None` | Valid `period.period_end` exists. Use `period.period_end` as authority. Backfill `sub.current_period_end` from `period.period_end` upon settlement. | Entitled until `period.period_end`. | If `cancel_at_period_end=True`: **YES** (`reason="missing_provider_period_end"`) |
| **Corrupted Missing Internal End** | `period.period_end is None` | Schema check: `period_end` is `NOT NULL` in PostgreSQL. If ever null in memory, **fail closed immediately** (`INVALID_INPUT`). | Denied immediately. | **YES** (`reason="corrupted_billing_period_null_end"`) |

### 3.4 Requirement for Separately Settled Billing Periods
**Guiding Principle:** Gateway schedule changes, courtesy extensions, or manual administrative grants **cannot** synthesize entitlement through `Subscription.current_period_end` alone.
- If a customer is granted additional service days (promotional credit, customer support SLA concession, or tier change proration), this **must be recorded via an authoritative settlement event** (e.g., a \$0 concession invoice or payment ledger entry in Step 5.2D.3.3).
- The settlement handler creates or updates `OrganizationBillingPeriod`.
- Schedule projections on `Subscription` alone **never** expand or create an internal billing period.

---

## 4. Issue 3 Resolution: Concurrency When No Active Billing Period Exists

### 4.1 The Concurrency Dilemma
When an organization has **zero active billing periods** (e.g., a Free org before initial provisioning, or an org whose Pro period expired and was deactivated):
- `OrganizationBillingPeriod.is_active == True` matches 0 rows.
- A query executing `SELECT ... WHERE is_active = true FOR UPDATE` acquires **0 row locks**.
- How do simultaneous operations serialize without deadlocks, double checkouts, or race conditions?

### 4.2 Matrix of Simultaneous Operations

```
                                      Simultaneous Operations
                                                 │
          ┌──────────────────────┬───────────────┴───────────────┬──────────────────────┐
          ▼                      ▼                               ▼                      ▼
  Accounting Reservation   Razorpay Checkout           Full-Payment Settlement      Lazy Expiry
  (orchestrator.py)       (checkout.py)               (Step 5.2D.3.3 Handler)     (checkout.py)
```

#### A. Accounting Reservation Serialization
- In [`apps/api/app/services/llm/orchestrator.py` lines 185–196](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/llm/orchestrator.py#L185-L196):
  ```python
  stmt_bp = select(OrganizationBillingPeriod).where(
      OrganizationBillingPeriod.organization_id == chatbot.organization_id,
      OrganizationBillingPeriod.is_active == True,
  )
  billing_period = (await db.execute(stmt_bp)).scalar_one_or_none()
  if billing_period is None:
      return OrchestrationResult(status=GenerationStatus.failed, error_code=OrchestratorErrorCode.NO_ACTIVE_BILLING_PERIOD)
  ```
- **Fails Closed Pre-Reservation:** If no active billing period exists, the orchestrator aborts before invoking `reserve_usage()`.
- **Direct Invocation Defense:** If `reserve_usage()` is called with an invalid or inactive period ID, lines 207–228 return `BILLING_PERIOD_NOT_FOUND` or `BILLING_PERIOD_INACTIVE` without querying or locking `Subscription`.
- **Outcome:** Accounting reservations perform **zero database mutations** and **acquire zero locks** when no active billing period exists.

#### B. Checkout & Lazy Expiry Serialization
When `create_razorpay_subscription_checkout()` executes:
1. It queries active `OrganizationBillingPeriod` under `FOR UPDATE`. When none exists, 0 row locks are acquired.
2. It queries active `Subscription` (`WHERE organization_id == org_id AND status IN ('active', 'trialing')`) under `FOR UPDATE` ($L_2$).
   - If an expired Pro subscription exists with `cancel_at_period_end = True`:
     - The `Subscription` row **does exist** in the database.
     - The row lock on `Subscription` **is successfully acquired**.
     - Checkout detects `is_active_bp is None` (or expired). It lazily finalizes:
       ```python
       existing_sub.status = SubscriptionStatus.canceled
       existing_sub.plan_tier = "free"
       existing_sub.cancel_at_period_end = False
       await db.flush()
       ```
     - This immediately vacates the partial unique constraint `uq_org_active_subscription`!
3. What if no `Subscription` row exists in `'active'` status?
   - The query acquires 0 row locks.
   - How are two concurrent checkout attempts for the same organization serialized?
   - **Database Anchor:** PostgreSQL partial unique index **`uq_active_checkout_attempt`**:
     ```sql
     CREATE UNIQUE INDEX uq_active_checkout_attempt ON billing_checkout_attempts
     (organization_id, provider, merchant_account_id, environment)
     WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending');
     ```
   - Checkout inserts a row in `status = 'creating'` within its transaction.
   - If Worker A and Worker B concurrently attempt checkout:
     - Worker A inserts `creating` and succeeds.
     - Worker B attempts to insert `creating` and immediately blocks/fails on `uq_active_checkout_attempt` with an `IntegrityError`.
     - Worker B catches the conflict, rolls back its sub-transaction, and either attaches to Worker A's open attempt or returns `CheckoutInProgressError`.

#### C. Full-Payment Settlement Serialization
When a payment webhook (`invoice.paid` / `payment.captured`) arrives when no active period exists:
1. **Advisory Lock ($L_0$):** Acquires `pg_advisory_xact_lock("invoice", invoice_id)` or `("payment", payment_id)`, serializing duplicate deliveries of the same gateway event.
2. **Subscription Lock ($L_2$):** Locks `Subscription` `FOR UPDATE` by `(organization_id, provider_subscription_id)`.
3. **In-Lock Period Re-Query:** Re-queries active `OrganizationBillingPeriod`. When confirmed None, it provisions a new active period:
   ```python
   new_period = OrganizationBillingPeriod(organization_id=org_id, is_active=True, period_start=..., period_end=...)
   db.add(new_period)
   ```
4. **Database Anchor Against Concurrent Settlements:**
   - Partial unique index **`uq_org_active_billing_period`**:
     ```sql
     CREATE UNIQUE INDEX uq_org_active_billing_period ON organization_billing_periods
     (organization_id) WHERE is_active = true;
     ```
   - If two different payment webhooks arrive simultaneously, both serialize on the `Subscription` row lock ($L_2$).
   - If they somehow targeted different subscriptions, PostgreSQL enforces `uq_org_active_billing_period` at the storage engine level, preventing more than one active period from ever being committed.

### 4.3 Rigorous Race-Condition Walkthrough

#### Scenario: Checkout (Worker A) Races with Settlement (Worker B) when No Active Billing Period Exists

```
Timeline:
──────────────────────────────────────────────────────────────────────────────────────────
Time   Worker A (Checkout)                     Worker B (Payment Settlement)
──────────────────────────────────────────────────────────────────────────────────────────
T0     Lock active period (0 rows locked)     Acquires pg_advisory_xact_lock(invoice_id)
T1     Lock Subscription S1 (FOR UPDATE)      Attempts to lock Subscription S1 (FOR UPDATE)
       -> Lock GRANTED to Worker A            -> BLOCKS waiting for Worker A
T2     Worker A inspects S1:
       - S1.cancel_at_period_end == True
       - No active period exists
       - Demotes S1.status = canceled
       - Vacates uq_org_active_subscription
T3     Worker A commits transaction
       -> S1 is now 'canceled'
       -> Releases lock on S1
T4                                            Worker B UNBLOCKS with fresh lock on S1
T5                                            Worker B inspects S1 under lock:
                                              - S1.status == 'canceled'!
                                              - Enforces REFUSAL OF RESURRECTION!
                                              - Quarantines invoice / records as ignored.
                                              - Zero billing periods created.
                                              - Worker B commits safely.
──────────────────────────────────────────────────────────────────────────────────────────
```

**Evaluation:**
- Deadlock cannot occur: Worker B waits on Worker A; Worker A never requests any lock held by Worker B.
- Resurrection refusal is guaranteed: Worker B re-evaluates `S1.status` *under lock*, catching the transition to `canceled` and refusing to revive it.

---

## 5. Summary of System-Wide Lock Hierarchy

Every operation across Aptlyst AI adheres to this strict, acyclic 4-tier hierarchy:

$$\text{Advisory Lock } (L_0) \prec \text{Billing Period } (L_1) \prec \text{Subscription } (L_2) \prec \text{UsageReservation / Attempt / Invoice } (L_3)$$

```
Level 0: PostgreSQL Transaction Advisory Lock
         └── pg_advisory_xact_lock("subscription"|"invoice"|"payment", entity_id)
Level 1: OrganizationBillingPeriod Row Lock
         └── SELECT ... FROM organization_billing_periods WHERE ... FOR UPDATE
Level 2: Subscription Row Lock
         └── SELECT ... FROM subscriptions WHERE organization_id = ... FOR UPDATE
Level 3: Auxiliary Entity Row Locks / Unique Constraints
         ├── UsageReservation (FOR UPDATE on settlement)
         ├── BillingCheckoutAttempt (uq_active_checkout_attempt)
         └── BillingInvoice / StripeInvoice (FOR UPDATE on ledger recording)
```

### Concurrency Rules
1. Any operation locking both $L_1$ and $L_2$ **must acquire $L_1$ first and $L_2$ second**.
2. Any operation locking only $L_2$ (e.g. agreement handlers) must **never acquire $L_1$**.
3. If $L_1$ contains 0 rows (no active period), the transaction proceeds to $L_2$ without altering the acquisition sequence.
4. Mathematical deadlock freedom is guaranteed by the strict total ordering of resources.

---

## 6. Required Test Specifications

### 6.1 Mocked Unit Test Suite (`test_razorpay_option_a_final.py`)
1. `test_accounting_unified_step4_finalizes_completed_subscription_on_period_expiry`:
   - Set up `period` with `period_end` in the past (`now > period_end`) and `sub` with `status=active`, `cancel_at_period_end=True`.
   - Call `reserve_usage()`.
   - Verify: returns `SUBSCRIPTION_INACTIVE`, `sub.status == canceled`, `sub.plan_tier == "free"`, `period.is_active == False`.
2. `test_accounting_unified_step4_does_not_cancel_auto_renewing_subscription_on_period_expiry`:
   - Set up `period` with `period_end` in the past, but `sub.cancel_at_period_end=False`.
   - Call `reserve_usage()`.
   - Verify: returns `BILLING_PERIOD_EXPIRED`, `sub.status` remains `active`.
3. `test_in_flight_reservation_settles_cleanly_after_lazy_expiry`:
   - Create active reservation in active period.
   - Advance clock past period end and trigger lazy finalization via `reserve_usage()`.
   - Call `settle_usage()` on the in-flight reservation.
   - Verify: settlement succeeds (`SettlementResult.success is True`), usage log recorded, counters decremented cleanly.
4. `test_provider_period_earlier_does_not_prematurely_revoke_access`:
   - Set `sub.current_period_end = Day 28`, `period.period_end = Day 30`, `sub.cancel_at_period_end = True`.
   - Call `reserve_usage()` on Day 29 (`Day 28 < now < Day 30`).
   - Verify: reservation succeeds! Access is **not** revoked on Day 28.
5. `test_provider_period_later_does_not_grant_unbilled_tokens`:
   - Set `sub.current_period_end = Day 35`, `period.period_end = Day 30`.
   - Call `reserve_usage()` on Day 31 (`Day 30 < now < Day 35`).
   - Verify: returns `BILLING_PERIOD_EXPIRED` (or `SUBSCRIPTION_INACTIVE`). Zero tokens granted on Day 31.
6. `test_checkout_serializes_on_subscription_lock_when_no_active_period_exists`:
   - Create expired subscription `sub.cancel_at_period_end=True` with no active period.
   - Call `create_razorpay_subscription_checkout()`.
   - Verify: lazily cancels `sub`, vacates constraint, and creates new attempt in `open` state.

### 6.2 Real PostgreSQL Integration Test Specifications (Staging Environment)
*Must be executed against a live PostgreSQL 15+ instance to test real physical locking and index semantics.*
1. `test_postgresql_concurrent_accounting_and_checkout_race`:
   - Launch 20 concurrent asyncio tasks competing on the exact millisecond of period expiry for an organization.
   - 10 tasks execute `reserve_usage()`; 10 tasks execute `create_razorpay_subscription_checkout()`.
   - Assert: Zero deadlock exceptions (`40P01`), zero integrity violations, exactly one new checkout attempt succeeds, and subscription is cleanly demoted to `canceled`.
2. `test_postgresql_uq_active_checkout_attempt_physical_exclusion`:
   - Attempt parallel insert of two `BillingCheckoutAttempt` rows in `status = 'creating'` for the same tenant.
   - Verify second insert throws `UniqueViolation` (SQLSTATE `23505`).

---

## 7. Remaining Unresolved Risks & Mitigations

1. **Passive Nature of Expiry on Dormant Accounts:**
   - *Risk:* If an organization whose subscription completed makes no inference calls and never visits checkout, its row remains `status = active` in `subscriptions`.
   - *Mitigation:* Harmless to quota and billing. Any subsequent interaction immediately finalizes it. For administrative analytics, subscriber metrics queries should filter:
     `WHERE status = 'active' AND (cancel_at_period_end = false OR current_period_end > now())`.
2. **Clock Synchronization:**
   - *Risk:* NTP clock drift between distributed application worker nodes.
   - *Mitigation:* Ensure all servers utilize AWS Time Sync / Google NTP or evaluate timestamps via database server `func.now()`.

---

## 8. Final Audit Verdict

The three design issues have been exhaustively addressed:
- The unified Step 4 sequencing in `accounting.py` guarantees lazy finalization while strictly enforcing global lock hierarchy ($L_1 \to L_2$).
- `OrganizationBillingPeriod.period_end` is established as the sole authority for paid entitlement, eliminating unauthorized token inflation.
- Concurrency with zero active billing periods is fully secured by Level 0 advisory locks, Level 2 Subscription row locks, and PostgreSQL partial unique constraints.

### Verdict: **READY FOR IMPLEMENTATION**
