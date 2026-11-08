# ADR-004: Payment Association Provenance, Composite Foreign Keys, and Conflict Gates

## Status
**Accepted** (Architectural Design Approved; Migration & Schema Implemented; PostgreSQL CI Execution Pending Step 5.2D.3.3A.2)

## Context
When migrating historical Stripe invoices and establishing Razorpay payment settlement records, establishing an immutable audit trail between checkout attempts, payments, invoices, subscriptions, and billing periods is critical.

Several integrity vulnerabilities were identified in initial migration drafts:
1. **PostgreSQL Composite Foreign Key Null Bypass (`MATCH SIMPLE`)**:
   PostgreSQL defaults to `MATCH SIMPLE` for composite foreign keys. Under this behavior, if any column in a composite foreign key tuple is `NULL`, foreign key validation is completely bypassed for the entire tuple. For instance, a composite FK could have valid provider credentials but an arbitrary or corrupted attempt ID if any attribute were null.
2. **Disjoint Backfill and Heuristic Tie-Breaking**:
   Initial migration drafts attempted to backfill Stripe invoice provenance and provider-neutral invoice provenance in separate SQL queries. When multiple subscriptions appeared in history for the same billing period, queries used heuristic tie-breaking (such as `MIN(subscription_id)` or sorting by timestamps), silently associating billing periods with incorrect subscriptions.
3. **Post-Settlement Provenance Reassignment**:
   Application logic errors or concurrent webhook retries could attempt to reassign `subscription_id` or `checkout_attempt_id` on already-settled `billing_payments` or `organization_billing_periods`.

---

## Accepted Architectural Decisions
1. **Unified Provenance Conflict Detection Migration Gate**:
   - Migration `e2f3a4b5c6d1` enforces a unified candidate mapping query across both `stripe_invoices` and `billing_invoices`.
   - Distinct subscriptions per billing period are aggregated deterministically.
   - If any billing period is associated with more than one distinct subscription across all invoice sources, or if existing non-null `subscription_id` values conflict with invoice evidence, the migration immediately **aborts with an explicit error**.
   - No heuristic tie-breaking (`MIN()`, `MAX()`, arbitrary limits) is permitted.
2. **Composite Foreign Key and Null-Bypass Check Constraint**:
   - `billing_payments` references `billing_checkout_attempts` via a 6-column composite foreign key:
     `fk_billing_payments_checkout_attempt_composite` targeting `(checkout_attempt_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)`.
   - To eliminate the PostgreSQL `MATCH SIMPLE` loophole, a strict database check constraint is added:
     ```sql
     CONSTRAINT ck_billing_payments_checkout_attempt_complete CHECK (
         (checkout_attempt_id IS NULL) OR (
             provider IS NOT NULL AND
             merchant_account_id IS NOT NULL AND
             environment IS NOT NULL AND
             provider_subscription_id IS NOT NULL AND
             provider_payment_id IS NOT NULL
         )
     )
     ```
3. **PostgreSQL Immutability Triggers**:
   - Immutability triggers are installed on `organization_billing_periods` and `billing_payments`.
   - Once `subscription_id`, `checkout_attempt_id`, or `provider_payment_id` is populated with a non-null value, any subsequent `UPDATE` statement attempting to alter that value to a different non-null value is aborted at the database trigger level.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code)
- Authored migration `20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py` containing:
  - Pre-backfill unified candidate conflict detection query (raising an exception on ambiguous provenance).
  - Deterministic single-candidate backfill for `organization_billing_periods.subscription_id`.
  - Transactional abort gate ensuring zero active Pro periods lack proven subscription provenance.
  - Partial unique indexes `uq_checkout_attempts_provider_sub` and `uq_checkout_attempts_provider_payment`.
  - Composite unique constraint `uq_bca_composite_identity`.
  - Check constraint `ck_billing_payments_checkout_attempt_complete`.
  - Composite foreign key `fk_billing_payments_checkout_attempt_composite`.
  - Procedural immutability triggers `trg_prevent_billing_payment_checkout_attempt_mutation` and `trg_prevent_billing_period_subscription_mutation`.
- SQLAlchemy declarative models updated in `apps/api/app/models/payment.py` and `apps/api/app/models/organization.py`.
- 17 unit and model tests passed locally on SQLite (`test_provider_neutral_billing_models.py`, `test_payment_association_migration.py`).
- 14 integration test scenarios authored in `apps/api/tests/test_postgres_provenance_integration.py`.

### 2. Proposed Implementation
- None for the schema; the schema DDL and constraints are fully authored.

### 3. Outstanding Validation
- **Pending PostgreSQL CI Execution**: The 14 integration tests in `test_postgres_provenance_integration.py` have **not yet been executed against a real PostgreSQL instance in CI**. They are currently skipped in local SQLite test runs because SQLite lacks PostgreSQL procedural trigger and `MATCH SIMPLE` support.

### 4. Rejected Approaches
- **Disjoint Stripe and Neutral Invoicing Backfills**: Discarded because separate queries could independently select conflicting subscriptions for the same period.
- **Heuristic Tie-Breaking (`MIN()`, `ORDER BY created_at`)**: Discarded because arbitrary selection silently corrupts financial provenance.
- **Default `MATCH SIMPLE` Composite Foreign Key**: Discarded because partial NULL tuples bypass foreign key validation in PostgreSQL.

---

## Consequences

### Positive
- Guarantee of zero silent data corruption during migrations: conflicts fail fast and require human review.
- Prevents cross-tenant, cross-environment, or cross-merchant payment associations at the database constraint level.
- Reassignment of payment or period provenance is impossible even if application code suffers a bug.

### Negative / Trade-offs
- Migrations cannot run on corrupt or ambiguous data without explicit manual data cleanup beforehand.
- Integration tests for migration logic require a real PostgreSQL engine because SQLite does not support PostgreSQL procedural triggers or `MATCH SIMPLE` constraints.

---

## Relevant Implementation References
- `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py`
- `apps/api/app/models/payment.py` (`BillingPayment`)
- `apps/api/app/models/organization.py` (`OrganizationBillingPeriod`)
- `apps/api/tests/test_payment_association_migration.py`
- `apps/api/tests/test_postgres_provenance_integration.py`

## Supporting Historical Reports
- [`STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md)
- [`STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md`](../archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md)
- [`STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md`](../archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md)
- [`STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md`](../archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md)
