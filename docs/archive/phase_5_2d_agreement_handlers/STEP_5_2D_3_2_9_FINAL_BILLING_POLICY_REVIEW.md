# Aptlyst AI — Step 5.2D.3.2.9 Final Billing Policy Review & Concurrency Hardening

**Date:** 2026-11-08  
**Scope:** Resolution of Provider Interval vs. Entitlement Divergence, Partial Quota Semantics, Deterministic Stripe Provenance, and Checkout Entitlement-State Race  
**Alembic Head:** `d1e2f3a4b5c6` (Verified single linear head)  
**Task Type:** STRICTLY READ-ONLY ARCHITECTURAL REVIEW (Zero code/schema/test/Git modifications)  
**Target File Delivered:** `STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md`  

---

## 1. Executive Summary & Verdict

This architectural review provides the final hardening for Step 5.2D Razorpay Billing before any code or migrations are written. It resolves four deep policy and concurrency questions:

1. **Rejection of Artificial Date Shifting (Issue 1):**
   The fallback proposed in Step 5.2D.3.2.8—shifting period dates to arbitrary consecutive blocks (`Day 31–61`, `Day 61–91`) when Razorpay invoices cover `Day 11–41` and `Day 41–71`—is **formally rejected**. It violates accounting auditability, decouples tax invoices from service intervals, grants excess entitlement upon gateway cancellation, and masks gateway delinquency. We recommend **Gateway Schedule Primacy** as the sole coherent policy: either aligning checkout via native `start_at = P1.period_end`, or resetting the billing cycle upon immediate checkout with explicit user consent.
2. **Quota Invariant Under Delayed Settlement (Issue 2):**
   Aptlyst AI’s data model ([`OrganizationBillingPeriod.monthly_token_quota`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py#L77-L80)) enforces an immutable integer allocation matching [`settings.default_pro_monthly_token_quota`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/config.py#L393). Proration does not exist in the codebase and is explicitly rejected by [`stripe_invoice_paid_handler.py` lines 276–277](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py#L276-L277). When a full payment of ₹2,900 captures on Day 65 for an invoice interval ending on Day 71, granting the full 500,000 tokens for the remaining 6 days **is compliant** with the Full-Payment Settlement rule (the customer paid 100% of the price). However, to protect against extreme payment delays where service has substantially lapsed, payments arriving with less than a configurable threshold ($\le 3$ days) remaining stage `PaymentReconciliation` for manual finance credit rather than auto-provisioning a degraded period.
3. **Strict Deterministic Stripe Provenance (Issue 3):**
   All heuristic fallbacks (including associating periods to an organization's only active subscription) are **completely removed**. We specify an exact five-scenario provenance matrix. If an active Pro period cannot be linked to a verified invoice receipt from Stripe, the Alembic migration **aborts with an exception**, guaranteeing zero silent breakage of paying customers. Inactive historical unlinked periods are retained with `subscription_id = NULL` and logged to `payment_reconciliations`.
4. **Elimination of the Checkout Entitlement-State Race (Issue 4):**
   We trace the exact transaction boundaries in [`checkout.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py). We document that checkout locks the active billing period row $P_1$ `FOR UPDATE` in Phase 1, releases all row locks upon committing the leased `creating` attempt at the end of Phase 2 ($T_1$), holds zero locks during the external network call (Phase 3), and executes a strict **Phase 4 revalidation query** under lock before marking the attempt `open` in Transaction $T_2$. If concurrent settlement or rollover occurs during Phase 3, Phase 4 auto-expires the attempt, preventing redundant checkout links.

### Final Verdict: **READY FOR IMPLEMENTATION**

---

## 2. Issue 1 — Provider Invoice Period versus Internal Entitlement Period

### 2.1 Evidence from Inspected Code & Established Provider Payload Semantics

#### A. Stripe Precedent:
In [`apps/api/app/services/billing/stripe_invoice_paid_handler.py` lines 312–320 & 674–689](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py#L312-L320):
```python
pro_line = candidate_lines[0]
line_period = pro_line.get("period")
line_period_start = _validate_positive_timestamp(line_period.get("start"), "line.period.start", event_id)
line_period_end = _validate_positive_timestamp(line_period.get("end"), "line.period.end", event_id)

new_pro_period = OrganizationBillingPeriod(
    id=uuid.uuid4(),
    organization_id=org_id,
    period_start=line_period_start,
    period_end=line_period_end,
    plan_tier="pro",
    monthly_token_quota=pro_quota,
    ...
)
```
In Stripe, `OrganizationBillingPeriod.period_start` and `period_end` are **strictly copied from the invoice line item** (`line_period_start`, `line_period_end`). The database entitlement period is an exact mirror of the gateway invoice paid period.

#### B. Razorpay Payload Semantics:
In [`apps/api/app/services/billing/razorpay/handler_base.py` lines 147–152 & 196–198](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/handler_base.py#L147-L152):
- `RazorpaySubscriptionEntity`: Exposes `current_start`, `current_end`, `start_at`, `end_at`, `charge_at`.
- `RazorpayInvoiceEntity`: Exposes `billing_start`, `billing_end`, `paid_at`, `amount`, `amount_paid`, `amount_due`.
- `BillingInvoice` ([`apps/api/app/models/payment.py` lines 344–350](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py#L344-L350)): Exposes `period_start`, `period_end`, `paid_at`.

**Code Finding:** Both Stripe and Razorpay gateway invoices expose **authoritative, explicit Unix timestamps** defining the exact calendar service interval funded by that invoice.

---

### 2.2 Mathematical Breakdown of Cadence Divergence

In Step 5.2D.3.2.8, the fallback proposal attempted to solve an immediate checkout on Day 11 by artificially shifting internal period dates:
- Gateway Invoice $I_1$ (paid Day 11): covers `[Day 11, Day 41]`.
- Internal Period $P_2$: shifted to `[Day 31, Day 61]`.
- Gateway Invoice $I_2$ (paid Day 41): covers `[Day 41, Day 71]`.
- Internal Period $P_3$: shifted to `[Day 61, Day 91]`.

#### Why Artificial Shifting Fails Catastrophically:
1. **Tax & Legal Incoherence:**
   Invoice $I_1$ is a legal tax document issued by Razorpay stating: *"Service Period: Nov 11 to Dec 11"*. The internal database ledger states: *"Service Period: Dec 01 to Dec 31"*. In financial audits or customer dispute proceedings, Aptlyst AI cannot reconcile internal usage against gateway tax receipts.
2. **Excess Entitlement on Cancellation:**
   Suppose the customer cancels subscription $S_2$ on Day 42. Razorpay marks the subscription non-renewing; it will never bill Cycle 3. Gateway service expires on **Day 71**.
   Under the shifted model, Aptlyst AI's internal period $P_3$ runs until **Day 91**! Aptlyst AI would grant 20 days of unearned Pro tokens after the customer's agreement has fully terminated at the gateway.
3. **Masked Delinquency / Payment Failure:**
   Suppose on Day 41, Cycle 2 payment **fails** (e.g. card expired). Razorpay enters past due / dunning.
   Under the shifted model, Aptlyst AI is only on Day 10 of $P_2$! Aptlyst AI would continue granting Pro access until Day 61, completely blind to the fact that Cycle 2 was rejected on Day 41.
4. **Cumulative Phase Drift:**
   Every cycle bills at $T_0 + 30k$. The entitlement executes at $T_{\text{expiry}} + 30k$. The phase shift is permanent and unresolvable without manual database surgery.

---

### 2.3 Recommended Policy: Gateway Schedule Primacy

We formally **REJECT** the artificial date-shifting fallback.

We establish **Gateway Schedule Primacy** as the sole authoritative policy:
> **The service interval `[period_start, period_end]` of an `OrganizationBillingPeriod` must be strictly derived from the verified paid invoice service dates (`[billing_start, billing_end]`) provided by the payment gateway.**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    GATEWAY SCHEDULE PRIMACY ARCHITECTURE                    │
│                                                                             │
│  RULE 1: NO DECOUPLED DATES                                                 │
│  - An internal period CANNOT safely start later than its funding invoice's  │
│    paid service start date.                                                 │
│  - period_start == invoice.billing_start                                    │
│  - period_end   == invoice.billing_end                                      │
│                                                                             │
│  RULE 2: PREVENTING CADENCE SKEW AT THE SOURCE (CHECKOUT)                   │
│  When customer holding active period P1 requests checkout for S2:           │
│                                                                             │
│  PATH A (PRIMARY): Gateway start_at Alignment                               │
│  - Pass start_at = int(P1.period_end.timestamp()) to Razorpay.              │
│  - Mandate is authorized on Day 11.                                         │
│  - Razorpay debits first payment on Day 31 (exact moment P1 expires).       │
│  - Invoice I1 dates: [Day 31, Day 61]. Period P2 dates: [Day 31, Day 61].  │
│  - Perfect 1:1 alignment. Zero date shifting.                               │
│                                                                             │
│  PATH B (EXPLICIT USER UPGRADE): Immediate Reset with Pro-Rata Consent      │
│  - If customer explicitly chooses "Upgrade / Reset Billing Cycle Now":      │
│  - Checkout creates S2 starting immediately on Day 11.                      │
│  - P1 is closed on Day 11 (period_end truncated to Day 11, is_active=false).│
│  - P2 starts on Day 11 ([Day 11, Day 41]). Funded by Invoice I1.            │
│  - Not a "silent overwrite": customer explicitly initiated immediate reset.│
│                                                                             │
│  PATH C (DEFAULT GUARD): Deferred Checkout                                  │
│  - If remaining days on P1 > RENEWAL_WINDOW (e.g. 3 days):                  │
│  - Checkout is disallowed: customer already holds active Pro entitlement.   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Issue 2 — Partial Remaining Service Period and Quota Analysis

### 3.1 The Day 65 Scenario Analysis

- **Context:**
  - Invoice $I_2$ service interval: Day 41 to Day 71 (30 days).
  - Invoice gross amount: ₹2,900 (`amount_paid = 290000`, `amount_due = 0`).
  - Webhook delayed or payment retried; capture completes on **Day 65** (24 days late).
  - Remaining interval until invoice cycle end: Day 65 to Day 71 (6 days).

#### Inspection of Existing Quota Architecture:
1. Model Definition ([`apps/api/app/models/billing.py` line 77](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/billing.py#L77)):
   ```python
   monthly_token_quota: Mapped[int] = mapped_column(BigInteger, nullable=False)
   ```
   Stored as a discrete 64-bit integer.
2. Configuration ([`apps/api/app/config.py` line 393](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/config.py#L393)):
   ```python
   default_pro_monthly_token_quota: int = 500_000
   ```
3. Stripe Implementation Precedent ([`stripe_invoice_paid_handler.py` lines 276–277 & 491](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py#L276-L277)):
   - `if line_price_id == configured_pro_price_id and not line.get("proration"): candidate_lines.append(line)`
   - Prorated lines are **explicitly quarantined and rejected**.
   - `monthly_token_quota` is always assigned the full `default_pro_monthly_token_quota`.
   - **Zero proration or fractional calculation exists anywhere in the codebase.**

---

### 3.2 Evaluation of Quota Options

| Option | Mechanics | Full-Payment Rule | Excess Entitlement Risk | Codebase Viability |
| :--- | :--- | :--- | :--- | :--- |
| **Option A: Proportional Quota** (e.g. $\frac{6}{30} \times 500k = 100k$ tokens) | Calculate fraction of elapsed days and reduce quota. | SATISFIED. | LOW. | **REJECTED.** No proration logic exists in project. Introducing token fractionalization changes billing contract without product-owner approval. |
| **Option B: Full Monthly Quota for Remaining Window** (`[Day 41, Day 71]`, 500,000 tokens) | Set period dates to authoritative invoice interval `[Day 41, Day 71]`. Assign full 500k tokens. | **SATISFIED.** Customer paid full ₹2,900. | **LOW.** Bounded by 500k cap; tokens expire at Day 71. Unused tokens are lost at Day 71. | **ACCEPTED** (for delays within grace window). |
| **Option C: Late Settlement Quarantine Threshold** (Arrival with $\le 3$ days remaining) | If payment captures when remaining cycle duration is $\le 3$ days, do not auto-provision. Record ledger and stage reconciliation. | **SATISFIED.** Payment captured and ledgered. | **ZERO.** | **ACCEPTED** (for extreme delays). |

---

### 3.3 Recommended Policy for Delayed Payments

1. **Authoritative Dates Preserved:**
   When payment captures late on Day 65 for invoice interval `[Day 41, Day 71]`:
   - `OrganizationBillingPeriod` is created with **the authoritative invoice dates**: `period_start = Day 41`, `period_end = Day 71`.
   - When `reserve_usage()` executes on Day 65, `period_start <= now < period_end` is **TRUE** (`41 <= 65 < 71`). Pro access is authorized from Day 65 through Day 71!
   - On Day 71, `now >= period_end` triggers natural deactivation.
   - Zero date shifting occurs. The invoice and period dates match identically.
2. **Full Quota Granted for Legitimate Grace Window:**
   Because the customer paid the full monthly fee of ₹2,900, assigning `monthly_token_quota = 500_000` is **strictly compliant** with the Full-Payment Settlement rule. Unused tokens are naturally forfeited when the period ends at Day 71.
3. **Late-Arrival Quarantine Guard ($\le 3$ Days Remaining):**
   If a payment arrives when `invoice.billing_end - now <= timedelta(days=3)`:
   - Auto-provisioning a period for $< 72$ hours is degraded service.
   - Settlement records `BillingPayment(status="captured")` and `BillingInvoice(status="paid")`.
   - Stages `PaymentReconciliation(reason="delayed_payment_near_cycle_end", diagnostic_payload={"remaining_hours": ...})`.
   - Flags account for administrative service extension or credit note.

---

## 4. Issue 3 — Deterministic Stripe Historical Association Evidence

### 4.1 Strict Removal of Heuristic Fallbacks

The fallback proposed in Step 5.2D.3.2.8 Section 6.3—which assigned historical periods to an organization's active Stripe subscription solely because it was the organization's only active subscription—is **completely removed**.

**Mandate:** Zero guessing. An association is established **if and only if direct, immutable cryptographic or foreign-key provenance exists**.

---

### 4.2 Exact Provenance Matrix Across All Five Data Cardinalities

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STRIPE PROVENANCE EVIDENCE HIERARCHY                     │
│                                                                             │
│  SCENARIO 1: Unambiguous 1:1 Invoice Link                                   │
│  - Exactly one stripe_invoices row points to billing_period_id.             │
│  - invoice.stripe_subscription_id maps to subscriptions.stripe_sub_id.      │
│  - PROVENANCE: 100% Deterministic.                                          │
│  - ACTION: UPDATE organization_billing_periods SET subscription_id = sub.id │
│                                                                             │
│  SCENARIO 2: Unambiguous N:1 Invoice Link                                   │
│  - Multiple stripe_invoices rows point to billing_period_id.                │
│  - ALL matching invoices share the identical stripe_subscription_id.        │
│  - PROVENANCE: 100% Deterministic (multiple receipts for same cycle).       │
│  - ACTION: UPDATE organization_billing_periods SET subscription_id = sub.id │
│                                                                             │
│  SCENARIO 3: Conflicting N:M Invoices (Multiple Different Subscriptions)    │
│  - Multiple stripe_invoices point to period, with >= 2 distinct sub IDs.    │
│  - PROVENANCE: Ambiguous / Corrupt historical links.                        │
│  - ACTION:                                                                  │
│    * If is_active == true: MIGRATION ABORTS IMMEDIATELY.                    │
│    * If is_active == false: subscription_id = NULL. Stage reconciliation.   │
│                                                                             │
│  SCENARIO 4: Active Pro Period with Zero Invoice Links                      │
│  - is_active == true AND plan_tier != 'free', but zero invoices link to it. │
│  - PROVENANCE: Unproven entitlement.                                        │
│  - ACTION: MIGRATION ABORTS IMMEDIATELY. Zero guessing allowed.             │
│                                                                             │
│  SCENARIO 5: Inactive Historical Period with Zero Invoice Links             │
│  - is_active == false, zero invoices link to it.                            │
│  - PROVENANCE: Expected for Free periods; legacy audit gap for Pro periods.  │
│  - ACTION: subscription_id = NULL. Stage reconciliation for Pro tier.       │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 4.3 Deterministic Migration SQL & Mandatory Abort Gate

```python
def upgrade() -> None:
    # 1. Add subscription_id column and index
    op.add_column(
        "organization_billing_periods",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_billing_periods_subscription_id",
        "organization_billing_periods",
        ["subscription_id"],
    )
    op.create_foreign_key(
        "fk_billing_periods_subscription",
        "organization_billing_periods",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 2. Deterministic Aggregate CTE Backfill (Unambiguous 1:1 and N:1 only)
    op.execute(
        """
        WITH period_candidates AS (
            SELECT 
                si.billing_period_id,
                s.id AS subscription_id,
                COUNT(DISTINCT s.id) OVER (PARTITION BY si.billing_period_id) AS distinct_sub_count
            FROM stripe_invoices si
            JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
            WHERE si.billing_period_id IS NOT NULL
              AND si.stripe_subscription_id IS NOT NULL
        ),
        unambiguous_provenance AS (
            SELECT DISTINCT 
                billing_period_id, 
                subscription_id
            FROM period_candidates
            WHERE distinct_sub_count = 1
        )
        UPDATE organization_billing_periods obp
        SET subscription_id = up.subscription_id
        FROM unambiguous_provenance up
        WHERE obp.id = up.billing_period_id
          AND obp.subscription_id IS NULL;
        """
    )

    # 3. Stage Reconciliation for Conflicting Historical Periods (is_active = false)
    op.execute(
        """
        INSERT INTO payment_reconciliations (
            id, provider, merchant_account_id, environment, case_key,
            organization_id, reason, status, diagnostic_payload, created_at, updated_at
        )
        SELECT 
            gen_random_uuid(), 'stripe', 'default', 'legacy',
            'migration_conflicting_period:' || obp.id::text,
            obp.organization_id,
            'conflicting_historical_stripe_subscriptions',
            'pending_review'::reconciliation_status,
            jsonb_build_object('billing_period_id', obp.id, 'plan_tier', obp.plan_tier),
            NOW(), NOW()
        FROM organization_billing_periods obp
        WHERE obp.is_active = false
          AND obp.plan_tier != 'free'
          AND obp.subscription_id IS NULL
          AND obp.id IN (
              SELECT billing_period_id 
              FROM stripe_invoices 
              WHERE billing_period_id IS NOT NULL 
              GROUP BY billing_period_id 
              HAVING COUNT(DISTINCT stripe_subscription_id) > 1
          );
        """
    )

    # -------------------------------------------------------------------------
    # 4. CRITICAL ABORT GATE: Zero Active Pro Periods May Remain Unassociated
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            unresolved_active = conn.execute(
                sa.text(
                    """
                    SELECT obp.id, obp.organization_id 
                    FROM organization_billing_periods obp
                    WHERE obp.plan_tier != 'free' 
                      AND obp.is_active = true 
                      AND obp.subscription_id IS NULL;
                    """
                )
            ).fetchall()

            if unresolved_active:
                unresolved_details = [f"(period={r[0]}, org={r[1]})" for r in unresolved_active]
                raise RuntimeError(
                    f"MIGRATION ABORT: {len(unresolved_active)} active Pro billing periods "
                    f"lack proven subscription provenance: {', '.join(unresolved_details)}. "
                    f"Aborting migration to prevent breaking production LLM token access. "
                    f"Audit invoice records before re-running migration."
                )
```

---

## 5. Issue 4 — Checkout Entitlement-State Race & Transaction Boundaries

### 5.1 Exact Trace of Checkout Transaction Boundaries

In [`apps/api/app/services/billing/razorpay/checkout.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       CHECKOUT TRANSACTION LIFECYCLE                        │
│                                                                             │
│  PHASE 1 & 2: VALIDATION & DURABLE ATTEMPT LEASE (TRANSACTION T1)           │
│  ─────────────────────────────────────────────────────────────────────────  │
│  1. BEGIN Transaction T1                                                    │
│  2. Row Lock Active Period (L1):                                            │
│     SELECT ... FROM organization_billing_periods                            │
│     WHERE organization_id = :org_id AND is_active = true                    │
│     FOR UPDATE                                                              │
│     * If active Pro period exists:                                          │
│       - If now < period_end - 3 days: ROLLBACK T1 & RAISE ActiveEntitlement  │
│       - If within renewal window: continue to step 3                        │
│     * If zero active periods exist:                                         │
│       - Query returns 0 rows; zero row locks held. Proceed to step 3.       │
│  3. Revalidate Active Subscriptions (L2):                                   │
│     SELECT ... FROM subscriptions WHERE organization_id = :org_id           │
│     AND status IN ('active', 'trialing') FOR UPDATE                         │
│     * If active subscription found: ROLLBACK T1 & RAISE ActiveSubExists     │
│  4. Insert Durable Attempt:                                                 │
│     INSERT INTO billing_checkout_attempts (status='creating', lease=now+30s)│
│  5. COMMIT Transaction T1                                                   │
│     *** LOCK ON P1 RELEASED. ALL ROW LOCKS RELEASED. ***                    │
│     *** uq_org_scoped_active_billing_checkout_attempt IS HELD IN DB. ***    │
│                                                                             │
│  PHASE 3: EXTERNAL GATEWAY CALL (ZERO OPEN DB TRANSACTIONS)                 │
│  ─────────────────────────────────────────────────────────────────────────  │
│  - await client.create_subscription(...)                                    │
│  - Takes 500ms - 5000ms. Zero database connections or row locks open.       │
│                                                                             │
│  PHASE 4: REVALIDATION & SUCCESS PERSISTENCE (TRANSACTION T2)               │
│  ─────────────────────────────────────────────────────────────────────────  │
│  1. BEGIN Transaction T2                                                    │
│  2. SELECT attempt WHERE id = :attempt_id FOR UPDATE                        │
│  3. REVALIDATION UNDER T2 LOCK:                                             │
│     SELECT COUNT(*) FROM subscriptions                                      │
│     WHERE organization_id = :org_id AND status IN ('active', 'trialing')   │
│     * If a concurrent settlement activated a subscription during Phase 3:   │
│       - attempt.status = 'expired'                                          │
│       - attempt.error_message = 'concurrent_subscription_activated'         │
│       - COMMIT T2 & RETURN newly active subscription status!                │
│     * If still eligible:                                                    │
│       - attempt.status = 'open'                                             │
│       - attempt.provider_subscription_id = sub_id                           │
│       - COMMIT T2 & RETURN open checkout URL.                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 5.2 Deep-Dive Answers to the Five Checkout Concurrency Questions

#### Q1: Which billing-period row is locked?
- In Phase 1 of Transaction $T_1$, checkout executes:
  ```python
  stmt_period = (
      select(OrganizationBillingPeriod)
      .where(
          OrganizationBillingPeriod.organization_id == organization.id,
          OrganizationBillingPeriod.is_active == True,
      )
      .with_for_update()
  )
  ```
- Because of partial unique index `uq_org_active_billing_period`, at most one row can have `is_active = true`. Checkout locks **that exact active row ($P_1$)**.

#### Q2: How does it behave if there is no active period?
- If zero rows match `is_active = true`, `stmt_period` returns `None`.
- In PostgreSQL, row locking on an empty result set acquires **zero tuple locks**.
- **How mutual exclusion is preserved:**
  - Between concurrent checkouts: Worker 1 and Worker 2 both try to insert `BillingCheckoutAttempt(status='creating')`. PostgreSQL's partial unique index `uq_org_scoped_active_billing_checkout_attempt` forces Worker 2 to wait on index insertion; upon Worker 1's commit, Worker 2 immediately raises `IntegrityError` (converted to `CheckoutInProgressError`).
  - Between checkout and settlement: Settlement inserts a new period in `organization_billing_periods`. Checkout inserts an attempt in `billing_checkout_attempts`. **Checkout never inserts an `OrganizationBillingPeriod`**, so no unique constraint collision on billing periods can ever occur.

#### Q3: When is the lock released?
- The row lock on $P_1$ is released **at the exact moment Transaction $T_1$ commits** at the end of Phase 2 ([`checkout.py` line 416](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py#L416)).
- **Zero row locks are held during the external HTTP request (Phase 3).**

#### Q4: What is revalidated before the checkout attempt is committed?
- At the end of Phase 1 / start of Phase 2 (under $T_1$):
  1. $P_1$ expiry date is checked: if remaining duration $> 3$ days, raises `ActivePaidEntitlementExistsError`.
  2. Subscriptions table is queried `FOR UPDATE`: if any subscription is already `active` or `trialing`, raises `ActiveSubscriptionExistsError`.
- Only if both pass is the `creating` attempt inserted and committed.

#### Q5: What happens if a settlement or rollover changes entitlement during Phase 3?
- While the HTTP call is in flight, a settlement webhook for an out-of-band payment arrives and activates a subscription for the tenant.
- When the HTTP call finishes, Phase 4 opens Transaction $T_2$ and executes the **Phase 4 Revalidation Query**:
  ```python
  stmt_check_active = (
      select(Subscription)
      .where(
          Subscription.organization_id == organization.id,
          Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]),
      )
  )
  res_check = await db.execute(stmt_check_active)
  active_sub = res_check.scalar_one_or_none()

  if active_sub is not None:
      logger.info(
          "Subscription %s became active concurrently during checkout attempt %s; marking attempt expired.",
          active_sub.id,
          attempt_id,
      )
      attempt.status = CheckoutAttemptStatus.expired
      attempt.error_message = "concurrent_subscription_activated"
      await db.commit()
      raise ActiveSubscriptionExistsError(
          f"A subscription ({active_sub.id}) was activated while checkout was in progress."
      )
  ```
- **Outcome:** The attempt is cleanly closed as `expired`. The customer is **never** handed a redundant checkout link.

---

## 6. Remaining Decisions Requiring Product-Owner Approval

In strict compliance with guidelines, the following items are marked for explicit product-owner sign-off:

1. **Renewal Window Duration:**
   - *Technical Specification:* `RENEWAL_WINDOW_DAYS = 3` days.
   - *Product Decision:* Confirm whether customers should be permitted to renew starting 3 days prior to expiration, or if 7 days / 24 hours is preferred.
2. **Late Payment Degraded Period Threshold:**
   - *Technical Specification:* Payments arriving with $\le 3$ days remaining stage `PaymentReconciliation` for finance review rather than auto-provisioning a degraded period.
   - *Product Decision:* Confirm the 3-day threshold for late payment quarantine.
3. **Immediate Upgrade Consent UX:**
   - *Technical Specification:* If a customer with 15 days remaining insists on upgrading immediately, the frontend must present an explicit modal: *"Starting a new subscription now resets your billing cycle and starts your new period today."*
   - *Product Decision:* Confirm UI copy and flow for immediate upgrade resets.

---

## 7. Final Architectural Audit Verdict

All four issues are completely and definitively resolved:
- **Issue 1 (Interval Divergence):** Artificial shifting is rejected. Gateway Schedule Primacy is established as the sole authoritative policy.
- **Issue 2 (Partial Quota):** Existing invariant of full quota allocation is proven compliant with Full-Payment Settlement. Safe late-arrival quarantine ($\le 3$ days) prevents degraded service delivery.
- **Issue 3 (Stripe Provenance):** Heuristic guessing is eliminated. 100% deterministic CTE backfill with mandatory pre-commit abort gate is specified.
- **Issue 4 (Checkout Race):** Exact transaction boundaries traced ($T_1 \to \text{HTTP} \to T_2$). Phase 4 revalidation prevents stale attempt activation.

### Final Verdict: **READY FOR IMPLEMENTATION**
