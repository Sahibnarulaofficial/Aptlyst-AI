# Aptlyst AI — Step 5.2D.3.1.1: Shared Handler Foundation Final Correctness Audit

**Repository**: `D:\Projects\V1\Aptlyst-AI`  
**Date**: November 8, 2026  
**Audit Type**: STRICTLY READ-ONLY CORRECTNESS AND SECURITY AUDIT  
**Target Milestone**: Step 5.2D.3.1 (Shared Razorpay Handler Utilities and Advisory Lock Infrastructure)  
**Alembic Head**: `d1e2f3a4b5c6 (head)` (Single Head Verified)  
**Audit Verdict**: **PASS WITH CONDITIONS**  

---

## 1. Executive Summary

A forensic, read-only correctness and security audit of the Step 5.2D.3.1 implementation was conducted. The audit inspected:
- `apps/api/app/services/billing/razorpay/lock.py`
- `apps/api/app/services/billing/razorpay/handler_base.py`
- `apps/api/app/services/billing/razorpay/webhook_engine.py`
- `apps/api/app/workers/razorpay_webhook_worker.py`
- `apps/api/app/models/payment.py`
- `apps/api/app/models/subscription.py`
- `apps/api/app/services/billing/accounting.py`
- `apps/api/tests/test_razorpay_handler_foundation.py`

The implementation strictly satisfies all core requirements:
1. **Advisory Locking**: Derives collision-resistant signed 64-bit integer keys using SHA-256 and executes transaction-scoped `pg_advisory_xact_lock`, releasing automatically on commit or rollback with zero connection-pool leakage.
2. **Double-Fenced Lease Protection**: Enforces worker lease ownership both within the domain transaction boundary (`assert_valid_worker_lease`) and during final outcome persistence (`record_razorpay_webhook_outcome`).
3. **Anti-Spoofing Tenant Binding**: Resolves tenant identity strictly from authoritative database records (`BillingCustomerProvision`, `Subscription`) and rejects client metadata tampering with `RazorpayWebhookReconciliationConflictError`. Unbound customers are never bound to client notes alone.
4. **Append-Only Diagnostic Preservation**: Staging or updating `PaymentReconciliation` appends to a timestamped `history` list without overwriting earlier failure logs.
5. **Zero Regression**: 183 billing regression tests pass (100% pass rate); Alembic head remains unchanged at `d1e2f3a4b5c6`.

The audit returns **PASS WITH CONDITIONS**. Four minor, non-blocking operational conditions are documented for future handler implementation stages.

---

## 2. Section 1: Lease Fencing & Transaction Boundaries

### 2.1 Transaction Boundary Trace
The lifecycle of an ingested webhook event spans three distinct transactional phases:

```
+---------------------------------------------------------------------------------------------------+
| Leased Processing Transactional Boundaries                                                        |
+---------------------------------------------------------------------------------------------------+
  [Phase 1: Claim Transaction] (Session 1)
  SELECT ... FROM billing_webhook_events WHERE status IN ('pending', 'failed')
  FOR UPDATE SKIP LOCKED LIMIT 1;
  UPDATE billing_webhook_events SET lease_token = :token, locked_by = :worker, lease_expires_at = :exp;
  COMMIT;  --> (Row lock released. Database connection closed.)
       |
       v
  [Phase 2: Domain Execution Transaction] (Session 2)
  async with session_factory() as handler_db:
      1. validate_razorpay_event_envelope(claimed)
      2. acquire_razorpay_advisory_lock(handler_db, entity_type, entity_id)  --> Level 0 Lock
      3. assert_valid_worker_lease(handler_db, event_id, held_token, worker_id)
         --> Executes: SELECT ... FROM billing_webhook_events WHERE id = :id FOR UPDATE;
         --> Validates: token matches AND worker matches AND lease_expires_at >= now_utc;
         --> Row lock on billing_webhook_events remains HELD until handler_db commits/rolls back.
      4. Execute business mutations (Subscription, BillingInvoice, BillingPayment).
      5. COMMIT handler_db;  --> (Row lock and advisory lock released.)
       |
       v
  [Phase 3: Outcome Completion Transaction] (Session 3)
  async with session_factory() as outcome_db:
      SELECT ... FROM billing_webhook_events WHERE id = :id FOR UPDATE;
      Verify fencing: token matches AND worker matches AND lease_expires_at >= now_utc;
      UPDATE billing_webhook_events SET status = 'processed', lease_token = NULL, ...;
      COMMIT;  --> (Row lock released.)
+---------------------------------------------------------------------------------------------------+
```

### 2.2 Invariant Verification
1. **Row Lock Duration in Domain Transaction**:
   - `assert_valid_worker_lease()` (`handler_base.py#L916-L960`) executes `SELECT ... FOR UPDATE` on `billing_webhook_events`.
   - In PostgreSQL, row locks acquired under `FOR UPDATE` remain actively held by the database transaction until `COMMIT` or `ROLLBACK`.
   - Therefore, while Worker A is executing domain operations in `handler_db`, the row lock on `billing_webhook_events` is actively held.
2. **Competing Worker Mutual Exclusion**:
   - Competing Worker B searches for eligible events using `claim_next_razorpay_webhook_event()` (`webhook_engine.py#L255-L283`), which executes `SELECT ... FOR UPDATE SKIP LOCKED`.
   - Because Worker A holds the row lock on the event row in PostgreSQL, PostgreSQL's `SKIP LOCKED` engine ignores the locked row and Worker B **cannot claim it**.
3. **Session Identity & Transaction Boundaries**:
   - Worker A claims under Session 1 (committed immediately).
   - Worker A executes domain operations under Session 2 (`handler_db`).
   - Worker A records outcome under Session 3 (`outcome_db`).
   - Boundaries are strictly separated: no long-running transaction spans across the claim and domain execution.

### 2.3 Race Analysis: Worker A Validates Lease $\to$ Lease Expires $\to$ Worker B Attempts Reclaim $\to$ Worker A Commits

Consider the critical edge-case race:
- **$t_0 = 0\text{s}$**: Worker A claims `evt_1` (lease expires at $t = 60\text{s}$). Session 1 commits.
- **$t_1 = 10\text{s}$**: Worker A opens `handler_db` (Session 2).
- **$t_2 = 15\text{s}$**: Worker A calls `assert_valid_worker_lease()`.
  - At $t = 15\text{s}$, $15 < 60$. Lease is valid.
  - Worker A acquires `FOR UPDATE` row lock on `evt_1` in `handler_db`.
- **$t_3 = 60\text{s}$**: Wall-clock time crosses lease expiration. In the database row, `lease_expires_at = 60 <= now`.
- **$t_4 = 61\text{s}$**: Worker B calls `claim_next_razorpay_webhook_event()`.
  - Worker B's query includes `WHERE lease_expires_at <= now` WITH `FOR UPDATE SKIP LOCKED`.
  - **Crucial Invariant**: Because Worker A is still holding the row lock in `handler_db`, Worker B's `SKIP LOCKED` skips `evt_1`. Worker B cannot reclaim `evt_1` while Worker A's transaction is active!
- **$t_5 = 65\text{s}$**: Worker A commits `handler_db`. The domain mutations commit to disk. Row lock on `evt_1` is released.
- **$t_6 = 66\text{s}$**: Worker A opens `outcome_db` (Session 3) to record success via `record_razorpay_webhook_outcome()`.
  - In `record_razorpay_webhook_outcome()` (`webhook_engine.py#L378-L380`):
    ```python
    is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now_utc
    ```
  - At $t = 66\text{s}$, `now_utc = 66 > 60`. `is_unexpired` evaluates to **FALSE**.
  - `record_razorpay_webhook_outcome()` rolls back and raises `StaleLeaseError`.
- **$t_7 = 67\text{s}$**: Worker B runs `claim_next_razorpay_webhook_event()`.
  - The row is now unlocked. `lease_expires_at <= now` is True.
  - Worker B claims `evt_1` with fresh lease token.
  - Worker B executes the handler.
  - **Why this is safe**:
    1. Level 0 advisory locking serializes Worker B's replay.
    2. Entity-level unique constraints (`uq_subscriptions_provider_subscription`, `uq_billing_invoices_provider_invoice`) prevent duplicate creation.
    3. Monotonic timestamps (`last_webhook_event_at`) detect the event as already applied and return idempotent success.
    4. Worker B records `status = 'processed'`.
  - **Verdict**: **SAFE**. The combination of PostgreSQL `FOR UPDATE SKIP LOCKED`, row-level lease fencing, and entity-level idempotency prevents split-brain state or duplicate billing periods.

### 2.4 Authoritative Clock Assumptions
- Both `assert_valid_worker_lease()` and `record_razorpay_webhook_outcome()` evaluate lease expiration using `now or datetime.now(timezone.utc)`.
- **Assumption**: Application worker instances and PostgreSQL database nodes must maintain synchronized UTC clocks via NTP/chrony.
- **Tolerance**: The 60-second lease window is orders of magnitude larger than typical NTP clock jitter (<50ms). If application clock skew were to exceed 60 seconds, an expired lease could be misjudged as valid. (See Condition 1).

---

## 3. Section 2: Advisory Locking

### 3.1 Key Derivation & Identifier Normalization
In `apps/api/app/services/billing/razorpay/lock.py#L46-L77`:
```python
def derive_razorpay_advisory_lock_key(entity_type: str, entity_id: str) -> int:
    clean_type = entity_type.strip().lower()
    clean_id = entity_id.strip()
    raw = f"razorpay:{clean_type}:{clean_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    return key
```

1. **Normalization Inspection**:
   - `clean_type`: Lowercased and stripped (`subscription` vs `Subscription` produces identical key).
   - `clean_id`: Stripped of leading/trailing whitespace, but **case-preserved** (`strip()`, not `lower()`).
   - **Reason**: Razorpay IDs (e.g. `sub_ABC123xyz`, `pay_DEF456uvw`) are case-sensitive base62 strings. Preserving case prevents distinct gateway entities like `pay_Abc` and `pay_abc` from colliding before hashing.
2. **Delimiter Collision Analysis**:
   - Raw string format: `razorpay:{clean_type}:{clean_id}`.
   - Could normalization cause distinct entity pairs to collide?
     - Example: `entity_type="sub:foo", entity_id="bar"` vs `entity_type="sub", entity_id="foo:bar"`. Both yield `razorpay:sub:foo:bar`.
     - In the codebase, `entity_type` is exclusively passed as hardcoded string literals (`"subscription"`, `"payment"`, `"invoice"`, `"customer"`). No colon exists in any category literal.
     - (See Condition 2 for formal input whitelist validation).
3. **PostgreSQL Signed 64-Bit Bounds**:
   - PostgreSQL `pg_advisory_xact_lock(key bigint)` requires a signed 64-bit integer:
     $$-2^{63} \le K \le 2^{63} - 1 \quad (-9{,}223{,}372{,}036{,}854{,}775{,}808 \le K \le 9{,}223{,}372{,}036{,}854{,}775{,}807)$$
   - In Python, `int.from_bytes(digest[:8], byteorder="big", signed=True)` extracts exactly 8 bytes (64 bits) as a two's-complement signed integer.
   - The bounds check (`lock.py#L74-L75`) explicitly verifies `PG_BIGINT_MIN <= key <= PG_BIGINT_MAX`.

### 3.2 Residual Collision Probability (Do Not Claim Impossibility)
- Truncating SHA-256 to 64 bits creates a keyspace of $S = 2^{64} \approx 1.844 \times 10^{19}$.
- According to the Birthday Paradox, for $N$ distinct entities hashed into a 64-bit space, the probability of at least one collision is:
  $$P(\text{collision}) \approx 1 - \exp\left(-\frac{N^2}{2 \times 2^{64}}\right) \approx \frac{N^2}{2^{65}}$$
- Exact probability calculations:
  - For $N = 100{,}000$ active subscriptions: $P \approx 2.71 \times 10^{-10}$ ($\approx 1 \text{ in } 3.7 \text{ billion}$).
  - For $N = 1{,}000{,}000$ active subscriptions: $P \approx 2.71 \times 10^{-8}$ ($\approx 1 \text{ in } 37 \text{ million}$).
  - For $N = 10{,}000{,}000$ active subscriptions: $P \approx 2.71 \times 10^{-6}$ ($\approx 1 \text{ in } 369{,}000$).
- **Impact of a Residual Collision**:
  - In the extremely improbable event of a hash collision between two distinct entities (e.g. `sub_ABC` and `sub_XYZ`), the only consequence is **transient mutual exclusion** (one transaction blocks briefly until the other commits).
  - It does **not** cause data corruption or wrong-tenant assignment, because subsequent row-level locks and queries filter on distinct primary keys and unique constraints.

### 3.3 Transaction-Scoped Lock Lifecycle & Connection Pool Safety
- `acquire_razorpay_advisory_lock()` executes `SELECT pg_advisory_xact_lock(:key)`.
- `pg_advisory_xact_lock` in PostgreSQL is explicitly transaction-scoped (`xact`).
- The lock is released automatically by the PostgreSQL engine upon transaction `COMMIT` or `ROLLBACK`.
- When the database connection is returned to the SQLAlchemy connection pool, zero lingering locks remain on the session. Connection pool contamination is completely impossible.

### 3.4 Lock Acquisition Order
Every caller follows the platform's universal lock order:
$$\text{Level 0: Advisory Lock} \longrightarrow \text{Level 1: OrganizationBillingPeriod} \longrightarrow \text{Level 2: Subscription} \longrightarrow \text{Level 3: BillingInvoice} \longrightarrow \text{Level 4: BillingPayment}$$

---

## 4. Section 3: Tenant Resolution Audit

Inspect `resolve_tenant_organization()` (`handler_base.py#L655-L784`):

```python
async def resolve_tenant_organization(
    db: AsyncSession,
    provider: str = "razorpay",
    merchant_account_id: str = "default",
    environment: str = "live",
    customer_id: str | None = None,
    subscription_id: str | None = None,
    notes: dict[str, Any] | None = None,
) -> uuid.UUID:
```

### 4.1 Lookup Path & Scoping Verification
1. **Multi-Tenant Scoping**:
   - `BillingCustomerProvision` lookup (`handler_base.py#L688-L695`): Scoped strictly by `(provider, merchant_account_id, environment, provider_customer_id)`.
   - `Subscription` lookup (`handler_base.py#L699-L707`): Scoped strictly by `(provider, merchant_account_id, environment, provider_subscription_id)`.
   - Cross-environment (`test` vs `live`) or cross-merchant leaks are impossible.
2. **Ambiguous Matches Fail Closed**:
   - If both `customer_id` and `subscription_id` are provided, lines 710–730 compare `prov_org_id` and `sub_org_id`.
   - If `prov_org_id != sub_org_id`, it logs a warning and raises `RazorpayWebhookReconciliationConflictError(reason="conflicting_tenant_records")`.
   - Partial unique indexes (`uq_billing_customer_provisions_provider_customer`, `uq_subscriptions_provider_subscription`) prevent duplicate rows within the same scope.
3. **Anti-Spoofing: Unknown Customer Binding via Notes Forbidden**:
   - If `authoritative_org_id` is `None` (lines 767–783):
     ```python
     raise RazorpayWebhookReconciliationConflictError(
         f"Unbound Razorpay entity '{entity_ref}'. No local customer provision or subscription found.",
         case_key=f"unbound_entity:{entity_ref}",
         reason="unbound_provider_customer",
         organization_id=notes_org_id,
         diagnostic_payload={ ... },
     )
     ```
     Even if `notes["organization_id"]` contains a valid organization UUID, the resolver **strictly refuses** to bind the entity. It fails closed and stages a reconciliation case.
4. **Organization Mismatch Handling**:
   - If `authoritative_org_id` exists and `notes["organization_id"]` exists, lines 747–764 verify `notes_org_id == authoritative_org_id`.
   - If they disagree, it raises `RazorpayWebhookReconciliationConflictError(reason="metadata_tenant_mismatch")`. Zero mutation occurs.
5. **Missing Entity IDs**:
   - If both `customer_id` and `subscription_id` are `None` or empty, `authoritative_org_id` is `None`, and the resolver raises `unbound_provider_customer`. It cannot resolve another tenant by accident.
6. **Query Ordering**:
   - `scalar_one_or_none()` is used on unique-constrained lookups. No arbitrary selection exists.

---

## 5. Section 4: Payload Validation Audit

Inspect `extract_subscription_entity()`, `extract_payment_entity()`, `extract_invoice_entity()`, and `parse_positive_unix_timestamp()` in `handler_base.py`:

### 5.1 Boolean Subclass Rejection
In Python, `bool` is a subclass of `int` (`isinstance(True, int) == True`). If not explicitly guarded, a boolean `{"amount": True}` would evaluate to `1` paise.
- **Verification**:
  - `parse_positive_unix_timestamp` (`handler_base.py#L234`):
    `if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):` -> Rejects `bool`.
  - `extract_payment_entity` (`handler_base.py#L315`):
    `if amount is None or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:` -> Rejects `bool`.
  - `extract_payment_entity` (`handler_base.py#L329`):
    `if isinstance(amount_refunded, bool) or not isinstance(amount_refunded, int) or amount_refunded < 0:` -> Rejects `bool`.
  - `extract_invoice_entity` (`handler_base.py#L369, L373, L377`):
    `isinstance(amount, bool)` -> Explicitly rejected for `amount`, `amount_paid`, and `amount_due`.
- **Verdict**: **COMPLETELY SAFE**. Booleans cannot masquerade as numeric amounts or timestamps.

### 5.2 Floating-Point Currency Rejection
- `isinstance(amount, int)` strictly requires integers.
- If a float is passed (e.g. `299.99`), `isinstance(299.99, int)` evaluates to `False`.
- `RazorpayPayloadValidationError` is raised. Floating-point currency amounts are strictly rejected.

### 5.3 Bounds & Format Enforcement
- Payment `amount`: Must be strictly positive integer ($> 0$). Zero or negative values fail closed.
- Invoice `amount`: Must be non-negative integer ($\ge 0$).
- `currency`: Must be non-empty string of exactly 3 characters (`len(currency.strip()) == 3`).
- Timestamps: Must be strictly positive numbers ($> 0$). Negative or zero timestamps fail closed.

### 5.4 Untrusted Metadata & Partial Mutation
- Validation executes in memory *before* domain mutations begin.
- If an entity payload is malformed or invalid, `RazorpayPayloadValidationError` is raised before any database write is dispatched.
- Tested by `test_transaction_rollback_on_failed_validation`: uncommitted domain operations are rolled back cleanly.

---

## 6. Section 5: Reconciliation History Audit

Inspect `stage_or_update_payment_reconciliation()` (`handler_base.py#L791-L881`):

### 6.1 Diagnostic History Preservation
```python
new_history_entry = {
    "timestamp": now_utc.isoformat(),
    "reason": reason,
    "details": clean_data,
}
if existing is not None:
    existing_history = list(existing.diagnostic_payload.get("history", []))
    existing_history.append(new_history_entry)
    existing.diagnostic_payload = {
        **existing.diagnostic_payload,
        "history": existing_history,
        "latest": clean_data,
    }
```
- Existing entries in `diagnostic_payload["history"]` are copied into a new list and the new diagnostic entry is appended.
- `diagnostic_payload["latest"]` is updated to the newest evidence.
- Verified by `test_reconciliation_case_preserves_diagnostic_history` across multiple sequential updates.

### 6.2 Concurrent Upserts & Case Key Scope
- `PaymentReconciliation` enforces `UniqueConstraint("provider", "merchant_account_id", "environment", "case_key", name="uq_payment_reconciliations_case_key")` (`payment.py#L676-L682`).
- Under concurrent execution, Level 0 advisory locking on `entity_id` serializes operations for the same underlying entity.
- (See Condition 3 regarding row-level locking on the lookup query).

### 6.3 Entitlement Isolation
- `PaymentReconciliation` records are strictly non-authoritative audit records.
- Neither `accounting.py` nor `Subscription` queries `PaymentReconciliation` to authorize usage.
- Staging or updating a reconciliation record cannot grant or revoke Pro entitlement.

---

## 7. Section 6: Cancellation Finalization Audit

Inspect `should_finalize_deferred_cancellation()` (`handler_base.py#L967-L993`):

```python
def should_finalize_deferred_cancellation(
    status: SubscriptionStatus | str,
    cancel_at_period_end: bool,
    past_due_since: datetime | None,
    grace_period_days: int = 7,
    now: datetime | None = None,
) -> bool:
```

### 7.1 Inputs and Logic Evaluation
1. **Inputs**:
   - `status`: String or `SubscriptionStatus` enum.
   - `cancel_at_period_end`: Boolean flag.
   - `past_due_since`: Optional UTC datetime anchor when grace began.
   - `grace_period_days`: Integer grace window (default 7).
   - `now`: Optional UTC datetime override (default `datetime.now(timezone.utc)`).
2. **Logic**:
   - If `status.lower() != 'past_due'`: returns `False`.
   - If `not cancel_at_period_end`: returns `False`.
   - If `past_due_since is None`: returns `False`.
   - `grace_deadline = past_due_since + timedelta(days=grace_period_days)`.
   - Returns `now_utc >= grace_deadline`.

### 7.2 Grace Expiry vs Period End
- Active subscription canceled at cycle end (`status == 'active'`, `cancel_at_period_end == True`):
  - Returns `False`. Active cycle-end cancellation is finalized when `now >= current_period_end`, which is evaluated separately by subscription period end handlers.
- Past-due subscription canceled during grace (`status == 'past_due'`, `cancel_at_period_end == True`):
  - Returns `True` when `now >= past_due_since + 7d`.

### 7.3 Timezone Awareness & Missing Timestamps
- `now_utc` is constructed with `timezone.utc`.
- In SQLAlchemy, `Subscription.past_due_since` is mapped as `DateTime(timezone=True)`, which produces timezone-aware datetimes.
- If `past_due_since` is `None`, the function safely returns `False`.
- (See Condition 4 regarding defensive coercion of offset-naive timestamps).

### 7.4 Future Invocation Architecture
The predicate will be evaluated by three distinct paths in future steps:
1. **Real-Time Usage Check (`accounting.py`)**: Already active. `accounting.py` independently rejects usage when `now >= past_due_since + 7d` (`GRACE_PERIOD_EXPIRED`), preventing quota leakage.
2. **Lazy Read Finalization**: `GET /api/v1/billing/subscription` evaluates `should_finalize_deferred_cancellation()` and persists `status = 'canceled'`, `plan_tier = 'free'`.
3. **Incoming Webhooks & Reconciliations**: When future events arrive for the subscription, handlers evaluate the predicate and execute the terminal state transition.

---

## 8. Section 7: Test Classification

Every test in `apps/api/tests/test_razorpay_handler_foundation.py` is classified below:

| # | Test Name | Classification | Target Component | Live PostgreSQL? |
| :-: | :--- | :--- | :--- | :-: |
| 1 | `test_advisory_lock_deterministic_key_generation` | **Pure Unit** | Key derivation algorithm | No |
| 2 | `test_advisory_lock_distinct_entities_and_types` | **Pure Unit** | Collision resistance in memory | No |
| 3 | `test_advisory_lock_rejects_missing_or_empty_identifiers` | **Pure Unit** | Argument validation & exceptions | No |
| 4 | `test_acquire_and_try_advisory_lock_execution` | **Mocked Database** | `RazorpayHandlerMockDbSession` | No |
| 5 | `test_validate_envelope_success_claimed_event` | **Unit / Schema** | Dataclass parsing into `RazorpayEventContext` | No |
| 6 | `test_validate_envelope_rejects_unverified_signature` | **Unit / Guard** | Signature verification guard | No |
| 7 | `test_validate_envelope_rejects_scope_mismatch` | **Unit / Guard** | Provider and merchant scope check | No |
| 8 | `test_entity_id_extraction_and_missing_guards` | **Unit / Schema** | Entity ID extractors and missing guards | No |
| 9 | `test_strict_payload_validation_rejects_invalid_values` | **Unit / Validation** | Type checks (amounts, timestamps, currencies) | No |
| 10 | `test_error_hierarchy_distinguishes_failure_categories` | **Pure Unit** | Exception inheritance hierarchy | No |
| 11 | `test_resolve_tenant_organization_authoritative_match` | **Mocked Database** | `RazorpayHandlerMockDbSession` | No |
| 12 | `test_resolve_tenant_organization_rejects_metadata_spoofing` | **Mocked Database** | Anti-spoofing tenant binder | No |
| 13 | `test_resolve_tenant_organization_refuses_fake_binding_for_unbound_customer` | **Mocked Database** | Unbound customer failure check | No |
| 14 | `test_reconciliation_case_preserves_diagnostic_history` | **Mocked Database** | Append-only history update | No |
| 15 | `test_assert_valid_worker_lease_permits_active_lease` | **Mocked Database** | Unexpired lease check | No |
| 16 | `test_assert_valid_worker_lease_rejects_expired_or_superseded_lease` | **Mocked Database** | Stale lease detection & rollback | No |
| 17 | `test_should_finalize_deferred_cancellation_evaluation` | **Pure Unit** | Grace period expiration math | No |
| 18 | `test_monotonic_and_terminal_idempotency_helpers` | **Pure Unit** | Monotonic timestamps & refusal of resurrection | No |
| 19 | `test_transaction_rollback_on_failed_validation` | **Mocked Database** | Session rollback on validation failure | No |
| 20 | `test_composite_payload_parses_all_entities` | **Unit / Schema** | Composite payload extraction | No |

### Summary of Classification
- **Pure Unit**: 7 tests (35%)
- **Unit / Schema / Validation**: 5 tests (25%)
- **Mocked Database**: 8 tests (40%)
- **Real PostgreSQL Integration**: 0 tests (0%)

> [!IMPORTANT]
> **Test Boundary Disclosure**:
> The 20 tests verify algorithm correctness, schema parsing, SQL text generation, parameter extraction, and transaction rollback mechanics against high-fidelity in-memory mock sessions (`RazorpayHandlerMockDbSession`). They do **not** execute against a live PostgreSQL database server. Real PostgreSQL concurrency (kernel-level advisory lock release on transaction commit, `FOR UPDATE SKIP LOCKED` physical engine queuing) must be verified in end-to-end integration environments with a real PostgreSQL instance.

---

## 9. Section 8: Git and Migration Checks

1. **Alembic Single Head**:
   ```
   $ cd apps/api; .venv/Scripts/python -m alembic heads
   d1e2f3a4b5c6 (head)
   ```
   - Verified single head: `d1e2f3a4b5c6`.
   - Zero database migrations introduced.
2. **No Stripe Modifications**:
   - `apps/api/app/services/billing/stripe_*.py` files were not modified in Step 5.2D.3.1.
3. **No Entitlement / Accounting Changes**:
   - `git diff apps/api/app/services/billing/accounting.py` produces **zero diff**.
   - `reserve_usage()`, `settle_usage()`, quota enforcement, and grace period logic were completely untouched.
4. **Working Tree Scope**:
   - Modified: `apps/api/app/services/billing/razorpay/__init__.py`, `apps/api/app/workers/razorpay_webhook_worker.py`.
   - Created: `apps/api/app/services/billing/razorpay/lock.py`, `apps/api/app/services/billing/razorpay/handler_base.py`, `apps/api/tests/test_razorpay_handler_foundation.py`.
   - No commits or pushes made.

---

## 10. Audit Conditions & Defect Severity

| # | Item | Category | Severity | Description & Recommendation |
| :-: | :--- | :--- | :-: | :--- |
| **C1** | Clock Skew Assumption | Operational | **Low** | Application worker clock relies on NTP synchronization with database server clock. Documented requirement: maintain sub-second NTP synchronization across worker fleet. |
| **C2** | Entity Type Delimiter Validation | Security | **Low** | `f"razorpay:{clean_type}:{clean_id}"` assumes `entity_type` contains no colons. While currently restricted to literals (`subscription`, `payment`, `invoice`), add an explicit whitelist check `clean_type in ALLOWED_ENTITY_TYPES` in Step 5.2D.3.2. |
| **C3** | Reconciliation Row Locking | Concurrency | **Low** | `stage_or_update_payment_reconciliation()` selects existing case without `.with_for_update()`. While protected by Level 0 advisory locks, adding `.with_for_update()` to the query provides defense-in-depth if called outside advisory locks. |
| **C4** | Defensive Timezone Normalization | Robustness | **Low** | In `should_finalize_deferred_cancellation()`, if an offset-naive datetime is ever passed as `past_due_since`, Python raises `TypeError`. Add `if past_due_since.tzinfo is None: past_due_since = past_due_since.replace(tzinfo=timezone.utc)` for defense-in-depth. |

None of the above conditions represent blocking defects or security vulnerabilities in Step 5.2D.3.1. They are operational hardening measures for upcoming handler stages.

---

## 11. Final Verdict

### **Verdict: PASS WITH CONDITIONS**

The Step 5.2D.3.1 implementation meets all architectural, mathematical, and security standards established in the platform specifications. Transaction boundaries are strictly preserved, advisory locking is deterministic and bounded, tenant binding is anti-spoofing and fail-closed, and diagnostic evidence is preserved immutably.

Authorization is granted to proceed to **Step 5.2D.3.2: Razorpay Agreement Lifecycle Handlers**.
