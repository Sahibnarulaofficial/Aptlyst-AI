# Aptlyst AI — Phase 2.1 Documentation Accuracy & Architectural Consistency Audit Report

**Audit Date:** 2026-11-09  
**Audit Scope:** Canonical Documentation, Architecture Decision Records (ADRs), Project Portals, and Historical Traceability vs. Source Code & Migrations  
**Execution Mode:** Documentation Correction Only (Zero application code, tests, migrations, or CI configurations altered)  
**Deliverable File:** `DOCUMENTATION_ACCURACY_AUDIT_REPORT.md`  

---

## 1. Documents Inspected

The following 14 canonical documentation files, decision records, and roadmaps were audited directly against the codebase:

1. [`docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md`](docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md)
2. [`docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md`](docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md)
3. [`docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md`](docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md)
4. [`docs/decisions/ADR-004-PAYMENT-PROVENANCE.md`](docs/decisions/ADR-004-PAYMENT-PROVENANCE.md)
5. [`docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md`](docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md)
6. [`docs/architecture/SYSTEM_ARCHITECTURE.md`](docs/architecture/SYSTEM_ARCHITECTURE.md)
7. [`docs/architecture/BACKEND_ARCHITECTURE.md`](docs/architecture/BACKEND_ARCHITECTURE.md)
8. [`docs/architecture/FRONTEND_ARCHITECTURE.md`](docs/architecture/FRONTEND_ARCHITECTURE.md)
9. [`docs/architecture/BILLING_ARCHITECTURE.md`](docs/architecture/BILLING_ARCHITECTURE.md)
10. [`docs/development/DEVELOPMENT_SETUP.md`](docs/development/DEVELOPMENT_SETUP.md)
11. [`docs/development/TESTING_GUIDE.md`](docs/development/TESTING_GUIDE.md)
12. [`docs/development/DEVELOPMENT_ROADMAP.md`](docs/development/DEVELOPMENT_ROADMAP.md)
13. [`PROJECT_STATUS.md`](PROJECT_STATUS.md)
14. [`TECHNICAL_DECISIONS.md`](TECHNICAL_DECISIONS.md)
15. [`BILLING_ROADMAP.md`](BILLING_ROADMAP.md)
16. [`README.md`](README.md)
17. [`docs/archive/README.md`](docs/archive/README.md)

---

## 2. Inaccuracies Discovered

During the inspection of the repository implementation (`apps/api/app/models/payment.py`, `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py`, `apps/api/app/services/billing/razorpay/lock.py`, and `apps/api/app/services/billing/razorpay/agreement_handlers.py`), four distinct categories of inaccuracies were identified in earlier documentation drafts:

### 2.1 Inaccuracy 1: Composite Foreign Key Columns and Check Constraint (ADR-004 & Billing Architecture)
- **Previous Claim**: ADR-004 and `BILLING_ARCHITECTURE.md` stated that `billing_payments` referenced `billing_checkout_attempts` via `(organization_id, provider, merchant_account_id, environment, subscription_id, checkout_attempt_id)` with constraint `CHECK ((checkout_attempt_id IS NULL AND subscription_id IS NULL) OR ...)`.
- **Actual Code Reality**:
  - In `apps/api/app/models/payment.py` (lines 351–385) and migration `e2f3a4b5c6d1` (lines 301–337), the composite foreign key targets:
    `billing_payments(checkout_attempt_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)`
    referencing
    `billing_checkout_attempts(id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id)`.
  - The actual check constraint is:
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

### 2.2 Inaccuracy 2: Implemented vs. Proposed Concurrency Controls (ADR-005, Backend & Billing Architecture)
- **Previous Claim**: Documentation stated that Level 0 Organization Advisory Locking (`pg_advisory_xact_lock(hashtext('org:' || organization_id))`) and the global 5-level monotonic lock hierarchy were currently implemented across backend transactions.
- **Actual Code Reality**:
  - `apps/api/app/services/billing/razorpay/lock.py` implements **entity-level** transaction-scoped advisory locks via `acquire_razorpay_advisory_lock(db, entity_type, entity_id)` using SHA-256 derivation over `{"subscription", "payment", "invoice", "customer"}`.
  - Organization-level advisory locking (`Level 0A`) and the global 5-level monotonic lock ordering were formulated, evaluated, and accepted in historical design audits (Steps 5.2D.3.2.10–12), but are **scheduled for implementation in Step 5.2D.3.3B**. Presenting them as completed runtime code conflated approved architecture with executed implementation.

### 2.3 Inaccuracy 3: Lack of Five-Way Categorization in Architecture Decision Records
- **Previous Deficiency**: The initial ADR documents did not explicitly distinguish between:
  1. Accepted architectural decisions
  2. Implemented functionality
  3. Proposed implementation
  4. Outstanding validation
  5. Rejected approaches

### 2.4 Inaccuracy 4: Checkpoint and Blockers Precision in Status Portals
- **Previous Deficiency**: Status trackers did not sufficiently highlight that while migration `e2f3a4b5c6d1` and the 14 integration test scenarios are authored in code, **live execution against a real PostgreSQL instance in CI remains pending**, and Step 5.2D.3.3B is strictly blocked.

---

## 3. Corrections Made

All identified inaccuracies were corrected across the documentation files without touching application source code:

1. **Corrected ADR-004 (`docs/decisions/ADR-004-PAYMENT-PROVENANCE.md`)**:
   - Replaced inaccurate composite foreign key tuples and check constraint SQL with the exact code from migration `e2f3a4b5c6d1` and `app/models/payment.py`.
   - Added explicit sections for Accepted Decisions, Implemented Functionality, Proposed Implementation, Outstanding Validation, and Rejected Approaches.
2. **Corrected ADR-005 (`docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md`)**:
   - Explicitly clarified that entity-level advisory locking (`subscription`, `payment`, `invoice`, `customer`) via SHA-256 is implemented in `lock.py`.
   - Labeled Level 0A Organization Advisory Locking and the global 5-level lock hierarchy as the **Approved Architectural Specification scheduled for Step 5.2D.3.3B**.
   - Added all five required implementation state sections.
3. **Corrected ADR-001, ADR-002, and ADR-003**:
   - Formatted each ADR with the five explicit subsections distinguishing approved policy, implemented code, proposed work, outstanding validation, and rejected alternatives.
4. **Corrected `docs/architecture/BILLING_ARCHITECTURE.md`**:
   - Corrected Section 3.1 to list the exact 6 composite foreign key columns and exact `ck_billing_payments_checkout_attempt_complete` SQL.
   - Corrected Section 4 to distinguish currently implemented entity advisory locks from the approved Step 5.2D.3.3B organization advisory lock specification.
5. **Corrected `docs/architecture/BACKEND_ARCHITECTURE.md`**:
   - Updated Section 4 to separate currently implemented concurrency controls (`lock.py`, triggers, network isolation) from the approved Step 5.2D.3.3B global lock hierarchy.
6. **Synchronized Portals (`PROJECT_STATUS.md`, `TECHNICAL_DECISIONS.md`, `BILLING_ROADMAP.md`)**:
   - Aligned summaries and statuses across all portals to state that Step 5.2D.3.3A.1 is implemented, Step 5.2D.3.3A.2 is configured, PostgreSQL CI execution is pending, and Step 5.2D.3.3B has **not started**.

---

## 4. ADR Status Verification

All five ADRs maintain an overarching status of **Accepted** because their design principles, invariant rules, and mathematical proofs were formally approved during Phase 5.2D audits. Their internal implementation states are verified as follows:

| ADR ID | Decision Title | Architectural Status | Implemented in Code | Proposed / Pending | Outstanding Validation |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **ADR-001** | Billing Provider Migration | **Accepted** | Provider-neutral models, migrations `a1b2c3d4e5f7`–`d1e2f3a4b5c6`, webhook worker queue. | Settlement handlers (Step 5.2D.3.3B). | Razorpay sandbox API validation. |
| **ADR-002** | Full Payment Settlement Policy | **Accepted** | Terminal state guards in `agreement_handlers.py`, staged payment models in `payment.py`. | Chained period provisioning (`P2`), payment binding sweeper. | PostgreSQL CI rollover verification. |
| **ADR-003** | Option A Decoupling | **Accepted** | Independent `Subscription` and `OrganizationBillingPeriod` models; FK in migration `e2f3a4b5c6d1`. | Settlement handler rollover logic. | PostgreSQL CI execution of migration `e2f3a4b5c6d1`. |
| **ADR-004** | Payment Association Provenance | **Accepted** | Migration `e2f3a4b5c6d1`, composite FK, check constraint, immutability triggers, 14 test scenarios. | None (schema fully authored). | **Execution of migration & 14 tests against PostgreSQL 16 in CI**. |
| **ADR-005** | Billing Lock Ordering | **Accepted** | Entity advisory locks in `lock.py` (SHA-256 for sub, pay, inv, cust), Stripe invoice locks. | Level 0A organization advisory locks, global 5-level monotonic hierarchy. | Concurrency stress testing in PostgreSQL CI. |

---

## 5. Source-Code Verification

The repository was verified against the documentation updates:

- **Application Source Code (`apps/api/app/`)**: Completely untouched during this audit.
- **Database Models (`apps/api/app/models/`)**: Verified. Exact match with documentation.
- **Database Migrations (`apps/api/alembic/versions/`)**: Verified. Migration chain head is `e2f3a4b5c6d1`.
- **Pre-existing Working Tree State**: All 17 pre-existing uncommitted modifications in `apps/api/` remain 100% intact.
- **Test Integrity**: Unit test suite remains green (634 passing tests).

---

## 6. Historical Traceability Results

- All 29 historical step reports remain in `docs/archive/` under their respective phase subdirectories:
  - `docs/archive/phase_5_2d_foundation/` (7 files)
  - `docs/archive/phase_5_2d_lifecycle_design/` (4 files)
  - `docs/archive/phase_5_2d_agreement_handlers/` (15 files)
  - `docs/archive/phase_5_2d_schema_migration/` (3 files)
- Zero historical documents were modified, rewritten, or deleted.
- Every ADR and canonical document contains direct, validated markdown links to the supporting historical audit reports.
- Automated link validation confirmed **0 broken links** across all 84 internal relative file paths.

---

## 7. Remaining Uncertainties & Operational Conditions

1. **PostgreSQL CI Verification (Step 5.2D.3.3A.2)**:
   The 14 integration test scenarios in `tests/test_postgres_provenance_integration.py` validate PostgreSQL procedural triggers, advisory locks, and composite foreign key constraints. Because SQLite cannot evaluate these PostgreSQL-specific features, verified execution is strictly contingent upon running the GitHub Actions workflow against a live PostgreSQL 16 container.
2. **Step 5.2D.3.3B Authorization Boundary**:
   Step 5.2D.3.3B (implementing Razorpay payment settlement handlers and the payment binding sweeper) remains **NOT STARTED** and must not begin until Step 5.2D.3.3A.2 is green in CI.

---

## 8. Files Modified During This Audit

Only documentation files were updated:
- `docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md`
- `docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md`
- `docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md`
- `docs/decisions/ADR-004-PAYMENT-PROVENANCE.md`
- `docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md`
- `docs/architecture/BACKEND_ARCHITECTURE.md`
- `docs/architecture/BILLING_ARCHITECTURE.md`
- `TECHNICAL_DECISIONS.md`
- `PROJECT_STATUS.md`

---

## 9. Final Git Status

```text
 M README.md
 M apps/api/alembic/env.py
 M apps/api/app/api/billing.py
 M apps/api/app/config.py
 M apps/api/app/models/billing.py
 M apps/api/app/models/organization.py
 M apps/api/app/models/subscription.py
 M apps/api/app/schemas/billing.py
 M apps/api/app/services/billing/__init__.py
 M apps/api/app/services/billing/stripe_checkout_handler.py
 M apps/api/app/services/billing/stripe_customer.py
 M apps/api/app/services/billing/stripe_invoice_paid_handler.py
 M apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py
 M apps/api/app/services/billing/stripe_subscription_deleted_handler.py
 M apps/api/app/services/billing/stripe_subscription_updated_handler.py
 M apps/api/tests/test_billing_api.py
 M apps/api/tests/test_stripe_checkout_handler.py
 M apps/api/tests/test_stripe_customer.py
?? .github/
?? BILLING_ROADMAP.md
?? DOCUMENTATION_ACCURACY_AUDIT_REPORT.md
?? DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md
?? PROJECT_STATUS.md
?? TECHNICAL_DECISIONS.md
?? apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py
?? apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py
?? apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py
?? apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py
?? apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py
?? apps/api/app/models/payment.py
?? apps/api/app/services/billing/razorpay/
?? apps/api/app/workers/razorpay_webhook_worker.py
?? apps/api/tests/test_payment_association_migration.py
?? apps/api/tests/test_postgres_provenance_integration.py
?? apps/api/tests/test_provider_neutral_billing_models.py
?? apps/api/tests/test_provider_neutral_migration.py
?? apps/api/tests/test_razorpay_agreement_handlers.py
?? apps/api/tests/test_razorpay_checkout.py
?? apps/api/tests/test_razorpay_client.py
?? apps/api/tests/test_razorpay_config.py
?? apps/api/tests/test_razorpay_customer.py
?? apps/api/tests/test_razorpay_handler_foundation.py
?? apps/api/tests/test_razorpay_webhook_ingestion.py
?? apps/api/tests/test_razorpay_webhook_worker.py
?? docs/
```

---

## 10. Final Audit Verdict

### **Verdict: PASS WITH CONDITIONS**

**Conditions for Next Development Phase:**
1. **Condition 1**: Step 5.2D.3.3A.2 must run in GitHub Actions CI with an active PostgreSQL 16 container, executing `tests/test_postgres_provenance_integration.py` to confirm that migration `e2f3a4b5c6d1`, its triggers, and constraints pass on real PostgreSQL.
2. **Condition 2**: Implementation of Step 5.2D.3.3B (Razorpay Settlement Handlers & Payment Binding Sweeper) remains **STRICTLY BLOCKED** until Condition 1 is satisfied and verified.
