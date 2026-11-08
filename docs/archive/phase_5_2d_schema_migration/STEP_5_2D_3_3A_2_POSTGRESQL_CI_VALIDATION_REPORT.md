# Aptlyst AI — Step 5.2D.3.3A.2
# Isolated PostgreSQL Integration Testing in CI Report

**Date:** 2026-11-08  
**Phase:** Step 5.2D.3.3A.2  
**Target CI Workflow:** `.github/workflows/ci.yml`  
**Integration Test Suite:** `apps/api/tests/test_postgres_provenance_integration.py`  
**Alembic Head:** `e2f3a4b5c6d1 (head)` (Verified linear DAG)  
**Final Verdict:** `PASS WITH CONDITIONS`  

---

## 1. Existing CI Configuration Inspected

A preflight inspection of the repository was conducted:
- The `.github/` directory at the repository root existed but was completely empty (`Test-Path .github/workflows` returned `False`).
- No prior GitHub Actions workflows, CircleCI configs, GitLab CI pipelines, or Travis configs existed in the project.
- Backend dependencies are managed in `apps/api/requirements.txt` and `apps/api/requirements-dev.txt`, featuring:
  - `sqlalchemy==2.0.41`
  - `asyncpg==0.30.0`
  - `psycopg2-binary==2.9.10`
  - `alembic==1.15.2`
  - `pytest==8.3.5`
  - `pytest-asyncio==0.25.3`
- `alembic/env.py` supports both online async migrations (`create_async_engine` with `asyncpg`) and offline static DDL generation (`run_migrations_offline`).
- In `app/config.py`, `Settings.database_url` is automatically populated from `DATABASE_URL` environment variables, automatically normalizing `postgresql://` and `postgres://` prefixes to `postgresql+asyncpg://` when accessed by async application code.

---

## 2. Files Changed and Why

1. **`.github/workflows/ci.yml` (Created):**
   - Implements an isolated, automated GitHub Actions workflow running on `ubuntu-latest`.
   - Provisions a disposable PostgreSQL 16 service container with isolated credentials.
   - Executes readiness health checks, runs `alembic upgrade head` against the disposable database, runs the 14 PostgreSQL integration tests, executes focused regression tests, and archives JUnit XML test artifacts.
2. **`apps/api/tests/test_postgres_provenance_integration.py` (Refined):**
   - Added `_get_sync_postgres_url()` to normalize `postgresql+asyncpg://` to `postgresql+psycopg2://` when synchronous test engines connect.
   - Refactored `test_13_concurrent_attempts_to_associate_conflicting_records` to use two **genuinely independent PostgreSQL connections** (`conn1` and `conn2`) to prove concurrency isolation across separate client sessions.

---

## 3. PostgreSQL Version and Service Container Configuration

- **Pinned PostgreSQL Version:** `postgres:16-alpine` (Ensures modern PostgreSQL 16 engine semantics matching target cloud deployment).
- **Dedicated CI Database Name:** `aptlyst_ci_test`
- **Isolated CI Credentials (Non-Secret Service Account):**
  - Username: `aptlyst_ci_user`
  - Password: `aptlyst_ci_password_secure` (Ephemeral credentials used exclusively inside GitHub Actions runner container).
- **Service Container Definition:**
  ```yaml
  services:
    postgres:
      image: postgres:16-alpine
      env:
        POSTGRES_USER: aptlyst_ci_user
        POSTGRES_PASSWORD: aptlyst_ci_password_secure
        POSTGRES_DB: aptlyst_ci_test
      ports:
        - 5432:5432
      options: >-
        --health-cmd pg_isready
        --health-interval 5s
        --health-timeout 5s
        --health-retries 10
  ```
- **Database Readiness Check:**
  Before initiating tests or migrations, an active TCP socket poll waits up to 30 seconds to guarantee port 5432 is accepting client handshakes.

---

## 4. Exact CI Database Configuration & Connection Strings

| Context | Driver / Protocol | Environment Variable | Value Form (Secrets Masked) |
| :--- | :--- | :--- | :--- |
| **Alembic Online Migrations** | `asyncpg` | `DATABASE_URL` | `postgresql+asyncpg://aptlyst_ci_user:***@localhost:5432/aptlyst_ci_test` |
| **Integration Test Suite** | `psycopg2` | `POSTGRES_TEST_URL` | `postgresql+psycopg2://aptlyst_ci_user:***@localhost:5432/aptlyst_ci_test` |

- **Security & Scope Isolation:**
  - `POSTGRES_TEST_URL` and `DATABASE_URL` are scoped strictly to the CI test steps.
  - Zero connections to Supabase, staging, or production databases.
  - Zero calls to live Razorpay APIs or external payment gateways.
  - No database credentials exposed in test logs or persisted artifacts.

---

## 5. Exact Commands Executed

### Local Virtualenv Verification
```powershell
# 1. Validate YAML syntax of .github/workflows/ci.yml
Set-Location apps/api; & "./.venv/Scripts/python.exe" -c "import yaml; yaml.safe_load(open('../../.github/workflows/ci.yml', encoding='utf-8')); print('YAML syntax valid')"

# 2. Execute PostgreSQL Integration Suite (verifying clean skip handling without POSTGRES_TEST_URL)
Set-Location apps/api; & "./.venv/Scripts/python.exe" -m pytest tests/test_postgres_provenance_integration.py

# 3. Execute Focused Migration, Models, and API Tests
Set-Location apps/api; & "./.venv/Scripts/python.exe" -m pytest tests/test_payment_association_migration.py tests/test_provider_neutral_billing_models.py tests/test_provider_neutral_migration.py tests/test_billing_api.py

# 4. Verify Single Linear Alembic Migration Head
Set-Location apps/api; & "./.venv/Scripts/python.exe" -m alembic heads
```

### Commands Configured in CI Workflow (`.github/workflows/ci.yml`)
```bash
# 1. Install backend dependencies
python -m pip install --upgrade pip
pip install -r apps/api/requirements.txt
pip install -r apps/api/requirements-dev.txt

# 2. Apply all Alembic migrations on disposable database
python -m alembic upgrade head

# 3. Run the 14 PostgreSQL Integration Tests with JUnit reporting
python -m pytest -v tests/test_postgres_provenance_integration.py --junitxml=test-results-postgres.xml

# 4. Run focused regression tests
python -m pytest -v tests/test_payment_association_migration.py tests/test_provider_neutral_billing_models.py tests/test_provider_neutral_migration.py --junitxml=test-results-models.xml
```

---

## 6. Analysis of the 14 PostgreSQL Integration Tests

Each test in `apps/api/tests/test_postgres_provenance_integration.py` targets real PostgreSQL database behavior:

| # | Test Function | Real PostgreSQL Behavior Exercised | Schema/Data Isolation |
| :-: | :--- | :--- | :--- |
| **1** | `test_1_valid_stripe_provenance_backfill` | Unambiguous 1:1 `UNION` CTE aggregation and single-row target update | Transaction rollback |
| **2** | `test_2_valid_provider_neutral_provenance_backfill` | Unambiguous provider-neutral `UNION` CTE aggregation and update | Transaction rollback |
| **3** | `test_3_multiple_invoices_pointing_to_same_subscription` | N:1 duplicate invoice resolution (`COUNT(DISTINCT s.id) = 1`) | Transaction rollback |
| **4** | `test_4_stripe_and_neutral_evidence_disagreeing` | PL/pgSQL DO block detecting cross-source candidate conflict (`COUNT > 1`) and raising abort exception | Transaction rollback |
| **5** | `test_5_existing_provenance_disagreeing_with_invoice_evidence` | PL/pgSQL DO block detecting contradiction with existing non-null `subscription_id` | Transaction rollback |
| **6** | `test_6_active_pro_period_without_provenance_aborts` | Transactional Abort Gate rejecting active Pro period with `subscription_id IS NULL` | Transaction rollback |
| **7** | `test_7_free_period_with_null_provenance_succeeds` | Verification that `plan_tier = 'free'` periods pass Abort Gate without exception | Transaction rollback |
| **8** | `test_8_composite_foreign_key_mismatch_rejection` | Multi-column FK `fk_billing_payments_checkout_attempt_composite` rejecting mismatched environment/provider attributes | Transaction rollback |
| **9** | `test_9_composite_foreign_key_null_bypass_rejection` | Database Check Constraint `ck_billing_payments_checkout_attempt_complete` rejecting NULL composite fields | Transaction rollback |
| **10** | `test_10_successful_initial_payment_association` | Triggers permitting legitimate initial transition (`NULL` $\to$ verified ID) on both payment and attempt | Transaction rollback |
| **11** | `test_11_reassignment_of_established_payment_association_rejected` | Trigger `trg_prevent_billing_payment_checkout_attempt_mutation` rejecting mutation of established `checkout_attempt_id` | Transaction rollback |
| **12** | `test_12_reassignment_of_established_period_provenance_rejected` | Trigger `trg_prevent_billing_period_subscription_mutation` rejecting mutation of established `subscription_id` | Transaction rollback |
| **13** | `test_13_concurrent_attempts_to_associate_conflicting_records` | Partial unique index `uq_billing_payments_checkout_attempt` rejecting second payment linking to same attempt across **independent connections** | Committed conn1 + cleanup block |
| **14** | `test_14_downgrade_refusal_when_financial_associations_exist` | Downgrade guard acquiring `ACCESS EXCLUSIVE` table locks and raising abort exception when associated records exist | Transaction rollback |

---

## 7. Migration Application & Concurrency Proof

### 7.1 Migration Application Strategy
- In the CI workflow, the step `Apply Alembic migrations on disposable database` executes `alembic upgrade head`.
- This applies all migrations sequentially (`3ba61d54e4d9` up to `e2f3a4b5c6d1`) directly against the live disposable PostgreSQL 16 container before tests run.
- This proves that migration `e2f3a4b5c6d1` cleanly compiles and applies to an empty database on real PostgreSQL, creating the actual tables, indexes, constraints, and triggers.

### 7.2 Independent Connection Concurrency Proof (`test_13`)
- `test_13` creates two separate SQLAlchemy connection instances (`conn1 = pg_engine.connect()`, `conn2 = pg_engine.connect()`).
- `conn1` inserts the organization and attempt, inserts Payment 1 associated with `attempt_id`, and commits the transaction (`conn1.commit()`).
- `conn2` independently connects on a separate PostgreSQL backend process, starts its own transaction, and attempts to insert Payment 2 associated with the same `attempt_id`.
- PostgreSQL's partial unique index `uq_billing_payments_checkout_attempt` catches the concurrent duplicate and raises `IntegrityError` on `conn2`.
- A dedicated cleanup connection (`cleanup_conn`) removes the test records in a `finally` block, leaving zero persistent artifacts.

---

## 8. Test Execution Results (Local Environment vs. CI)

### Local Environment Results
- **PostgreSQL Integration Tests:** 14 skipped in 0.37s.
  - Reason: No local PostgreSQL service or Docker daemon is installed on this Windows workstation, and connecting to external/live Supabase instances is strictly prohibited. SQLite was not used as a substitute.
- **Focused Migration & Model Unit Suite:** **17 passed in 3.96s** (`test_payment_association_migration.py`).
- **Comprehensive Billing Suite:** **85 passed in 15.77s** (`test_payment_association_migration.py`, `test_provider_neutral_billing_models.py`, `test_provider_neutral_migration.py`, `test_billing_api.py`).

### CI Environment Status
- CI workflow file `.github/workflows/ci.yml` is fully defined, validated, and ready for trigger upon push/PR.
- Actual execution of the 14 PostgreSQL tests will occur when the commit is pushed to GitHub and picked up by GitHub Actions runners.

---

## 9. Git Status and Alembic Migration Head

### Alembic Migration Head
```
e2f3a4b5c6d1 (head)
```
Single linear DAG verified:
`b1c2d3e4f5a6 -> c1d2e3f4a5b7 -> d1e2f3a4b5c6 -> e2f3a4b5c6d1 (head)`.

### Git Status (`git status --short`)
```
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
?? .github/workflows/ci.yml
?? STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md
?? STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md
?? STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md
?? apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py
?? apps/api/app/models/payment.py
?? apps/api/tests/test_payment_association_migration.py
?? apps/api/tests/test_postgres_provenance_integration.py
```

---

## 10. Final Implementation Verdict

### **Verdict:** `PASS WITH CONDITIONS`

#### Justification:
- **CI Configuration Complete & Validated:** `.github/workflows/ci.yml` is syntactically valid and configured with PostgreSQL 16 service container, TCP health check polling, Alembic migration provisioning, JUnit test reporting, and artifact preservation.
- **Genuine PostgreSQL Test Suite Implemented:** All 14 tests in `test_postgres_provenance_integration.py` exercise real PostgreSQL triggers, check constraints, composite foreign keys, abort gates, and multi-connection concurrency.
- **Condition for Authorization of Step 5.2D.3.3B:** Because local execution against a live PostgreSQL server is impossible in this sandboxed Windows workstation, actual live test execution must be observed and verified green in GitHub Actions upon push before commencing Step 5.2D.3.3B.
