# D-04A Stage 3 — Implementation Completion Report
## Atomic PaymentReconciliation Upsert & Handler Conflict / FK / ORM Hardening

**Date:** 2026-11-14  
**Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`  
**Branch:** `implementation/d04a-stage1-exception-foundation`  
**Baseline HEAD Commit:** `497852e19360d85787107dbae6921519074f0773`  
**Authoritative References:**
- `docs/reports/D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` (§6.2, §6.3, §7.3, §12.1)
- `docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` (§4.1 ED-04, §7.2, §7.3, §9 Stage 3, §10.1)
- `docs/reports/D2_D04A_STAGE2_IMPLEMENTATION_REPORT.md`
- `docs/reports/D2_D04A_STAGE2_INDEPENDENT_REVIEW.md`

---

## 1. Executive Summary

Implementation of **D-04A Stage 3** has been completed successfully and verified.

Stage 3 resolves core architectural flaws and race vulnerabilities surrounding reconciliation persistence, foreign key safety, and ORM identity-map cache staleness:
1. **Atomic Reconciliation Upsert (`ON CONFLICT DO UPDATE`):** Implemented `build_payment_reconciliation_upsert_stmt()` and `upsert_payment_reconciliation()` using native PostgreSQL `sqlalchemy.dialects.postgresql.insert(PaymentReconciliation).on_conflict_do_update(...)` targeting unique constraint `uq_payment_reconciliations_case_key`. This structurally eliminates `SQLSTATE 23505` (`unique_violation`) race conditions when concurrent workers or duplicate webhook events attempt to create identical reconciliation case keys.
2. **Reopening Resolved Reconciliation Cases:** Configured `ON CONFLICT DO UPDATE` to automatically reopen previously resolved reconciliation cases (`resolved_refunded`, `resolved_ignored`) back to `pending_review` via a SQL `CASE` expression when newly conflicting events arrive for the same case key.
3. **Diagnostic History Preservation:** Implemented PostgreSQL JSONB array concatenation (`||`) and `jsonb_build_object` to append timestamped diagnostic entries to `diagnostic_payload["history"]` and update `diagnostic_payload["latest"]` without overwriting prior evidence.
4. **Spoofed UUID FK Trap Elimination (`SQLSTATE 23503`):** Fixed `resolve_tenant_organization()` in `handler_base.py` on `unbound_provider_customer` to set `organization_id=None` (preserving candidate org ID safely in sanitized `diagnostic_payload["notes_claimed_organization_id"]`), eliminating the PostgreSQL `23503` foreign key violation trap caused by unverified client notes UUIDs.
5. **Committed-FK Safety on Handler Conflicts:** Removed inline `stage_or_update_payment_reconciliation()` calls and uncommitted `sub.id` references from conflict paths in `agreement_handlers.py` (`ambiguous_checkout_attempt` and `checkout_attempt_tenant_mismatch`). Handlers now raise `RazorpayWebhookReconciliationConflictError` cleanly with `raw_event_id=ctx.event_id`, sanitized `diagnostic_payload`, and `subscription_id=None`, leaving reconciliation persistence to the outer atomic transaction.
6. **ORM Identity-Map Refresh (`populate_existing=True`):** Added `.execution_options(populate_existing=True)` on all 6 `FOR UPDATE` queries in `agreement_handlers.py` (`Subscription` and `BillingCheckoutAttempt`), ensuring that when a transaction wakes from lock contention, SQLAlchemy refreshes the in-memory object attributes directly from committed PostgreSQL row state.
7. **Elimination of Raw Payload Dumps:** Replaced raw request dumps (`"payload": ctx.raw_payload`) in observational happy-path reconciliations with allowlisted scalar metadata, preventing customer PII and secret leakage into the database.
8. **Comprehensive Verification:** 702 unique unit tests executed across 16 test files: **100% passing with zero failures and zero skips**.

---

## 2. Repository & Working-Tree Verification

### 2.1 Active Worktree & Branch Status
- **Worktree:** `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16`
- **Branch:** `implementation/d04a-stage1-exception-foundation`
- **Baseline HEAD:** `497852e19360d85787107dbae6921519074f0773`
- **Git Status Summary:**
  ```text
  Changes not staged for commit:
    modified:   apps/api/app/services/billing/razorpay/__init__.py
    modified:   apps/api/app/services/billing/razorpay/agreement_handlers.py
    modified:   apps/api/app/services/billing/razorpay/handler_base.py
    modified:   apps/api/app/services/billing/razorpay/webhook_engine.py
    modified:   apps/api/tests/test_razorpay_agreement_handlers.py
    modified:   apps/api/tests/test_razorpay_handler_foundation.py

  Untracked files:
    apps/api/app/services/billing/webhook_errors.py
    apps/api/tests/test_billing_webhook_errors.py
    apps/api/tests/test_razorpay_webhook_exceptions.py
    docs/reports/
  ```

### 2.2 Sibling Worktrees Verification
Sibling worktrees were inspected and confirmed completely untouched:
- `D:\Projects\V1\Aptlyst-AI` on branch `main` @ `027ab7a`: Untouched (13 pre-existing dirty files preserved).
- `D:\Projects\V1\Aptlyst-AI-pg16-validation` on branch `validation/postgres16-ci-hardening` @ `c932cc4`: Untouched (zero tracked changes).

---

## 3. Inventory of Files & SHA-256 Hashes

| File Path | Status | SHA-256 Hash | Notes |
|---|---|---|---|
| `apps/api/app/services/billing/razorpay/handler_base.py` | Modified | `B13E29110546F69CE642099DBE672D70F732F0F66DDCF12F801A4550A3EAF7C1` | Added upsert helper, fixed unbound org FK bug, added worker_now |
| `apps/api/app/services/billing/razorpay/agreement_handlers.py` | Modified | `23E5B0EE84E694B76AF745CC49BD6AE2808206D42F400A0D4967F6CB2F212202` | Added populate_existing=True (6 sites), removed inline conflict writes, sanitized payloads |
| `apps/api/app/services/billing/razorpay/__init__.py` | Modified | `442E0A6872D54B750E0DB22032C0BFCF637EE16CCF5748A0486C98C3528B7529` | Re-exported `build_payment_reconciliation_upsert_stmt` & `upsert_payment_reconciliation` |
| `apps/api/tests/test_razorpay_handler_foundation.py` | Modified | `2B3C28D7197A6635F2FA3759E960E2683660A7A85F760CCAA3DEEE2C3BFE8239` | Added `UNIT-REC-01` AST compilation test + unbound org_id=None assertion |
| `apps/api/tests/test_razorpay_agreement_handlers.py` | Modified | `E3CD09B38CA74ABBE6A506D60E24C4A98CEAC03F20AD7156CBBC5DB8BAC2AB53` | Added upsert interception in mock DB, updated conflict assertions |
| `apps/api/app/services/billing/razorpay/webhook_engine.py` | Unchanged from Stage 2 | `7098575BBBD68DDC9B144E6185F96A4B01D57AEF9CA20FDB940AA5869AAE912D` | Stage 2 canonical exception hierarchy intact |
| `apps/api/app/services/billing/webhook_errors.py` | Unchanged from Stage 1 | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | Stage 1 provider-neutral foundation intact |
| `apps/api/tests/test_billing_webhook_errors.py` | Unchanged from Stage 2 | `7130AB8156CA175B043EEB1C4CFCE79C6927075953B4975EED39B6EC1338C033` | Stage 1 & 2 tests intact |
| `apps/api/tests/test_razorpay_webhook_exceptions.py` | Unchanged from Stage 2 | `3C6C8406BDA919B45B0C0707D58EA947AC738B54B62A1E4B5CA4B1A125956AFA` | Stage 2 tests intact |

---

## 4. Detailed Technical Implementation

### 4.1 Native PostgreSQL Atomic Upsert (`build_payment_reconciliation_upsert_stmt`)
Implemented in `apps/api/app/services/billing/razorpay/handler_base.py`:
- Target constraint: `UniqueConstraint("provider", "merchant_account_id", "environment", "case_key", name="uq_payment_reconciliations_case_key")`.
- Native construct: `sqlalchemy.dialects.postgresql.insert(PaymentReconciliation).on_conflict_do_update(...)`.
- **Status Reopening Rule:**
  ```python
  "status": case(
      (
          PaymentReconciliation.status.in_([
              ReconciliationStatus.resolved_refunded,
              ReconciliationStatus.resolved_ignored,
          ]),
          ReconciliationStatus.pending_review,
      ),
      else_=PaymentReconciliation.status,
  )
  ```
  If a case was previously closed/resolved, a new conflicting delivery reopens the case to `pending_review` so finance operations triage it immediately, preventing unresolved discrepancies from remaining masked under historical resolutions.
- **Foreign Key Coalescing:**
  Foreign keys `organization_id`, `subscription_id`, `invoice_id`, `payment_id`, and `raw_event_id` use `func.coalesce(PaymentReconciliation.<col>, stmt.excluded.<col>)`. This ensures that if a field was previously populated, it is never nulled out by subsequent events with partial metadata.
- **Diagnostic History Merging:**
  Uses PostgreSQL JSONB concatenation:
  ```python
  payload_update = PaymentReconciliation.diagnostic_payload.concat(
      func.jsonb_build_object(
          "history",
          func.coalesce(
              PaymentReconciliation.diagnostic_payload["history"].as_json(),
              cast("[]", JSONB),
          ).concat(cast(json.dumps(new_history_entry), JSONB)),
          "latest",
          cast(json.dumps(clean_data), JSONB),
      )
  )
  ```
- **Allowlisted Sanitization:** Before query construction, diagnostic inputs pass through `sanitize_conflict_diagnostic_payload()`, which strips sensitive credential keys (`api_secret`, `razorpay_key_secret`, etc.) and scrubs embedded tokens via `sanitize_billing_error_message()`.
- **Atomic Returning:** Includes `.returning(PaymentReconciliation)` for direct access to the committed ORM/row values without secondary `SELECT` roundtrips.

### 4.2 Preserving Stage 1 Compatibility
In strict accordance with blueprint §4.1 (ED-04), `stage_or_update_payment_reconciliation()` in `handler_base.py` was kept 100% intact. This preserves existing consumers in `customer.py` and `checkout.py`, as well as `test_razorpay_customer.py`'s regex SQL subset parser in CI Step 13.

### 4.3 Eliminating the Spoofed-UUID `23503` FK Trap
In `handler_base.py::resolve_tenant_organization()`, when an incoming customer or subscription has no authoritative local binding, the code previously set `organization_id=notes_org_id`. If an attacker or external client supplied an arbitrary unverified UUID in `notes.organization_id`, inserting this value into `payment_reconciliations` triggered a fatal PostgreSQL `ForeignKeyViolationError` (`SQLSTATE 23503`) referencing `organizations.id`.

**Correction:**
```python
    # Case B: No authoritative record found (Unbound customer/subscription)
    entity_ref = clean_subscription_id or clean_customer_id or "unspecified_entity"
    logger.warning(
        "Unbound Razorpay entity %s: no authoritative customer provision or subscription found.",
        entity_ref,
    )
    raise RazorpayWebhookReconciliationConflictError(
        f"Unbound Razorpay entity '{entity_ref}'. No local customer provision or subscription found.",
        case_key=f"unbound_entity:{entity_ref}",
        reason="unbound_provider_customer",
        organization_id=None,  # STRICT: Never set unverified notes org ID as FK!
        diagnostic_payload={
            "customer_id": clean_customer_id,
            "subscription_id": clean_subscription_id,
            "notes_claimed_organization_id": str(notes_org_id) if notes_org_id else None,
        },
    )
```
The unverified UUID is safely retained in sanitized `diagnostic_payload["notes_claimed_organization_id"]` for audit inspection, while `organization_id` remains `None`.

### 4.4 Handler Conflict Staging & Committed-FK Safety
In `agreement_handlers.py`:
- In `handle_subscription_authenticated` and `handle_subscription_activated`, previously lines 221-250 and 285-315 executed inline `await stage_or_update_payment_reconciliation(...)` passing `subscription_id=sub.id`. If `sub` was newly instantiated inside the uncommitted transaction/savepoint, `sub.id` was an uncommitted UUID. When the handler raised `RazorpayWebhookReconciliationConflictError`, savepoint rollback cancelled the `Subscription` insert; any attempt to reference `sub.id` violated PostgreSQL FK constraints (`23503`).
- **Correction:** Removed inline `stage_or_update_payment_reconciliation()` calls from all conflict paths (`ambiguous_checkout_attempt` and `checkout_attempt_tenant_mismatch`). Handlers now raise `RazorpayWebhookReconciliationConflictError` cleanly, carrying `raw_event_id=ctx.event_id`, sanitized `diagnostic_payload`, and `subscription_id=None`. Outer Phase 3 (Stage 4) will execute atomic reconciliation persistence.

### 4.5 ORM Identity-Map Refresh (`populate_existing=True`)
Added `.execution_options(populate_existing=True)` on 6 critical `FOR UPDATE` queries:
1. `handle_subscription_authenticated`: `Subscription` query (`line 130`)
2. `handle_subscription_authenticated`: `BillingCheckoutAttempt` query (`line 213`)
3. `handle_subscription_activated`: `Subscription` query (`line 389`)
4. `handle_subscription_activated`: `BillingCheckoutAttempt` query (`line 485`)
5. `handle_subscription_updated`: `Subscription` query (`line 630`)
6. `handle_subscription_completed`: `Subscription` query (`line 754`)

**Technical Invariant:** Under PostgreSQL `READ COMMITTED`, when a session waits on a row lock held by another transaction that updates and commits the row, SQLAlchemy's default identity map returns the pre-lock cached in-memory instance without re-reading updated columns. Specifying `populate_existing=True` instructs SQLAlchemy to overwrite all attributes of existing objects in the identity map with the freshly loaded row data from PostgreSQL WAL.

### 4.6 Elimination of Raw Webhook Payload Dumps
In `handle_subscription_updated` and `handle_subscription_completed`, observational reconciliation cases previously stored `"payload": ctx.raw_payload`. This dumped unredacted gateway payloads (including card details, customer emails, phone numbers, and notes) into `payment_reconciliations`.

**Correction:** Replaced raw payload dumps with explicit allowlisted scalar metadata:
- `provider_subscription_id`
- `provider_plan_id`
- `provider_customer_id`
- `subscription_status`
- `organization_id`
- `event_type`
- `provider_event_id`

Zero instances of `ctx.raw_payload` are now passed to reconciliation persistence.

---

## 5. Test Execution & Verification

### 5.1 Test Execution Commands and Results

#### Canonical Group A: Webhook & Billing Suite (9 files, 327 unique tests)
All 9 files are dedicated to webhook errors, ingress, engine, handlers, clients, configuration, and worker logic:
- `tests/test_billing_webhook_errors.py`: 163 passed
- `tests/test_razorpay_webhook_exceptions.py`: 36 passed
- `tests/test_razorpay_handler_foundation.py`: 25 passed (includes new `UNIT-REC-01` test)
- `tests/test_razorpay_agreement_handlers.py`: 18 passed
- `tests/test_razorpay_webhook_worker.py`: 23 passed
- `tests/test_razorpay_webhook_ingestion.py`: 23 passed
- `tests/test_razorpay_client.py`: 13 passed
- `tests/test_razorpay_config.py`: 8 passed
- `tests/test_stripe_webhook_engine.py`: 18 passed
**Subtotal Group A:** **327 unique tests** (100% passed, 0 failed, 0 skipped).

#### Canonical Group B: CI Step 13 Offline Regression Suite (7 files, 375 unique tests)
The exact 7 files specified in `.github/workflows/ci.yml` Step 13:
- `tests/test_postgres_test_guard.py`: 118 passed
- `tests/test_payment_association_migration.py`: 17 passed
- `tests/test_provider_neutral_billing_models.py`: 15 passed
- `tests/test_provider_neutral_migration.py`: 9 passed
- `tests/test_razorpay_customer.py`: 92 passed
- `tests/test_razorpay_checkout.py`: 79 passed
- `tests/test_billing_api.py`: 45 passed
**Subtotal Group B:** **375 unique tests** (100% passed, 0 failed, 0 skipped).

#### Overlap and Disjoint Surface Analysis
- **Group A Unique Node IDs:** 327
- **Group B Unique Node IDs:** 375
- **Intersection (Group A ∩ Group B):** **0** (The file sets and node IDs are completely disjoint).
- **Grand Total Unique Test Surface (Group A ∪ Group B):** **702 unique tests** (327 + 375 = 702).
- **Execution Outcome:** **702 passed, 0 failed, 0 skipped** across offline suites.

*(Note on ad-hoc 495-test run: An exploratory test run executed an ad-hoc 9-file combination substituting customer, checkout, billing_api, and postgres_provenance_integration, which collected 495 tests yielding 481 passed and 14 skipped due to live PostgreSQL being offline. That ad-hoc run was not the canonical 9-file Group A suite and contains overlap with Group B; the authoritative unique test surface remains exactly 702 disjoint items).*

### 5.2 `UNIT-REC-01` Test Details
Added `test_unit_rec_01_postgresql_upsert_statement_ast_and_compilation` in `test_razorpay_handler_foundation.py`:
- Compiles `build_payment_reconciliation_upsert_stmt()` against `sqlalchemy.dialects.postgresql.dialect()`.
- Asserts exact SQL text matches:
  - `ON CONFLICT ON CONSTRAINT uq_payment_reconciliations_case_key DO UPDATE SET`
  - `coalesce(payment_reconciliations.<col>, excluded.<col>)` across all 5 FK columns
  - `reason = excluded.reason` and `updated_at = excluded.updated_at`
  - `status = CASE WHEN (payment_reconciliations.status IN ...) THEN ... ELSE ... END`
  - `jsonb_build_object` and `||` JSONB array concatenation
  - `RETURNING payment_reconciliations.id`
- Verifies that diagnostic payload inputs undergo strict sanitization (sensitive keys dropped entirely, embedded secrets redacted via `[REDACTED_KEY]`).

---

## 6. Scope Boundaries & Explicit Deferrals

In strict accordance with the Stage 3 authorization:
1. **Stage 4 Scope Deferred:**
   - Did **NOT** modify `apps/api/app/workers/razorpay_webhook_worker.py`.
   - Did **NOT** implement `execute_claimed_razorpay_webhook_event()` single atomic TX engine.
   - Did **NOT** alter the worker transaction loop, claim/lease logic, or batch cancellation propagation.
2. **Stage 5 Scope Deferred:**
   - Did **NOT** modify `.github/workflows/ci.yml`.
   - Did **NOT** execute or claim live multi-connection PostgreSQL 16 transaction tests (`PG-WRK-*`).
3. **General Boundaries:**
   - Did **NOT** modify `customer.py`, `checkout.py`, `stripe/`, or database models/migrations.
   - Did **NOT** execute `git commit`, `git add`, `git push`, `git merge`, or change branches/worktrees.

---

## 7. Stage 4 Readiness Recommendation

D-04A Stage 3 is fully implemented, verified, and self-contained. All requirements from `D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md` and `D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` for Stage 3 have been met.

### Recommendation:
**Proceed to Stage 3 Independent Review** before authorizing Stage 4 (`execute_claimed_razorpay_webhook_event()` and worker transaction engine).
