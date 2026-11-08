# Aptlyst AI — Step 5.2D.3.3A Implementation Report
# Payment Association and Entitlement Provenance Schema Migration

**Date:** 2026-11-08  
**Phase:** Step 5.2D.3.3A  
**Alembic Revision ID:** `e2f3a4b5c6d1`  
**Down Revision ID:** `d1e2f3a4b5c6`  
**Target Migration File:** `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py`  
**Current Alembic Head:** `e2f3a4b5c6d1 (head)` (Verified single linear DAG)  
**Test Suite Status:** 634 passed, 0 failures (17 dedicated migration/model tests)  

---

## 1. Executive Summary

This report documents the completion of **Step 5.2D.3.3A (Payment Association and Entitlement Provenance Schema Migration)**. 

### Scope Adherence & Strict Boundaries
- **Strictly Limited Scope:** The implementation was strictly confined to SQLAlchemy models (`billing.py`, `subscription.py`, `payment.py`), an Alembic schema migration (`e2f3a4b5c6d1`), database constraints, scoped partial indexes, PL/pgSQL immutability triggers, and comprehensive offline/model test suites.
- **Zero Premature Business Handlers:** Neither the payment settlement handlers (`apps/api/app/services/billing/razorpay/settlement_handlers.py`) nor the payment-binding background sweeper (`apps/api/app/workers/payment_binding_sweeper.py`) were created or modified in this phase.
- **Zero External API / Database Connections:** No calls were made to live Razorpay APIs or live Supabase databases. All migrations and DDL checks were validated via Alembic offline SQL generation (`--sql`) and local virtualenv test execution.
- **Preservation of Repository Modifications:** All pre-existing untracked files and working tree modifications were preserved intact (`git status --short`).

---

## 2. Schema & Model Architecture Changes

### 2.1 `organization_billing_periods` (Entitlement Provenance)

| Element | Type / Definition | Purpose / Enforcement |
| :--- | :--- | :--- |
| **Column:** `subscription_id` | `UUID`, `nullable=True` | Stores the authoritative originating subscription for the billing period. Nullable to support `free` plan tiers. |
| **Foreign Key:** `fk_billing_periods_subscription_id` | `FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE RESTRICT` | Prevents cascading deletion or deletion of an active/historical subscription that generated token quotas. |
| **Index:** `ix_billing_periods_subscription_id` | `Index('ix_billing_periods_subscription_id', 'subscription_id')` | High-speed lookup of periods by subscription during checkout renewal and agreement completion audits. |
| **Relationship:** `subscription` | `Mapped["Subscription \| None"] = relationship(back_populates="billing_periods")` | Bidirectional ORM navigation from billing period to its parent subscription. |
| **Immutability Trigger:** `trg_prevent_billing_period_subscription_mutation` | `BEFORE UPDATE ON organization_billing_periods` | Rejects mutation if `OLD.subscription_id IS NOT NULL AND NEW.subscription_id IS DISTINCT FROM OLD.subscription_id`. |

### 2.2 `billing_checkout_attempts` (Checkout Identity & Direct Association)

| Element | Type / Definition | Purpose / Enforcement |
| :--- | :--- | :--- |
| **Column:** `provider_payment_id` | `VARCHAR(255)`, `nullable=True` | Stores the gateway payment ID (`pay_...`) that settled this attempt. |
| **Partial Unique Index:** `uq_checkout_attempts_provider_sub` | `UNIQUE (provider, merchant_account_id, environment, provider_subscription_id) WHERE provider_subscription_id IS NOT NULL` | Ensures exactly one checkout attempt can claim a provider subscription ID per tenant scope. |
| **Partial Unique Index:** `uq_checkout_attempts_provider_payment` | `UNIQUE (provider, merchant_account_id, environment, provider_payment_id) WHERE provider_payment_id IS NOT NULL` | Ensures one captured payment cannot be claimed by multiple checkout attempts. |
| **Composite Unique Constraint:** `uq_bca_composite_identity` | `UNIQUE (id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)` | Provides the multi-column unique target required by the composite foreign key in `billing_payments`. |
| **Immutability Trigger:** `trg_prevent_checkout_attempt_payment_mutation` | `BEFORE UPDATE ON billing_checkout_attempts` | Rejects modification of `provider_payment_id` once populated. |

### 2.3 `billing_payments` (Payment-to-Attempt Direct Association)

| Element | Type / Definition | Purpose / Enforcement |
| :--- | :--- | :--- |
| **Column:** `checkout_attempt_id` | `UUID`, `nullable=True` | Direct foreign key referencing `billing_checkout_attempts.id`. Nullable for recurring monthly renewals. |
| **Simple Foreign Key:** `fk_billing_payments_checkout_attempt` | `FOREIGN KEY (checkout_attempt_id) REFERENCES billing_checkout_attempts(id) ON DELETE RESTRICT` | Relational integrity link between payment and checkout attempt. |
| **Index:** `ix_billing_payments_checkout_attempt` | `Index('ix_billing_payments_checkout_attempt', 'checkout_attempt_id')` | Efficient lookup of payments by attempt ID. |
| **Partial Unique Index:** `uq_billing_payments_checkout_attempt` | `UNIQUE (checkout_attempt_id) WHERE checkout_attempt_id IS NOT NULL` | Enforces that one checkout attempt can settle at most **one** captured payment. |
| **Check Constraint:** `ck_billing_payments_checkout_attempt_complete` | `CHECK ((checkout_attempt_id IS NULL) OR (provider IS NOT NULL AND merchant_account_id IS NOT NULL AND environment IS NOT NULL AND provider_subscription_id IS NOT NULL AND provider_payment_id IS NOT NULL))` | Closes the PostgreSQL `MATCH SIMPLE` composite foreign key loophole by guaranteeing all 5 provider columns are non-null when linked. |
| **Composite Foreign Key:** `fk_billing_payments_checkout_attempt_composite` | `FOREIGN KEY (checkout_attempt_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id) REFERENCES billing_checkout_attempts(...) ON DELETE RESTRICT` | Compile-time and database-time guarantee that payment and attempt share identical provider, account, environment, subscription ID, and payment ID. |
| **Immutability Trigger:** `trg_prevent_billing_payment_checkout_attempt_mutation` | `BEFORE UPDATE ON billing_payments` | Rejects mutation if `OLD.checkout_attempt_id IS NOT NULL AND NEW.checkout_attempt_id IS DISTINCT FROM OLD.checkout_attempt_id`. |

### 2.4 Resolution of Dual Foreign Key Ambiguity in SQLAlchemy ORM

When both a scalar foreign key (`fk_billing_payments_checkout_attempt`) and a 6-column composite foreign key (`fk_billing_payments_checkout_attempt_composite`) exist between `billing_payments` and `billing_checkout_attempts`, SQLAlchemy mapper initialization fails with `InvalidRequestError: Could not determine join condition between parent/child tables on relationship BillingCheckoutAttempt.payments - there are multiple foreign key paths linking the tables`.

**Resolution Implemented:**
In `apps/api/app/models/payment.py`, explicit `foreign_keys` parameters were declared on both sides of the relationship:
```python
# In BillingCheckoutAttempt:
payments: Mapped[list["BillingPayment"]] = relationship(
    back_populates="checkout_attempt",
    foreign_keys="[BillingPayment.checkout_attempt_id]",
)

# In BillingPayment:
checkout_attempt: Mapped["BillingCheckoutAttempt | None"] = relationship(
    back_populates="payments",
    foreign_keys="[BillingPayment.checkout_attempt_id]",
)
```
This instructs SQLAlchemy's relationship loader to use the primary surrogate key join path for ORM identity navigation, while PostgreSQL enforces both relational constraints at the database engine level.

---

## 3. Backfill Strategy & Historical Stripe Entitlement Provenance

### 3.1 Replacement of Invalid Window Function
In Step 5.2D.3.2.9, the proposed backfill query contained `COUNT(DISTINCT s.id) OVER (PARTITION BY si.billing_period_id)`. In PostgreSQL (all versions through 16+), window functions do not support `DISTINCT` aggregates (`ERROR: DISTINCT is not implemented for window functions`).

In migration `e2f3a4b5c6d1`, this was replaced with standard ANSI SQL-92 aggregated Common Table Expressions (`GROUP BY ... HAVING COUNT(DISTINCT ...) = 1`):

```sql
-- Step 1: Deterministic Backfill for Unambiguous 1:1 and N:1 Stripe Invoices
WITH unambiguous_stripe_periods AS (
    SELECT 
        si.billing_period_id,
        MIN(s.id) AS single_subscription_id
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

-- Step 2: Deterministic Backfill for Multi-Provider Invoices (billing_invoices)
WITH unambiguous_neutral_periods AS (
    SELECT 
        bi.billing_period_id,
        MIN(bi.subscription_id) AS single_subscription_id
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
```

### 3.2 Handling Edge Cases
- **Unambiguous 1:1:** Period linked to single invoice $\to$ backfilled with `s.id`.
- **Duplicate Invoices (N:1):** Multiple invoices for same period with identical subscription $\to$ `COUNT(DISTINCT s.id) = 1`, deterministically backfilled with `MIN(s.id)`.
- **Conflicting Candidates (N:M):** Invoices with $\ge 2$ different subscriptions $\to$ filtered out by `HAVING` clause, leaving `subscription_id = NULL` to be caught by the Abort Gate.
- **Unmatched External IDs / Missing Fields:** Filtered out by `WHERE` and `JOIN`, leaving `subscription_id = NULL`.
- **Free Tier Billing Periods:** `plan_tier = 'free'` legitimately has `subscription_id = NULL`.

### 3.3 Transactional Abort Gate (Online and Offline Modes)
To guarantee zero production deployments proceed if an active Pro period lacks proven subscription provenance, the migration executes a PL/pgSQL assertion block:

```sql
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
```
- **Online Execution (`alembic upgrade head`):** Propagates error to Python and automatically triggers `ROLLBACK`.
- **Offline Execution (`alembic upgrade ... --sql`):** Emitted within the transaction block `BEGIN; ... COMMIT;`. Executing via `psql` halts on exception and aborts without committing.

---

## 4. Composite Foreign Key & Constraint Architecture

### 4.1 Closing the PostgreSQL `MATCH SIMPLE` Loophole
Under SQL standard and PostgreSQL's default `MATCH SIMPLE` semantics:
> If any referencing column of a multi-column foreign key is `NULL`, the entire foreign key check is skipped.

If a developer or rogue query inserted `BillingPayment(checkout_attempt_id=uuid, provider=None, ...)`, the composite foreign key would be bypassed.  
To prevent this, `ck_billing_payments_checkout_attempt_complete` explicitly enforces:
```sql
CHECK (
    (checkout_attempt_id IS NULL) OR (
        provider IS NOT NULL AND
        merchant_account_id IS NOT NULL AND
        environment IS NOT NULL AND
        provider_subscription_id IS NOT NULL AND
        provider_payment_id IS NOT NULL
    )
)
```
When `checkout_attempt_id IS NOT NULL`, all 5 composite attributes must be populated, forcing the composite foreign key `fk_billing_payments_checkout_attempt_composite` to validate all 6 columns against `billing_checkout_attempts(uq_bca_composite_identity)`.

### 4.2 Support for Recurring Renewals
Because `checkout_attempt_id` is nullable and `uq_billing_payments_checkout_attempt` is a partial unique index (`WHERE checkout_attempt_id IS NOT NULL`), subsequent recurring subscription renewals (which do not originate from an interactive browser checkout attempt) store `checkout_attempt_id = NULL` without constraint violations.

---

## 5. Immutability Triggers Specification

Financial auditability requires that once an association is established, it cannot be tampered with. Three PostgreSQL `BEFORE UPDATE` triggers enforce this:

1. **`trg_prevent_billing_payment_checkout_attempt_mutation` on `billing_payments`:**
   ```sql
   IF OLD.checkout_attempt_id IS NOT NULL AND NEW.checkout_attempt_id IS DISTINCT FROM OLD.checkout_attempt_id THEN
       RAISE EXCEPTION 'Immutability violation: billing_payments.checkout_attempt_id cannot be reassigned once established (attempted change from % to %)',
           OLD.checkout_attempt_id, NEW.checkout_attempt_id;
   END IF;
   ```
2. **`trg_prevent_checkout_attempt_payment_mutation` on `billing_checkout_attempts`:**
   ```sql
   IF OLD.provider_payment_id IS NOT NULL AND NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id THEN
       RAISE EXCEPTION 'Immutability violation: billing_checkout_attempts.provider_payment_id cannot be reassigned once established (attempted change from % to %)',
           OLD.provider_payment_id, NEW.provider_payment_id;
   END IF;
   ```
3. **`trg_prevent_billing_period_subscription_mutation` on `organization_billing_periods`:**
   ```sql
   IF OLD.subscription_id IS NOT NULL AND NEW.subscription_id IS DISTINCT FROM OLD.subscription_id THEN
       RAISE EXCEPTION 'Immutability violation: organization_billing_periods.subscription_id cannot be reassigned once established (attempted change from % to %)',
           OLD.subscription_id, NEW.subscription_id;
   END IF;
   ```

---

## 6. Concurrency-Safe Downgrade Guard

To prevent accidental data loss of financial and entitlement links during an administrative rollback, the `downgrade()` function acquires `ACCESS EXCLUSIVE` table locks and asserts zero linked records exist:

```sql
DO $$
DECLARE
    cnt_linked_payments INTEGER;
    cnt_linked_periods INTEGER;
    cnt_linked_attempts INTEGER;
    total_records INTEGER;
BEGIN
    LOCK TABLE billing_payments IN ACCESS EXCLUSIVE MODE;
    LOCK TABLE organization_billing_periods IN ACCESS EXCLUSIVE MODE;
    LOCK TABLE billing_checkout_attempts IN ACCESS EXCLUSIVE MODE;

    SELECT count(*) INTO cnt_linked_payments FROM billing_payments WHERE checkout_attempt_id IS NOT NULL;
    SELECT count(*) INTO cnt_linked_periods FROM organization_billing_periods WHERE subscription_id IS NOT NULL;
    SELECT count(*) INTO cnt_linked_attempts FROM billing_checkout_attempts WHERE provider_payment_id IS NOT NULL;

    total_records := cnt_linked_payments + cnt_linked_periods + cnt_linked_attempts;
    IF total_records > 0 THEN
        RAISE EXCEPTION 
            'Alembic Downgrade Aborted: % associated records exist (linked payments: %, linked periods: %, linked attempts: %). Downgrading payment association schema is strictly forbidden to preserve financial provenance.',
            total_records, cnt_linked_payments, cnt_linked_periods, cnt_linked_attempts;
    END IF;
END $$;
```
If clean, all triggers, functions, foreign keys, check constraints, partial indexes, and columns are dropped in reverse dependency order.

---

## 7. Verification & Test Execution Results

### 7.1 Dedicated Migration & Model Test Suite
File: `apps/api/tests/test_payment_association_migration.py`  
Result: **17 passed in 3.93s**

| Test Name | Verification Focus | Result |
| :--- | :--- | :---: |
| `test_migration_metadata` | Revision ID `e2f3a4b5c6d1`, down revision `d1e2f3a4b5c6` | Passed |
| `test_offline_upgrade_ddl_generation` | Offline DDL contains all columns, constraints, CTE backfill, PL/pgSQL abort gate, triggers, RLS | Passed |
| `test_offline_downgrade_ddl_generation` | Offline DDL contains downgrade guard, locks, reverse drop order | Passed |
| `test_organization_billing_period_model_provenance_instantiation` | Model instantiation with `subscription_id` (free=None, pro=UUID) | Passed |
| `test_organization_billing_period_table_args` | `ix_billing_periods_subscription_id` present in `__table_args__` | Passed |
| `test_billing_checkout_attempt_payment_id_instantiation` | Model instantiation with `provider_payment_id` | Passed |
| `test_billing_checkout_attempt_table_args_constraints` | `uq_bca_composite_identity`, `uq_checkout_attempts_provider_sub`, `uq_checkout_attempts_provider_payment` | Passed |
| `test_billing_payment_checkout_attempt_instantiation` | Model instantiation with `checkout_attempt_id` | Passed |
| `test_billing_payment_table_args_constraints` | `ck_billing_payments_checkout_attempt_complete`, `fk_billing_payments_checkout_attempt_composite`, `uq_billing_payments_checkout_attempt` | Passed |
| `test_sqlalchemy_mapper_relationships_configuration` | Disambiguated `foreign_keys` mapper initialization prevents `InvalidRequestError` | Passed |
| `test_subscription_and_billing_period_relationship_bidirectional` | Bidirectional ORM navigation `Subscription <-> OrganizationBillingPeriod` | Passed |
| `test_checkout_attempt_and_payment_relationship_bidirectional` | Bidirectional ORM navigation `BillingCheckoutAttempt <-> BillingPayment` | Passed |
| `test_check_constraint_sql_text_structure` | Exact SQL expression validation for composite null-bypass guard | Passed |
| `test_composite_foreign_key_column_alignment` | 6 source columns match 6 target columns in composite FK | Passed |
| `test_composite_unique_identity_column_alignment` | 6 target columns in `uq_bca_composite_identity` | Passed |
| `test_metadata_table_columns_and_nullability` | Column existence, data types, and nullability across `Base.metadata.tables` | Passed |
| `test_absence_of_settlement_handlers_or_sweeper_in_step_3_3a` | Confirms payment settlement handlers and sweeper files are not present | Passed |

### 7.2 Full Billing Test Suite Status
Executed across the entire billing module:
- `test_payment_association_migration.py`: 17 passed
- `test_provider_neutral_billing_models.py`: 15 passed
- `test_provider_neutral_migration.py`: 9 passed
- `test_billing_api.py`: 44 passed
- `test_razorpay*.py`: 187 passed
- `test_stripe*.py`: 362 passed
- **Total:** **634 passed, 0 failures**

---

## 8. Implementation Gate Readiness for Step 5.2D.3.3B

With the completion and verification of Step 5.2D.3.3A, all database-level prerequisites for Razorpay payment settlement are fulfilled:

1. **Immutable Association Anchor:** `billing_payments.checkout_attempt_id` and composite foreign key `fk_billing_payments_checkout_attempt_composite` guarantee unambiguous bidirectional linkage between checkout sessions and captured payments.
2. **Entitlement Provenance Proven:** `organization_billing_periods.subscription_id` establishes durable provenance from payment capture to token quota granting.
3. **Immutability Enforced:** Triggers protect established relationships from retroactive tampering or accidental overwrites.
4. **Clean Implementation Gate:** The repository is cleanly poised for **Step 5.2D.3.3B (Razorpay Settlement Handlers)** to implement:
   - `handle_payment_captured()` handler
   - `handle_invoice_paid()` handler
   - Level $0A \prec Level\ 0B \prec Level\ 1 \prec Level\ 2 \prec Level\ 3$ transaction-scoped lock acquisition order
   - Fast-path attempt completion and late-capture post-expiration handling
   - `PaymentBindingSweeper` background worker for payment-first retry recovery
