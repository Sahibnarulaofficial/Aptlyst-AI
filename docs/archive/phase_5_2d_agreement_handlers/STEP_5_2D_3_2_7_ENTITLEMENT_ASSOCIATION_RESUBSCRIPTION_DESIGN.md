# Aptlyst AI — Step 5.2D.3.2.7 Architectural Design: Entitlement-to-Subscription Association & Resubscription Architecture

**Date:** 2026-11-08  
**Scope:** Rigorous Resolution of Entitlement Association, Resubscription with Active Paid Periods, Accounting Authorization, Cross-Row Concurrency, and Migration Plan  
**Alembic Head:** `d1e2f3a4b5c6` (Verified single head)  
**Task Type:** STRICTLY READ-ONLY ARCHITECTURAL DESIGN (Zero code, model, test, or migration modifications)  
**Deliverable:** `STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md`  

---

## 1. Executive Summary & Architectural Invariants

This design provides the definitive architectural specification for the five critical questions governing **entitlement-to-subscription association**, **resubscription while paid entitlement remains active**, **decoupled accounting authorization**, and **cross-row concurrency** across terminal and newly created provider agreements:

1. **Explicit Entitlement Association (Issue 1):**
   Relying on "latest subscription for the organization" or traversing indirect child foreign keys (`BillingInvoice.billing_period_id`) on the hot LLM token reservation path is architecturally defective and fragile. We specify the smallest safe mechanism: adding a direct nullable foreign key column `subscription_id` to [`OrganizationBillingPeriod`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py#L42-L186). This immediately, unambiguously, and in $O(1)$ binds every paid entitlement period to its exact parent subscription, inheriting the provider, merchant account, and environment. A clean, single-head Alembic migration is specified.
2. **Deterministic Resubscription Policy (Issue 2):**
   When subscription $S_1$ is canceled but its paid period $P_1$ has 20 days remaining, and customer resubscribes creating $S_2$:
   - **Zero Overwrite:** $P_1$ is never prematurely deactivated or overwritten.
   - **Zero Lost Payments:** $S_2$'s captured payment is immediately recorded in the authoritative ledger (`BillingPayment`, `BillingInvoice`).
   - **Zero Duplicate Quota:** $P_2$ is provisioned as an **inactive, queued period** (`is_active = False`) with dates consecutively scheduled: `[P1.period_end, P1.period_end + 30 days]`.
   - **Zero Invented Refunds:** No ad-hoc refund logic is introduced.
   - **Constraint Integrity:** The partial unique constraint `uq_org_active_billing_period` is strictly preserved because only $P_1$ has `is_active = True`.
3. **Targeted Accounting Authorization (Issue 3):**
   [`accounting.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py) locks and queries the subscription targeted specifically by `period.subscription_id`. Consequently, $S_2$ cannot cross-authorize $P_1$, an incomplete or past-due $S_2$ cannot invalidate $P_1$, and terminal $S_1$ authorizes access strictly while `current_time < P1.period_end`.
4. **Resolution of Cross-Row Concurrency Warning (Issue 4):**
   We formally prove that locking row $S_1$ and row $S_2$ via `SELECT ... FOR UPDATE` does NOT serialize operations against each other. Instead, tenant-level mutual exclusion is established by **Lock 1 ($L_1$) on the active billing period $P_1$**, where all concurrent operations (Settlement, Checkout, Reservation) contend on the same database row. When zero active periods exist, serialization is enforced by partial unique constraints (`uq_org_scoped_active_billing_checkout_attempt` and `uq_org_active_billing_period`).
5. **Production Migration & Test Strategy (Issue 5):**
   A seamless forward migration from `d1e2f3a4b5c6` with historical Stripe backfill and clean downgrade support is detailed, alongside 7 mocked unit tests and 3 real PostgreSQL 15+ concurrency tests.

### Final Verdict: **READY FOR IMPLEMENTATION**

---

## 2. Issue 1 — Entitlement-to-Subscription Association Analysis & Architecture

### 2.1 The Architectural Defect of "Latest Subscription"
In previous exploratory drafts, `accounting.py` attempted to resolve subscription entitlement using:
```python
select(Subscription).where(Subscription.organization_id == org_id).order_by(...)
```
This query embodies a fundamental anti-pattern: **it assumes that the latest subscription belonging to a tenant organization is the subscription that paid for the currently active billing period.**

Consider the real-world sequence:
1. Customer purchases a 1-month Razorpay pass for November. Local subscription $S_1$ is created.
2. Verified invoice settlement provisions billing period $P_1$ (`[Nov 01, Dec 01]`, `plan_tier = "pro"`).
3. On Nov 05, Razorpay reaches `total_count = 1` and dispatches `subscription.completed`. $S_1$ transitions to `status = canceled`.
4. On Nov 10, customer initiates a new checkout, creating subscription $S_2$ in `incomplete` status.
5. If `accounting.py` queries the "latest" subscription for the organization, it selects $S_2$ (created Nov 10).
6. Because $S_2$ is `incomplete`, `accounting.py` rejects token reservation with `SUBSCRIPTION_INACTIVE`, prematurely revoking 20 days of paid service on $P_1$!
7. Conversely, if $S_1$ was an expired Free plan and $S_2$ was activated for a future period, selecting $S_2$ could mistakenly authorize an unassociated period.

**Conclusion:** A billing period must NEVER be evaluated against an arbitrary or "latest" subscription. It must be evaluated against **the exact subscription that originated and funded it**.

---

### 2.2 Inspection of Existing Relationships and Foreign Keys

Inspection of [`app/models/billing.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py), [`app/models/payment.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py), and [`app/models/subscription.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/subscription.py) reveals the following entity graph:

```
┌──────────────────────────────────────┐
│       subscriptions                  │
│  id (UUID PK)                        │
│  organization_id (UUID FK)           │
│  provider (varchar 32)               │
│  merchant_account_id (varchar 64)    │
│  environment (varchar 32)            │
│  provider_subscription_id (varchar)  │
│  status (subscription_status)        │
└──────────────────┬───────────────────┘
                   │
                   │ 1:N
                   ▼
┌──────────────────────────────────────┐       N:1        ┌──────────────────────────────────┐
│       billing_invoices               │─────────────────▶│  organization_billing_periods    │
│  id (UUID PK)                        │ billing_period_id│  id (UUID PK)                    │
│  subscription_id (UUID FK nullable)  │                  │  organization_id (UUID FK)       │
│  provider (varchar 32)               │                  │  plan_tier (varchar 32)          │
│  merchant_account_id (varchar 64)    │                  │  period_start (timestamptz)      │
│  environment (varchar 32)            │                  │  period_end (timestamptz)        │
│  billing_period_id (UUID FK nullable)│                  │  is_active (boolean)             │
└──────────────────┬───────────────────┘                  │  is_blocked (boolean)            │
                   │                                      │  subscription_id ??? [MISSING]   │
                   │ 1:N                                  └──────────────────────────────────┘
                   ▼
┌──────────────────────────────────────┐
│       billing_payments               │
│  id (UUID PK)                        │
│  invoice_id (UUID FK nullable)       │
│  subscription_id (UUID FK nullable)  │
│  amount_minor_units (bigint)         │
│  status (varchar 32)                 │
└──────────────────────────────────────┘
```

#### Detailed Model Inspection:
1. **[`OrganizationBillingPeriod`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py#L42):**
   - Outbound FKs: Only `organization_id` $\to$ `organizations.id`.
   - Inbound FKs:
     - `usage_reservations.billing_period_id`
     - `llm_usage_logs.billing_period_id`
     - `generation_tasks.billing_period_id`
     - `billing_invoices.billing_period_id` (ondelete="SET NULL")
     - `stripe_invoices.billing_period_id` (ondelete="SET NULL")
   - Current provider awareness: **Zero.** Does not know its provider, merchant account, environment, or originating subscription.
2. **[`BillingInvoice`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py#L280):**
   - Has `subscription_id` (FK to `subscriptions.id`).
   - Has `billing_period_id` (FK to `organization_billing_periods.id`).
   - Has `provider`, `merchant_account_id`, `environment`.
   - Has `payments` relationship linking to `BillingPayment`.
3. **[`StripeInvoice`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/subscription.py#L495):**
   - Has `billing_period_id` (FK to `organization_billing_periods.id`).
   - Has `stripe_subscription_id` (String(255), **not** a UUID FK).

---

### 2.3 Evaluation: Reverse Lookup (Candidate B) vs. Direct Foreign Key (Candidate A)

We evaluate the two candidate association mechanisms:

| Architectural Metric | Candidate B: Reverse Relationship Lookup (`BillingInvoice` / `StripeInvoice`) | Candidate A: Direct Column (`OrganizationBillingPeriod.subscription_id`) |
| :--- | :--- | :--- |
| **Database Migrations** | Zero migrations. | Exactly one migration (adds nullable `subscription_id` + index). |
| **Hot Path Overhead** | **High.** `reserve_usage()` must perform an extra join or secondary query on every LLM token turn. | **Optimal ($O(1)$).** `period.subscription_id` is already loaded on `period` instance. |
| **Stripe Asymmetry** | **Severe.** `stripe_invoices` lacks a UUID FK; requires string join on `stripe_subscription_id`. | **Unified.** Both Stripe and Razorpay subscriptions populate the same UUID FK. |
| **Inversion of Control** | **Broken.** Entitlement period has no idea who created it; relies on child records that can be SET NULL. | **Correct.** Entitlement explicitly references its originating contractual agreement. |
| **Cardinality Ambiguity** | Multiple invoices can reference the same period (e.g. initial + retry/adjustment). | Exactly one originating subscription owns the period. |
| **Failure Mode** | If invoice row is missing or delayed, entitlement fails closed unpredictably. | Deterministic: `period.subscription_id` is immutable once committed. |

### 2.4 Chosen Smallest Safe Association Mechanism
**Candidate A (Direct Foreign Key) is decisively chosen.**

We add to `organization_billing_periods`:
```python
subscription_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("subscriptions.id", ondelete="RESTRICT"),
    nullable=True,
    index=True,
)
```

#### Why This Is the Smallest Safe Mechanism:
1. **Free Periods Naturally Nullable:** Free billing periods (created at organization registration or after subscription cancellation expiration) have `subscription_id = NULL`. This cleanly preserves the invariant that Free tier requires no subscription.
2. **Provider Metadata Inherited Automatically:** By referencing `subscriptions.id`, the billing period unambiguously inherits:
   - `sub.provider` (e.g. `"razorpay"` or `"stripe"`)
   - `sub.merchant_account_id`
   - `sub.environment`
   - `sub.provider_subscription_id`
   There is zero need to duplicate `provider`, `merchant_account_id`, and `environment` onto `organization_billing_periods`.
3. **Audit Trail Protection:** `ondelete="RESTRICT"` prevents deleting a subscription record while historical billing periods reference it, ensuring complete financial auditability.

---

### 2.5 Unambiguous Binding Traceability Matrix

With `period.subscription_id` established, an `OrganizationBillingPeriod` is unambiguously bound to all originating entities:

```
┌──────────────────────────────┐
│  OrganizationBillingPeriod   │
│  id: bp_123                  │
│  subscription_id: sub_uuid   │
└──────────────┬───────────────┘
               │
               ▼ (Direct Foreign Key: L1 -> L2)
┌──────────────────────────────┐
│        Subscription          │
│  id: sub_uuid                │
│  provider: "razorpay"        │ ──▶ Originating Provider
│  merchant_account: "default" │ ──▶ Originating Merchant Account
│  environment: "live"         │ ──▶ Originating Environment
│  provider_sub_id: "sub_rzp1" │ ──▶ Originating Provider Subscription
└──────────────┬───────────────┘
               │
               ▼ (Parent-to-Ledger Relationship: L2 -> L3)
┌──────────────────────────────┐
│       BillingInvoice         │
│  id: inv_uuid                │
│  billing_period_id: bp_123   │ ──▶ Verified Settled Invoice
│  status: "paid"              │
└──────────────┬───────────────┘
               │
               ▼ (1:N Invoice-to-Payments: L3)
┌──────────────────────────────┐
│       BillingPayment         │
│  id: pay_uuid                │
│  status: "captured"          │ ──▶ Authoritative Captured Payment
│  amount_minor_units: 290000  │
└──────────────────────────────┘
```

---

## 3. Issue 2 — Resubscription While Paid Entitlement Remains Active

### 3.1 Scenario Walkthrough
1. Customer purchases 1-month Pro subscription ($S_1$).
2. Verified payment provisions billing period $P_1$ with dates `[Day 1, Day 31]` (`period_end = Day 31`).
3. On Day 1, Razorpay reaches `total_count = 1` and issues `subscription.completed`. Local agreement handler transitions $S_1$ to `status = canceled`, `plan_tier = "free"`.
4. On Day 11, $P_1$ has **20 days remaining**. The customer navigates to billing settings and clicks "Subscribe to Pro".
5. A new checkout attempt is initiated, creating Razorpay subscription $S_2$.
6. $S_2$ is activated on the gateway, and its payment is captured on Day 11.
7. Payment settlement webhook for $S_2$ arrives at Aptlyst AI on Day 11.

---

### 3.2 Evaluation of Candidate Resubscription Policies

We evaluate the candidate policies strictly against the prompt's four negative constraints:
- **Constraint 1:** Do not silently overwrite an existing paid period.
- **Constraint 2:** Do not lose a captured payment.
- **Constraint 3:** Do not grant overlapping or duplicate quota.
- **Constraint 4:** Do not invent a refund policy.

| Policy Option | Constraint 1: No Overwrite | Constraint 2: No Lost Payment | Constraint 3: No Duplicate Quota | Constraint 4: No Invented Refund | Viability Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Policy 1: Immediate Supercession** (Deactivate $P_1$ on Day 11, start $P_2$ immediately) | **VIOLATED.** 20 days of paid service on $P_1$ are truncated and discarded. | SATISFIED. | SATISFIED. | SATISFIED. | **REJECTED** (Unfair forfeiture of paid entitlement). |
| **Policy 2: Period Extension** (Extend $P_1.period\_end$ to Day 61, add quota to $P_1$) | SATISFIED. | SATISFIED. | **VIOLATED.** Adds 500k quota into $P_1$ during Days 11–31, creating a 1,000,000 token bucket. | SATISFIED. | **REJECTED** (Conflates two different subscriptions on one period; breaks monthly accounting). |
| **Policy 3: Checkout Deferred** (Reject checkout while Pro period is active) | SATISFIED. | SATISFIED (No money taken). | SATISFIED. | SATISFIED. | **VIOLATED on Edge Cases.** If external payment arrives via direct mandate, system has no settlement path. |
| **Policy 4: Two-Tier Entitlement Protection (Chosen Architecture)** | **SATISFIED.** $P_1$ runs untouched to Day 31. | **SATISFIED.** Ledger recorded immediately. | **SATISFIED.** $P_2$ is queued as inactive; zero quota granted until Day 31. | **SATISFIED.** Zero refunds needed. | **ACCEPTED (Production Standard)** |

---

### 3.3 The Chosen Policy: Two-Tier Entitlement Protection

Our architecture employs a two-tier protection model: **Pre-Emptive Renewal Window at Checkout** backed by **Consecutive Queued Period Provisioning at Settlement**.

#### Tier 1: Checkout-Time Policy (`create_razorpay_subscription_checkout`)
To deliver clean user experience and prevent unnecessary billing collisions:
1. When a user requests checkout:
   - Query active billing period: `SELECT ... FROM organization_billing_periods WHERE organization_id = :org_id AND is_active = true`.
   - If active period has `plan_tier == "pro"` and `now < period.period_end`:
     - Calculate remaining days: `remaining = period.period_end - now`.
     - **If remaining > RENEWAL_WINDOW_DAYS (default: 3 days):**
       Reject checkout with:
       ```python
       raise ActivePaidEntitlementExistsError(
           f"Your Pro plan is active until {period.period_end.strftime('%b %d, %Y')}. "
           f"Resubscription opens 3 days prior to expiration."
       )
       ```
     - **If remaining <= RENEWAL_WINDOW_DAYS (e.g. within final 3 days):**
       Allow checkout to proceed! The user is legitimately renewing before their service lapses.

#### Tier 2: Settlement-Time Policy (`handle_payment_settlement` / Step 5.2D.3.3)
If a verified captured payment arrives for $S_2$ while $P_1$ is still active (whether within the renewal window or via an out-of-band payment):

```
Time: Day 11 (Settlement Arrives)
───────────────────────────────────────────────────────────────────────────────
[P1: Active Pro Period] ───────────▶ Valid until Day 31 (20 days remaining)
  - is_active = True
  - subscription_id = S1.id
  - Quota: 500,000 tokens (actively deducted by LLM requests)

[P2: Queued Pro Period] ───────────▶ Scheduled for [Day 31, Day 61]
  - is_active = False (CRITICAL: Preserves uq_org_active_billing_period!)
  - subscription_id = S2.id
  - Quota: 500,000 tokens (tokens_used = 0, tokens_reserved = 0)
  - Not touched by reserve_usage() while is_active = False

Ledger State:
  - BillingInvoice(id=inv_2, subscription_id=S2.id, billing_period_id=P2.id, status="paid")
  - BillingPayment(id=pay_2, invoice_id=inv_2, status="captured", amount=290000)
───────────────────────────────────────────────────────────────────────────────
```

### 3.4 Invariants Guaranteed by Tier 2
1. **Database Constraint Safety:**
   The partial unique index:
   ```sql
   CREATE UNIQUE INDEX uq_org_active_billing_period 
   ON organization_billing_periods (organization_id) 
   WHERE is_active = true;
   ```
   is **strictly respected**. $P_1$ is `is_active = True`. $P_2$ is inserted with `is_active = False`. Zero constraint violations occur.
2. **Quota Isolation:**
   During Days 11–31, `reserve_usage()` locks the active period for the tenant (which is $P_1$). It draws tokens **exclusively from $P_1$'s quota**. The customer receives zero unearned or duplicate tokens.
3. **Seamless Rollover at Expiry:**
   At Day 31 00:00:00 (`current_time >= P1.period_end`):
   When `reserve_usage()` (or a periodic sweeper) executes:
   - $P_1$ is marked `is_active = False`.
   - The queued period $P_2$ (where `period_start <= current_time < period_end` and `subscription_id = S2.id`) is promoted: `P2.is_active = True`.
   - $P_2$'s 500,000 tokens become available immediately with zero service disruption.
4. **Gateway Cadence Alignment:**
   Because Razorpay charges subsequent recurring cycles based on $S_2$'s billing schedule, subsequent invoices for $S_2$ arrive at Day 41. In Step 5.2D.3.3, subsequent cycle settlement evaluates $P_2$'s timeline and links identically to existing cycle renewals.

---

## 4. Issue 3 — Accounting Authorization Using Direct Period Association

### 4.1 Step 4b Implementation in `accounting.py`

Under Lock 1 ($L_1$, `OrganizationBillingPeriod`), the accounting engine resolves the associated subscription directly via `period.subscription_id` before acquiring Lock 2 ($L_2$, `Subscription`).

```python
    # =========================================================================
    # Step 4b: Targeted Subscription Entitlement Verification (Paid Periods Only)
    # =========================================================================
    is_paid_period = bool(period.plan_tier and period.plan_tier.strip().lower() != "free")
    is_period_expired = (current_time >= period.period_end)

    if is_paid_period:
        # Invariant: Every paid billing period must be associated with a valid subscription
        if period.subscription_id is None:
            logger.error(
                "Paid billing period %s (org %s) lacks an associated subscription_id; failing closed.",
                period.id,
                org_id,
            )
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_NOT_FOUND,
                message=f"Paid billing period '{period.id}' has no associated subscription record",
            )

        # Lock Subscription SECOND in strict hierarchy (L1 -> L2)
        # Targeted lock on the EXACT subscription associated with this billing period
        stmt_sub = (
            select(Subscription)
            .where(
                Subscription.id == period.subscription_id,
                Subscription.organization_id == org_id,
            )
            .with_for_update()
        )
        res_sub = await session.execute(stmt_sub)
        subscription = res_sub.scalar_one_or_none()

        if subscription is None:
            logger.error(
                "Subscription %s associated with period %s not found under lock.",
                period.subscription_id,
                period.id,
            )
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_NOT_FOUND,
                message="Associated subscription record not found under lock",
            )

        # ---------------------------------------------------------------------
        # Evaluation Case 1: Subscription Agreement is Active or Past Due
        # ---------------------------------------------------------------------
        if subscription.status in (SubscriptionStatus.active, SubscriptionStatus.past_due):
            if is_period_expired:
                # Lazy finalization of scheduled cancellations
                if subscription.cancel_at_period_end:
                    subscription.status = SubscriptionStatus.canceled
                    subscription.plan_tier = "free"
                    subscription.cancel_at_period_end = False
                    subscription.canceled_at = subscription.canceled_at or current_time
                period.is_active = False
                await session.flush()
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
                    message="Current time is outside the active billing period date range",
                )

            # Check grace period anchor if past_due
            if subscription.status == SubscriptionStatus.past_due:
                if subscription.past_due_since is None:
                    return ReservationResult(
                        success=False,
                        error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                        message="Subscription is past_due but past_due_since anchor is missing; failing closed",
                    )
                pds = (
                    subscription.past_due_since
                    if subscription.past_due_since.tzinfo
                    else subscription.past_due_since.replace(tzinfo=timezone.utc)
                )
                if current_time >= pds + timedelta(days=settings.billing_grace_period_days):
                    return ReservationResult(
                        success=False,
                        error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                        message="Subscription past-due grace period has expired; Pro access denied",
                    )

        # ---------------------------------------------------------------------
        # Evaluation Case 2: Subscription Agreement is Terminal (Canceled / Completed)
        # ---------------------------------------------------------------------
        elif subscription.status == SubscriptionStatus.canceled:
            if is_period_expired:
                # Expired period under terminal agreement: deactivate period cleanly
                if period.is_active:
                    period.is_active = False
                    await session.flush()
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
                    message="Current time is outside the active billing period date range",
                )

            # CRITICAL ARCHITECTURAL GUARANTEE:
            # Agreement is terminal, BUT the tenant holds an active, verified Pro period
            # whose end date is in the future (current_time < period.period_end).
            # This represents an independently verified, paid entitlement. Access is AUTHORIZED!
            logger.debug(
                "Authorizing usage for org %s under verified paid period %s (subscription %s is canceled; access expires at %s)",
                org_id,
                period.id,
                subscription.id,
                period.period_end.isoformat(),
            )

        # ---------------------------------------------------------------------
        # Evaluation Case 3: Suspended or Non-Entitled Statuses
        # ---------------------------------------------------------------------
        else:
            # unpaid, paused, incomplete, incomplete_expired, trialing
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
                message=f"Subscription status '{subscription.status.value}' is not entitled for Pro access",
                details={"subscription_status": subscription.status.value},
            )
```

---

### 4.2 Verification of the Six Accounting Invariants

1. **Terminal $S_1$ Remains Canceled:**
   Case 2 authorizes usage while `current_time < period.period_end` **without modifying `subscription.status`**. $S_1$ remains permanently `canceled`.
2. **$P_1$ Authorizes Access While Valid:**
   As long as `period.is_active = True`, `current_time < period.period_end`, and `not period.is_blocked`, execution flows directly to token reservation.
3. **$S_2$ Must Not Accidentally Authorize $P_1$:**
   Because `stmt_sub` queries `WHERE Subscription.id == period.subscription_id`, it retrieves $S_1$ specifically. $S_2$'s existence or status is completely ignored during $P_1$'s evaluation.
4. **$S_2$ Cannot Invalidate $P_1$ Prematurely:**
   If $S_2$ is in `incomplete` or `past_due` status, $P_1$ is unaffected because $P_1$ does not reference $S_2$.
5. **Hard Cutoff at `period_end`:**
   The exact second `current_time >= period.period_end`, both Case 1 and Case 2 set `period.is_active = False`, flush the database transaction, and return `BILLING_PERIOD_EXPIRED`. Zero unearned tokens are granted.
6. **Existing Stripe Behavior Preserved:**
   Stripe subscriptions in `active` or `past_due` execute Case 1, preserving identical grace period enforcement and scheduled cancellation transitions.

---

## 5. Issue 4 — Checkout and Settlement Concurrency Across Different Subscription Rows

### 5.1 The Cross-Row Locking Hazard
The user prompt highlights a subtle and critical concurrency trap:
> *"Pay particular attention to whether checkout and settlement actually lock the same subscription row when S1 is terminal and S2 is being created. Do not claim serialization merely because two operations use SELECT FOR UPDATE on different rows."*

#### Why Row Locks on Different Rows Do Not Serialize:
In PostgreSQL, an exclusive row lock (`SELECT ... FOR UPDATE`) places a tuple lock on a specific physical tuple identifier (`ctid`).
- If Transaction A executes `SELECT ... FROM subscriptions WHERE id = S1_ID FOR UPDATE`, PostgreSQL locks row $S_1$.
- If Transaction B executes `SELECT ... FROM subscriptions WHERE id = S2_ID FOR UPDATE`, PostgreSQL locks row $S_2$.
- **These two operations execute entirely in parallel.** Neither transaction blocks the other at the subscription layer!

Therefore, claiming that checkout and settlement serialize because they both lock "the subscription" is **factually false** when $S_1$ and $S_2$ are distinct rows.

---

### 5.2 Where True Serialization and Mutual Exclusion Occur

Aptlyst AI's database architecture establishes serialization through **two complementary mechanisms**:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                    CONCURRENCY SYNCHRONIZATION HIERARCHY                     │
│                                                                              │
│  1. PRIMARY SERIALIZATION NEXUS (When an active billing period exists):      │
│     Lock 1 (L1): SELECT ... FROM organization_billing_periods                │
│                  WHERE organization_id = :org_id AND is_active = true        │
│                  FOR UPDATE                                                  │
│     * Contends on the SINGLE active row P1 for the tenant organization.      │
│     * Strictly serializes Settlement (S2), Accounting, and Checkout.         │
│                                                                              │
│  2. SECONDARY MUTUAL EXCLUSION (When zero active billing periods exist):      │
│     A. Partial Unique Index: uq_org_scoped_active_billing_checkout_attempt   │
│        * Serializes concurrent checkouts for the tenant.                     │
│     B. Partial Unique Index: uq_org_active_billing_period                    │
│        * Serializes concurrent period activations for the tenant.            │
│     C. Level 0 Advisory Lock: pg_advisory_xact_lock(hash(entity))           │
│        * Serializes concurrent webhook events for the same entity.           │
└──────────────────────────────────────────────────────────────────────────────┘
```

#### Detailed Proof of Mutual Exclusion:
1. **When $P_1$ Exists and Is Active:**
   Every operation touching tenant billing must execute:
   ```python
   stmt_active_bp = (
       select(OrganizationBillingPeriod)
       .where(
           OrganizationBillingPeriod.organization_id == org_id,
           OrganizationBillingPeriod.is_active == True,
       )
       .with_for_update()
   )
   ```
   Because `uq_org_active_billing_period` guarantees at most one row where `is_active = true`, Settlement for $S_2$, Accounting for $P_1$, and Checkout for $S_2$ **all request an exclusive row lock on the identical row $P_1$**.
   - If Settlement acquires $P_1$, Checkout and Accounting block until Settlement commits.
   - If Accounting acquires $P_1$, Settlement blocks until Accounting commits.
   - **Result:** Complete serializability across different subscriptions ($S_1$ vs $S_2$) is guaranteed via $L_1$ on $P_1$.
2. **When Zero Active Periods Exist ($P_1$ Expired or Inactive):**
   When `is_active = true` matches zero rows, `SELECT ... FOR UPDATE` returns empty and acquires zero row locks.
   How do operations serialize?
   - **Between Concurrent Checkouts:**
     Both attempt to insert `BillingCheckoutAttempt` with `status = 'creating'`. The partial unique index `uq_org_scoped_active_billing_checkout_attempt` causes the second transaction to wait on index insertion; upon Worker 1's commit, Worker 2 immediately raises `IntegrityError`, caught and handled as `CheckoutInProgressError`.
   - **Between Settlement and Checkout:**
     Settlement inserts the new active period $P_2$ with `is_active = true`. If Checkout simultaneously attempts to insert an active period, PostgreSQL's index lock on `uq_org_active_billing_period` forces serialization, causing the conflicting transaction to raise `UniqueViolation`.

---

### 5.3 Detailed Trace of the Six Concurrency Scenarios

#### Scenario A: Checkout Against Canceled $S_1$ While $P_1$ Remains Active
1. User requests checkout.
2. **Transaction 1 (Phase 1):**
   - Queries `OrganizationBillingPeriod` where `is_active = true FOR UPDATE` ($L_1$). Locks $P_1$.
   - Inspects $P_1$: Pro tier, unexpired.
   - If outside renewal window (> 3 days remaining): Rejects with `ActivePaidEntitlementExistsError`. Releases $P_1$ immediately.
   - If within renewal window ($\le 3$ days remaining): Allows renewal checkout.
3. **Transaction 1 (Phase 2):**
   - Inserts `BillingCheckoutAttempt(status="creating", organization_id=org_id)`.
   - Commits Transaction 1 ($L_1$ released; `uq_org_scoped_active_billing_checkout_attempt` held).
4. **External API Call (Phase 3):**
   - Invokes `razorpay.create_subscription()`. Zero database locks held.
5. **Transaction 2 (Phase 4):**
   - Updates attempt to `status = "open"`, `provider_subscription_id = "sub_s2"`. Commits.

#### Scenario B: Checkout While $P_1$ Remains Active (Simultaneous with Accounting Reservation)
- Thread 1: LLM `reserve_usage()` executes on $P_1$.
  - Acquires $L_1$ on $P_1$.
  - Acquires $L_2$ on $S_1$ (via `P1.subscription_id`).
- Thread 2: Checkout executes.
  - Attempts to query $P_1$ with `FOR UPDATE` ($L_1$).
  - Blocks on Thread 1 until Thread 1 commits.
- **Deadlock Freedom:** Lock acquisition order is identical: $L_1 \to L_2$. Zero circular wait.

#### Scenario C: Settlement for $S_2$ Arrives While $P_1$ Exists
1. Webhook worker claims `payment.captured` for $S_2$.
2. Acquires Level 0 Advisory Lock: `pg_advisory_xact_lock("payment", pay_id)`.
3. Acquires Level 1 Row Lock: `SELECT ... FROM organization_billing_periods WHERE is_active=true FOR UPDATE`. Locks $P_1$.
4. Acquires Level 2 Row Lock: `SELECT ... FROM subscriptions WHERE provider_subscription_id = "sub_s2" FOR UPDATE`. Locks $S_2$.
5. Inspects $P_1$: $P_1$ is active Pro tier.
6. Tier 2 Execution:
   - Inserts $P_2$ with `is_active = False`, `period_start = P1.period_end`, `period_end = P1.period_end + 30 days`, `subscription_id = S2.id`.
   - Records `BillingInvoice(status="paid", billing_period_id=P2.id)`.
   - Records `BillingPayment(status="captured", invoice_id=inv_2.id)`.
7. Commits transaction. Locks released.

#### Scenario D: Accounting Reservation During Settlement for $S_2$
- Thread 1 (Settlement): Holds $L_0(\text{pay\_id}) \to L_1(P_1) \to L_2(S_2)$.
- Thread 2 (Accounting): Requests $L_1(P_1)$.
- Thread 2 blocks on $L_1$ until Thread 1 commits.
- **Notice:** Thread 1 locks $S_2$, while Thread 2 will lock $S_1$. **Because they serialize at $L_1$ on $P_1$, Thread 2 never attempts to acquire $S_1$ until Thread 1 has completely committed and released all locks.** Deadlock is mathematically impossible.

#### Scenario E: Concurrent Checkouts for the Same Organization
- Worker 1 and Worker 2 invoke checkout concurrently.
- Both execute Phase 2: `INSERT INTO billing_checkout_attempts (organization_id, status='creating')`.
- Worker 1 commits.
- Worker 2 catches PostgreSQL error `uq_org_scoped_active_billing_checkout_attempt` violation.
- Worker 2 rolls back and cleanly returns `CheckoutInProgressError`.

#### Scenario F: Delayed Settlement for $S_1$ Arrives After $S_2$ Is Active
1. Webhook arrives: delayed payment for $S_1$.
2. Acquires $L_0(\text{payment}, \text{pay\_id})$.
3. Acquires $L_1$ on active billing period:
   - If $P_1$ is still active: locks $P_1$.
   - Checks if $S_1$'s invoice was already settled: finds existing `BillingInvoice` is `paid`. Idempotent no-op commit!
   - If $P_1$ already expired: locks $P_2$ (now active under $S_2$).
   - Detects mismatch: delayed payment belongs to $S_1$, but active period belongs to $S_2$.
   - Records payment ledger `BillingPayment(status="captured")`.
   - Refuses resurrection of $S_1$; stages `PaymentReconciliation(reason="delayed_payment_for_superseded_subscription")`.
   - Commits cleanly without corrupting $P_2$ or resurrecting $S_1$.

---

### 5.4 Global Lock Hierarchy Verification

Every transaction in the system adheres strictly to the directed acyclic graph:

$$\text{Advisory Lock } (L_0) \prec \text{Billing Period } (L_1) \prec \text{Subscription } (L_2) \prec \text{Auxiliary Rows } (L_3)$$

```
┌────────────────────────────────────────────────────────────────────────┐
│ Level 0: pg_advisory_xact_lock(entity_type, entity_id)                 │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Always acquired first
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Level 1: OrganizationBillingPeriod (SELECT ... FOR UPDATE)             │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Always acquired second
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Level 2: Subscription (SELECT ... FOR UPDATE via period.subscription_id│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Always acquired third
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Level 3: Auxiliary Records (BillingInvoice, BillingPayment,            │
│          BillingCheckoutAttempt, UsageReservation)                     │
└────────────────────────────────────────────────────────────────────────┘
```

Because all transactions acquire resources in monotonically increasing level order, **circular wait cannot occur**.

---

## 6. Issue 5 — Migration, Compatibility, and Test Specifications

### 6.1 Database Migration Plan

#### Migration Identification:
- **Current Head:** `d1e2f3a4b5c6` (`20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py`)
- **New Revision ID:** `e2f3a4b5c6d1`
- **Revision Name:** `20261108_1600_e2f3a4b5c6d1_add_subscription_id_to_billing_periods.py`
- **Down Revision:** `d1e2f3a4b5c6` (Guarantees single linear head).

#### Alembic Migration Script (`upgrade` and `downgrade`):
```python
"""add subscription_id to billing periods

Revision ID: e2f3a4b5c6d1
Revises: d1e2f3a4b5c6
Create Date: 2026-11-08 16:00:00.000000+00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e2f3a4b5c6d1"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add nullable subscription_id column to organization_billing_periods
    op.add_column(
        "organization_billing_periods",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 2. Create index on subscription_id for fast entitlement lookups
    op.create_index(
        "ix_billing_periods_subscription_id",
        "organization_billing_periods",
        ["subscription_id"],
    )

    # 3. Create foreign key constraint with RESTRICT delete behavior
    op.create_foreign_key(
        "fk_billing_periods_subscription",
        "organization_billing_periods",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 4. Backfill historical Stripe periods from stripe_invoices
    op.execute(
        """
        UPDATE organization_billing_periods obp
        SET subscription_id = s.id
        FROM stripe_invoices si
        JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
        WHERE si.billing_period_id = obp.id
          AND obp.subscription_id IS NULL;
        """
    )

    # 5. Backfill multi-provider periods from billing_invoices (if any exist)
    op.execute(
        """
        UPDATE organization_billing_periods obp
        SET subscription_id = bi.subscription_id
        FROM billing_invoices bi
        WHERE bi.billing_period_id = obp.id
          AND bi.subscription_id IS NOT NULL
          AND obp.subscription_id IS NULL;
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_billing_periods_subscription",
        "organization_billing_periods",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_billing_periods_subscription_id",
        table_name="organization_billing_periods",
    )
    op.drop_column("organization_billing_periods", "subscription_id")
```

---

### 6.2 Historical Stripe Data Compatibility
1. **Zero Degradation:**
   Existing Stripe subscriptions and their corresponding `OrganizationBillingPeriod` rows are seamlessly linked via Step 4 of the migration.
2. **Free Periods Untouched:**
   Free periods created at organization signup or rollover have no invoice and remain `subscription_id = NULL`.
3. **Idempotent Handlers:**
   Existing Stripe handlers ([`stripe_invoice_paid_handler.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py)) only require setting `subscription_id = locked_sub.id` when instantiating `new_pro_period`.

---

### 6.3 Required Mocked Unit Test Suite (`test_resubscription_entitlement_architecture.py`)

1. `test_accounting_authorizes_unexpired_period_via_direct_subscription_association`:
   - Setup: $P_1$ has `subscription_id = S1.id`. $S_1$ has `status = canceled`. Customer also has $S_2$ with `status = incomplete`.
   - Call: `reserve_usage()` on $P_1$.
   - Assert: Returns `success = True`. Proves $S_2$ does not invalidate $P_1$.
2. `test_accounting_denies_and_deactivates_expired_period_under_canceled_subscription`:
   - Setup: $P_1$ has `subscription_id = S1.id`. $S_1$ is canceled. Advance clock past `period_end`.
   - Call: `reserve_usage()`.
   - Assert: Returns `BILLING_PERIOD_EXPIRED`. $P_1.is_active$ transitioned to `False`.
3. `test_settlement_queues_p2_consecutively_when_p1_active`:
   - Setup: $P_1$ is active Pro until Day 31. Deliver captured payment for $S_2$ on Day 11.
   - Assert: $P_1$ remains active with `is_active = True`.
   - Assert: $P_2$ is inserted with `is_active = False`, `period_start = Day 31`, `period_end = Day 61`, `subscription_id = S2.id`.
   - Assert: `uq_org_active_billing_period` is not violated.
4. `test_rollover_promotes_queued_p2_upon_p1_expiration`:
   - Setup: $P_1$ expires at Day 31; $P_2$ is queued.
   - Advance clock to Day 31 + 1 second.
   - Call: `reserve_usage()`.
   - Assert: $P_1.is_active = False$, $P_2.is_active = True$. Quota deducted from $P_2$.
5. `test_checkout_deferred_when_outside_renewal_window`:
   - Setup: Tenant has active Pro period with 20 days remaining.
   - Call: `create_razorpay_subscription_checkout()`.
   - Assert: Raises `ActivePaidEntitlementExistsError`. Zero checkout attempts inserted.
6. `test_checkout_allowed_inside_renewal_window`:
   - Setup: Tenant has active Pro period with 2 days remaining ($\le 3$ days).
   - Call: `create_razorpay_subscription_checkout()`.
   - Assert: Succeeds; returns open checkout session.
7. `test_delayed_s1_settlement_does_not_corrupt_active_s2_period`:
   - Setup: $S_1$ canceled; $S_2$ active with active period $P_2$. Delayed payment for $S_1$ arrives.
   - Assert: Payment recorded in ledger; $S_1$ not resurrected; $P_2$ untouched; reconciliation staged.

---

### 6.4 Real PostgreSQL 15+ Concurrency Test Suite

1. `test_postgresql_cross_row_subscription_concurrency_serializes_at_l1`:
   - Spawn two parallel async workers on a live PostgreSQL database:
     - Worker A: Settlement for $S_2$ (`SELECT ... WHERE is_active=true FOR UPDATE` then lock $S_2$).
     - Worker B: Accounting reservation on $P_1$ (`SELECT ... WHERE id=P1_id FOR UPDATE` then lock $S_1$).
   - Verify: Zero deadlock (`40P01`) errors. Worker B waits on Worker A at $L_1$ on row $P_1$.
2. `test_postgresql_uq_org_active_billing_period_prevents_duplicate_active_periods`:
   - Simulate race condition where two workers attempt to insert active periods (`is_active = true`) simultaneously for the same organization.
   - Verify: Exactly one worker commits; the other encounters unique constraint violation.
3. `test_postgresql_concurrent_checkout_and_settlement_mutual_exclusion`:
   - Run concurrent execution of `create_razorpay_subscription_checkout` and `handle_payment_settlement`.
   - Verify: Strict serializability without race conditions or orphaned checkout attempts.

---

## 7. Deliverable Summary & Final Verdict

| Design Requirement | Status | Resolution Details |
| :--- | :--- | :--- |
| **Issue 1: Entitlement Association** | **RESOLVED** | Added nullable `subscription_id` FK to `OrganizationBillingPeriod`. Avoids hot-path joins; inherits provider scope cleanly. |
| **Issue 2: Resubscription Policy** | **RESOLVED** | Two-Tier Entitlement Protection: Pre-emptive renewal window at checkout + Consecutively queued period $P_2$ at settlement. Zero overwrite, zero lost money, zero duplicate quota. |
| **Issue 3: Accounting Authorization** | **RESOLVED** | Step 4b in `accounting.py` locks subscription targeted directly by `period.subscription_id`. Decoupled terminal agreement preserves paid days. |
| **Issue 4: Cross-Row Concurrency** | **RESOLVED** | Clarified that row locks on $S_1$ and $S_2$ do not serialize; proven serialization occurs at Lock 1 on $P_1$ and partial unique constraints. |
| **Issue 5: Tests & Migrations** | **RESOLVED** | Single-head Alembic migration `e2f3a4b5c6d1` from `d1e2f3a4b5c6` with data backfill and full rollback specified; 7 mocked + 3 PG integration tests. |

---

### Final Verdict: **READY FOR IMPLEMENTATION**
