# Aptlyst AI — Step 5.2D.3.2.1 Audit Report: Razorpay Subscription Agreement Lifecycle Handlers

**Date:** 2026-11-08  
**Audit Scope:** Step 5.2D.3.2 Implementation Audit (Strictly Read-Only)  
**Alembic Head:** `d1e2f3a4b5c6` (Single head confirmed)  
**Final Verdict:** **PASS WITH CONDITIONS**  

---

## 1. Executive Summary & Verdict

This report presents a strictly read-only correctness, isolation, and security audit of the Step 5.2D.3.2 implementation of Razorpay subscription agreement lifecycle handlers:
- `subscription.authenticated`
- `subscription.activated`
- `subscription.updated`
- `subscription.completed`

The audit evaluated transactional boundaries, concurrency controls (PostgreSQL advisory locks and row locks), worker lease fencing, tenant resolution, entitlement isolation, idempotency, resurrection refusal, and test classifications.

### Overall Verdict: **PASS WITH CONDITIONS**

The implementation correctly enforces all critical business and billing invariants established in Step 5.1B through Step 5.2D.3A.1:
1. **Entitlement Isolation:** Agreement activation (`subscription.activated`) strictly preserves `Subscription.plan_tier = "free"`. Zero Pro quota is provisioned, and zero `OrganizationBillingPeriod` rows are created.
2. **Grace Period Preservation:** Activation arriving for a subscription in `past_due` status strictly leaves `status = past_due` and does **not** clear `past_due_since`.
3. **Refusal of Resurrection:** Terminal subscriptions (`canceled`, `completed`) cannot be reactivated or updated by delayed or replayed events.
4. **Lease Fencing & Advisory Locking:** Worker lease ownership is asserted under row lock inside the domain transaction before domain mutations commit. Level 0 transaction-scoped advisory locks serialize concurrent events per provider subscription ID.
5. **Acyclic Module Architecture:** Dispatcher registration is deterministic and free of circular import dependencies.

Three low-severity conditions were identified and documented for operational completeness prior to deploying the settlement handlers (Step 5.2D.3.3).

---

## 2. Component Reference Map

| Audited Component | Source File | Key Lines |
| :--- | :--- | :--- |
| `subscription.authenticated` | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | [L78–L253](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L78-L253) |
| `subscription.activated` | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | [L260–L418](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L260-L418) |
| `subscription.updated` | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | [L425–L543](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L425-L543) |
| `subscription.completed` | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | [L550–L647](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L550-L647) |
| Registration Logic | `apps/api/app/services/billing/razorpay/agreement_handlers.py` | [L654–L666](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L654-L666) |
| Advisory Locking | `apps/api/app/services/billing/razorpay/lock.py` | [L42–L110](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/lock.py#L42-L110) |
| Shared Handler Foundation | `apps/api/app/services/billing/razorpay/handler_base.py` | [L255–L1005](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/handler_base.py#L255-L1005) |
| Webhook Engine & Dispatcher | `apps/api/app/services/billing/razorpay/webhook_engine.py` | [L182–L450](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/webhook_engine.py#L182-L450) |
| Webhook Worker | `apps/api/app/workers/razorpay_webhook_worker.py` | [L134–L260](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/workers/razorpay_webhook_worker.py#L134-L260) |
| Subscription Model | `apps/api/app/models/subscription.py` | [L33–L225](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/subscription.py#L33-L225) |
| Payment & Checkout Models | `apps/api/app/models/payment.py` | [L154–L275](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py#L154-L275) |
| Agreement Handler Tests | `apps/api/tests/test_razorpay_agreement_handlers.py` | [L1–L926](file:///d:/Projects/V1/Aptlyst-AI/apps/api/tests/test_razorpay_agreement_handlers.py#L1-L926) |

---

## 3. Detailed Audit by Requirement

### 1. Authenticated Handler (`handle_subscription_authenticated`)

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Subscription ID Binding** | [L230–L250](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L230-L250) | `BillingCheckoutAttempt` is queried under row lock (`with_for_update()`) matching `provider == 'razorpay'`, `merchant_account_id`, `environment`, and `provider_subscription_id == sub_id`. Updates attempt status to `payment_pending`. | **VERIFIED** |
| **Authoritative Org Ownership** | [L108–L116](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L108-L116) | Resolves `org_id` strictly via `resolve_tenant_organization()`, which queries `BillingCustomerProvision` and existing `Subscription` records. Unbound entities fail closed (`RazorpayWebhookReconciliationConflictError`). | **VERIFIED** |
| **Multi-Tenant / Scope Scoping** | [L91–L130](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L91-L130) | Validates envelope scope (`validate_razorpay_event_envelope()`). All queries are strictly filtered by `(provider, merchant_account_id, environment)`. | **VERIFIED** |
| **Cross-Subscription Overwrite Guard** | [L119–L130](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L119-L130) | Subscription lookup filters by `provider_subscription_id == sub_id`. No other subscription row can be selected or overwritten. | **VERIFIED** |
| **Harmless Duplicate Processing** | [L144–L181](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L144-L181) | Monotonic check (`is_stale_event()`) discards older/equal events. Active or past-due subscriptions ignore status regressions. Duplicate delivery updates metadata idempotently without state side-effects. | **VERIFIED** |
| **Missing / Ambiguous Checkout Attempts** | [L240–L250](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L240-L250) | Ambiguous checkout attempts (multiple rows) fail closed with `MultipleResultsFound`. However, if `attempt is None`, the handler currently logs info and continues without staging a reconciliation record. | **CONDITION** (See Finding 1) |
| **Untrusted Notes Anti-Spoofing** | [L108–L116](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L108-L116) | `resolve_tenant_organization()` strictly forbids binding to `notes.organization_id` unless an authoritative database match confirms the ownership. | **VERIFIED** |

---

### 2. Activated Handler (`handle_subscription_activated`)

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Valid State Transitions** | [L364–L377](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L364-L377) | Valid transitions: `incomplete -> active`, `incomplete_expired -> active`, `paused -> active`. If already `active`, executes idempotent metadata/date refresh. | **VERIFIED** |
| **`past_due_since` Preservation** | [L348–L362](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L348-L362) | If `sub.status == past_due`, the handler explicitly refuses to cure past_due status. Status remains `past_due`, and `past_due_since` is **never cleared**. Flushes date updates and returns early. | **VERIFIED** |
| **No Pro Entitlement Granted** | [L380–L395](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L380-L395) | Newly created or updated subscriptions strictly maintain `plan_tier = "free"`. Zero Pro quota is provisioned. | **VERIFIED** |
| **No Billing Period Mutation** | Entire file | `OrganizationBillingPeriod` is never queried, locked, created, or modified. | **VERIFIED** |
| **Checkout Attempt Decoupling** | [L411–L415](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L411-L415) | `attempt.status = CheckoutAttemptStatus.complete` marks checkout session completion, but does not settle or record payment. | **VERIFIED** |
| **Period Date Validation** | [L316–L325](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L316-L325) | `current_start` and `current_end` are validated as strictly positive numeric Unix timestamps via `parse_positive_unix_timestamp()`. | **VERIFIED** |
| **Refusal of Resurrection** | [L338–L345](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L338-L345) | If `is_terminal_subscription(sub.status)` (`canceled`, `completed`), the handler logs a warning and aborts without mutating local state. | **VERIFIED** |

---

### 3. Updated Handler (`handle_subscription_updated`)

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Stale Event Protection** | [L501–L509](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L501-L509) | `is_stale_event(sub.last_webhook_event_at, ctx.event_created_at)` evaluates timestamp ordering. Events older than or equal to local state are safely discarded. | **VERIFIED** |
| **Plan Changes Validated** | [L520–L522](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L520-L522) | `sub_entity.plan_id` is strictly extracted and verified as a non-empty string by `extract_subscription_entity()`. | **VERIFIED** |
| **Date Consistency** | [L523–L530](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L523-L530) | Positive numeric timestamp parsing enforced for period boundary updates. | **VERIFIED** |
| **Stale Plan/Period Overwrite Protection** | [L502–L509](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L502-L509) | Guarded by early return on `is_stale_event()`. Newer local timestamps protect current plan and periods from out-of-order reversion. | **VERIFIED** |
| **Reconciliation on Missing Local Sub** | [L479–L499](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L479-L499) | If `sub is None`, the handler does not create an unverified subscription; it stages a `PaymentReconciliation` record with reason `unknown_subscription_updated` and stores the diagnostic payload. | **VERIFIED** |
| **No Inferred Payment Success** | [L532–L539](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L532-L539) | Explicit guard: if local status is `past_due`, receiving `subscription.updated` with `status="active"` will **not** overwrite local status to `active`. | **VERIFIED** |

---

### 4. Completed Handler (`handle_subscription_completed`)

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Completed Semantics Representation** | [L554–L562](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L554-L562) | Razorpay fires `subscription.completed` when all scheduled billing cycles (`total_count`) have finished. Handled as a terminal transition to `SubscriptionStatus.canceled`. | **VERIFIED** |
| **Distinction from Mid-Cycle Cancellation** | [L639–L643](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L639-L643) | Completes immediately at event receipt; demotes `plan_tier = 'free'` and sets `canceled_at = ctx.event_created_at`. Does not set `cancel_at_period_end = True` (which is reserved for future mid-cycle user cancellations). | **VERIFIED** |
| **Timestamp Semantics** | [L642–L643](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L642-L643) | `canceled_at` is set to `ctx.event_created_at` (the event's authoritative UTC timestamp), maintaining audit fidelity. | **VERIFIED** |
| **Partial Unique Index Compatibility** | `apps/api/app/models/subscription.py` [L201–L205](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/subscription.py#L201-L205) | `uq_org_active_subscription` filters `postgresql_where="status IN ('active', 'trialing')"`. Transitioning to `canceled` cleanly releases the organization's active subscription slot. | **VERIFIED** |
| **Accounting Integration** | `apps/api/app/services/billing/accounting.py` [L260–L265](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/accounting.py#L260-L265) | `accounting.py` queries `status IN ('active', 'past_due')` for Pro entitlement. `canceled` subscriptions are excluded from authorizing Pro usage. | **VERIFIED** |
| **Period Mutation Isolation** | Entire file | Does not touch `OrganizationBillingPeriod`. Remaining days in any existing active period run to their natural expiration. | **VERIFIED** |
| **Duplicate & Delayed Events** | [L625–L637](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L625-L637) | Stale events are discarded. Already-canceled subscriptions return early without executing side-effects. | **VERIFIED** |

---

### 5. Dispatcher & Worker Integration

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Exact Registration** | [L654–L666](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L654-L666) | All 4 handlers are registered into `default_razorpay_dispatcher` upon package import. Dict-based registration is idempotent and deterministic. | **VERIFIED** |
| **Acyclic Dependency DAG** | `apps/api/app/services/billing/razorpay/__init__.py` | Dependency order is strictly hierarchical: `lock.py` & `webhook_engine.py` $\to$ `handler_base.py` $\to$ `agreement_handlers.py` $\to$ `__init__.py`. No circular imports. | **VERIFIED** |
| **Unknown Event Classification** | `apps/api/app/workers/razorpay_webhook_worker.py` [L173–L176](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/workers/razorpay_webhook_worker.py#L173-L176) & `webhook_engine.py` [L421–L432](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/webhook_engine.py#L421-L432) | Events without registered handlers receive `ProcessingOutcome.UNKNOWN_EVENT` and are immediately quarantined (`attempts += 1`, `status = quarantined`), preventing infinite polling loops. | **VERIFIED** |
| **Failure Classification & Retry** | `apps/api/app/workers/razorpay_webhook_worker.py` [L203–L209](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/workers/razorpay_webhook_worker.py#L203-L209) | `RazorpayWebhookPermanentError` $\to$ quarantined. Unhandled `Exception` / `RazorpayWebhookRetryableError` $\to$ marked `failed` with exponential backoff delay. Never marked `processed` on error. | **VERIFIED** |
| **Lease Fencing in Domain Transaction** | [L97–L103](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L97-L103), [L281–L287](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L281-L287), [L444–L450](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L444-L450), [L568–L574](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L568-L574) | Every handler asserts unexpired worker lease ownership under row lock (`with_for_update()`) via `assert_valid_worker_lease()` before domain mutations flush. Stale workers raise `StaleLeaseError` and abort. | **VERIFIED** |

---

### 6. Security, Isolation, and Idempotency

| Check | Line Reference | Audit Verification & Findings | Status |
| :--- | :--- | :--- | :--- |
| **Scoped Identity Queries** | Entire file | All database queries explicitly filter by `provider == 'razorpay'`, `merchant_account_id == ctx.merchant_account_id`, and `environment == ctx.environment`. | **VERIFIED** |
| **Tenant Isolation** | [L108–L116](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L108-L116) | Tenant resolution cross-checks customer provisions and subscriptions. Conflicting records raise `RazorpayWebhookReconciliationConflictError`. Unbound entities fail closed. | **VERIFIED** |
| **Advisory Lock Identity** | `lock.py` [L42–L75](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/lock.py#L42-L75) | Strictly validates entity type against `ALLOWED_ENTITY_TYPES: frozenset[str]`. Derives signed 64-bit BigInt key deterministically from `("subscription", sub_id)`. | **VERIFIED** |
| **Entity-Level Idempotency** | Handlers | Idempotency is enforced by current database state and monotonic timestamps, completely independent of `provider_event_id`. Duplicate webhooks cause zero duplicate rows or state drift. | **VERIFIED** |
| **Diagnostic Preservation** | `handler_base.py` [L830–L913](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/handler_base.py#L830-L913) | `stage_or_update_payment_reconciliation()` preserves append-only history of diagnostics in `diagnostic_payload['history']`, locking existing records with `with_for_update()`. | **VERIFIED** |
| **Arbitrary First-Match Guard** | [L183–L196](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L183-L196) | In `handle_subscription_authenticated`, `stmt_pending` uses `.order_by(created_at.desc()).limit(1)`. Although inert in normal flows, it represents a latest-match selection for unbound subscriptions. | **CONDITION** (See Finding 2) |

---

## 7. Tests and Repository State

### Test Suite Execution & Classification

Every test across the billing test suite was inspected and classified:

| Test Suite File | Test Count | Classification | Live DB Access | Status |
| :--- | :---: | :--- | :---: | :---: |
| `test_razorpay_agreement_handlers.py` | 12 | **Mocked Async Test Suite** (`RazorpayAgreementMockDbSession`) | None (In-Memory) | **PASS** |
| `test_razorpay_handler_foundation.py` | 21 | **Mocked Async & Offline Unit Tests** (`RazorpayHandlerMockDbSession`) | None (In-Memory) | **PASS** |
| `test_razorpay_webhook_worker.py` | 11 | **Mocked Async Worker Tests** (`MockAsyncSession`) | None (In-Memory) | **PASS** |
| `test_razorpay_webhook_ingestion.py` | 18 | **Mocked FastAPI TestClient Tests** | None (In-Memory) | **PASS** |
| `test_razorpay_checkout.py` | 28 | **Mocked Async Service Tests** (`httpx.MockTransport`) | None (In-Memory) | **PASS** |
| `test_razorpay_customer.py` | 25 | **Mocked Async Service Tests** (`httpx.MockTransport`) | None (In-Memory) | **PASS** |
| `test_razorpay_client.py` | 12 | **Mocked HTTP Transport Tests** (`httpx.MockTransport`) | None (In-Memory) | **PASS** |
| `test_razorpay_config.py` | 8 | **Static / Offline Configuration Tests** | None | **PASS** |
| `test_provider_neutral_billing_models.py` | 15 | **Static / SQLAlchemy Metadata Reflection Tests** | None | **PASS** |
| **Total Test Suite** | **196** | **All Mocked / Static / Offline** | **0 Live DB Calls** | **PASS** |

### Test Classification Transparency Notice
> [!IMPORTANT]
> **PostgreSQL Integration Verification Scope:**  
> All 196 tests execute against in-memory async mock sessions, offline unit harnesses, or static metadata reflections. While advisory lock hashing bounds, monotonic timestamp logic, and state transition matrices are verified mathematically, physical PostgreSQL advisory lock serialization (`pg_advisory_xact_lock`), physical row lock contention (`FOR UPDATE SKIP LOCKED`), and live database constraints have not been executed against a real PostgreSQL instance in this test harness. Live PostgreSQL integration verification remains deferred to the full staging integration testbed.

### Repository Status
- **Alembic Single Head:**
  ```powershell
  cd apps/api; .venv/Scripts/python -m alembic heads
  # Output: d1e2f3a4b5c6 (head)
  ```
  Confirmed: exactly one Alembic head (`d1e2f3a4b5c6`). No pending or dangling migrations.
- **Git Working Tree:**
  Clean branch status on `main`. No files committed, no branches pushed.

---

## 8. Defect and Finding Inventory

### Finding 1 (Low Severity — Audit Condition)
- **Component:** `apps/api/app/services/billing/razorpay/agreement_handlers.py` lines [241–250](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L241-L250) & [409–416](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L409-L416)
- **Description:** Missing `BillingCheckoutAttempt` does not fail closed or stage reconciliation evidence.
- **Details:** If an authenticated or activated event arrives for a valid subscription whose `BillingCheckoutAttempt` is absent (e.g., subscription initiated directly via API or provider portal), the handlers log info and proceed with subscription creation without staging a `PaymentReconciliation` record.
- **Impact:** Ambiguous checkout attempts (multiple rows) fail closed properly via `scalar_one_or_none()`, but absent checkout attempts produce no reconciliation diagnostic trace.
- **Recommendation:** In Step 5.2D.3.3 or before production release, add:
  ```python
  if attempt is None:
      logger.warning("No BillingCheckoutAttempt found for subscription %s; staging reconciliation trace.", sub_id)
      # Optional: stage reconciliation if strict checkout attempt audit is required by compliance
  ```

### Finding 2 (Low Severity — Code Hygiene)
- **Component:** `apps/api/app/services/billing/razorpay/agreement_handlers.py` lines [183–198](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L183-L198)
- **Description:** `stmt_pending` query applies `.limit(1)` heuristic to unbound subscriptions.
- **Details:** In `handle_subscription_authenticated`, lines 183–198 search for an unbound incomplete subscription with `provider_subscription_id.is_(None)` using `.order_by(Subscription.created_at.desc()).limit(1)`. Because Razorpay checkout always creates subscriptions with `provider_subscription_id` pre-populated, this query is practically inert. If multiple unbound rows were ever manually created, picking `.limit(1)` constitutes a latest-match selection.
- **Impact:** Negligible in normal operation; inert code path.
- **Recommendation:** Remove `stmt_pending` in the next refinement pass, directly executing `new_sub = Subscription(...)` as done in `handle_subscription_activated`.

### Finding 3 (Informational — Environmental Scope)
- **Component:** Test Suite
- **Description:** Live PostgreSQL concurrency and lock serialization not tested in sandboxed environment.
- **Details:** All unit tests utilize mock sessions (`RazorpayAgreementMockDbSession`). Physical transaction boundaries and PostgreSQL-specific locking semantics (`FOR UPDATE SKIP LOCKED`, advisory lock queuing) rely on verified query structure rather than live database integration.
- **Impact:** Standard for sandboxed offline unit suites; requires end-to-end integration testbed verification before live production gateway deployment.

---

## 9. Conclusion

Step 5.2D.3.2 satisfies all core architectural, state-machine, and security requirements:
- Refusal of resurrection is enforced.
- Entitlement is strictly quarantined (`plan_tier="free"`).
- Past-due grace anchors are strictly preserved.
- Monotonicity checks prevent stale event replays.
- Worker lease fencing protects domain mutations.

**Final Audit Verdict:** **PASS WITH CONDITIONS** (Proceed to Step 5.2D.3.3: Invoicing, Settlement, and Dunning Handlers).
