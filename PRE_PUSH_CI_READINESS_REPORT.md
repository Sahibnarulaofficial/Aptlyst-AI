# Aptlyst AI — Pre-Push CI Readiness Audit Report
**PostgreSQL CI Validation Checkpoint (Step 5.2D.3.3A.2)**

**Audit Date:** 2026-11-09  
**Execution Mode:** STRICTLY READ-ONLY (Zero files staged, committed, pushed, stashed, reset, or modified)  
**Deliverable File:** `PRE_PUSH_CI_READINESS_REPORT.md`  

---

## 1. Branch, HEAD Commit & Remote Tracking

### 1.1 Git Branch & HEAD Details
- **Current Active Branch:** `main`
- **Current Local HEAD Commit:** `3b50a1a82a0153d214f4b1e8735daf096ec95a93`
- **Commit Summary:** `feat: checkpoint milestone 4 stripe billing implementation`
- **Commit Author:** `Sahibnarulaofficial <sahibnarulaofficial@gmail.com>`
- **Commit Timestamp:** `Fri Nov 6 00:03:05 2026 +0530`

### 1.2 Recent Git History (Last 5 Commits)
```text
3b50a1a (HEAD -> main) feat: checkpoint milestone 4 stripe billing implementation
d45a942 (origin/main, origin/HEAD) fix: use idempotent DROP TYPE IF EXISTS in organizations migration downgrade
4f1bcc4 test: add Supabase integration test suite and opt-in guards for DB-dependent tests
a591e98 feat: integrate billing-aware orchestrator into public chatbot API and add orchestration tests
35edf0f feat: add AI generation orchestrator with billing-aware lease fencing and provider integration tests
```

### 1.3 Remote Configuration & Tracking Status
- **Remote Name:** `origin`
- **Remote Fetch URL:** `https://github.com/Sahibnarulaofficial/Aptlyst-AI.git`
- **Remote Push URL:** `https://github.com/Sahibnarulaofficial/Aptlyst-AI.git`
- **Tracking Differential:** `Your branch is ahead of 'origin/main' by 1 commit.` (Commit `3b50a1a` has been committed locally but has not yet been pushed to GitHub).
- **Staging State:** `0 files staged` (`git diff --cached --stat` is empty).
- **Working Tree State:** `18 modified files` (1 root `README.md` + 17 `apps/api` files) and `26 untracked paths`.

---

## 2. Complete Changed-File Classification

Every modified and untracked file across the repository has been audited and classified into its exact functional category:

| File Path | Git Status | Category | Scope & Purpose |
| :--- | :---: | :--- | :--- |
| `.github/workflows/ci.yml` | Untracked | **PostgreSQL CI Workflow** | Defines GitHub Actions workflow with PostgreSQL 16 service container and integration testing steps. |
| `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py` | Untracked | **Provenance Migration** | Step 5.2D.3.3A migration: composite FK, null-bypass check constraint, immutability triggers, unified conflict gate. |
| `apps/api/app/models/payment.py` | Untracked | **Provenance Migration** | SQLAlchemy declarative models for `BillingPayment` (composite FK), `BillingWebhookEvent`, and reconciliation. |
| `apps/api/alembic/env.py` | Modified | **Provenance Migration** | Imports `app.models.payment` entities into Alembic metadata target. |
| `apps/api/app/models/billing.py` | Modified | **Provenance Migration** | Adds bidirectional provenance relationship for `OrganizationBillingPeriod.subscription`. |
| `apps/api/app/models/organization.py` | Modified | **Provenance Migration** | Defines `subscription_id` relationship on `OrganizationBillingPeriod`. |
| `apps/api/app/models/subscription.py` | Modified | **Provenance Migration** | Extends `Subscription` model with billing periods and payment relationships. |
| `apps/api/tests/test_postgres_provenance_integration.py` | Untracked | **PostgreSQL Integration Tests** | 14 test scenarios validating unified provenance conflict detection, triggers, composite FKs, and downgrade gates against real PostgreSQL. |
| `apps/api/tests/test_payment_association_migration.py` | Untracked | **Migration/Model Regression Tests** | 17 unit/mock tests validating migration DDL generation and schema constraints on SQLite. |
| `apps/api/tests/test_provider_neutral_billing_models.py` | Untracked | **Migration/Model Regression Tests** | 15 unit tests validating provider-neutral billing models, column types, and constraints. |
| `apps/api/tests/test_provider_neutral_migration.py` | Untracked | **Migration/Model Regression Tests** | 9 unit tests validating offline DDL compilation for provider-neutral migrations. |
| `apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py` | Untracked | **Billing Implementation (Phase 5.2A)** | Migration provisioning provider-neutral billing tables. |
| `apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py` | Untracked | **Billing Implementation (Phase 5.2B)** | Migration provisioning customer mapping tables. |
| `apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py` | Untracked | **Billing Implementation (Phase 5.2B)** | Migration creating scoped partial unique index on checkout attempts. |
| `apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py` | Untracked | **Billing Implementation (Phase 5.2C)** | Migration adding lease ownership and worker claiming fields to `billing_webhook_events`. |
| `apps/api/app/services/billing/razorpay/` | Untracked | **Billing Implementation (Phase 5.2D)** | Razorpay client, customer, checkout, webhook ingress, and agreement handlers. |
| `apps/api/app/workers/razorpay_webhook_worker.py` | Untracked | **Billing Implementation (Phase 5.2C)** | Background webhook queue worker with lease-based claiming and exponential backoff. |
| `apps/api/app/config.py` | Modified | **Billing Implementation** | Pydantic Settings extensions validating Razorpay environment variables and key formats. |
| `apps/api/app/schemas/billing.py` | Modified | **Billing Implementation** | Pydantic v2 schemas for Razorpay checkout sessions and webhook responses. |
| `apps/api/app/api/billing.py` | Modified | **Billing Implementation** | API routers for Razorpay checkout, verification, and webhook ingestion endpoints. |
| `apps/api/app/services/billing/__init__.py` | Modified | **Billing Implementation** | Service exports for billing domain. |
| `apps/api/app/services/billing/stripe_checkout_handler.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring aligning Stripe with provider-neutral structures. |
| `apps/api/app/services/billing/stripe_customer.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring. |
| `apps/api/app/services/billing/stripe_invoice_paid_handler.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring. |
| `apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring. |
| `apps/api/app/services/billing/stripe_subscription_deleted_handler.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring. |
| `apps/api/app/services/billing/stripe_subscription_updated_handler.py` | Modified | **Billing Implementation** | Backward-compatibility refactoring. |
| `apps/api/tests/test_billing_api.py` | Modified | **Billing Implementation** | API route integration tests. |
| `apps/api/tests/test_stripe_checkout_handler.py` | Modified | **Billing Implementation** | Stripe checkout test suite. |
| `apps/api/tests/test_stripe_customer.py` | Modified | **Billing Implementation** | Stripe customer test suite. |
| `apps/api/tests/test_razorpay_agreement_handlers.py` | Untracked | **Billing Implementation** | Agreement handler unit tests. |
| `apps/api/tests/test_razorpay_checkout.py` | Untracked | **Billing Implementation** | Razorpay checkout session tests. |
| `apps/api/tests/test_razorpay_client.py` | Untracked | **Billing Implementation** | Razorpay HTTP client tests. |
| `apps/api/tests/test_razorpay_config.py` | Untracked | **Billing Implementation** | Razorpay configuration validation tests. |
| `apps/api/tests/test_razorpay_customer.py` | Untracked | **Billing Implementation** | Razorpay customer provisioning tests. |
| `apps/api/tests/test_razorpay_handler_foundation.py` | Untracked | **Billing Implementation** | Base handler and advisory lock tests. |
| `apps/api/tests/test_razorpay_webhook_ingestion.py` | Untracked | **Billing Implementation** | Webhook HMAC signature verification tests. |
| `apps/api/tests/test_razorpay_webhook_worker.py` | Untracked | **Billing Implementation** | Webhook worker lease and claiming tests. |
| `README.md` | Modified | **Documentation Restructuring** | Overhauled production project README. |
| `PROJECT_STATUS.md` | Untracked | **Documentation Restructuring** | Live project status dashboard and gate tracker. |
| `TECHNICAL_DECISIONS.md` | Untracked | **Documentation Restructuring** | Master index linking to ADR-001 through ADR-005. |
| `BILLING_ROADMAP.md` | Untracked | **Documentation Restructuring** | Detailed roadmap for billing engine migration. |
| `DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md` | Untracked | **Documentation Restructuring** | Audit report documenting Phase 2 reorganization. |
| `DOCUMENTATION_ACCURACY_AUDIT_REPORT.md` | Untracked | **Documentation Restructuring** | Audit report documenting Phase 2.1 corrections. |
| `docs/` | Untracked | **Documentation Restructuring** | Central documentation directory containing architecture, development, decisions (ADR-001–005), audits, and archive (29 historical reports). |

---

## 3. CI Workflow Verification (`.github/workflows/ci.yml`)

The GitHub Actions workflow configuration was audited against all functional and security requirements:

| Verification Criteria | Workflow Implementation | Status |
| :--- | :--- | :---: |
| **1. Trigger Events** | Triggers on `push` and `pull_request` against `[main, master, develop]`, plus manual trigger via `workflow_dispatch`. | **PASS** |
| **2. Database Provisioning** | Provisions a disposable `postgres:16-alpine` service container on port 5432 with isolated test credentials (`aptlyst_ci_user`, `aptlyst_ci_password_secure`, `aptlyst_ci_test`). | **PASS** |
| **3. Readiness Healthcheck** | Container uses native `pg_isready` (interval: 5s, timeout: 5s, retries: 10) followed by a Python TCP socket probe loop (up to 30s) before executing migration steps. | **PASS** |
| **4. Database Migrations** | Executes `python -m alembic upgrade head` inside `apps/api` with `DATABASE_URL` pointing strictly to the local disposable container. | **PASS** |
| **5. Integration Test Execution** | Executes `python -m pytest -v tests/test_postgres_provenance_integration.py` with `POSTGRES_TEST_URL` set, enabling all 14 PostgreSQL scenarios. | **PASS** |
| **6. Regression Test Execution** | Executes focused migration and model tests: `test_payment_association_migration.py`, `test_provider_neutral_billing_models.py`, `test_provider_neutral_migration.py` (41 tests). | **PASS** |
| **7. Error Handling & Exit Codes** | Uses standard pytest CLI execution without swallow flags; non-zero exit codes fail the CI build immediately. | **PASS** |
| **8. External Service Isolation** | Zero network calls or secrets configured for Supabase, Stripe, or Razorpay. No external dependencies required. | **PASS** |
| **9. Test Artifact Retention** | Uses `actions/upload-artifact@v4` with `if: always()` to capture JUnit XML test results (`test-results-postgres.xml`, `test-results-models.xml`). | **PASS** |

---

## 4. Migration Safety & Linearity Review

The Alembic revision history was inspected using `.venv\Scripts\python.exe -m alembic heads` and `history --verbose`:

### 4.1 Unbroken Linear Revision Chain
Alembic detects **exactly one linear head**, verifying that no branch points or conflicting migrations exist:

$$\text{Base} \rightarrow \dots \rightarrow \texttt{f6a1b2c3d4e5} \rightarrow \texttt{a1b2c3d4e5f7} \rightarrow \texttt{b1c2d3e4f5a6} \rightarrow \texttt{c1d2e3f4a5b7} \rightarrow \texttt{d1e2f3a4b5c6} \rightarrow \mathbf{\texttt{e2f3a4b5c6d1 (head)}}$$

- **Head Revision:** `e2f3a4b5c6d1` (`add_payment_association_and_entitlement_provenance`)
- **Parent Revision:** `d1e2f3a4b5c6` (`add_webhook_event_lease_ownership`)
- **Historical Migrations:** All 12 prior migration scripts remain completely unmodified.

### 4.2 Offline DDL Compilation & Downgrade Safety
- **Upgrade DDL Generation:** Successfully compiled offline SQL via `alembic upgrade d1e2f3a4b5c6:e2f3a4b5c6d1 --sql`. Verified syntax for unified candidate conflict detection, deterministic backfill, abort gates, 6-column composite foreign key, check constraint, and procedural immutability triggers.
- **Downgrade DDL Generation:** Successfully compiled offline SQL via `alembic downgrade e2f3a4b5c6d1:d1e2f3a4b5c6 --sql`. Verified `ACCESS EXCLUSIVE` table locking and non-null count assertion preventing schema rollback if linked financial records exist.
- **Remote Environment State:** Unknown. As per strict audit rules, no live database connections were attempted to external Supabase environments.

---

## 5. PostgreSQL Test Readiness Review

The 14 integration test scenarios in `apps/api/tests/test_postgres_provenance_integration.py` were audited:

1. **Local Test Execution (Without `POSTGRES_TEST_URL`):**
   - Result: `14 skipped in 0.47s`.
   - Reason: The test runner correctly verifies `POSTGRES_TEST_URL` and skips cleanly when running on developer machines without PostgreSQL, preventing false positives on SQLite.
2. **CI Container Execution (With `POSTGRES_TEST_URL`):**
   - In GitHub Actions, `.github/workflows/ci.yml` passes `POSTGRES_TEST_URL: postgresql+psycopg2://...`.
   - The test fixture connects, creates test tables, seeds candidate invoice evidence across `stripe_invoices` and `billing_invoices`, and executes all 14 scenarios:
     - `test_1`: Valid Stripe invoice backfill.
     - `test_2`: Valid provider-neutral invoice backfill.
     - `test_3`: Multiple invoices pointing to same subscription (valid aggregation).
     - `test_4`: Cross-invoice disagreement (abort gate).
     - `test_5`: Disagreement between existing provenance and invoices (abort gate).
     - `test_6`: Active Pro period without provenance (abort gate).
     - `test_7`: Free plan period with null provenance (allowed).
     - `test_8`: Composite FK mismatch rejection (relational integrity).
     - `test_9`: Composite FK null-bypass rejection via check constraint.
     - `test_10`: Initial payment association binding.
     - `test_11`: Trigger abort on reassigning payment attempt.
     - `test_12`: Trigger abort on reassigning period subscription.
     - `test_13`: Concurrency race conflict rejection.
     - `test_14`: Downgrade abort guard when associated records exist.
3. **Accompanying Regression Suite:**
   - 41 unit/model tests in `test_payment_association_migration.py`, `test_provider_neutral_billing_models.py`, and `test_provider_neutral_migration.py` passed in 16.72s.

---

## 6. Security Inspection Results

A comprehensive pattern scan for credentials, tokens, and private connection strings was conducted across candidate commit files:

- **Production API Keys:** Zero production API keys (`sk_live_`, `rzp_live_`, `ghp_`) identified in candidate files.
- **Environment Configuration Files:**
  - `.env`, `.env.local`, and `apps/api/.env` are properly ignored by `.gitignore` (line 42: `.env*`).
  - `apps/api/.env.example` contains only documentation placeholders.
- **CI Workflow Credentials:**
  - Uses ephemeral, isolated container credentials (`aptlyst_ci_user`, `aptlyst_ci_password_secure`) bound exclusively to `localhost:5432` inside GitHub Actions runners.
  - No repository secrets or production tokens are exposed.

---

## 7. Commit Boundary Recommendation

To maintain atomic Git history, clean review boundaries, and bisectability, the working tree changes should be staged and committed in **two separate, focused commits**:

```
[Local HEAD: 3b50a1a]
       │
       ▼ Commit 1: Group A (Backend Billing Foundation & CI Workflow)
       │ - .github/workflows/ci.yml
       │ - Alembic migrations a1b2c3d4e5f7 through e2f3a4b5c6d1
       │ - SQLAlchemy models (payment.py, billing.py, organization.py, subscription.py)
       │ - Razorpay services, worker & configuration
       │ - Integration & regression test suites (14 Postgres + 41 unit tests)
       │
       ▼ Commit 2: Group B (Canonical Documentation & ADRs)
       │ - Overhauled root README.md
       │ - docs/ (architecture, development, decisions ADR-001–005, archive, audits)
       │ - Root portals (PROJECT_STATUS.md, TECHNICAL_DECISIONS.md, BILLING_ROADMAP.md)
       │ - Audit reports (DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md, DOCUMENTATION_ACCURACY_AUDIT_REPORT.md, PRE_PUSH_CI_READINESS_REPORT.md)
```

### 7.1 Group A — PostgreSQL CI Checkpoint & Provider-Neutral Billing Foundation
*Recommended Commit Message:*  
`feat(billing): add provider-neutral foundation, payment provenance migration, and PostgreSQL CI workflow`

**Files to Stage in Group A:**
1. `.github/workflows/ci.yml`
2. `apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py`
3. `apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py`
4. `apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py`
5. `apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py`
6. `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py`
7. `apps/api/alembic/env.py`
8. `apps/api/app/models/payment.py`
9. `apps/api/app/models/billing.py`
10. `apps/api/app/models/organization.py`
11. `apps/api/app/models/subscription.py`
12. `apps/api/app/config.py`
13. `apps/api/app/schemas/billing.py`
14. `apps/api/app/api/billing.py`
15. `apps/api/app/services/billing/__init__.py`
16. `apps/api/app/services/billing/stripe_checkout_handler.py`
17. `apps/api/app/services/billing/stripe_customer.py`
18. `apps/api/app/services/billing/stripe_invoice_paid_handler.py`
19. `apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py`
20. `apps/api/app/services/billing/stripe_subscription_deleted_handler.py`
21. `apps/api/app/services/billing/stripe_subscription_updated_handler.py`
22. `apps/api/app/services/billing/razorpay/`
23. `apps/api/app/workers/razorpay_webhook_worker.py`
24. `apps/api/tests/test_billing_api.py`
25. `apps/api/tests/test_stripe_checkout_handler.py`
26. `apps/api/tests/test_stripe_customer.py`
27. `apps/api/tests/test_payment_association_migration.py`
28. `apps/api/tests/test_postgres_provenance_integration.py`
29. `apps/api/tests/test_provider_neutral_billing_models.py`
30. `apps/api/tests/test_provider_neutral_migration.py`
31. `apps/api/tests/test_razorpay_agreement_handlers.py`
32. `apps/api/tests/test_razorpay_checkout.py`
33. `apps/api/tests/test_razorpay_client.py`
34. `apps/api/tests/test_razorpay_config.py`
35. `apps/api/tests/test_razorpay_customer.py`
36. `apps/api/tests/test_razorpay_handler_foundation.py`
37. `apps/api/tests/test_razorpay_webhook_ingestion.py`
38. `apps/api/tests/test_razorpay_webhook_worker.py`

### 7.2 Group B — Documentation Cleanup & Canonical Architecture
*Recommended Commit Message:*  
`docs: reorganize documentation hierarchy, author canonical guides, and record ADRs 001-005`

**Files to Stage in Group B:**
1. `README.md`
2. `PROJECT_STATUS.md`
3. `TECHNICAL_DECISIONS.md`
4. `BILLING_ROADMAP.md`
5. `DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md`
6. `DOCUMENTATION_ACCURACY_AUDIT_REPORT.md`
7. `PRE_PUSH_CI_READINESS_REPORT.md`
8. `docs/` (Entire directory: `docs/README.md`, `docs/architecture/`, `docs/development/`, `docs/decisions/`, `docs/audits/`, `docs/archive/`)

---

## 8. Files Requiring Manual Review

1. **Commit `3b50a1a` on `main`**:
   `3b50a1a` is already committed locally ahead of `origin/main`. When pushing `main` to `origin`, `3b50a1a` will be pushed along with Groups A and B. Review whether `3b50a1a` should be pushed as-is or if remote GitHub branch protection requires a pull request branch.
2. **Remote Supabase Database Revision**:
   Before running `alembic upgrade head` in staging or production environments, verify the current `alembic_version` in Supabase to confirm whether migrations `a1b2c3d4e5f7` through `d1e2f3a4b5c6` were already executed or if they will be applied sequentially.

---

## 9. Final Git Status

```text
On branch main
Your branch is ahead of 'origin/main' by 1 commit.
  (use "git push" to publish your local commits)

Changes not staged for commit:
	modified:   README.md
	modified:   apps/api/alembic/env.py
	modified:   apps/api/app/api/billing.py
	modified:   apps/api/app/config.py
	modified:   apps/api/app/models/billing.py
	modified:   apps/api/app/models/organization.py
	modified:   apps/api/app/models/subscription.py
	modified:   apps/api/app/schemas/billing.py
	modified:   apps/api/app/services/billing/__init__.py
	modified:   apps/api/app/services/billing/stripe_checkout_handler.py
	modified:   apps/api/app/services/billing/stripe_customer.py
	modified:   apps/api/app/services/billing/stripe_invoice_paid_handler.py
	modified:   apps/api/app/services/billing/stripe_invoice_payment_failed_handler.py
	modified:   apps/api/app/services/billing/stripe_subscription_deleted_handler.py
	modified:   apps/api/app/services/billing/stripe_subscription_updated_handler.py
	modified:   apps/api/tests/test_billing_api.py
	modified:   apps/api/tests/test_stripe_checkout_handler.py
	modified:   apps/api/tests/test_stripe_customer.py

Untracked files:
	.github/
	BILLING_ROADMAP.md
	DOCUMENTATION_ACCURACY_AUDIT_REPORT.md
	DOCUMENTATION_CLEANUP_PHASE_2_REPORT.md
	PRE_PUSH_CI_READINESS_REPORT.md
	PROJECT_STATUS.md
	TECHNICAL_DECISIONS.md
	apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py
	apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py
	apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py
	apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py
	apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py
	apps/api/app/models/payment.py
	apps/api/app/services/billing/razorpay/
	apps/api/app/workers/razorpay_webhook_worker.py
	apps/api/tests/test_payment_association_migration.py
	apps/api/tests/test_postgres_provenance_integration.py
	apps/api/tests/test_provider_neutral_billing_models.py
	apps/api/tests/test_provider_neutral_migration.py
	apps/api/tests/test_razorpay_agreement_handlers.py
	apps/api/tests/test_razorpay_checkout.py
	apps/api/tests/test_razorpay_client.py
	apps/api/tests/test_razorpay_config.py
	apps/api/tests/test_razorpay_customer.py
	apps/api/tests/test_razorpay_handler_foundation.py
	apps/api/tests/test_razorpay_webhook_ingestion.py
	apps/api/tests/test_razorpay_webhook_worker.py
	docs/
```

---

## 10. Exact Recommended Next Actions

When you are ready to authorize committing and pushing for GitHub Actions CI validation:

1. **Stage and Commit Group A (Billing Foundation & CI Workflow):**
   ```bash
   git add .github/
   git add apps/api/alembic/
   git add apps/api/app/
   git add apps/api/tests/
   git commit -m "feat(billing): add provider-neutral foundation, payment provenance migration, and PostgreSQL CI workflow"
   ```
2. **Stage and Commit Group B (Documentation & Canonical Portals):**
   ```bash
   git add README.md PROJECT_STATUS.md TECHNICAL_DECISIONS.md BILLING_ROADMAP.md docs/ *.md
   git commit -m "docs: reorganize documentation hierarchy, author canonical guides, and record ADRs 001-005"
   ```
3. **Push to Remote & Trigger CI:**
   ```bash
   git push origin main
   ```
4. **Monitor GitHub Actions CI Run:**
   - Verify that the `PostgreSQL 16 Integration & Provenance Verification` workflow triggers on GitHub.
   - Inspect job logs for the execution of the 14 PostgreSQL scenarios and 41 regression tests.
   - Download the test results artifact (`test-results-ci`).
5. **Authorization Gate:**
   - Only after GitHub Actions CI reports a 100% green pass on the 14 PostgreSQL integration scenarios is Step 5.2D.3.3B (Razorpay Payment Settlement Handlers) authorized for implementation.
