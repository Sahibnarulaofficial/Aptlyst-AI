# Aptlyst AI — Documentation Cleanup Phase 2 Report

**Execution Date:** 2026-11-08  
**Scope:** Repository Reorganization, Canonical Documentation Authoring & Invariant Protection  
**Status:** Completed Successfully  
**Deliverable:** `DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md`  

---

## 1. Executive Summary

Phase 2 of the documentation cleanup has been executed in full compliance with the findings of [`docs/audits/DOCUMENTATION_AUDIT_REPORT.md`](docs/audits/DOCUMENTATION_AUDIT_REPORT.md).

All 29 historical Step 5.2D reports that previously cluttered the repository root have been archived into four chronological phase subdirectories with their filenames and contents 100% preserved. The boilerplate Next.js root `README.md` has been replaced with a comprehensive, production-grade project guide. Canonical living architecture documentation, developer onboarding guides, five formal Architecture Decision Records (ADRs), and root navigation portals have been established.

---

## 2. Markdown File Counts: Before vs. After

| Metric | Phase 1 (Baseline) | Phase 2 (Completed) | Delta / Notes |
| :--- | :---: | :---: | :--- |
| **Total Markdown Files** | **33** | **52** | +19 new canonical, ADR, portal, and report files |
| **Files at Repository Root (`.`)** | 32 | **7** | **-25 files** (Root clutter eliminated) |
| **Files in `docs/architecture/`** | 0 | **4** | Canonical living architecture specifications |
| **Files in `docs/development/`** | 0 | **3** | Setup, testing, and roadmap developer guides |
| **Files in `docs/decisions/`** | 0 | **5** | Formal Architecture Decision Records (ADR-001 to 005) |
| **Files in `docs/audits/`** | 0 | **1** | Preserved Phase 1 inventory audit report |
| **Files in `docs/archive/` (Total)** | 0 | **30** | Master archive index + 29 historical step reports |
| ├── *`phase_5_2d_foundation/`* | 0 | *7* | Ingestion & worker implementation/audit reports |
| ├── *`phase_5_2d_lifecycle_design/`* | 0 | *4* | Subscription state machine & shared handler reports |
| ├── *`phase_5_2d_agreement_handlers/`* | 0 | *15* | Concurrency proofs, Option A decoupling, locking audits |
| └── *`phase_5_2d_schema_migration/`* | 0 | *3* | Schema migration DDL, provenance conflict gate, CI reports |
| **Files in `apps/api/`** | 1 | **1** | Package-level README preserved intact |

---

## 3. Historical Files Moved and Their Destinations

All 29 historical reports were moved from the repository root into dedicated chronological archive directories. Original filenames and contents are strictly preserved:

### 3.1 Phase 5.2D Foundation (`docs/archive/phase_5_2d_foundation/`)
1. `STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md)
2. `STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md)
3. `STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md)
4. `STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md)
5. `STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md)
6. `STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md)
7. `STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md` ➔ [`docs/archive/phase_5_2d_foundation/STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md`](docs/archive/phase_5_2d_foundation/STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md)

### 3.2 Phase 5.2D Lifecycle Design (`docs/archive/phase_5_2d_lifecycle_design/`)
8. `STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md` ➔ [`docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md`](docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md)
9. `STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md` ➔ [`docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md`](docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md)
10. `STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md`](docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md)
11. `STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md` ➔ [`docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md`](docs/archive/phase_5_2d_lifecycle_design/STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md)

### 3.3 Phase 5.2D Agreement Handlers & Concurrency (`docs/archive/phase_5_2d_agreement_handlers/`)
12. `STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md)
13. `STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md)
14. `STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md)
15. `STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md)
16. `STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md)
17. `STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md)
18. `STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md)
19. `STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md)
20. `STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md)
21. `STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md)
22. `STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md)
23. `STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md)
24. `STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md)
25. `STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md)
26. `STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md` ➔ [`docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md`](docs/archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md)

### 3.4 Phase 5.2D Schema Migration (`docs/archive/phase_5_2d_schema_migration/`)
27. `STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md`](docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md)
28. `STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md`](docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md)
29. `STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md` ➔ [`docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md`](docs/archive/phase_5_2d_schema_migration/STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md)

### 3.5 Documentation Audit Report (`docs/audits/`)
- `DOCUMENTATION_AUDIT_REPORT.md` ➔ [`docs/audits/DOCUMENTATION_AUDIT_REPORT.md`](docs/audits/DOCUMENTATION_AUDIT_REPORT.md)

---

## 4. Newly Authored Canonical Documentation

The following living documents were authored:

1. [**`README.md`**](README.md): Overhauled root README replacing Next.js boilerplate with complete Aptlyst AI overview, architecture diagrams, backend/frontend quickstarts, test instructions, and status.
2. [**`PROJECT_STATUS.md`**](PROJECT_STATUS.md): Live dashboard tracking completed phases, current active checkpoint (Step 5.2D.3.3A.2), test metrics (634 passed / 14 CI skipped), blockers, and next steps.
3. [**`TECHNICAL_DECISIONS.md`**](TECHNICAL_DECISIONS.md): Central index linking to all formal Architecture Decision Records.
4. [**`BILLING_ROADMAP.md`**](BILLING_ROADMAP.md): Detailed milestone tracker and boundary rules for Phase 5.2D billing development.
5. [**`docs/README.md`**](docs/README.md): Documentation portal indexing architecture, development, decisions, audits, and archive.
6. [**`docs/architecture/SYSTEM_ARCHITECTURE.md`**](docs/architecture/SYSTEM_ARCHITECTURE.md): Multi-tier system architecture, tenancy model, and subsystem overview.
7. [**`docs/architecture/BACKEND_ARCHITECTURE.md`**](docs/architecture/BACKEND_ARCHITECTURE.md): FastAPI structure, SQLAlchemy declarative models, locking, and webhook workers.
8. [**`docs/architecture/FRONTEND_ARCHITECTURE.md`**](docs/architecture/FRONTEND_ARCHITECTURE.md): Next.js 16 App Router, React 19, Supabase SSR auth, organization context, and embeddable widget.
9. [**`docs/architecture/BILLING_ARCHITECTURE.md`**](docs/architecture/BILLING_ARCHITECTURE.md): Provider-neutral billing foundation, Option A decoupling, composite foreign keys, and advisory locking.
10. [**`docs/development/DEVELOPMENT_SETUP.md`**](docs/development/DEVELOPMENT_SETUP.md): Local onboarding guide for frontend and backend with environment configuration.
11. [**`docs/development/TESTING_GUIDE.md`**](docs/development/TESTING_GUIDE.md): Testing guide explaining unit test suite vs. PostgreSQL CI service container integration suite.
12. [**`docs/development/DEVELOPMENT_ROADMAP.md`**](docs/development/DEVELOPMENT_ROADMAP.md): Platform development roadmap and upcoming phases.
13. [**`docs/archive/README.md`**](docs/archive/README.md): Master historical archive index with full mapping table.

---

## 5. Architecture Decision Record (ADR) Summary & Statuses

Five formal ADRs were authored under `docs/decisions/`. All five have been assigned status **Accepted** based on comprehensive mathematical proofs and implementation evidence in the repository:

| ADR ID | Title | Status | Primary Rationale & Invariants Protected |
| :--- | :--- | :---: | :--- |
| [**ADR-001**](docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md) | **Billing Provider Migration and Provider-Neutral Foundation** | **Accepted** | Decouples application logic from payment gateway APIs; transitions primary provider to Razorpay Subscriptions (INR/RBI e-mandate support) while preserving historical Stripe records. |
| [**ADR-002**](docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md) | **Full Payment Settlement Policy and Immutability Invariants** | **Accepted** | Formalizes the *Conservative Core Billing Policy* (excising immediate resets; unexpired paid quotas run to `period_end`; resubscriptions queue future periods). Formalizes *Refusal of Resurrection* and *Refusal to Rewrite History*. |
| [**ADR-003**](docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md) | **Subscription and Entitlement Separation (Option A Decoupling)** | **Accepted** | Strictly separates recurring payment contracts (`Subscription`) from tenant entitlements (`OrganizationBillingPeriod`). Subscription cancellation stops auto-renewal without cutting off paid period quota. |
| [**ADR-004**](docs/decisions/ADR-004-PAYMENT-PROVENANCE.md) | **Payment Association Provenance, Composite Foreign Keys, and Conflict Gates** | **Accepted** | Closes the PostgreSQL composite foreign key `MATCH SIMPLE` null bypass via `ck_billing_payments_checkout_attempt_complete`. Enforces PostgreSQL immutability triggers and unified candidate conflict detection during migration `e2f3a4b5c6d1`. |
| [**ADR-005**](docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md) | **Billing Concurrency Control and Monotonic Lock Ordering** | **Accepted** | Closes the zero-period concurrency void using Level 0 Organization Advisory Locking (`pg_advisory_xact_lock`). Enforces a strict monotonic 5-level lock hierarchy to prevent deadlocks and isolates network HTTP calls outside database transactions. |

---

## 6. Link Integrity and Verification Results

An automated link validation script was executed across all canonical documents, ADRs, developer guides, and root portals.

- **Total Links Checked:** **84 internal relative file links**
- **Broken Links Found:** **0 (Zero broken links)**
- **Result:** All relative paths between root files, `docs/architecture/`, `docs/development/`, `docs/decisions/`, `docs/audits/`, and `docs/archive/` resolve to valid existing files.

---

## 7. Documents Left for Manual Review

- **`AGENTS.md`**: Left at repository root. Enforced by rule and required by Next.js development server.
- **`CLAUDE.md`**: Left at repository root. Required by Anthropic Claude tooling.
- **`apps/api/README.md`**: Preserved in place. Contains essential backend startup instructions.

Zero documents require ambiguous classification; all 29 historical step reports were confidently categorized into their respective chronological phase buckets.

---

## 8. Source-Code Change Verification

A strict read-only boundary on application code, migrations, tests, and configuration was maintained throughout Phase 2:

- **Application Code Modified:** **0 files**
- **Alembic Migrations Modified:** **0 files**
- **Test Scripts Modified:** **0 files**
- **Runtime Configuration Modified:** **0 files**
- **Database Operations Executed:** **None**
- **Third-Party API Calls (Stripe, Razorpay, Supabase):** **None**
- **Git Commits or Pushes:** **None**
- **Pre-existing Working Tree State:** All 17 pre-existing uncommitted modifications in `apps/api/` remain completely intact.

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

The documentation cleanup Phase 2 is complete, verified, and ready for review.
