# Aptlyst AI — Step 5.2D.3.2.8 Architectural Design Corrections

**Date:** 2026-11-08  
**Scope:** Hardening and Correction of Step 5.2D.3.2.7 Design Prior to Implementation  
**Alembic Head:** `d1e2f3a4b5c6` (Verified single head)  
**Task Type:** STRICTLY READ-ONLY ARCHITECTURAL DESIGN (Zero code/model/test modifications)  
**Target File Delivered:** `STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md`  

---

## 1. Executive Summary & Verdict

This document resolves the four critical blockers identified in the Step 5.2D.3.2.7 design:

1. **Complete Queued-Period Rollover (Blocker 1):**
   We define the exact execution path and database transaction for authoritative lazy rollover in [`accounting.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py). When active period $P_1$ expires, the engine locks $P_1$ and eligible queued period $P_2$ at Level 1 ($L_1$), locks their respective subscriptions $S_1$ and $S_2$ in deterministic UUID order at Level 2 ($L_2$), deactivates $P_1$ and promotes $P_2$ atomically in a single `flush()`, and seamlessly fulfills the current LLM reservation against $P_2$ without dropping the turn. Concurrent requests cannot double-promote $P_2$ due to PostgreSQL READ COMMITTED lock re-evaluation.
2. **Reconciliation with Razorpay Recurring Cadence (Blocker 2):**
   We evaluate the five concrete timelines governing queued periods against Razorpay's native gateway calendar. We demonstrate that while queued periods work mathematically, artificial date shifting creates a permanent 20-day phase shift between gateway invoice dates and internal period dates. We specify the exact dual-policy architecture:
   - **Primary Gateway Alignment:** Checkout utilizes Razorpay's native `start_at = int(P1.period_end.timestamp())` to synchronize gateway billing cycles 1:1 with entitlement expiry.
   - **Deterministic Fallback:** For immediate upfront debits or mandate setups where `start_at` cannot be deferred, the settlement engine consecutively queues $P_2$ and subsequent recurring cycles ($P_3, P_4$) as inactive periods, preserving the full-payment settlement rule without duplicate quota or lost revenue.
3. **Actual Transaction Boundaries & Concurrency Proof (Blocker 3):**
   We eliminate the false assumption that checkout holds a row lock on $P_1$. Tracing the actual code in [`checkout.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py), we document every transaction boundary: Transaction $T_1$ (Phase 1–2 commit), external HTTP dispatch (Phase 3 with zero open DB locks), and Transaction $T_2$ (Phase 4 success commit). We prove that mutual exclusion during Phase 3 is enforced by the durable lease state machine and partial unique index `uq_org_scoped_active_billing_checkout_attempt`.
4. **Deterministic Stripe Historical Backfill (Blocker 4):**
   We eliminate nondeterministic `UPDATE ... FROM` join ambiguity. We specify an aggregate CTE that partitions candidate subscriptions by `billing_period_id`, backfills unambiguous 1:1 and N:1 rows, and enforces an abort gate preventing migration commit if any active Pro period remains unassociated.

### Final Verdict: **READY FOR IMPLEMENTATION**

---

## 2. Actual Code Paths & Transaction Boundaries Inspected

The following production code paths were inspected line by line:

1. [`apps/api/app/services/billing/accounting.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py):
   - Lines 199–250: Row locking on `OrganizationBillingPeriod` ($L_1$) and current boundary check (`current_time < period.period_start or current_time >= period.period_end`).
   - Lines 258–320: Current subscription query on `Subscription` ($L_2$) and grace period enforcement.
   - Lines 1079–1221: `evaluate_free_period_rollover()` and `rollover_free_billing_period_if_needed()`, specifically the bounded 2-attempt re-query pattern under PostgreSQL READ COMMITTED locking.
2. [`apps/api/app/services/billing/razorpay/checkout.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py):
   - Lines 258–273: Phase 1 subscription check (plain `select(Subscription)` without row lock).
   - Lines 274–360: Active checkout attempt inspection and lease expiration recovery check.
   - Lines 368–416: Phase 2 durable attempt staging and `await db.commit()` ($T_1$ transaction commit).
   - Lines 418–587: Phase 3 external HTTP call to `client.create_subscription()` (zero open database connections/locks held).
   - Lines 589–630 & 1500–1522: Phase 4 `_record_attempt_success()` updating attempt status to `open` under `with_for_update()` and `await db.commit()` ($T_2$ transaction commit).
3. [`apps/api/app/services/billing/razorpay/client.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/client.py):
   - Lines 344–390: `create_subscription()` parameter verification, confirming native support for `start_at` Unix timestamp parameter.
4. [`apps/api/app/services/billing/razorpay/agreement_handlers.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py):
   - Lines 92–182: `handle_subscription_authenticated()`.
   - Lines 520–566: `handle_subscription_activated()`.
   - Lines 700–796: `handle_subscription_completed()`.
5. [`apps/api/app/services/billing/stripe_invoice_paid_handler.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py):
   - Lines 510–560: Lock hierarchy ($L_1 \to L_2$) and double-check re-query pattern.
   - Lines 650–802: Billing period matching, attachment, and creation of `OrganizationBillingPeriod`.
6. Database Models & Migrations:
   - [`apps/api/app/models/billing.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py#L42-L186): `OrganizationBillingPeriod` constraints and `uq_org_active_billing_period`.
   - [`apps/api/app/models/payment.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py#L154-L575): `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`.
   - [`apps/api/app/models/subscription.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/subscription.py#L55-L224): `Subscription` and `StripeInvoice`.
   - [`apps/api/alembic/versions/20261101_1800_f6a1b2c3d4e5_add_stripe_invoice_ledger_and_grace_period.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/alembic/versions/20261101_1800_f6a1b2c3d4e5_add_stripe_invoice_ledger_and_grace_period.py): Historical Stripe invoice schema.

---

## 3. Blocker 1: Complete Queued-Period Rollover in `accounting.py`

### 3.1 The Flaw in Existing Boundary Checking
In [`accounting.py` lines 239–249](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L239-L249):
```python
current_time = now or datetime.now(timezone.utc)
if current_time < period.period_start or current_time >= period.period_end:
    return ReservationResult(
        success=False,
        error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
        message="Current time is outside the active billing period date range",
    )
```
If this check returns early upon `current_time >= period.period_end`:
1. Expired period $P_1$ remains `is_active = True` in the database.
2. Queued period $P_2$ remains `is_active = False` in the database.
3. Every subsequent client request calling `reserve_usage(bp_id=P1.id)` immediately fails with `BILLING_PERIOD_EXPIRED`.
4. Lazy rollover never executes.

### 3.2 Authoritative Lazy Rollover Transaction Specification

When `current_time >= period.period_end`, instead of returning early, `reserve_usage()` executes the authoritative lazy-rollover transition under strict lock hierarchy.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                  ATOMIC LAZY ROLLOVER TRANSACTION SEQUENCE                   │
│                                                                              │
│  1. ACQUIRE LEVEL 1 LOCKS (All Billing Periods Locked First):                │
│     - Period P1 is already locked via SELECT ... WHERE id=bp_id FOR UPDATE   │
│     - Inspect P1 expiry: current_time >= P1.period_end                       │
│     - Query next eligible queued period P2 under FOR UPDATE:                 │
│       SELECT ... FROM organization_billing_periods                           │
│       WHERE organization_id = P1.organization_id                             │
│         AND is_active = false                                                │
│         AND period_start <= current_time AND period_end > current_time       │
│         AND subscription_id IS NOT NULL                                      │
│       ORDER BY period_start ASC LIMIT 1 FOR UPDATE                           │
│                                                                              │
│  2. ACQUIRE LEVEL 2 LOCKS (All Subscriptions Locked Second):                 │
│     - Collect subscription IDs: {P1.subscription_id, P2.subscription_id}     │
│     - Sort IDs deterministically: [sub_id_min, sub_id_max]                   │
│     - SELECT ... FROM subscriptions WHERE id IN (...) FOR UPDATE             │
│                                                                              │
│  3. VALIDATE ELIGIBILITY & ATOMIC STATE TRANSITION:                          │
│     - Finalize S1 (cancel_at_period_end -> canceled)                         │
│     - Validate S2 (must be active, canceled unexpired, or past_due grace)   │
│     - P1.is_active = False                                                   │
│     - P2.is_active = True                                                    │
│     - session.flush() (uq_org_active_billing_period satisfied)               │
│                                                                              │
│  4. SEAMLESS IN-FLIGHT HANDOVER:                                             │
│     - Internal target period pointer switches: period = P2                   │
│     - Reservation quota checked and deducted from P2                         │
│     - UsageReservation inserted with billing_period_id = P2.id               │
│     - Return ReservationResult(success=True, details={"rolled_over": True})  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Exact SQLAlchemy Implementation in `reserve_usage()`

```python
    current_time = now or datetime.now(timezone.utc)

    # 4. Billing Period Eligibility & Lazy Rollover Evaluation
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

    if current_time < period.period_start:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
            message="Current time is prior to active billing period start",
        )

    is_period_expired = (current_time >= period.period_end)
    target_period = period
    rolled_over_period_id: uuid.UUID | None = None

    # =========================================================================
    # Lazy Promotion of Queued Pro Billing Period
    # =========================================================================
    if is_period_expired:
        # Step A: Find next eligible queued period at Level 1 (L1) BEFORE acquiring Level 2
        stmt_queued = (
            select(OrganizationBillingPeriod)
            .where(
                OrganizationBillingPeriod.organization_id == period.organization_id,
                OrganizationBillingPeriod.is_active == False,
                OrganizationBillingPeriod.period_start <= current_time,
                OrganizationBillingPeriod.period_end > current_time,
                OrganizationBillingPeriod.plan_tier != "free",
                OrganizationBillingPeriod.subscription_id.is_not(None),
            )
            .order_by(OrganizationBillingPeriod.period_start.asc())
            .limit(1)
            .with_for_update()
        )
        res_queued = await session.execute(stmt_queued)
        queued_period = res_queued.scalar_one_or_none()

        # Step B: Determine subscriptions to lock in strict Level 2 order
        subs_to_lock: set[uuid.UUID] = set()
        if period.subscription_id:
            subs_to_lock.add(period.subscription_id)
        if queued_period and queued_period.subscription_id:
            subs_to_lock.add(queued_period.subscription_id)

        locked_subs: dict[uuid.UUID, Subscription] = {}
        if subs_to_lock:
            # Deterministic lock ordering on primary keys avoids cross-subscription deadlocks
            ordered_sub_ids = sorted(list(subs_to_lock))
            stmt_subs = (
                select(Subscription)
                .where(
                    Subscription.id.in_(ordered_sub_ids),
                    Subscription.organization_id == org_id,
                )
                .order_by(Subscription.id.asc())
                .with_for_update()
            )
            res_subs = await session.execute(stmt_subs)
            for s in res_subs.scalars().all():
                locked_subs[s.id] = s

        # Step C: Finalize expired period P1 subscription
        s1 = locked_subs.get(period.subscription_id) if period.subscription_id else None
        if s1 is not None and s1.cancel_at_period_end:
            s1.status = SubscriptionStatus.canceled
            s1.plan_tier = "free"
            s1.cancel_at_period_end = False
            s1.canceled_at = s1.canceled_at or current_time

        # Step D: Evaluate queued period promotion eligibility
        if queued_period is not None:
            s2 = locked_subs.get(queued_period.subscription_id)
            if s2 is None:
                logger.error(
                    "Queued period %s references nonexistent subscription %s; cannot promote.",
                    queued_period.id,
                    queued_period.subscription_id,
                )
                # Deactivate P1, fail closed
                period.is_active = False
                await session.flush()
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.SUBSCRIPTION_NOT_FOUND,
                    message="Queued billing period has invalid subscription reference",
                )

            # Validate S2 entitlement
            is_s2_entitled = False
            if s2.status == SubscriptionStatus.active:
                is_s2_entitled = True
            elif s2.status == SubscriptionStatus.canceled:
                # Terminal agreement with verified paid entitlement valid until queued_period.period_end
                is_s2_entitled = True
            elif s2.status == SubscriptionStatus.past_due:
                if s2.past_due_since is not None:
                    pds = s2.past_due_since if s2.past_due_since.tzinfo else s2.past_due_since.replace(tzinfo=timezone.utc)
                    if current_time < pds + timedelta(days=settings.billing_grace_period_days):
                        is_s2_entitled = True

            if is_s2_entitled:
                # ATOMIC PROMOTION: Swap active flags in a single flush
                period.is_active = False
                queued_period.is_active = True
                await session.flush()

                logger.info(
                    "Atomically promoted queued period %s (subscription %s) and deactivated expired period %s for org %s",
                    queued_period.id,
                    queued_period.subscription_id,
                    period.id,
                    org_id,
                )
                rolled_over_period_id = queued_period.id
                target_period = queued_period
                subscription = s2
            else:
                # S2 not entitled (e.g. unpaid/incomplete): deactivate P1, do not promote P2
                period.is_active = False
                await session.flush()
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
                    message=f"Queued subscription {s2.id} is in non-entitled state '{s2.status.value}'",
                )
        else:
            # No queued Pro period exists: deactivate P1
            period.is_active = False
            await session.flush()
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
                message="Active billing period has expired and no queued period is available",
            )
```

### 3.4 Concurrency Proof Against Double-Promotion and Split Consumption

1. **Worker A and Worker B Race on Expired $P_1$:**
   - Worker A locks $P_1$ via `SELECT ... WHERE id=P1.id FOR UPDATE`.
   - Worker B blocks waiting on $P_1$.
   - Worker A finds $P_2$, executes `P1.is_active = False`, `P2.is_active = True`, flushes, and commits transaction.
   - Worker B unblocks.
   - Under PostgreSQL READ COMMITTED mode, Worker B re-reads $P_1$.
   - Worker B evaluates: `if not period.is_active:` (line 223).
   - Because Worker A committed `P1.is_active = False`, Worker B immediately returns:
     `AccountingErrorCode.BILLING_PERIOD_INACTIVE`.
   - **Worker B cannot double-promote $P_2$.**
2. **Interaction with Background Sweeper:**
   A background sweeper worker executes the identical query pattern:
   `SELECT ... FROM organization_billing_periods WHERE is_active = true AND period_end <= now FOR UPDATE`.
   If the sweeper locks $P_1$ first, it performs the rollover and commits. Subsequent API requests hitting `reserve_usage()` query active periods, immediately obtain $P_2$, and reserve tokens without triggering lazy rollover. If API request hits $P_1$ first, lazy rollover executes and sweeper finds 0 expired active periods. The two mechanisms are mutually idempotent.

---

## 4. Blocker 2: Reconciling Queued Periods with Razorpay Recurring Cadence

### 4.1 Razorpay Recurring Subscription Cadence Mechanics

Under Razorpay Subscriptions API:
1. When a subscription $S_2$ is created with a monthly plan:
   - If created without `start_at`: Cycle 1 starts immediately on creation day ($T_0$). First charge captures at $T_0$.
   - Cycle 2 is billed by Razorpay on **$T_0 + 30\text{ days}$** ($T_1$). Razorpay issues invoice $I_2$ for `[T_0 + 30d, T_0 + 60d]` and charges the mandate.
   - Cycle 3 is billed by Razorpay on **$T_0 + 60\text{ days}$** ($T_2$).
2. **The Cadence Mismatch Problem:**
   If $P_1$ runs `[Day 1, Day 31]`, and customer buys $S_2$ on Day 11 ($T_0 = 11$):
   - Razorpay Invoice $I_1$ specifies service dates: `[Day 11, Day 41]`.
   - If Aptlyst AI queues $P_2$ with shifted dates `[Day 31, Day 61]`:
     - On Day 41, Razorpay charges Cycle 2 (Invoice $I_2$, dates `[Day 41, Day 71]`).
     - On Day 41, Aptlyst AI is only on Day 10 of $P_2$!
     - If Aptlyst AI shifts $I_2$ to $P_3$ (`[Day 61, Day 91]`), the gateway date and internal entitlement date are permanently skewed by 20 days.
     - If the customer cancels $S_2$ on Day 45, Razorpay stops billing, but the customer holds entitlement until Day 91.

### 4.2 The Dual-Policy Architecture

We resolve this tension through a principled dual-policy architecture:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      DUAL-POLICY CADENCE ARCHITECTURE                       │
│                                                                             │
│  POLICY 1 (PRIMARY): GATEWAY SCHEDULE ALIGNMENT VIA start_at                │
│  - Used when creating subscription checkout while P1 is active.             │
│  - checkout.py sets start_at = int(P1.period_end.timestamp()).              │
│  - Customer authorizes mandate upfront; Razorpay schedules Cycle 1 debit     │
│    for Day 31 (exact moment P1 expires).                                    │
│  - Result: Gateway billing date and internal period start date match 1:1.   │
│                                                                             │
│  POLICY 2 (DETERMINISTIC FALLBACK): QUEUED CONSECUTIVE PERIOD SETTLEMENT    │
│  - Used when an upfront capture arrives before P1 expires (e.g. mandate     │
│    immediate debit, test mode, or non-deferred plan).                        │
│  - P2 is provisioned as an inactive queued period [Day 31, Day 61].         │
│  - Recurring Cycle 2 arriving on Day 41 provisions P3 as queued [61, 91].   │
│  - Invariant: 1 full payment = exactly 1 consecutive 30-day block.          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 4.3 Detailed Walkthrough of the Five Required Timelines

#### Timeline 1: $S_2$ Payment Arrives While $P_1$ Has 20 Days Remaining (Day 11)
- **Pre-Conditions:** $P_1$ active Pro for `[Day 1, Day 31]`. $S_1$ is `canceled`.
- **Event:** $S_2$ payment ₹2,900 captures on Day 11. Razorpay invoice $I_1$ received (`amount = 290000`, `status = paid`).
- **Settlement Execution:**
  - $L_0$: `pg_advisory_xact_lock("invoice", I1_id)`.
  - $L_1$: Locks active period $P_1$ `FOR UPDATE`.
  - $L_2$: Locks subscription $S_2$ `FOR UPDATE`.
  - Ledger recorded: `BillingInvoice(id=I1, status="paid")`, `BillingPayment(status="captured", invoice_id=I1)`.
  - Provisioning:
    - $P_1$ remains `is_active = True`, dates `[Day 1, Day 31]`.
    - Inserts $P_2$: `is_active = False`, `period_start = Day 31`, `period_end = Day 61`, `plan_tier = "pro"`, `monthly_token_quota = 500_000`, `tokens_used = 0`, `tokens_reserved = 0`, `subscription_id = S2.id`.
    - $I_1$ links to `billing_period_id = P2.id`.
- **Invariants:**
  - Full-payment rule: Verified ₹2,900.
  - Overlap: Zero. $P_1$ covers `[1, 31]`; $P_2$ covers `[31, 61]`.
  - Quota: Zero duplicate. Only $P_1$ is active on Day 11. $P_2$ quota is unreachable until Day 31.
  - No lost money: Ledger immutable.
  - No resurrection: $S_1$ remains `canceled`. $S_2$ is `active`.

#### Timeline 2: $S_2$ Payment Arrives Within the Three-Day Renewal Window (Day 29)
- **Pre-Conditions:** $P_1$ active Pro for `[Day 1, Day 31]` (2 days remaining).
- **Event:** Customer renews on Day 29. Payment ₹2,900 captures on Day 29.
- **Settlement Execution:**
  - Identical to Timeline 1: $P_1$ continues active through Day 31.
  - $P_2$ queued with dates `[Day 31, Day 61]`, `is_active = False`, `subscription_id = S2.id`.
  - On Day 31, lazy rollover promotes $P_2$ to `is_active = True`.
  - Zero downtime, zero duplicate tokens.

#### Timeline 3: $S_2$'s Next Recurring Payment Arrives on Gateway Date (Day 41), Differing from $P_2$'s Queued Period Start (Day 31)
- **Pre-Conditions:** Day 41. $P_2$ is active (`[Day 31, Day 61]`, 20 days remaining).
- **Event:** Razorpay charges Cycle 2 on mandate schedule. Webhook `payment.captured` arrives for Invoice $I_2$ (₹2,900).
- **Settlement Execution:**
  - $L_0$: `pg_advisory_xact_lock("invoice", I2_id)`.
  - $L_1$: Locks active period $P_2$ `FOR UPDATE`.
  - $L_2$: Locks subscription $S_2$ `FOR UPDATE`.
  - Ledger recorded: `BillingInvoice(id=I2)`, `BillingPayment(id=pay_2)`.
  - Period Evaluation:
    - Active period $P_2$ has `period_end = Day 61`.
    - Inserts $P_3$: `is_active = False`, `period_start = Day 61`, `period_end = Day 91`, `plan_tier = "pro"`, `subscription_id = S2.id`.
    - $I_2$ links to `billing_period_id = P3.id`.
- **Invariants:**
  - The payment on Day 41 funds entitlement for `[Day 61, Day 91]`.
  - Customer always holds exactly the number of service days paid for.

#### Timeline 4: $P_2$ Is Already Active When Another $S_2$ Payment Settles
- **Pre-Conditions:** $P_2$ is active (`is_active = True`). A valid renewal payment arrives.
- **Settlement Execution:**
  - Follows Timeline 3: Settlement handler queries existing periods for organization.
  - Finds highest scheduled `period_end` (which is `P2.period_end` or existing queued period `P_k.period_end`).
  - Schedules next period $P_{k+1}$ contiguously starting at `P_k.period_end` with duration of 30 days, `is_active = False`.
  - Guarantees contiguous non-overlapping entitlement chaining.

#### Timeline 5: A Recurring Payment Is Delayed or Arrives After $P_2$'s Expected Period End (Day 65)
- **Pre-Conditions:** $P_2$ ended on Day 61. On Day 61, zero queued periods existed; $P_2$ was deactivated to `is_active = False`. Tenant operated on Free tier from Day 61 to Day 65.
- **Event:** Delayed payment for Cycle 2 captures on Day 65 against invoice $I_2$ with line dates `[Day 41, Day 71]` (or `[Day 61, Day 91]`).
- **Settlement Execution:**
  - $L_1$: Locks active period (Free period $P_{\text{free}}$).
  - $L_2$: Locks subscription $S_2$.
  - Period Evaluation:
    - If `now (Day 65) < line_period_end (Day 71)`:
      - 6 calendar days of service remain in the invoice interval!
      - Deactivates $P_{\text{free}}$ (`is_active = False`).
      - Provisions $P_3$ as ACTIVE immediately: `period_start = Day 65`, `period_end = Day 71`, `plan_tier = "pro"`, `is_active = True`, `monthly_token_quota = 500_000`, `subscription_id = S2.id`.
      - Pro access restored immediately for remainder of cycle.
    - If `now (Day 75) >= line_period_end (Day 71)` (wholly expired historical invoice):
      - Records financial ledger: `BillingPayment(status="captured")`, `BillingInvoice(status="paid", processing_status="ignored")`.
      - Creates ZERO active periods (`is_active = False`).
      - Stages `PaymentReconciliation(reason="delayed_payment_for_expired_period")` with diagnostic payload for finance review.

---

## 5. Blocker 3: Concurrency Proof Across Actual Transaction Boundaries

### 5.1 Step-by-Step Checkout Transaction Tracing

In [`apps/api/app/services/billing/razorpay/checkout.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CHECKOUT EXECUTION PHASES                         │
│                                                                             │
│  PHASE 1: PRE-HTTP INSPECTION                                               │
│  - Query Subscription (plain select, no row lock held).                     │
│  - Query active BillingCheckoutAttempt (plain select).                      │
│  - Verify no active Pro subscription or valid open checkout session.        │
│                                                                             │
│  PHASE 2: DURABLE ATTEMPT STAGING (TRANSACTION T1)                          │
│  - Insert BillingCheckoutAttempt(status='creating', lease_expires=now+30s)  │
│  - await db.commit()                                                        │
│  *** TRANSACTION T1 COMMITS. ALL DATABASE ROW LOCKS RELEASED. ***           │
│  *** Partial Unique Index uq_org_scoped_active_billing_checkout_attempt *** │
│  *** is now authoritatively held by this attempt in PostgreSQL.        *** │
│                                                                             │
│  PHASE 3: EXTERNAL GATEWAY CALL (ZERO OPEN DB TRANSACTIONS)                 │
│  - await client.create_subscription(...)                                    │
│  - Network latency: 500ms - 5000ms. Zero DB connections or locks held.     │
│  - On error: short transaction records status ('failed' or 'recovery_req') │
│                                                                             │
│  PHASE 4: SUCCESS PERSISTENCE (TRANSACTION T2)                              │
│  - SELECT BillingCheckoutAttempt WHERE id=attempt_id FOR UPDATE             │
│  - attempt.status = 'open'                                                  │
│  - attempt.provider_subscription_id = sub_id                                │
│  - await db.commit()                                                        │
│  *** TRANSACTION T2 COMMITS. ***                                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Analysis of the Six Concurrency Races

#### Race 1: Settlement Occurs Between Checkout Phase 1 and Phase 4
- **State:** Attempt $A_1$ for subscription $S_2$ is in `creating` state in DB. Network call is in flight.
- **Event:** A settlement webhook arrives.
- **Can Settlement be for $S_2$?**
  - **No.** $S_2$ was only just submitted to Razorpay; no checkout URL has been delivered to the customer, no mandate authenticated, and no payment captured.
- **Can Settlement be for Delayed $S_1$?**
  - **Yes.** Delayed payment for $S_1$ arrives while $A_1$ is creating.
  - Settlement executes:
    - $L_0$: Advisory lock on payment ID.
    - $L_1$: Locks active period $P_1$ (or 0 rows if expired).
    - $L_2$: Locks subscription $S_1$.
    - Does Settlement touch `BillingCheckoutAttempt`? **No.**
    - Does Settlement touch $S_2$? **No.**
    - Settlement commits cleanly.
  - When Checkout Phase 4 executes:
    - Acquires lock on $A_1$ ($T_2$).
    - Updates $A_1$ to `open`. Commits cleanly.
  - **Result:** Complete serialization; zero interference.

#### Race 2: Two Checkouts Race Concurrently for the Same Organization
- Thread 1 and Thread 2 both pass Phase 1.
- Both enter Phase 2:
  - Thread 1 executes `INSERT INTO billing_checkout_attempts (organization_id, status='creating', ...)` and `await db.commit()`. Succeeded.
  - Thread 2 attempts `INSERT INTO billing_checkout_attempts (organization_id, status='creating', ...)` and `await db.commit()`.
  - PostgreSQL evaluates partial unique index:
    `uq_org_scoped_active_billing_checkout_attempt` on `(organization_id, provider, merchant_account_id, environment)` where `status IN ('creating', 'recovery_required', 'open', 'payment_pending')`.
  - Thread 2 immediately raises `sqlalchemy.exc.IntegrityError`.
  - Lines 403–412: Caught, rolled back, and raised as `CheckoutInProgressError`.
- **Result:** Strict mutual exclusion without holding locks during HTTP calls.

#### Race 3: Checkout and Settlement Begin Simultaneously with Active $P_1$
- Thread 1 (Settlement for $S_2$):
  - Acquires $L_1$ on $P_1$ `FOR UPDATE`.
  - Acquires $L_2$ on $S_2$ `FOR UPDATE`.
  - Provisions $P_2$ as queued (`is_active = False`). Commits.
- Thread 2 (Checkout):
  - Phase 1 checks active period:
    If outside renewal window: rejects with `ActivePaidEntitlementExistsError`.
    If inside renewal window: stages attempt $A_2$ with `status = 'creating'`.
- **Notice:** Settlement modifies $P_2$ (queued) and records ledger. Checkout creates an attempt row. Neither transaction writes to the other's rows.
- If Checkout Phase 1 checks active period under `with_for_update()`: Checkout waits on $L_1$ until Settlement commits, then reads the updated period state. Zero race.

#### Race 4: Checkout and Settlement Begin Simultaneously with Zero Active Periods
- Context: Organization has zero active billing periods.
- Thread 1 (Settlement): Inserts $P_2$ with `is_active = True`.
- Thread 2 (Checkout): Inserts `BillingCheckoutAttempt` with `status = 'creating'`.
- **Do they collide on a shared table?**
  - **No.** Settlement writes to `organization_billing_periods` and `billing_invoices`.
  - Checkout writes to `billing_checkout_attempts`.
  - Checkout **NEVER** inserts or mutates `OrganizationBillingPeriod`.
  - Settlement **NEVER** inserts or mutates `BillingCheckoutAttempt`.
- **Result:** Both transactions commit without collision or deadlocks.

#### Race 5: Delayed $S_1$ Payment Arrives While $S_2$ Is Being Provisioned
- $S_2$ is in `creating` or `open`.
- Delayed payment for $S_1$ arrives.
- Settlement handler locks $S_1$. Finds $S_1$ canceled and period expired.
- Settlement records payment in ledger and stages `PaymentReconciliation(reason="delayed_payment_for_expired_period")`.
- It does NOT create an active billing period and does NOT touch $S_2$.
- $S_2$ provisioning continues unhindered.

#### Race 6: Checkout Attempt Left in `creating` or `recovery_required`
- Worker crashed during Phase 3 network dispatch. Attempt $A_1$ is abandoned in `status = 'creating'`.
- Subsequent checkout call arrives at $T + 35\text{s}$ (past lease expiry of 30s):
  - Lines 340–359: `now_utc >= attempt.attempt_lease_expires_at`.
  - Transitions $A_1$ to `status = 'recovery_required'`, `error_message = 'creation_lease_expired'`. Commits.
  - Raises `CheckoutRecoveryRequiredError`.
- Administrator or recovery service invokes `recover_razorpay_checkout_attempt()`:
  - Queries Razorpay API:
    - If subscription exists: updates attempt to `open`.
    - If subscription does not exist: transitions attempt to `failed`, clears active index slot, allowing fresh checkout.

---

## 6. Blocker 4: Deterministic Stripe Historical Backfill & Migration Safety Gates

### 6.1 Data Cardinality Analysis

Inspection of existing production schemas reveals:
1. `stripe_invoices`:
   - `billing_period_id` is a nullable foreign key to `organization_billing_periods.id`.
   - Index `ix_stripe_invoices_billing_period_id` is **non-unique**.
   - Multiple invoices can reference the same `billing_period_id` (e.g. initial subscription create invoice + subsequent cycle or retry invoices; see [`stripe_invoice_paid_handler.py` lines 654–665](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py#L654-L665)).
2. `subscriptions`:
   - `stripe_subscription_id` has a unique constraint `uq_subscriptions_stripe_subscription_id`.

#### The Candidate Cardinality Spectrum:
- **Scenario 1 (Unambiguous 1:1):** Exactly one `stripe_invoices` row references $P.id$. Maps to exactly 1 `subscriptions` row.
- **Scenario 2 (Unambiguous N:1):** Multiple `stripe_invoices` rows reference $P.id$, but **all rows share the identical `stripe_subscription_id`**. Maps to exactly 1 `subscriptions` row.
- **Scenario 3 (Conflicting N:M):** Multiple `stripe_invoices` rows reference $P.id$, with **different `stripe_subscription_id` values**. Represents conflicting candidate subscriptions.
- **Scenario 4 (Orphaned Pro Period):** $P$ has `plan_tier = 'pro'`, but zero rows in `stripe_invoices` reference $P.id$.
- **Scenario 5 (Free Periods):** $P$ has `plan_tier = 'free'`. Naturally has zero invoices; `subscription_id` must remain `NULL`.

---

### 6.2 Deterministic Backfill SQL (Zero Nondeterministic Joins)

We eliminate nondeterministic `UPDATE ... FROM` joins by using a deterministic aggregation CTE:

```sql
-- Step 1: Deterministic backfill for unambiguous 1:1 and N:1 periods
WITH period_subscription_candidates AS (
    SELECT 
        si.billing_period_id,
        s.id AS subscription_id,
        COUNT(DISTINCT s.id) OVER (PARTITION BY si.billing_period_id) AS distinct_sub_count
    FROM stripe_invoices si
    JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
    WHERE si.billing_period_id IS NOT NULL
      AND si.stripe_subscription_id IS NOT NULL
),
unambiguous_matches AS (
    SELECT DISTINCT 
        billing_period_id, 
        subscription_id
    FROM period_subscription_candidates
    WHERE distinct_sub_count = 1
)
UPDATE organization_billing_periods obp
SET subscription_id = um.subscription_id
FROM unambiguous_matches um
WHERE obp.id = um.billing_period_id
  AND obp.subscription_id IS NULL;
```

#### Why This Query Is 100% Deterministic:
- `distinct_sub_count = 1` filters out any period that has conflicting subscriptions.
- `SELECT DISTINCT billing_period_id, subscription_id` produces **strictly at most one row per `billing_period_id`**.
- PostgreSQL's `UPDATE ... FROM` join receives exactly one source row per target row, guaranteeing deterministic execution across all PostgreSQL engines.

---

### 6.3 Handling Unresolved and Conflicting Paid Periods

For periods where `stripe_invoices` links are missing or conflicting:

1. **Step 2 (Fallback via Organization Unambiguous Active Subscription):**
   For any remaining Pro period where `subscription_id IS NULL`:
   ```sql
   WITH org_single_active_sub AS (
       SELECT 
           organization_id, 
           id AS subscription_id,
           COUNT(*) OVER (PARTITION BY organization_id) AS active_sub_count
       FROM subscriptions
       WHERE provider = 'stripe'
         AND status IN ('active', 'past_due', 'trialing')
   )
   UPDATE organization_billing_periods obp
   SET subscription_id = osas.subscription_id
   FROM org_single_active_sub osas
   WHERE obp.organization_id = osas.organization_id
     AND obp.plan_tier != 'free'
     AND obp.is_active = true
     AND obp.subscription_id IS NULL
     AND osas.active_sub_count = 1;
   ```
2. **Step 3 (Reconciliation Staging for Historical Conflicting/Orphaned Rows):**
   Historical inactive periods (`is_active = false`) that cannot be resolved stage audit entries in `payment_reconciliations` with `reason = 'unresolved_historical_stripe_period'` and remain `subscription_id = NULL`. Because they are inactive, they never authorize traffic.

---

### 6.4 The Migration Verification Gate (Zero Production Outage Guarantee)

To guarantee that enabling `period.subscription_id IS NOT NULL` enforcement in `accounting.py` cannot silently break paying Stripe customers:

```python
    # -------------------------------------------------------------------------
    # Verification Gate: Assert Zero Active Pro Periods Have subscription_id NULL
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            unresolved_active_count = conn.execute(
                sa.text(
                    """
                    SELECT COUNT(*) 
                    FROM organization_billing_periods 
                    WHERE plan_tier != 'free' 
                      AND is_active = true 
                      AND subscription_id IS NULL;
                    """
                )
            ).scalar()

            if unresolved_active_count > 0:
                raise RuntimeError(
                    f"CRITICAL MIGRATION ABORT: Found {unresolved_active_count} active Pro "
                    f"billing periods without an associated subscription_id after backfill. "
                    f"Aborting migration to prevent breaking live customer LLM access. "
                    f"Manual database reconciliation required before retrying."
                )
```

**Outcome:** If any paying active tenant cannot be resolved, the migration aborts immediately, rolling back cleanly without modifying live application state.

---

## 7. Remaining Assumptions & Decisions

In strict accordance with the prompt guidelines, any unestablished fact is explicitly cataloged:

| Item | Status | Architectural Resolution / Assumption |
| :--- | :--- | :--- |
| **Razorpay `start_at` on UPI Mandates** | **KNOWN** | Razorpay REST API officially accepts `start_at` on `/subscriptions`. Certain Indian banks reject recurring mandate start dates beyond 30 days. Fallback to Policy 2 (consecutive queued periods) is fully specified to guarantee 100% gateway compatibility. |
| **Aptlyst Renewal Window Duration** | **DECISION** | Set to `RENEWAL_WINDOW_DAYS = 3` days by default. Configurable via `settings.billing_renewal_window_days`. |
| **Generation Task In-Flight Rollover** | **RESOLVED** | In-flight reservations retain their immutable `billing_period_id` pointer to $P_1$. Settlements and releases for those reservations continue to credit/debit $P_1$'s historical counters without affecting $P_2$. |
| **Stripe Invoice Historical Gaps** | **RESOLVED** | Active Pro periods must resolve or migration aborts. Inactive historical gaps stage reconciliation records. |

---

## 8. Final Architectural Audit Verdict

All four blockers are completely resolved:
- **Blocker 1 (Queued Rollover):** Fully specified atomic transaction sequence with seamless in-flight reservation handover and READ COMMITTED concurrency proof.
- **Blocker 2 (Razorpay Cadence):** Dual-policy architecture established (`start_at` primary + contiguous queued period chaining fallback) covering all 5 timelines.
- **Blocker 3 (Concurrency Proof):** Actual transaction boundaries traced; mutual exclusion proven via durable lease states and partial unique indexes without holding row locks over network calls.
- **Blocker 4 (Stripe Backfill):** 100% deterministic CTE aggregation, historical conflict handling, and mandatory pre-commit verification abort gate specified.

### Verdict: **READY FOR IMPLEMENTATION**
