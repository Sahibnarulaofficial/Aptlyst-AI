# D-04A Stage 3 — Test-Count Reconciliation Report

**Date:** 2026-11-14  
**Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`  
**Branch:** `implementation/d04a-stage1-exception-foundation`  
**Baseline HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`  
**Python Runtime:** `D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe` (Python 3.11.0, pytest-8.3.5)  
**Execution Environment:** Isolated worktree with `-p no:cacheprovider` and `PYTHONPATH=".;app"`  

---

## 1. Executive Summary

This targeted reconciliation resolves the test-count accounting across the D-04A Stage 3 test surface, explaining the exact provenance of and relationship between the reported numbers (**327**, **375**, **495**, and **702**):

1. **Group A (Canonical Webhook & Billing Suite):** Exactly **327 unique test items** collected across 9 dedicated webhook/billing test files (100% passed, 0 failed, 0 skipped).
2. **Group B (CI Step 13 Offline Regression Suite):** Exactly **375 unique test items** collected across the 7 files specified in `.github/workflows/ci.yml` (100% passed, 0 failed, 0 skipped).
3. **Disjoint Sets (Zero Overlap):** Group A and Group B share **0 files in common** and **0 test node IDs in common** (`Group A ∩ Group B = ∅`).
4. **Authoritative Total Surface:** The union of Group A and Group B comprises exactly **702 unique collected test node IDs** (`327 + 375 = 702`), all passing offline.
5. **Ad-Hoc 495-Test Run Clarified:** The count of 495 tests (481 passed, 14 skipped) stemmed from an ad-hoc 9-file test run that substituted 3 files from Group B (`customer`, `checkout`, `billing_api`) and added `test_postgres_provenance_integration.py` (which skips 14 tests offline). That run was not the canonical Group A suite.

---

## 2. Separate Collection Commands & Exact Node IDs

### 2.1 Group A: Canonical Webhook & Billing Suite (9 files)

**Command Executed:**
```powershell
Set-Location "D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16\apps\api"
$env:PYTHONPATH = ".;app"
& "D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe" -B -m pytest --collect-only -q -p no:cacheprovider `
    tests/test_billing_webhook_errors.py `
    tests/test_razorpay_webhook_exceptions.py `
    tests/test_razorpay_handler_foundation.py `
    tests/test_razorpay_agreement_handlers.py `
    tests/test_razorpay_webhook_worker.py `
    tests/test_razorpay_webhook_ingestion.py `
    tests/test_razorpay_client.py `
    tests/test_razorpay_config.py `
    tests/test_stripe_webhook_engine.py
```

**Collection Output:**
`327 tests collected in 4.05s (exit code 0)`

**Per-File Item Breakdown:**
| File Path | Collected Node IDs | Primary Scope |
|---|---|---|
| `tests/test_billing_webhook_errors.py` | 163 | Stage 1 provider-neutral exception foundation, SQLSTATE classifier, sanitizer |
| `tests/test_razorpay_webhook_exceptions.py` | 36 | Stage 2 canonical Razorpay exception hierarchy, classifiers, and aliases |
| `tests/test_razorpay_handler_foundation.py` | 25 | Foundation schemas, lease fencing, anti-spoofing, and `UNIT-REC-01` AST test |
| `tests/test_razorpay_agreement_handlers.py` | 18 | Agreement handlers, conflict raising, `upsert_payment_reconciliation` mock double |
| `tests/test_razorpay_webhook_worker.py` | 23 | Worker claim loop, dispatch outcomes, and retry schedules |
| `tests/test_razorpay_webhook_ingestion.py` | 23 | Ingress parsing, HMAC signature verification, payload bounds |
| `tests/test_razorpay_client.py` | 13 | Client error mapping, API error classifications, credential sanitization |
| `tests/test_razorpay_config.py` | 8 | Configuration parameter parsing and validation |
| `tests/test_stripe_webhook_engine.py` | 18 | Stripe webhook engine regression isolation |
| **Total Group A Collected Node IDs** | **327** | **Zero duplicates within group** |

---

### 2.2 Group B: CI Step 13 Offline Regression Suite (7 files)

**Command Executed:**
```powershell
Set-Location "D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16\apps\api"
$env:PYTHONPATH = ".;app"
& "D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe" -B -m pytest --collect-only -q -p no:cacheprovider `
    tests/test_postgres_test_guard.py `
    tests/test_payment_association_migration.py `
    tests/test_provider_neutral_billing_models.py `
    tests/test_provider_neutral_migration.py `
    tests/test_razorpay_customer.py `
    tests/test_razorpay_checkout.py `
    tests/test_billing_api.py
```

**Collection Output:**
`375 tests collected in 4.38s (exit code 0)`

**Per-File Item Breakdown:**
| File Path | Collected Node IDs | Primary Scope |
|---|---|---|
| `tests/test_postgres_test_guard.py` | 118 | PostgreSQL test guard, AST dialect validation, fixture isolation |
| `tests/test_payment_association_migration.py` | 17 | Payment association schema and migration integrity |
| `tests/test_provider_neutral_billing_models.py` | 15 | Provider-neutral billing data models and table definitions |
| `tests/test_provider_neutral_migration.py` | 9 | Migration rollback and forward verification |
| `tests/test_razorpay_customer.py` | 92 | Customer provisioning, session ownership, and regex SQL parser |
| `tests/test_razorpay_checkout.py` | 79 | Checkout creation, recovery, and signature verification |
| `tests/test_billing_api.py` | 45 | Billing REST endpoints and portal URL validation |
| **Total Group B Collected Node IDs** | **375** | **Zero duplicates within group** |

---

## 3. Mathematical Overlap Analysis

Using the collected node IDs captured into external scratch files:

```text
Group A unique node IDs:              327
Group B unique node IDs:              375
Duplicates within Group A:              0
Duplicates within Group B:              0
Intersection (Group A ∩ Group B):       0
Union (Group A ∪ Group B):            702
```

### Key Architectural Observation:
- Group A consists exclusively of webhook engine, worker, error hierarchy, client, and agreement handler tests.
- Group B consists exclusively of database models, migrations, test guard fixtures, customer provisioning, checkout services, and API endpoints.
- Because these two suites inspect separate layers of the billing architecture, their file sets and collected test node IDs are **strictly disjoint**.
- The mathematical union is strictly the sum of both sets:
  $$\text{Total Unique Tests} = 327 + 375 = 702$$

---

## 4. Origin and Explanation of Previous Count Discrepancies

### 4.1 Progression from Stage 2 to Stage 3 Baseline
- In **Stage 2** (`D2_D04A_STAGE2_IMPLEMENTATION_REPORT.md` Section 4.1):
  - Group A (9 files) had **326 tests** (`test_razorpay_handler_foundation.py` had 24 tests).
  - Group B (7 files) had **375 tests**.
  - Total Stage 2 surface: **701 tests** ($326 + 375 = 701$).
- In **Stage 3** (`D2_D04A_STAGE3_IMPLEMENTATION_REPORT.md` Section 5):
  - `UNIT-REC-01` (`test_unit_rec_01_postgresql_upsert_statement_ast_and_compilation`) was added to `tests/test_razorpay_handler_foundation.py`, bringing its test count from 24 to **25**.
  - Group A increased from 326 to **327 tests**.
  - Group B remained unchanged at **375 tests**.
  - Total Stage 3 surface increased from 701 to **702 tests** ($327 + 375 = 702$).

### 4.2 The Ad-Hoc 495-Test Run Discrepancy
During post-implementation verification, an ad-hoc 9-file command was executed:
```bash
pytest tests/test_razorpay_handler_foundation.py (25) \
       tests/test_razorpay_agreement_handlers.py (18) \
       tests/test_billing_webhook_errors.py (163) \
       tests/test_razorpay_webhook_exceptions.py (36) \
       tests/test_razorpay_checkout.py (79) \
       tests/test_razorpay_customer.py (92) \
       tests/test_razorpay_webhook_worker.py (23) \
       tests/test_billing_api.py (45) \
       tests/test_postgres_provenance_integration.py (14)
```
- Total collected items: $25 + 18 + 163 + 36 + 79 + 92 + 23 + 45 + 14 = 495$.
- Result: **481 passed, 14 skipped** (the 14 provenance integration tests skip when live PostgreSQL 16 is offline).
- **Cause of Confusion:** That ad-hoc run swapped out 4 webhook files (`ingestion`, `client`, `config`, `stripe_webhook_engine`) and substituted 3 Group B files (`checkout`, `customer`, `billing_api`) plus 1 live integration file. It was an ad-hoc verification run, *not* the canonical 327-test Group A suite.

---

## 5. Documentation Corrections Completed

1. **`docs/reports/D2_D04A_STAGE3_IMPLEMENTATION_REPORT.md` Section 5.1:**
   - Updated to explicitly list all 9 files of Group A (327 tests) and all 7 files of Group B (375 tests).
   - Documented the exact disjoint overlap (intersection = 0, union = 702).
   - Clarified the ad-hoc 495-test run as a non-canonical exploratory command.
2. **`docs/reports/D2_D04A_STAGE3_INDEPENDENT_REVIEW.md`:**
   - Left untouched in accordance with instructions.

---

## 6. Execution Limitations & Environmental Boundary Confirmation

- All test collection was performed offline using `-p no:cacheprovider` to prevent Windows worktree subprocess teardown hangs.
- Zero source code files or tests were modified during this reconciliation.
- Sibling worktrees `D:\Projects\V1\Aptlyst-AI` and `D:\Projects\V1\Aptlyst-AI-pg16-validation` remain untouched.
- The Stage 3 baseline is verified at HEAD `497852e19360d85787107dbae6921519074f0773`.
