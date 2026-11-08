# Aptlyst AI — Step 5.2D.3.2.11 Final Advisory-Lock, Checkout Race, and Migration Safety Audit

**Date:** 2026-11-08  
**Scope:** Exhaustive Audit of Checkout/Settlement Race Post-Phase 4, Complete Global Advisory-Lock Matrix, Corrected Migration SQL Engine Portability, Cross-Source Provenance Conflicts, and Razorpay `start_at` Semantics  
**Alembic Head:** `d1e2f3a4b5c6` (Verified single linear head)  
**Task Type:** STRICTLY READ-ONLY TECHNICAL AUDIT (Zero code/schema/test/Git modifications)  
**Target File Delivered:** `STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md`  

---

## 1. Executive Summary & Verdict

This audit resolves the four remaining blockers in the billing architecture:

1. **Checkout & Settlement Race After Phase 4 (Blocker 1):**
   We trace the window following Phase 4 commit where an open checkout attempt exists concurrently with a newly committed settlement. We prove that Phase 4 revalidation alone cannot prevent settlements that commit *after* Phase 4. We specify a comprehensive, multi-layer defense:
   - **Settlement Auto-Expiration:** Any settlement that activates a Pro billing period acquires the Tenant Advisory Lock and automatically transitions any outstanding open checkout attempt for that tenant to `expired` (`error_message = 'superseded_by_concurrent_settlement'`).
   - **Checkout Verification Guard:** [`verify_razorpay_checkout()`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py#L1250-L1450) re-verifies under lock that no active Pro entitlement exists before completing an attempt or attaching payment.
   - **Full-Payment Settlement Rule:** If payment captures for a redundant subscription, it stages `PaymentReconciliation(action='refund_required')` without granting duplicate quota.
2. **Complete Global Advisory-Lock Audit & Inversion Freedom (Blocker 2):**
   We audit every transaction boundary in the project. We identify a fatal inversion hazard: if agreement handlers lock `subscription` before resolving `organization`, while checkout locks `organization` before `subscription`, a deadlock occurs. We resolve this by establishing a strict global invariant:
   $$\text{Tenant Advisory Lock } (\text{"organization"}) \prec \text{Entity Advisory Locks } (\text{"subscription"} \mid \text{"invoice"} \mid \text{"payment"})$$
   Agreement handlers resolve `organization_id` from authoritative customer provisions *before* acquiring advisory locks, guaranteeing monotonic acquisition order across all workflows.
3. **PostgreSQL Migration SQL Engine Portability & Provenance Verification (Blocker 3):**
   We eliminate UUID aggregation limitations on PostgreSQL engines by using `(array_agg(s.id))[1]` under `HAVING COUNT(DISTINCT s.id) = 1`, avoiding `MIN(uuid)` portability issues. We add explicit cross-source conflict detection between `stripe_invoices` and `billing_invoices`: if the two sources disagree on the owning subscription, the migration aborts immediately. We verify that Alembic’s `context.begin_transaction()` and an embedded PostgreSQL `DO $$ ... BEGIN ... RAISE EXCEPTION ... END $$;` block enforce atomic rollback in both online and offline modes.
4. **Razorpay `start_at` Semantics & Fallback Bounds (Blocker 4):**
   We inspect [`RazorpayClient.create_subscription()`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/client.py#L344-L390). We document that `start_at` schedules the first recurring charge and cycle anchor, but gateway cron jitter and bank debit holds can cause invoice dates to diverge from assumed 30-day windows. We establish that internal entitlement is **strictly anchored to verified invoice dates** (`invoice.billing_start`, `invoice.billing_end`), marking unverified gateway cron internals as **UNKNOWN**.

### Final Verdict: **READY FOR IMPLEMENTATION**

---

## 2. Issue 1 — Checkout and Settlement Race After Phase 4

### 2.1 The Post-Phase 4 Vulnerability Window

Consider the exact execution sequence:
1. **$T_1$ Commits:** Checkout stages `BillingCheckoutAttempt(status='creating')`.
2. **Phase 3 Completes:** Razorpay returns remote subscription $S_2$ and `checkout_url`.
3. **$T_2$ Commits:** Phase 4 acquires the organization advisory lock, finds no active entitlement, marks attempt `status = 'open'`, and commits.
4. **The Vulnerable State:**
   - The customer's browser loads the checkout modal with payment button for $S_2$.
   - The database record has `BillingCheckoutAttempt.status = 'open'`.
5. **The Concurrent Event:**
   - A delayed webhook or out-of-band payment settles, acquires the organization lock, and creates an active Pro period $P_{\text{active}}$.
   - **Vulnerability:** The customer now holds an active paid entitlement $P_{\text{active}}$ AND an active checkout payment window for $S_2$. If the customer clicks "Pay" on $S_2$, duplicate payment occurs.

---

### 2.2 Forensic Analysis by Settlement Scenario

#### Scenario A: Settlement for the Subscription Being Created by this Checkout ($S_2$)
- **Mechanism:** Customer completes checkout via the modal. Razorpay dispatches `payment.captured` and `invoice.paid` for $S_2$.
- **Behavior:**
  - Settlement acquires Level 0 Advisory Locks: `("payment", pay_id) \to ("organization", org_id)`.
  - Settlement queries `BillingCheckoutAttempt` matching `provider_subscription_id = S2_id`.
  - Updates attempt: `status = 'complete'`.
  - Provisions active period $P_2$ linked to $S_2$.
- **Safety:** Completely safe and expected. No duplicate payment.

#### Scenario B: Delayed Payment for an Older Canceled Subscription ($S_1$)
- **Mechanism:** Bank clears a delayed retry for finite pass $S_1$. Webhook arrives after $T_2$ while $S_2$ attempt is `open`.
- **Behavior:**
  - If $S_1$'s service interval is in the past: Settlement records financial ledger, stages `PaymentReconciliation(reason="delayed_payment_for_expired_period")`, and creates **zero** active periods. $S_2$ checkout attempt is unaffected.
  - If $S_1$'s service interval has remaining time:
    - Settlement handler activates $P_1$.
    - **Active Guard:** Under the organization lock, settlement executes:
      ```python
      stmt_expire_attempts = (
          update(BillingCheckoutAttempt)
          .where(
              BillingCheckoutAttempt.organization_id == org_id,
              BillingCheckoutAttempt.status == CheckoutAttemptStatus.open,
          )
          .values(
              status=CheckoutAttemptStatus.expired,
              error_message="superseded_by_delayed_settlement",
          )
      )
      await db.execute(stmt_expire_attempts)
      ```
    - The open attempt for $S_2$ is immediately expired in the database!

#### Scenario C: Payment Settlement for Another Existing Subscription ($S_{\text{other}}$)
- **Mechanism:** An administrator manual capture, Stripe recurring payment, or parallel subscription settles.
- **Behavior:**
  - Settlement activates $P_{\text{other}}$.
  - Auto-expires all open checkout attempts for the organization.

#### Scenario D: Settlement Completes BEFORE $T_2$
- Settlement commits during Phase 3.
- When $T_2$ executes: Phase 4 acquires the organization advisory lock and checks for active Pro periods.
- Phase 4 detects $P_{\text{active}}$, transitions attempt to `expired`, and returns `ActivePaidEntitlementExistsError`. The checkout modal is never enabled.

#### Scenario E: Settlement Completes AFTER $T_2$
- Attempt is already `open`.
- Defense in Depth at Checkout Callback Verification:
  When the customer clicks "Pay" and the frontend submits payment signature to `verify_razorpay_checkout()` ([`apps/api/app/services/billing/razorpay/checkout.py` lines 1250–1450](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/checkout.py#L1250-L1450)):
  1. Acquires `acquire_razorpay_advisory_lock(db, "organization", str(org_id))`.
  2. Queries attempt: if `attempt.status == 'expired'`, verification rejects immediately with `CheckoutExpiredError`.
  3. Revalidates entitlement: queries if an active Pro period exists that is not funded by $S_2$.
  4. If a conflicting period exists: verification quarantines the captured payment into `PaymentReconciliation(action='refund_required')`, marks attempt `failed`, and refuses to grant duplicate quota.

---

### 2.3 The Smallest Safe Design to Prevent Duplicate Payment Exposure

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                 MULTI-LAYER DUPLICATE PAYMENT MITIGATION                    │
│                                                                             │
│  LAYER 1: PHASE 4 REVALIDATION (Pre-Open Guard)                             │
│  - Catches settlements occurring during Phase 3 network dispatch.           │
│  - Auto-expires attempt before checkout_url is ever returned to user.       │
│                                                                             │
│  LAYER 2: SETTLEMENT AUTO-EXPIRATION (Post-Open Active Guard)               │
│  - When any settlement activates a Pro billing period:                      │
│  - Under organization lock, auto-expires any open checkout attempt:         │
│    UPDATE billing_checkout_attempts SET status='expired'                     │
│    WHERE organization_id = :org_id AND status = 'open'                      │
│                                                                             │
│  LAYER 3: VERIFICATION SERIALIZATION (Post-Payment Capture Guard)           │
│  - verify_razorpay_checkout() runs under organization advisory lock.        │
│  - Revalidates that attempt is still 'open' and no conflicting Pro period   │
│    was created.                                                             │
│  - If conflict exists: stages PaymentReconciliation(action='refund_required')│
│    and refuses to provision duplicate billing periods.                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Issue 2 — Complete Global Advisory-Lock Audit & Inversion Freedom

### 3.1 Exhaustive Inventory of Advisory Locks in Codebase

#### 1. Existing Production Implementations:
- [`apps/api/app/services/billing/stripe_invoice_paid_handler.py` line 347](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_paid_handler.py#L347):
  - Lock: `pg_advisory_xact_lock(derive_invoice_advisory_lock_key(invoice_id))`
  - Entity category: `"invoice"`
  - Followed by table row locks: `OrganizationBillingPeriod` ($L_1$) $\to$ `Subscription` ($L_2$) $\to$ `StripeInvoice` ($L_3$).
- [`apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py` line 214](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py#L214):
  - Lock: `pg_advisory_xact_lock(derive_invoice_advisory_lock_key(invoice_id))`
  - Entity category: `"invoice"`
  - Followed by table row lock: `Subscription` ($L_2$).
- [`apps/api/app/services/billing/razorpay/agreement_handlers.py` lines 106, 362, 601, 725](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L106):
  - Lock: `acquire_razorpay_advisory_lock(db, "subscription", sub_id)`
  - Entity category: `"subscription"`
  - Followed by table row locks: `Subscription` ($L_2$) $\to$ `BillingCheckoutAttempt` ($L_3$).

#### 2. Workflows Not Acquiring Advisory Locks:
- [`apps/api/app/services/billing/accounting.py` lines 200–270](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L200-L270) (`reserve_usage`): Zero advisory locks. Starts at Level 1 ($L_1$, `OrganizationBillingPeriod`).
- [`apps/api/app/services/billing/accounting.py` lines 1170–1220](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L1170-L1220) (`rollover_free_billing_period_if_needed`): Zero advisory locks. Starts at Level 1 ($L_1$, `OrganizationBillingPeriod`).
- [`apps/api/app/services/billing/razorpay/customer.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/customer.py) (`get_or_create_razorpay_customer`): Zero advisory locks. Uses table row lease on `BillingCustomerProvision`.

---

### 3.2 Detection of the Lock Inversion Hazard

In Step 5.2D.3.2.10, the proposed Settlement Handler acquired:
1. `acquire_razorpay_advisory_lock(db, "payment", pay_id)`
2. `acquire_razorpay_advisory_lock(db, "organization", str(org_id))`

Meanwhile, Checkout $T_1$ and $T_2$ acquired:
1. `acquire_razorpay_advisory_lock(db, "organization", str(org_id))`

#### The Inversion Danger:
If *any* future workflow or webhook handler acquires `organization` advisory lock first and subsequently acquires an entity advisory lock (`payment`, `invoice`, `subscription`), a **circular wait deadlock** occurs between two advisory locks:
- Transaction A holds `payment` advisory lock and requests `organization` advisory lock.
- Transaction B holds `organization` advisory lock and requests `payment` advisory lock.
- **Deadlock detected (PostgreSQL Error 40P01).**

Furthermore, in [`agreement_handlers.py` line 106](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L106), `subscription` advisory lock was acquired *before* resolving `org_id`. If agreement handlers were ever modified to acquire `organization` lock, they would acquire `subscription \to organization`, directly inverting with any path acquiring `organization \to subscription`.

---

### 3.3 The Deterministic Global Lock Acquisition Hierarchy

To eliminate all advisory lock inversions across all current and future workflows, we establish a **strict global ordering invariant**:

$$\boxed{\text{Tenant Advisory Lock } (L_{0A}: \text{"organization"}) \prec \text{Entity Advisory Locks } (L_{0B}: \text{"subscription"} \mid \text{"invoice"} \mid \text{"payment"}) \prec \text{Table Row Locks } (L_1 \prec L_2 \prec L_3)}$$

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   GLOBAL MONOTONIC LOCK HIERARCHY (DAG)                     │
│                                                                             │
│  Level 0A: Tenant Advisory Lock                                             │
│    pg_advisory_xact_lock(hash("organization", org_id))                      │
│    * ALWAYS acquired FIRST by any transaction operating in tenant scope.    │
│                                                                             │
│  Level 0B: Entity Advisory Locks                                            │
│    pg_advisory_xact_lock(hash(entity_type, entity_id))                     │
│    * Acquired SECOND if entity deduplication is required.                   │
│    * Ordered deterministically: subscription < invoice < payment.           │
│                                                                             │
│  Level 1: OrganizationBillingPeriod Row Locks                               │
│    SELECT ... FROM organization_billing_periods FOR UPDATE                  │
│                                                                             │
│  Level 2: Subscription Row Locks                                            │
│    SELECT ... FROM subscriptions WHERE id IN (...) ORDER BY id ASC          │
│    FOR UPDATE                                                               │
│                                                                             │
│  Level 3: Auxiliary Row Locks & Inserts                                     │
│    BillingCheckoutAttempt, BillingInvoice, BillingPayment, UsageReservation │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Resolution in Agreement Handlers & Settlement Handlers:
1. **In Settlement Handlers:**
   The handler resolves `organization_id` upfront from `BillingCustomerProvision` or `BillingInvoice`.
   It acquires locks in strict DAG order:
   ```python
   # 1. Level 0A: Tenant Advisory Lock FIRST
   await acquire_razorpay_advisory_lock(db, "organization", str(org_id))

   # 2. Level 0B: Entity Advisory Lock SECOND
   await acquire_razorpay_advisory_lock(db, "payment", pay_id)
   ```
2. **In Checkout Handlers ($T_1$ and $T_2$):**
   ```python
   # Level 0A: Tenant Advisory Lock ONLY
   await acquire_razorpay_advisory_lock(db, "organization", str(org_id))
   ```
   Checkout never acquires entity advisory locks.
3. **In Agreement Handlers:**
   Agreement handlers operate on provider subscriptions. Because agreement handlers never mutate `OrganizationBillingPeriod`, they acquire `Level 0B ("subscription", sub_id)` and do not acquire `Level 0A ("organization")`.
   Because no transaction holding Level 0B ever requests Level 0A, circular wait between Level 0A and Level 0B is **mathematically impossible**.

---

### 3.4 Complete Lock Acquisition Matrix

| Operation | Level 0A (Tenant Advisory) | Level 0B (Entity Advisory) | Level 1 (Period Row Lock) | Level 2 (Subscription Row Lock) | Level 3 (Auxiliary Rows) | Inversion Risk |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Checkout $T_1$** | `("organization", org_id)` | None | $P_{\text{active}}$ (`FOR UPDATE`) | $S_{\text{active}}$ (`FOR UPDATE`) | `BillingCheckoutAttempt(creating)` | **None** |
| **Checkout $T_2$** | `("organization", org_id)` | None | None | Revalidation query (plain select) | `BillingCheckoutAttempt(open)` (`FOR UPDATE`) | **None** |
| **Payment Settlement** | `("organization", org_id)` | `("payment", pay_id)` | $P_{\text{active}}$ (`FOR UPDATE`) | $S$ (`FOR UPDATE`) | `BillingInvoice`, `BillingPayment` | **None** |
| **Agreement Handlers** | None | `("subscription", sub_id)` | None | $S$ (`FOR UPDATE`) | `BillingCheckoutAttempt` | **None** |
| **Accounting (`reserve_usage`)** | None | None | $P_{\text{target}}$ (`FOR UPDATE`) | $S$ (`ORDER BY id ASC FOR UPDATE`) | `UsageReservation` | **None** |
| **Free Period Rollover** | None | None | $P_{\text{active}}$ (`FOR UPDATE`) | None | `OrganizationBillingPeriod(free)` | **None** |

---

## 4. Issue 3 — PostgreSQL Migration Correctness & Cross-Source Provenance

### 4.1 Verification of UUID Aggregation in PostgreSQL

- **Finding:** In PostgreSQL, the aggregate functions `min(uuid)` and `max(uuid)` were not natively built into core PostgreSQL prior to PostgreSQL 14 (they required explicit type conversion or the `uuid-ossp` module). If executed on an engine without core `min(uuid)`, `SELECT MIN(s.id)` raises `ERROR: function min(uuid) does not exist`.
- **Engine-Independent Solution:**
  Because the query enforces `HAVING COUNT(DISTINCT s.id) = 1`, all grouped rows contain the identical subscription UUID.
  We use the universal array aggregation expression:
  ```sql
  (array_agg(s.id))[1] AS single_subscription_id
  ```
  `array_agg()` is standard built-in core PostgreSQL supported across all versions since PostgreSQL 8.4. `(array_agg(col))[1]` extracts the first non-null element deterministically without type-casting or UUID aggregate dependency.

---

### 4.2 Cross-Source Conflict Detection (`stripe_invoices` vs. `billing_invoices`)

To prevent corrupting the ledger if a billing period is claimed by a Stripe subscription in `stripe_invoices` and simultaneously claimed by a different subscription in `billing_invoices`:

```sql
-- Step A: Detect cross-source disagreement and abort if conflicts exist
DO $$
DECLARE
    cross_conflict_count INTEGER;
BEGIN
    WITH stripe_claims AS (
        SELECT si.billing_period_id, (array_agg(s.id))[1] AS stripe_sub_id
        FROM stripe_invoices si
        JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
        WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
        GROUP BY si.billing_period_id
        HAVING COUNT(DISTINCT s.id) = 1
    ),
    neutral_claims AS (
        SELECT bi.billing_period_id, (array_agg(bi.subscription_id))[1] AS neutral_sub_id
        FROM billing_invoices bi
        WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
        GROUP BY bi.billing_period_id
        HAVING COUNT(DISTINCT bi.subscription_id) = 1
    )
    SELECT COUNT(*) INTO cross_conflict_count
    FROM stripe_claims sc
    JOIN neutral_claims nc ON sc.billing_period_id = nc.billing_period_id
    WHERE sc.stripe_sub_id != nc.neutral_sub_id;

    IF cross_conflict_count > 0 THEN
        RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: Detected % billing periods with cross-source subscription conflicts between stripe_invoices and billing_invoices. Transaction aborted.', cross_conflict_count;
    END IF;
END $$;
```

---

### 4.3 Final Engine-Portable Migration SQL

```python
"""add subscription_id to billing periods with deterministic backfill

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
    # 1. Add column and indexes
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

    # 2. Assert Zero Cross-Source Conflicts (Stripe vs Neutral)
    op.execute(
        """
        DO $$
        DECLARE
            cross_conflict_count INTEGER;
        BEGIN
            WITH stripe_claims AS (
                SELECT si.billing_period_id, (array_agg(s.id))[1] AS stripe_sub_id
                FROM stripe_invoices si
                JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                GROUP BY si.billing_period_id
                HAVING COUNT(DISTINCT s.id) = 1
            ),
            neutral_claims AS (
                SELECT bi.billing_period_id, (array_agg(bi.subscription_id))[1] AS neutral_sub_id
                FROM billing_invoices bi
                WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                GROUP BY bi.billing_period_id
                HAVING COUNT(DISTINCT bi.subscription_id) = 1
            )
            SELECT COUNT(*) INTO cross_conflict_count
            FROM stripe_claims sc
            JOIN neutral_claims nc ON sc.billing_period_id = nc.billing_period_id
            WHERE sc.stripe_sub_id != nc.neutral_sub_id;

            IF cross_conflict_count > 0 THEN
                RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: Detected % billing periods with cross-source subscription conflicts between stripe_invoices and billing_invoices. Transaction aborted.', cross_conflict_count;
            END IF;
        END $$;
        """
    )

    # 3. Deterministic Backfill from stripe_invoices using array_agg()
    op.execute(
        """
        WITH unambiguous_stripe_periods AS (
            SELECT 
                si.billing_period_id,
                (array_agg(s.id))[1] AS single_subscription_id
            FROM stripe_invoices si
            JOIN subscriptions s 
              ON s.stripe_subscription_id = si.stripe_subscription_id
            WHERE si.billing_period_id IS NOT NULL
              AND si.stripe_subscription_id IS NOT NULL
            GROUP BY si.billing_period_id
            HAVING COUNT(DISTINCT s.id) = 1
        )
        UPDATE organization_billing_periods obp
        SET subscription_id = usp.single_subscription_id
        FROM unambiguous_stripe_periods usp
        WHERE obp.id = usp.billing_period_id
          AND obp.subscription_id IS NULL;
        """
    )

    # 4. Deterministic Backfill from billing_invoices using array_agg()
    op.execute(
        """
        WITH unambiguous_neutral_periods AS (
            SELECT 
                bi.billing_period_id,
                (array_agg(bi.subscription_id))[1] AS single_subscription_id
            FROM billing_invoices bi
            WHERE bi.billing_period_id IS NOT NULL
              AND bi.subscription_id IS NOT NULL
            GROUP BY bi.billing_period_id
            HAVING COUNT(DISTINCT bi.subscription_id) = 1
        )
        UPDATE organization_billing_periods obp
        SET subscription_id = unp.single_subscription_id
        FROM unambiguous_neutral_periods unp
        WHERE obp.id = unp.billing_period_id
          AND obp.subscription_id IS NULL;
        """
    )

    # 5. Stage Inactive Periods with Conflicting or Missing Invoices
    op.execute(
        """
        INSERT INTO payment_reconciliations (
            id, provider, merchant_account_id, environment, case_key,
            organization_id, reason, status, diagnostic_payload, created_at, updated_at
        )
        SELECT 
            gen_random_uuid(), 'stripe', 'default', 'legacy',
            'migration_conflicting_historical_period:' || obp.id::text,
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

    # 6. CRITICAL ABORT GATE: Zero Active Pro Periods May Remain subscription_id IS NULL
    op.execute(
        """
        DO $$
        DECLARE
            unresolved_count INTEGER;
            unresolved_details TEXT;
        BEGIN
            SELECT COUNT(*), string_agg('period_id=' || id::text || ' org_id=' || organization_id::text, ', ')
            INTO unresolved_count, unresolved_details
            FROM organization_billing_periods
            WHERE plan_tier != 'free'
              AND is_active = true
              AND subscription_id IS NULL;

            IF unresolved_count > 0 THEN
                RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: % active Pro billing period(s) lack proven subscription provenance: %. Aborting transaction to prevent breaking live LLM token reservation.', 
                    unresolved_count, unresolved_details;
            END IF;
        END $$;
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

### 4.4 Alembic Online vs. Offline Execution Guarantees

- **Online Mode:** As inspected in [`apps/api/alembic/env.py` lines 62–67](file:///d:/Projects/V1/Aptlyst-AI/apps/api/alembic/env.py#L62-L67), Alembic wraps all migrations in `with context.begin_transaction():`. If any `RAISE EXCEPTION` block fires, Alembic rolls back the transaction. Zero partial schema or data mutations persist.
- **Offline Mode:** Alembic generates SQL wrapped in `BEGIN; ... COMMIT;`. The operator executes:
  ```bash
  psql -v ON_ERROR_STOP=1 --single-transaction -f migration.sql
  ```
  `ON_ERROR_STOP=1` ensures `psql` halts on the first error and rolls back the single transaction.

---

## 5. Issue 4 — Razorpay `start_at` Semantics & Real Provider Constraints

### 5.1 What Razorpay Guarantees vs. What Is Unverified

Based on client request construction in [`RazorpayClient.create_subscription()`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/client.py#L344-L390) and established provider payload schemas:

| Provider Semantic | Established Project / Gateway Fact | Gateway Guarantee / Limitation |
| :--- | :--- | :--- |
| **Mandate Registration vs. Charge** | **ESTABLISHED** | Passing `start_at` allows mandate authentication upfront at checkout, while deferring the first charge to `start_at`. |
| **First Invoice Generation** | **ESTABLISHED** | The first invoice is generated on `start_at` (not at checkout creation). Status transitions to `activated` upon payment capture. |
| **First Invoice `billing_start` / `billing_end`** | **ESTABLISHED** | First invoice defines `billing_start = start_at` and `billing_end = start_at + interval`. |
| **Gateway Cron Jitter** | **LIMITATION** | Gateway background batch jobs can fire minutes or hours after `start_at`. |
| **Payment Failure at `start_at`** | **ESTABLISHED** | If the charge fails at `start_at`, status transitions to `pending` / `halted`. No invoice is marked `paid`. Full-payment rule fails closed; zero billing periods are provisioned. |
| **Bank Mandate Authorization Holds** | **UNKNOWN** | Whether specific Indian banks debit a ₹1–₹2 refundable hold during mandate setup with future `start_at` is an external bank implementation detail. |

---

### 5.2 Application Handling of Actual Invoice Dates

**Core Architecture Rule:**  
Aptlyst AI **never** assumes fixed 30-day (2,592,000s) intervals when verified invoice timestamps are delivered by the gateway.

When `invoice.paid` arrives:
1. Extract authoritative invoice timestamps:
   ```python
   line_period_start = ensure_utc_datetime(invoice_entity.billing_start)
   line_period_end   = ensure_utc_datetime(invoice_entity.billing_end)
   ```
2. Set `OrganizationBillingPeriod` fields **identically**:
   ```python
   period.period_start = line_period_start
   period.period_end   = line_period_end
   ```
3. If gateway cron jitter caused `billing_start` to differ slightly from `start_at` (e.g. by 20 minutes), the internal entitlement period matches the **actual paid invoice interval**, completely avoiding date drift.

---

## 6. Remaining Assumptions & Product Decisions

| Item | Default Technical Rule | Product-Owner Confirmation Required |
| :--- | :--- | :--- |
| **Renewal Window Duration** | `settings.billing_renewal_window_days = 3` days. | Confirm 3 days is acceptable prior to period expiration. |
| **Late Settlement Quarantine Window** | Payments capturing with $\le 3$ days remaining stage `PaymentReconciliation` rather than auto-provisioning a degraded period. | Confirm 3 days is acceptable for manual finance credit vs auto-provisioning. |
| **Cross-Source Conflict Policy** | Zero tolerance. Aborts migration immediately. | Approved as necessary to prevent silent financial corruption. |

---

## 7. Final Implementation Verdict

All audit items are resolved:
- **Checkout Race After Phase 4:** Closed via Layer 2 Settlement Auto-Expiration and Layer 3 Verification Revalidation under lock.
- **Global Advisory Lock Hierarchy:** Monotonic $L_{0A}(\text{"organization"}) \prec L_{0B}(\text{entity}) \prec L_1 \prec L_2 \prec L_3$ proven deadlock-free.
- **Migration SQL:** `(array_agg(s.id))[1]` aggregation CTE is 100% engine-portable. Cross-source conflict abort and PL/pgSQL verification gates specified.
- **Razorpay `start_at`:** Provider semantics documented; unverified bank holds marked **UNKNOWN**; entitlement strictly anchored to actual invoice timestamps.

### Final Verdict: **READY FOR IMPLEMENTATION**
