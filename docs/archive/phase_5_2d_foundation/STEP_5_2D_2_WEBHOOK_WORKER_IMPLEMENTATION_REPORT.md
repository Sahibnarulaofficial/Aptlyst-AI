# Step 5.2D.2: Durable Razorpay Webhook Worker and Lease Ownership Implementation Report

## Executive Summary

Step 5.2D.2 implements the **Durable Razorpay Webhook Processing Infrastructure** with explicit database-backed lease ownership, fencing verification, and bounded retry/quarantine semantics.

Unlike the legacy Stripe worker which held long-running database transactions and row locks across handler execution, the Razorpay worker implements a decoupled **Two-Phase Leased Ownership Pattern**:
1. **Phase 1 (Claim)**: A short database transaction claims the next eligible event using `SELECT ... FOR UPDATE SKIP LOCKED`, stamps an explicit cryptographic lease (`lease_token`, `locked_by`, `lease_expires_at`), and commits immediately, releasing all database locks before handler execution.
2. **Phase 2 (Execution)**: Handlers and external network calls execute outside the database claim transaction.
3. **Phase 3 (Completion & Fencing)**: A short transaction locks the row, verifies that the worker still holds the exact valid unexpired lease token, records the processing outcome, and atomically clears the lease fields. Any stale or expired worker is rejected with `StaleLeaseError` and forbidden from mutating event state or attempts.

---

## 1. Initial Findings

Before writing code or migrations, an architectural audit compared the existing Stripe worker and engine against the provider-neutral database foundation:

1. **`BillingWebhookEvent` Schema vs. Lease Requirements**:
   - `billing_webhook_events` previously contained event identity, payload, status (`pending`, `processed`, `failed`, `quarantined`), `attempts` (initialized to 0 in Step 5.2D.1.1), `signature_verified`, `next_retry_at`, and timestamps (`received_at`, `processed_at`, `quarantined_at`).
   - The table lacked explicit lease ownership fields: `lease_token` (UUID), `lease_expires_at` (timestamptz), and `locked_by` (varchar).
2. **Stripe Worker Comparison**:
   - In `stripe_webhook_engine.py` and `stripe_webhook_worker.py`, events were claimed using `SELECT ... FOR UPDATE SKIP LOCKED` inside an open transaction that was held active throughout handler execution.
   - **Why this cannot be reused for Razorpay**:
     - Holding open transactions and row locks across network calls exhausts database connection pools and causes lock tree serialization.
     - A row lock is purely connection-bound; if the connection terminates or restarts, the row lock evaporates without leaving any audit trail or fencing token.
     - Stale workers that stall and resume can perform phantom out-of-order writes if they are not explicitly fenced by an unforgeable lease token.
3. **Architectural Decision**:
   - A committed row lock is **not** a lease. True ownership must be represented by persisted lease data committed to PostgreSQL.
   - The Razorpay worker must claim, stamp lease fields, commit, execute outside the claim transaction, and validate fencing tokens before recording outcomes.

---

## 2. Files Changed

### Created Files
- [`apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py)
  - Alembic migration adding `lease_token`, `lease_expires_at`, `locked_by`, partial composite claim index, and safe downgrade guards.
- [`apps/api/app/services/billing/razorpay/webhook_engine.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/webhook_engine.py)
  - Core lease claim operation (`claim_next_razorpay_webhook_event`), ownership validation and outcome recording (`record_razorpay_webhook_outcome`), retry backoff calculations, dispatcher, and domain exceptions (`StaleLeaseError`, `RazorpayWebhookRetryableError`, etc.).
- [`apps/api/app/workers/razorpay_webhook_worker.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/workers/razorpay_webhook_worker.py)
  - Background polling worker (`RazorpayWebhookWorker`) with decoupled transaction boundaries, graceful shutdown, and batch processing.
- [`apps/api/tests/test_razorpay_webhook_worker.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/tests/test_razorpay_webhook_worker.py)
  - 15 automated unit, concurrency, lease fencing, and offline migration tests.
- [`STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md`](file:///d:/Projects/V1/Aptlyst-AI/STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md)
  - This implementation report.

### Modified Files
- [`apps/api/app/models/payment.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/models/payment.py)
  - Added `lease_token`, `lease_expires_at`, and `locked_by` to `BillingWebhookEvent` model and added `ix_billing_webhook_events_claim` index.
- [`apps/api/app/services/billing/razorpay/__init__.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/__init__.py)
  - Exported all webhook engine symbols, exceptions, and claim/outcome functions.
- [`apps/api/tests/test_provider_neutral_billing_models.py`](file:///d:/Projects/V1/Aptlyst-AI/apps/api/tests/test_provider_neutral_billing_models.py)
  - Updated model tests to assert `lease_token`, `lease_expires_at`, `locked_by`, and the claim index.

---

## 3. Migration Details

- **Revision ID**: `d1e2f3a4b5c6`
- **Revises**: `c1d2e3f4a5b7`
- **Schema Alterations**:
  ```sql
  ALTER TABLE billing_webhook_events ADD COLUMN lease_token UUID;
  ALTER TABLE billing_webhook_events ADD COLUMN lease_expires_at TIMESTAMP WITH TIME ZONE;
  ALTER TABLE billing_webhook_events ADD COLUMN locked_by VARCHAR(64);

  CREATE INDEX ix_billing_webhook_events_claim ON billing_webhook_events (
      provider, status, next_retry_at, lease_expires_at, received_at
  ) WHERE status IN ('pending', 'failed') AND signature_verified = true;
  ```
- **Security & RLS**:
  - Re-applies `ENABLE ROW LEVEL SECURITY` and `REVOKE ALL ON billing_webhook_events FROM anon, authenticated`.
- **Backward Compatibility**:
  - All new columns are nullable without permanent default constraints, preventing table rewrite locks.
  - Existing historical records remain completely intact.
- **Safe Downgrade Guard**:
  - Downgrade executes an active lease check before dropping columns. If unexpired active leases exist, downgrade is aborted to prevent dropping in-flight worker ownership.

---

## 4. Claim and Lease Ownership Algorithm

### Step 1: Atomic Claim Operation
The worker requests an event via `claim_next_razorpay_webhook_event()`:
```python
stmt = (
    select(BillingWebhookEvent)
    .where(
        BillingWebhookEvent.provider == provider,
        BillingWebhookEvent.signature_verified.is_(True),
        BillingWebhookEvent.status.in_([WebhookProcessingStatus.pending, WebhookProcessingStatus.failed]),
        BillingWebhookEvent.attempts < max_attempts,
        or_(
            BillingWebhookEvent.next_retry_at.is_(None),
            BillingWebhookEvent.next_retry_at <= now_utc,
        ),
        or_(
            BillingWebhookEvent.lease_expires_at.is_(None),
            BillingWebhookEvent.lease_expires_at <= now_utc,
        ),
    )
    .order_by(BillingWebhookEvent.received_at.asc())
    .limit(1)
    .with_for_update(skip_locked=True)
)
```
- A fresh `lease_token = uuid.uuid4()` is assigned.
- `locked_by = worker_id` and `lease_expires_at = now + 60s` are stamped.
- `await db.commit()` commits the lease to PostgreSQL immediately and releases the database row lock.
- Returns an immutable `ClaimedWebhookEvent` data structure.

### Step 2: Unlocked Handler Execution
The worker invokes the registered event handler outside of any database transaction or row lock. External HTTP requests or payment gateway probes cannot block database worker threads or hold open database transactions.

### Step 3: Fencing Validation & Outcome Recording
When the handler completes (or fails), `record_razorpay_webhook_outcome()` opens a short completion transaction:
```python
stmt = select(BillingWebhookEvent).where(BillingWebhookEvent.id == claimed.event_id).with_for_update()
event = (await db.execute(stmt)).scalar_one_or_none()

# Strict Fencing Assertions:
is_same_token = event.lease_token == claimed.lease_token
is_same_worker = event.locked_by == claimed.locked_by
is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now_utc

if not (is_same_token and is_same_worker and is_unexpired):
    await db.rollback()
    raise StaleLeaseError(f"Worker '{claimed.locked_by}' lost lease ownership for event {claimed.event_id}")
```
- If fencing passes:
  - Updates `status`, `attempts`, `processed_at`, `quarantined_at`, or `next_retry_at`.
  - Atomically clears lease fields: `lease_token = None`, `locked_by = None`, `lease_expires_at = None`.
  - Commits transaction.
- If fencing fails:
  - Rolls back immediately.
  - Stale worker is completely blocked from modifying status, attempts, retries, or quarantine.

---

## 5. Retry and Crash Recovery Behavior

### Retry Semantics
- **Initial State**: Newly ingested events have `attempts = 0` (0 processing attempts performed).
- **1st Failure**: `attempts` increments to 1; backoff is 5 seconds (`next_retry_at = now + 5s`).
- **2nd Failure**: `attempts` increments to 2; backoff is 15 seconds (`next_retry_at = now + 15s`).
- **3rd Failure**: `attempts` increments to 3; backoff is 45 seconds (`next_retry_at = now + 45s`).
- **4th Failure**: `attempts` increments to 4; backoff is 120 seconds (`next_retry_at = now + 120s`).
- **5th Failure**: `attempts` increments to 5; `status = quarantined`, `quarantined_at = now`, `quarantine_reason = Exceeded maximum attempts (5/5)`. Event will never be polled automatically again.

### Crash Recovery
- If a worker process crashes or loses network connectivity mid-execution:
  - The event remains in PostgreSQL with its stamped `lease_expires_at`.
  - While `now < lease_expires_at`, no other worker can claim the event.
  - As soon as `now >= lease_expires_at`, the expired lease becomes eligible for recovery.
  - Another worker claims the stranded event and stamps a fresh `lease_token`.
  - **Critical Invariant**: Re-claiming an expired lease does **not** increment `attempts`.
  - If the crashed worker resumes later, its old lease token will no longer match the database row, and its outcome completion will be rejected with `StaleLeaseError`.

### Analysis: Crash After Business Mutation but Before Event Completion
In distributed webhook processing, if a worker executes an external mutation (e.g., updating a payment record) but crashes before committing the webhook event completion transaction:
1. The lease expires after 60 seconds.
2. A subsequent worker claims and re-executes the event.
3. Therefore, downstream business handlers must be **strictly idempotent** (e.g. keyed on `payment_id`, `invoice_id`, or `subscription_id` via unique constraints or state assertions).
4. Aptlyst AI guarantees **at-least-once** durable processing with idempotent deduplication; it does not claim impossible exactly-once delivery across process crashes.

---

## 6. Test Execution Summary

### Test Commands
```powershell
# 1. Razorpay Webhook Worker & Lease Suite (15 Tests)
apps/api/.venv/Scripts/pytest.exe apps/api/tests/test_razorpay_webhook_worker.py -v

# 2. Complete Billing Test Suite (Worker, Ingestion, Checkout, Stripe, Config, Migrations)
apps/api/.venv/Scripts/pytest.exe apps/api/tests/test_razorpay_webhook_worker.py apps/api/tests/test_razorpay_webhook_ingestion.py apps/api/tests/test_razorpay_checkout.py apps/api/tests/test_stripe_webhook_ingestion.py apps/api/tests/test_stripe_webhook_engine.py apps/api/tests/test_stripe_webhook_worker.py apps/api/tests/test_razorpay_config.py apps/api/tests/test_stripe_config.py apps/api/tests/test_provider_neutral_billing_models.py apps/api/tests/test_provider_neutral_migration.py -v
```

### Test Results

#### Razorpay Webhook Worker Tests (`test_razorpay_webhook_worker.py`)
```
collected 15 items

test_1_successful_claim_and_lease_assignment PASSED [  6%]
test_2_two_workers_competing_for_same_event PASSED [ 13%]
test_3_different_workers_claiming_different_events PASSED [ 20%]
test_4_unexpired_lease_cannot_be_stolen PASSED [ 26%]
test_5_expired_lease_can_be_recovered PASSED [ 33%]
test_6_stale_worker_cannot_complete_after_lease_replacement PASSED [ 40%]
test_7_stale_worker_cannot_increment_attempts_or_quarantine PASSED [ 46%]
test_8_valid_owner_can_mark_event_processed PASSED [ 53%]
test_9_valid_owner_can_schedule_retry PASSED [ 60%]
test_10_fifth_failed_attempt_quarantines PASSED [ 66%]
test_11_lease_recovery_does_not_increment_attempts PASSED [ 73%]
test_12_worker_crash_and_recovery_end_to_end PASSED [ 80%]
test_13_lease_fields_are_cleared_on_completion PASSED [ 86%]
test_14_provider_account_environment_scoping_intact PASSED [ 93%]
test_15_offline_alembic_ddl_and_metadata PASSED [100%]

============================= 15 passed in 6.38s ==============================
```

#### Full Billing Regression Suite
```
177 passed in 22.55s (100% green across all billing unit, concurrency, and migration tests)
```

---

## 7. PostgreSQL Integration vs. Mocked Test Distinction

1. **PostgreSQL DDL & Migration Integration**:
   - `test_15_offline_alembic_ddl_and_metadata` executes the real Alembic CLI (`alembic upgrade c1d2e3f4a5b7:d1e2f3a4b5c6 --sql` and downgrade) using `PostgresqlImpl`.
   - Directly compiles PostgreSQL-specific DDL, asserting exact column types (`UUID`, `TIMESTAMP WITH TIME ZONE`, `VARCHAR(64)`), partial index definitions (`WHERE status IN ('pending', 'failed') AND signature_verified = true`), and security privileges (`REVOKE ALL FROM anon, authenticated`).
2. **In-Memory Concurrency Test Doubles**:
   - `SharedRazorpayEventRepository` and `RazorpayWorkerMockDbSession` simulate PostgreSQL transaction isolation, row locking (`FOR UPDATE SKIP LOCKED`), and rollback.
   - Allows comprehensive multi-worker concurrency testing (competing workers, unexpired lease protection, recovery, fencing, and crash simulation) in hermetic, sandboxed test environments without external network or database dependencies.

---

## 8. Alembic Head

Confirmed via `alembic heads`:
```
d1e2f3a4b5c6 (head)
```

Migration Chain:
`f6a1b2c3d4e5` $\to$ `a1b2c3d4e5f7` $\to$ `b1c2d3e4f5a6` $\to$ `c1d2e3f4a5b7` $\to$ `d1e2f3a4b5c6` (head).

---

## 9. Git Status

```
On branch main
Your branch is ahead of 'origin/main' by 1 commit.

Changes not staged for commit:
	modified:   apps/api/alembic/env.py
	modified:   apps/api/app/api/billing.py
	modified:   apps/api/app/config.py
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
	modified:   apps/api/tests/test_provider_neutral_billing_models.py
	apps/api/tests/test_stripe_checkout_handler.py
	apps/api/tests/test_stripe_customer.py

Untracked files:
	STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md
	STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md
	STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md
	STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md
	apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py
	apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py
	apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py
	apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py
	apps/api/app/models/payment.py
	apps/api/app/services/billing/razorpay/
	apps/api/app/workers/razorpay_webhook_worker.py
	apps/api/tests/test_provider_neutral_migration.py
	apps/api/tests/test_razorpay_checkout.py
	apps/api/tests/test_razorpay_client.py
	apps/api/tests/test_razorpay_config.py
	apps/api/tests/test_razorpay_customer.py
	apps/api/tests/test_razorpay_webhook_ingestion.py
	apps/api/tests/test_razorpay_webhook_worker.py
```

---

## 10. Known Limitations and Unresolved Risks

1. **Clock Skew across Worker Nodes**:
   - Lease expiry relies on UTC timestamps stored in PostgreSQL (`lease_expires_at`).
   - If worker nodes have significant wall-clock skew relative to the PostgreSQL host, a worker might perceive a lease as expired before PostgreSQL or vice-versa.
   - *Mitigation*: NTP synchronization across all container/worker hosts; lease duration (60s) is sufficiently large to absorb standard NTP drift (< 100ms).
2. **Business Handler Idempotency (Step 5.2D.3)**:
   - Because exactly-once processing cannot be guaranteed across worker crash boundaries, all upcoming business event handlers (subscription activated/charged/cancelled, invoice paid, payment captured) must implement entity-level deduplication.
3. **No Business Handlers Yet**:
   - The worker infrastructure is fully functional, but no domain event handlers were registered in this step, as mandated by the scope boundaries.

---

## 11. Conclusion & Authorization Readiness

Step 5.2D.2 is complete. The Razorpay durable webhook worker and explicit database lease ownership model are implemented, covered by 15 dedicated tests and 177 passing billing regression tests, and verified against Alembic head `d1e2f3a4b5c6`.

Implementation is halted awaiting explicit authorization for the next step.
