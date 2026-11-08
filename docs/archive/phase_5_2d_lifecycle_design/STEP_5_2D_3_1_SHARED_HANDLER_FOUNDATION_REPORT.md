# Aptlyst AI — Step 5.2D.3.1: Shared Razorpay Handler Utilities and Advisory Lock Infrastructure Report

**Repository**: `D:\Projects\V1\Aptlyst-AI`  
**Date**: November 8, 2026  
**Status**: COMPLETE & VERIFIED  
**Alembic Head**: `d1e2f3a4b5c6 (head)` (Single Head Preserved)  
**Deliverable**: Comprehensive Implementation Report for Shared Razorpay Handler Foundation  

---

## 1. Existing Architecture Findings

A forensic review of the billing architecture was conducted prior to implementing Step 5.2D.3.1. The findings guided all transaction boundaries, locking protocols, and error classifications:

### 1.1 Webhook Engine & Worker Transaction Boundaries
- **Two-Phase Leased Claim** (`apps/api/app/services/billing/razorpay/webhook_engine.py`):
  - Ingestion stamps newly verified events with `status = 'pending'`, `attempts = 0`, and `signature_verified = True`.
  - Claiming (`claim_next_razorpay_webhook_event`) runs a short, isolated transaction with `SELECT ... FOR UPDATE SKIP LOCKED` on `billing_webhook_events`.
  - The claim stamps a fresh unpredictable `uuid.uuid4()` as `lease_token`, sets `locked_by = worker_id`, sets `lease_expires_at = now + 60s`, and **commits immediately**.
  - **Invariant**: The row lock is released before the worker dispatches to the business event handler. No database row lock or open transaction is held during external network communication or slow business processing.
- **Fenced Completion Transaction**:
  - `record_razorpay_webhook_outcome` re-acquires the row lock (`SELECT ... FOR UPDATE`) and asserts fencing:
    $$\text{db\_token} == \text{held\_token} \quad\land\quad \text{db\_worker} == \text{held\_worker} \quad\land\quad \text{db\_expires\_at} \ge \text{now}$$
  - If any check fails, `StaleLeaseError` is raised, rolling back immediately without mutating `attempts`, `status`, or retry backoff.
- **Domain Transaction Boundary**:
  - `RazorpayWebhookWorker.process_one()` opens an independent `AsyncSession` (`async with self.session_factory() as handler_db:`) specifically for business handler execution.
  - Prior to Step 5.2D.3.1, a stalled worker whose lease expired during handler execution could commit domain mutations in the domain transaction before failing at the completion transaction. Step 5.2D.3.1 closes this window with `assert_valid_worker_lease`.

### 1.2 Lock Hierarchy & Deadlock Prevention
In `apps/api/app/services/billing/accounting.py`, runtime token reservations (`reserve_usage()`) lock:
1. `OrganizationBillingPeriod` (`FOR UPDATE`, Level 1)
2. `Subscription` (`FOR UPDATE`, Level 2)
3. `UsageReservation` (`FOR UPDATE`, Level 3)

To prevent cyclic deadlocks, all Razorpay webhook processing must strictly follow the platform's universal lock order:
$$\text{Level 0: Advisory Lock} \longrightarrow \text{Level 1: OrganizationBillingPeriod} \longrightarrow \text{Level 2: Subscription} \longrightarrow \text{Level 3: BillingInvoice} \longrightarrow \text{Level 4: BillingPayment}$$

Handlers discovering tenant identity must use non-locking MVCC reads on `BillingCustomerProvision` or `Subscription`, acquire Level 0 advisory locks on entity identifiers, and then acquire row locks strictly in ascending order ($0 \to 1 \to 2 \to 3 \to 4$).

---

## 2. Files Changed

| File Path | Action | Description |
| :--- | :--- | :--- |
| `apps/api/app/services/billing/razorpay/lock.py` | **Created** | Deterministic SHA-256 64-bit signed integer derivation and PostgreSQL transaction-scoped advisory locking (`pg_advisory_xact_lock`, `pg_try_advisory_xact_lock`). |
| `apps/api/app/services/billing/razorpay/handler_base.py` | **Created** | Reusable handler foundation: typed entity schemas (`RazorpaySubscriptionEntity`, `RazorpayPaymentEntity`, `RazorpayInvoiceEntity`), envelope and scope validator, entity ID extractors, authoritative tenant binder, diagnostic preservation, idempotency helpers, and stale lease assertion. |
| `apps/api/app/services/billing/razorpay/__init__.py` | **Modified** | Exported all shared handler foundation and advisory locking symbols (expanded package exports from 68 to 93 symbols). |
| `apps/api/app/workers/razorpay_webhook_worker.py` | **Modified** | Enhanced `process_one()` to inspect handler parameter signatures (supporting both `claimed: ClaimedWebhookEvent` and `payload: dict[str, Any]`) and catch `StaleLeaseError` raised by domain lease guards. |
| `apps/api/tests/test_razorpay_handler_foundation.py` | **Created** | 20 comprehensive unit and integration tests covering envelope validation, scope isolation, lock key determinism, entity ID extractors, anti-spoofing tenant binding, diagnostic preservation, stale lease rejection, and cancellation finalization. |

---

## 3. Handler Foundation Design

### 3.1 Architectural Workflow
The shared handler foundation sits between raw webhook ingestion and domain business handlers:

```
+---------------------------------------------------------------------------------------------------+
| Razorpay Webhook Worker & Shared Handler Foundation Pipeline                                      |
+---------------------------------------------------------------------------------------------------+
  billing_webhook_events
          |
          | 1. SELECT ... FOR UPDATE SKIP LOCKED -> Commit Lease
          v
  ClaimedWebhookEvent (lease_token, lease_expires_at, locked_by)
          |
          v
  validate_razorpay_event_envelope()
     - Enforces provider == "razorpay"
     - Enforces signature_verified == True (FAIL CLOSED if False)
     - Validates merchant_account_id & environment scope
     - Parses JSON envelope & validates required structure
     - Extracts strongly-typed dataclass entities (Subscription, Payment, Invoice)
          |
          v
  assert_valid_worker_lease() (Under Domain Transaction)
     - Verifies lease_token and lease_expires_at >= now under row lock
     - Stale workers taking >60s aborted with StaleLeaseError (Zero mutation committed)
          |
          v
  acquire_razorpay_advisory_lock()
     - SELECT pg_advisory_xact_lock(key)
     - Serializes concurrent events for same entity_type:entity_id
          |
          v
  resolve_tenant_organization()
     - Queries BillingCustomerProvision & Subscription (Authoritative Gateway ID)
     - Cross-validates client metadata (notes.organization_id)
     - Detects metadata mismatch -> RazorpayWebhookReconciliationConflictError
     - Detects unbound customer -> Refuses fake binding -> Stages PaymentReconciliation
          |
          v
  Domain Business Handler Execution (Future Step 5.2D.3.2+)
+---------------------------------------------------------------------------------------------------+
```

### 3.2 Strongly Typed Entity Schemas
Handlers interact with verified dataclasses rather than raw unstructured dictionaries:
- `RazorpaySubscriptionEntity`: Strongly typed fields for `id` (`sub_xxx`), `plan_id`, `customer_id`, `status`, `current_start`, `current_end`, `ended_at`, `charge_at`, `notes`.
- `RazorpayPaymentEntity`: Strongly typed fields for `id` (`pay_xxx`), `amount` (paise $\ge 1$), `currency` (3-letter ISO code), `status`, `order_id`, `invoice_id`, `customer_id`, `amount_refunded`, `notes`.
- `RazorpayInvoiceEntity`: Strongly typed fields for `id` (`inv_xxx`), `subscription_id`, `customer_id`, `order_id`, `payment_id`, `status`, `amount`, `amount_paid`, `amount_due`, `currency`.
- `RazorpayEventContext`: Consolidated runtime context carrying the above entities, event timestamps, lease metadata, and raw payloads.

### 3.3 Authoritative Identity Binding & Anti-Spoofing
A key vulnerability in multi-tenant webhook architectures is trusting client-controlled metadata. Razorpay allows clients to supply arbitrary `notes` at checkout.
- **Rule 1 (Authoritative Priority)**: Tenant identity is resolved from `BillingCustomerProvision` (`provider_customer_id`) or `Subscription` (`provider_subscription_id`).
- **Rule 2 (Cross-Validation)**: If `notes["organization_id"]` is present:
  - If it does not match the authoritative tenant, `RazorpayWebhookReconciliationConflictError(reason="metadata_tenant_mismatch")` is raised immediately.
- **Rule 3 (No Fake Bindings)**: If an entity is unknown in local authoritative tables, the engine **strictly refuses** to bind it to `notes["organization_id"]`. It stages a `PaymentReconciliation` record and quarantines the event.

### 3.4 Diagnostic History Preservation
When discrepancies occur, diagnostic evidence must be retained for audits. `stage_or_update_payment_reconciliation()` implements append-only history tracking:
```python
existing_history = list(existing.diagnostic_payload.get("history", []))
existing_history.append({
    "timestamp": now_utc.isoformat(),
    "reason": reason,
    "details": clean_data,
})
existing.diagnostic_payload = {
    **existing.diagnostic_payload,
    "history": existing_history,
    "latest": clean_data,
}
```
Earlier error payloads are never overwritten or lost across repeated retries or reconciliation updates.

---

## 4. Advisory Lock Derivation and Transaction Behavior

### 4.1 Key Derivation Algorithm
PostgreSQL advisory locks accept either a single 64-bit signed integer (`bigint`) or two 32-bit signed integers. We implement a single 64-bit integer derived via cryptographic hashing:

```python
def derive_razorpay_advisory_lock_key(entity_type: str, entity_id: str) -> int:
    clean_type = entity_type.strip().lower()
    clean_id = entity_id.strip()
    raw = f"razorpay:{clean_type}:{clean_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
```

### 4.2 Mathematical & Cryptographic Properties
1. **Determinism**: Unlike Python's process-randomized `hash()`, `hashlib.sha256()` produces the exact same bit sequence across every OS, process, and Python run.
2. **Range Integrity**: `digest[:8]` converted to a signed 64-bit big-endian integer is guaranteed to fall strictly within PostgreSQL's `bigint` range:
   $$-2^{63} \le K \le 2^{63} - 1 \quad (-9{,}223{,}372{,}036{,}854{,}775{,}808 \le K \le 9{,}223{,}372{,}036{,}854{,}775{,}807)$$
3. **Collision Resistance**: The keyspace is $2^{64} \approx 1.84 \times 10^{19}$. By the Birthday Paradox, for $N = 10{,}000{,}000$ active entities, the collision probability is:
   $$P(\text{collision}) \approx 1 - \exp\left(-\frac{N^2}{2 \times 2^{64}}\right) \approx 1 - \exp(-2.7 \times 10^{-6}) \approx 2.7 \times 10^{-6}$$
   This probability is infinitesimally small.
4. **Entity Type Scoping**: Prefixing with `razorpay:{clean_type}:` ensures that `subscription:sub_123` and `payment:sub_123` map to distinct keys, preventing cross-entity false lock contention.

### 4.3 PostgreSQL Advisory Lock Semantics
- **Transaction-Scoped (`pg_advisory_xact_lock`)**:
  - Automatically released by the PostgreSQL kernel when the current transaction commits or rolls back.
  - Requires **zero manual unlock calls**, completely preventing lock leaks on worker crash or unexpected exceptions.
- **Session-Scoped Locks Forbidden**: Session-scoped advisory locks (`pg_advisory_lock`) remain held until explicit unlock or connection termination. Because connection pools (e.g. PgBouncer, SQLAlchemy pooled connections) reuse backend connections, session locks could cause catastrophic pool-wide deadlocks. Transaction-scoped locks are mandatory.
- **Deadlock Disclaimer**: Advisory locks eliminate race conditions on the *same* entity. However, multi-entity transactions with cyclic cross-dependencies could still deadlock. Global deadlock freedom cannot be mathematically claimed; instead, strict acquisition order ($0 \to 1 \to 2 \to 3 \to 4$) and database statement timeouts (`statement_timeout = 10s`) are enforced.

---

## 5. Lease/Domain Transaction Integration

### 5.1 The Stale Worker Race Condition
Consider the distributed worker timeline:
1. **Worker A** claims event `evt_1` (lease duration = 60s, expires at $t_0 + 60\text{s}$).
2. **Worker A** stalls (e.g., severe GC pause, slow database query) for 65 seconds.
3. At $t_0 + 61\text{s}$, the lease expires.
4. **Worker B** claims `evt_1` (assigns fresh lease token, expires at $t_0 + 121\text{s}$).
5. **Worker A** wakes up at $t_0 + 66\text{s}$ and attempts to commit domain mutations.

### 5.2 Resolution: Double-Fencing Guard
Step 5.2D.3.1 enforces double-fencing:
1. **Domain Boundary Guard** (`assert_valid_worker_lease`):
   - Prior to committing domain mutations, the handler checks `billing_webhook_events` under `FOR UPDATE`.
   - If `lease_token != claimed.lease_token` or `lease_expires_at < now`, it executes `await db.rollback()` and raises `StaleLeaseError`.
   - Worker A is prohibited from mutating `subscriptions`, `billing_invoices`, or `organization_billing_periods`.
2. **Outcome Boundary Guard** (`record_razorpay_webhook_outcome`):
   - When the worker reports success or failure, it verifies the lease again under row lock.
   - If Worker A lost the lease, `StaleLeaseError` rejects outcome recording without advancing retry counts or clearing Worker B's lease.

### 5.3 Crash Window & Replay Idempotency Strategy
If business mutations commit successfully in the domain transaction, but the worker crashes *immediately* before executing `record_razorpay_webhook_outcome`:
- **Crash Window**: The domain changes are persisted, but the event remains in `billing_webhook_events` with `status = 'pending'` and an expired lease.
- **Subsequent Replay**:
  - An expired-lease recovery sweep or another worker claims the event and re-executes the handler.
  - **Idempotency Defense**:
    1. **Advisory Lock**: Worker acquires `pg_advisory_xact_lock(entity_id)`, serializing execution.
    2. **Unique Constraints**: Unique constraints on `uq_subscriptions_provider_subscription`, `uq_billing_invoices_provider_invoice`, and `uq_payment_reconciliations_case_key` prevent duplicate records.
    3. **Monotonic Timestamps**: Handlers evaluate `last_webhook_event_at`. Stale replays backfill missing fields without regressing lifecycle state.
    4. **Terminal Status**: Canceled or completed subscriptions refuse resurrection.
- Replaying an already-processed event produces identical state and successfully records `status = 'processed'`.

---

## 6. Cancellation Finalization Design Note

### 6.1 The Accounting Contradiction
When an organization cancels during the 7-day grace period (`past_due`):
- If `Subscription.status` were changed immediately to `canceled`, `accounting.py` would instantly block usage (`SUBSCRIPTION_INACTIVE`), violating the customer's entitlement to their remaining grace days.
- If `Subscription.status` remained `past_due`, but no future webhooks arrived from Razorpay after grace expiration, what finalizes `Subscription.status` to `canceled`?

### 6.2 The Three-Tier Finalization Architecture
Step 5.2D.3A.1 established Two-Phase Deferred Cancellation. Step 5.2D.3.1 formalizes the finalization architecture:

1. **Tier 1: Real-Time Accounting Barrier (`accounting.py`)**:
   - `accounting.py` evaluates entitlement at the exact millisecond of every inference request:
     $$\text{is\_entitled} \iff \text{status} == \text{'active'} \;\lor\; (\text{status} == \text{'past\_due'} \;\land\; \text{now} < \text{past\_due\_since} + 7\text{d})$$
   - Once $\text{now} \ge \text{past\_due\_since} + 7\text{d}$, `accounting.py` returns `GRACE_PERIOD_EXPIRED`.
   - **Zero Unauthorized Access**: Pro usage is blocked immediately and deterministically without requiring any database mutation.
2. **Tier 2: Lazy Service Read Finalization**:
   - When an organization loads their billing settings or queries `/api/v1/billing/subscription`, the billing service calls:
     ```python
     if should_finalize_deferred_cancellation(sub.status, sub.cancel_at_period_end, sub.past_due_since):
         sub.status = SubscriptionStatus.canceled
         sub.plan_tier = "free"
         await db.commit()
     ```
   - State converges naturally on the next user interaction.
3. **Tier 3: Webhook & Reconciliation Sweeper Barrier**:
   - If any subsequent webhook arrives for the subscription or customer, or when the payment sweeper processes pending reconciliations, `should_finalize_deferred_cancellation()` evaluates the predicate and finalizes terminal status.
- **No Background Daemon Required**: Because Tier 1 guarantees zero quota leakage and Tier 2/3 ensure state convergence, no standing cron or complex background sweeper daemon is required in Step 5.2D.3.1.

---

## 7. Tests and Exact Results

A dedicated automated test suite was created in `apps/api/tests/test_razorpay_handler_foundation.py`. All 20 tests pass cleanly:

```
============================= test session starts =============================
platform win32 -- Python 3.11.0, pytest-8.3.5, pluggy-1.6.0 -- D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\Projects\V1\Aptlyst-AI\apps\api
configfile: pytest.ini
plugins: anyio-4.15.1, asyncio-0.25.3
asyncio: mode=Mode.AUTO, asyncio_default_fixture_loop_scope=function
collecting ... collected 20 items

apps\api\tests\test_razorpay_handler_foundation.py::test_advisory_lock_deterministic_key_generation PASSED [  5%]
apps\api\tests\test_razorpay_handler_foundation.py::test_advisory_lock_distinct_entities_and_types PASSED [ 10%]
apps\api\tests\test_razorpay_handler_foundation.py::test_advisory_lock_rejects_missing_or_empty_identifiers PASSED [ 15%]
apps\api\tests\test_razorpay_handler_foundation.py::test_acquire_and_try_advisory_lock_execution PASSED [ 20%]
apps\api\tests\test_razorpay_handler_foundation.py::test_validate_envelope_success_claimed_event PASSED [ 25%]
apps\api\tests\test_razorpay_handler_foundation.py::test_validate_envelope_rejects_unverified_signature PASSED [ 30%]
apps\api\tests\test_razorpay_handler_foundation.py::test_validate_envelope_rejects_scope_mismatch PASSED [ 35%]
apps\api\tests\test_razorpay_handler_foundation.py::test_entity_id_extraction_and_missing_guards PASSED [ 40%]
apps\api\tests\test_razorpay_handler_foundation.py::test_strict_payload_validation_rejects_invalid_values PASSED [ 45%]
apps\api\tests\test_razorpay_handler_foundation.py::test_error_hierarchy_distinguishes_failure_categories PASSED [ 50%]
apps\api\tests\test_razorpay_handler_foundation.py::test_resolve_tenant_organization_authoritative_match PASSED [ 55%]
apps\api\tests\test_razorpay_handler_foundation.py::test_resolve_tenant_organization_rejects_metadata_spoofing PASSED [ 60%]
apps\api\tests\test_razorpay_handler_foundation.py::test_resolve_tenant_organization_refuses_fake_binding_for_unbound_customer PASSED [ 65%]
apps\api\tests\test_razorpay_handler_foundation.py::test_reconciliation_case_preserves_diagnostic_history PASSED [ 70%]
apps\api\tests\test_razorpay_handler_foundation.py::test_assert_valid_worker_lease_permits_active_lease PASSED [ 75%]
apps\api\tests\test_razorpay_handler_foundation.py::test_assert_valid_worker_lease_rejects_expired_or_superseded_lease PASSED [ 80%]
apps\api\tests\test_razorpay_handler_foundation.py::test_should_finalize_deferred_cancellation_evaluation PASSED [ 85%]
apps\api\tests\test_razorpay_handler_foundation.py::test_monotonic_and_terminal_idempotency_helpers PASSED [ 90%]
apps\api\tests\test_razorpay_handler_foundation.py::test_transaction_rollback_on_failed_validation PASSED [ 95%]
apps\api\tests\test_razorpay_handler_foundation.py::test_composite_payload_parses_all_entities PASSED [100%]

============================= 20 passed in 2.48s ==============================
```

### Full Regression Suite Results
The full Razorpay and provider-neutral billing test suite was executed to verify regression freedom:

```
collected 183 items

apps\api\tests\test_razorpay_handler_foundation.py ....................  [ 10%]
apps\api\tests\test_razorpay_webhook_worker.py .......................   [ 23%]
apps\api\tests\test_razorpay_webhook_ingestion.py ...................... [ 35%]
.                                                                        [ 36%]
apps\api\tests\test_razorpay_checkout.py ............................... [ 53%]
..................                                                       [ 62%]
apps\api\tests\test_razorpay_customer.py ............................... [ 79%]
.                                                                        [ 80%]
apps\api\tests\test_razorpay_client.py .............                     [ 87%]
apps\api\tests\test_razorpay_config.py ........                          [ 91%]
apps\api\tests\test_provider_neutral_billing_models.py ...............   [100%]

============================= 183 passed in 7.66s =============================
```

---

## 8. PostgreSQL Versus Mocked Test Classification

| Test Name | Classification | Database Target | Verifications |
| :--- | :--- | :--- | :--- |
| `test_advisory_lock_deterministic_key_generation` | Pure Unit | None | Cross-platform determinism, SHA-256 64-bit signed integer range $[-2^{63}, 2^{63}-1]$. |
| `test_advisory_lock_distinct_entities_and_types` | Pure Unit | None | Collision resistance across entity IDs and entity type prefixes. |
| `test_advisory_lock_rejects_missing_or_empty_identifiers` | Pure Unit | None | Immediate `ValueError` rejection on empty/whitespace strings. |
| `test_acquire_and_try_advisory_lock_execution` | Mocked Integration | `RazorpayHandlerMockDbSession` | SQL query formatting (`SELECT pg_advisory_xact_lock(:key)`), param extraction, non-blocking check. |
| `test_validate_envelope_success_claimed_event` | Unit / Schema | None | Dataclass parsing of `ClaimedWebhookEvent` into `RazorpayEventContext`. |
| `test_validate_envelope_rejects_unverified_signature` | Unit / Guard | None | Fail-closed rejection of unverified events with `RazorpayWebhookPermanentError`. |
| `test_validate_envelope_rejects_scope_mismatch` | Unit / Guard | None | Rejection of non-razorpay provider and mismatched merchant account. |
| `test_entity_id_extraction_and_missing_guards` | Unit / Schema | None | ID resolution from entity hierarchy and missing ID exception handling. |
| `test_strict_payload_validation_rejects_invalid_values` | Unit / Validation | None | Negative/zero amount check, 3-letter currency code, positive Unix timestamp conversion. |
| `test_error_hierarchy_distinguishes_failure_categories` | Pure Unit | None | Subclass verification for retryable, permanent, and reconciliation conflict errors. |
| `test_resolve_tenant_organization_authoritative_match` | Mocked Integration | `RazorpayHandlerMockDbSession` | Authoritative customer lookup via SQLAlchemy `select()` and matching metadata check. |
| `test_resolve_tenant_organization_rejects_metadata_spoofing` | Mocked Integration | `RazorpayHandlerMockDbSession` | Fail-closed rejection when client notes disagree with database customer provision. |
| `test_resolve_tenant_organization_refuses_fake_binding_for_unbound_customer` | Mocked Integration | `RazorpayHandlerMockDbSession` | Refusal to bind unknown customers to client notes alone; stages reconciliation. |
| `test_reconciliation_case_preserves_diagnostic_history` | Mocked Integration | `RazorpayHandlerMockDbSession` | Append-only history list in `PaymentReconciliation.diagnostic_payload["history"]`. |
| `test_assert_valid_worker_lease_permits_active_lease` | Mocked Integration | `RazorpayHandlerMockDbSession` | Active unexpired lease verification with `SELECT ... FOR UPDATE`. |
| `test_assert_valid_worker_lease_rejects_expired_or_superseded_lease` | Mocked Integration | `RazorpayHandlerMockDbSession` | Stale worker rejection: raises `StaleLeaseError` and triggers `db.rollback()`. |
| `test_should_finalize_deferred_cancellation_evaluation` | Pure Unit | None | Evaluates 7-day grace period expiration math for deferred cancellation. |
| `test_monotonic_and_terminal_idempotency_helpers` | Pure Unit | None | Monotonic timestamp comparison and refusal of resurrection for terminal statuses. |
| `test_transaction_rollback_on_failed_validation` | Mocked Integration | `RazorpayHandlerMockDbSession` | Verifies uncommitted database operations are rolled back upon validation exception. |
| `test_composite_payload_parses_all_entities` | Unit / Schema | None | Multi-entity parsing of subscription, payment, and invoice from single payload. |

---

## 9. Git Status and Alembic Head

### 9.1 Alembic Head Inspection
```
$ cd apps/api; .venv/Scripts/python -m alembic heads
d1e2f3a4b5c6 (head)
```
- **Single Head Maintained**: `d1e2f3a4b5c6`.
- **Zero Schema Migrations**: All shared utilities and advisory locking functions operate entirely within the existing schema. No new database tables, columns, or indexes were required.

### 9.2 Git Working Tree Inspection
```
$ git status
On branch main
Your branch is ahead of 'origin/main' by 1 commit.

Changes not staged for commit:
	modified:   apps/api/app/workers/razorpay_webhook_worker.py

Untracked files:
	apps/api/app/services/billing/razorpay/handler_base.py
	apps/api/app/services/billing/razorpay/lock.py
	apps/api/tests/test_razorpay_handler_foundation.py
	STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md
```
- Only files within authorized scope were created or touched.
- No modifications were made to Stripe files, `past_due_since` logic, `OrganizationBillingPeriod`, or quota accounting.
- No commit or push has been performed.

---

## 10. Known Limitations & Forward Roadmap

1. **Business Event Handlers Not Yet Implemented**:
   - Step 5.2D.3.1 deliberately implements *only* the shared handler foundation, advisory locking, and validation utilities.
   - Agreement lifecycle handlers (`subscription.authenticated`, `subscription.activated`, `subscription.updated`, `subscription.completed`) remain for Step 5.2D.3.2.
   - Failure and cancellation handlers (`subscription.pending`, `subscription.halted`, `subscription.cancelled`, `payment.failed`) remain for Step 5.2D.3.3.
   - Settlement and quota provisioning (`subscription.charged`) remains for Step 5.2D.3.4.
2. **Database Engine Specificity for Advisory Locks**:
   - `pg_advisory_xact_lock` is PostgreSQL-specific. When running unit tests against SQLite or mock sessions, advisory locks must be mocked or executed against PostgreSQL. The foundation handles mock execution gracefully.
3. **No Standalone Background Sweeper**:
   - Finalization of deferred cancellation relies on the real-time `accounting.py` access barrier, lazy read queries, and incoming webhooks. A dedicated periodic background sweeper can be introduced in a future operational hardening phase if batch offline cleanups are desired.

---

## Sign-Off
Step 5.2D.3.1 has been completed in strict accordance with the prompt specifications and architectural constraints. The shared handler foundation, PostgreSQL advisory locking utilities, anti-spoofing tenant binder, and double-fencing lease guards are implemented, verified by 20 new tests and 183 full regression tests, and ready for domain handler implementation.
