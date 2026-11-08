# Aptlyst AI — Step 5.2D.2.1: Final Read-Only Webhook Worker Audit

**Date**: 2026-11-08  
**Audit Scope**: Razorpay Webhook Worker & Lease Ownership Implementation (Step 5.2D.2)  
**Audited Artifacts**:
- `apps/api/app/services/billing/razorpay/webhook_engine.py`
- `apps/api/app/workers/razorpay_webhook_worker.py`
- `apps/api/app/models/payment.py`
- `apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py`
- `apps/api/tests/test_razorpay_webhook_worker.py`
- Reference: `apps/api/app/workers/stripe_webhook_worker.py`
- Reference: `apps/api/app/services/billing/stripe_webhook_engine.py`

---

## 1. Executive Summary & Verdict

| Dimension | Assessment | Status |
| :--- | :--- | :--- |
| **Atomic Claiming** | `SELECT ... FOR UPDATE SKIP LOCKED` commits immediately; releases row locks prior to handler execution. | **VERIFIED** |
| **Lease Ownership & Fencing** | Unpredictable UUID4 token, dual-check (`locked_by` & `lease_token`), authoritative timestamp expiry check. | **VERIFIED** |
| **Crash & Replay Safety** | 7 failure scenarios evaluated. Handlers require idempotency; zero phantom commits or corrupted leases. | **VERIFIED** |
| **Retry & Quarantine** | Starts at 0; 5 actual failures to quarantine; backoff 5s, 15s, 45s, 120s; no penalty for expired lease reclaims. | **VERIFIED** |
| **Worker Lifecycle** | Graceful shutdown, batch processing, resilient polling loop. | **VERIFIED** |
| **CLI Invocation** | `razorpay_webhook_worker.py` imports `argparse`/`sys`/`signal` but lacks `main()` and `__main__` entrypoint. | **DEFECT IDENTIFIED** |
| **PostgreSQL Test Evidence** | Tests 1–14 are in-memory mock concurrency; Test 15 is offline Alembic `--sql` DDL syntax verification. | **CLASSIFIED** |
| **Migration & Schema** | Revision `d1e2f3a4b5c6` is the verified single head; model matches DDL; RLS revoked; downgrade guard active. | **VERIFIED** |

### Final Audit Verdict: **PASS WITH CONDITIONS**

The core lease ownership engine, database model, migration, and concurrency algorithms are technically sound, mathematically robust, and satisfy all security and transaction-boundary invariants. Authorization for production deployment or handler execution (Step 5.2D.3) is subject to resolving the following two conditions:

1. **Condition 1 (CLI Entrypoint)**: Add the missing CLI argument parser, signal handler setup, and `if __name__ == "__main__": main()` entrypoint in `apps/api/app/workers/razorpay_webhook_worker.py` to allow direct execution via `python -m app.workers.razorpay_webhook_worker`.
2. **Condition 2 (Live Database Concurrency Testing)**: Supplement the in-memory test doubles with live PostgreSQL multi-process concurrency integration tests during staging/CI pipeline validation.

---

## 2. Section A: Atomic Claiming Verification

### 2.1 SQLAlchemy Query & Predicate Inspection
In `apps/api/app/services/billing/razorpay/webhook_engine.py` (lines 255–284), the query is constructed as:

```python
stmt = (
    select(BillingWebhookEvent)
    .where(
        BillingWebhookEvent.provider == provider,
        BillingWebhookEvent.signature_verified.is_(True),
        BillingWebhookEvent.status.in_(
            [WebhookProcessingStatus.pending, WebhookProcessingStatus.failed]
        ),
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
)

if environment is not None:
    stmt = stmt.where(BillingWebhookEvent.environment == environment)

stmt = (
    stmt.order_by(BillingWebhookEvent.received_at.asc())
    .limit(1)
    .with_for_update(skip_locked=True)
)
```

### 2.2 Invariant Verification Checklist
1. **`FOR UPDATE SKIP LOCKED`**:
   - `with_for_update(skip_locked=True)` is directly applied.
   - Any event row currently being inspected or updated by another worker transaction is skipped instantly without blocking, avoiding connection pool starvation and database row lock contention.
2. **Transaction Boundaries & Premature Lock Release**:
   - In lines 291–298, the worker stamps `lease_token`, `locked_by`, and `lease_expires_at`, and immediately executes:
     ```python
     await db.commit()
     await db.refresh(event)
     ```
   - **Verification**: The claim transaction commits to disk and completes *before* returning `ClaimedWebhookEvent`. Row locks are held for milliseconds during the claim, never during handler execution or downstream HTTP network calls.
3. **Pending and Failed Eligibility**:
   - The query explicitly restricts status to `status.in_([WebhookProcessingStatus.pending, WebhookProcessingStatus.failed])`.
   - Events with `status == "processed"` or `status == "quarantined"` are excluded at the SQL level.
   - `attempts < max_attempts` ensures events that reached threshold are ineligible.
4. **Lease Expiry vs. Unexpired Leases**:
   - Unexpired leases (`lease_expires_at > now_utc`) evaluate to `False` in `or_(lease_expires_at.is_(None), lease_expires_at <= now_utc)` and cannot be claimed by any other worker.
   - Expired leases (`lease_expires_at <= now_utc`) evaluate to `True` and are eligible for reclamation.
   - Stamping a new lease on an expired event does *not* increment `attempts` during claim.
5. **Null `next_retry_at` Handling**:
   - Newly ingested events possess `next_retry_at = None`. The expression `or_(BillingWebhookEvent.next_retry_at.is_(None), ...)` ensures fresh events are immediately claimable without waiting.

---

## 3. Section B: Lease Ownership and Fencing Token Hardening

### 3.1 Fresh Token Generation
In `webhook_engine.py` (line 253):
```python
fresh_lease_token = uuid.uuid4()
```
Every claim generates a cryptographically unpredictable UUIDv4 token. A re-claimed event never inherits its previous lease token.

### 3.2 Fencing Check at Outcome Recording
In `webhook_engine.py` (lines 363–396), before any status mutation or retry calculation is applied, the event is re-queried under lock:

```python
stmt = (
    select(BillingWebhookEvent)
    .where(BillingWebhookEvent.id == claimed.event_id)
    .with_for_update()
)
result = await db.execute(stmt)
event = result.scalar_one_or_none()

if event is None:
    await db.rollback()
    raise StaleLeaseError(f"Event {claimed.event_id} was deleted or not found.")

is_same_token = event.lease_token == claimed.lease_token
is_same_worker = event.locked_by == claimed.locked_by
is_unexpired = event.lease_expires_at is not None and event.lease_expires_at >= now_utc

if not (is_same_token and is_same_worker and is_unexpired):
    await db.rollback()
    raise StaleLeaseError(...)
```

### 3.3 Verification of Fencing Invariants
1. **Dual Ownership Verification**:
   - Fencing requires `event.lease_token == claimed.lease_token` AND `event.locked_by == claimed.locked_by`.
   - If a different worker claimed the row or the lease was re-issued, `is_same_token` evaluates to `False`.
2. **Authoritative Timestamp Comparison**:
   - The check `event.lease_expires_at >= now_utc` enforces that the lease has not expired according to the authoritative timestamp passed into the transaction.
   - If a worker's handler hung for 70 seconds on a 60-second lease, `is_unexpired` evaluates to `False`.
3. **Atomic Outcome and Lease Clearance**:
   - In lines 400–438, within the *same* database transaction:
     - `event.status` is updated (`processed`, `failed`, or `quarantined`).
     - `event.attempts` is incremented if failed.
     - `event.lease_token = None`, `event.locked_by = None`, `event.lease_expires_at = None` are cleared.
     - `await db.commit()` commits both the status change and the lease clearance simultaneously.
4. **Edge Cases**:
   - **Row deleted during processing**: `event is None` triggers immediate `await db.rollback()` and raises `StaleLeaseError`.
   - **Null lease fields**: If an operator cleared the lease or another worker cleared it, `event.lease_token == claimed.lease_token` evaluates to `None == UUID(...)` $\to$ `False`. Rejected.

---

## 4. Section C: Crash and Failure Recovery Analysis

The implementation was analyzed against seven critical failure scenarios:

```
+---------------------------------------------------------------------------------------------------+
| Worker Processing Timeline & Crash Boundaries                                                     |
+---------------------------------------------------------------------------------------------------+
  (1) Claim & Lease Commit         (2) Handler Executing          (3) Outcome Record & Commit
---|--------------------------------|------------------------------|----------------------------->
   ^                                ^                              ^
   Crash 1                          Crash 2                        Crash 3
   [Zero side effects]              [Partial handler state]        [Handler done, outcome lost]
   Lease expires in 60s             Lease expires in 60s           Lease expires in 60s
   Worker B reclaims cleanly        Worker B reclaims (REPLAY)     Worker B reclaims (REPLAY)
```

### Scenario 1: Crash immediately after claim commit
- **State**: Event has `lease_token = UUID_1`, `locked_by = Worker_1`, `lease_expires_at = now + 60s`.
- **Behavior**: Worker 1 terminates prior to invoking handler. No domain mutation occurs. During the 60-second lease, no other worker touches the row. At second 61, Worker 2 claims the event with `UUID_2`.
- **Verdict**: **SAFE**. Zero domain corruption; automatic recovery without manual intervention.

### Scenario 2: Crash during handler execution
- **State**: Worker 1 executes external API calls or database operations, then crashes before completing.
- **Behavior**: The claim transaction was committed; uncommitted handler transactions roll back. The event remains leased until `lease_expires_at`. Worker 2 subsequently claims the event and re-executes the handler.
- **Duplicate Execution Risk**: **EXISTS**. If the handler performed partial external or non-transactional side effects, those operations will be replayed.
- **Architectural Requirement**: All Razorpay event handlers implemented in Step 5.2D.3 **MUST BE STRICTLY IDEMPOTENT** at the entity level (using idempotency keys and unique database constraints).

### Scenario 3: Crash after handler completion but before outcome recording
- **State**: Handler finishes and commits domain mutations (e.g., subscription activation). Worker crashes before executing `record_razorpay_webhook_outcome()`.
- **Behavior**: The webhook event status remains `pending`/`failed`. Upon lease expiration, Worker 2 claims the event and executes the handler again.
- **Duplicate Execution Risk**: **EXISTS**.
- **Mitigation**: Entity-level idempotency ensures that the second handler execution recognizes the entity is already in the target state (or detects existing invoice/payment records), succeeds as a no-op, and Worker 2 successfully records `status = 'processed'`.

### Scenario 4: Lease expires during handler execution (Slow Worker)
- **State**: Worker 1 takes 80 seconds on a 60-second lease. At second 61, Worker 2 claims the event, assigning `UUID_2`.
- **Behavior**: At second 80, Worker 1 invokes `record_razorpay_webhook_outcome(claimed_with_UUID_1)`.
- **Outcome**: The fencing check detects `event.lease_token (UUID_2) != claimed.lease_token (UUID_1)`.
- **Verdict**: **SAFE**. Fencing catches the stale lease, rolls back, raises `StaleLeaseError`, and discards Worker 1's outcome. Worker 1 cannot overwrite Worker 2's active lease.

### Scenario 5: Worker A awakens after Worker B has completed the event
- **State**: Worker A experiences a 90-second network partition or GC pause. Worker B claimed the event, executed the handler, and recorded `status = 'processed'` (clearing lease fields to `None`).
- **Behavior**: Worker A awakens and calls `record_razorpay_webhook_outcome()`.
- **Outcome**: Worker A inspects DB: `event.lease_token is None`, `event.locked_by is None`. Fencing rejects the request with `StaleLeaseError`.
- **Verdict**: **SAFE**. Worker A cannot overwrite Worker B's `processed` status or regress attempts.

### Scenario 6: Database connection failure during outcome recording
- **State**: Handler succeeds, but database connection drops while attempting `record_razorpay_webhook_outcome()`.
- **Behavior**: `record_razorpay_webhook_outcome()` raises an exception; transaction rolls back automatically. The worker catches the error and logs it. The event remains in DB until its lease expires, after which it is retried.
- **Verdict**: **SAFE**. Fail-closed transaction boundary protects against half-committed states.

### Scenario 7: Worker graceful shutdown while an event is being processed
- **State**: Worker process receives SIGINT/SIGTERM (`worker.stop()` sets `_stop_event`).
- **Behavior**: In `razorpay_webhook_worker.py` (lines 245–253):
  ```python
  for _ in range(limit):
      if self._stop_event.is_set():
          break
      result = await self.process_one()
  ```
  The stop check occurs between iterations. An in-flight event completes `process_one()`, records its outcome, and commits before the worker loop terminates.
- **Verdict**: **SAFE**. Clean draining without orphaned in-flight events.

---

## 5. Section D: Retry & Quarantine Correctness

### 5.1 Progression and State Transitions
- **Initial Ingestion**: Initialized with `attempts = 0` (enforced by Step 5.2D.1.1).
- **Execution Failures**:
  - `attempts` is only incremented when an execution outcome is recorded (`RETRYABLE_FAILURE`, `PERMANENT_FAILURE`, or `UNKNOWN_EVENT`).
  - Lease expiration reclamation does **NOT** increment `attempts`.
- **Backoff Schedule**:
  In `webhook_engine.py` (lines 90–97):
  ```python
  def get_retry_backoff_seconds(attempts: int) -> int:
      schedule = [5, 15, 45, 120]
      idx = max(0, min(attempts - 1, len(schedule) - 1))
      return schedule[idx]
  ```

| Failed Attempt | Status | `attempts` | Delay | `next_retry_at` |
| :---: | :---: | :---: | :---: | :---: |
| Ingested | `pending` | 0 | 0s | `None` |
| 1st Failure | `failed` | 1 | 5s | `now + 5s` |
| 2nd Failure | `failed` | 2 | 15s | `now + 15s` |
| 3rd Failure | `failed` | 3 | 45s | `now + 45s` |
| 4th Failure | `failed` | 4 | 120s | `now + 120s` |
| 5th Failure | `quarantined` | 5 | N/A | `None` (`quarantined_at = now`) |

### 5.2 Quarantine Invariants
When `event.attempts >= max_attempts` (lines 408–416):
- `event.status = WebhookProcessingStatus.quarantined`
- `event.quarantined_at = now_utc`
- `event.quarantine_reason = f"Quarantined: exceeded max attempts ({event.attempts}/{max_attempts})"`
- `event.next_retry_at = None`
- **Terminal State**: Because the claim query requires `status IN ('pending', 'failed') AND attempts < max_attempts`, quarantined events are permanently excluded from automated polling.

---

## 6. Section E: Worker Lifecycle & CLI Invocation Defect

### 6.1 Polling Loop and Multi-Process Scalability
- `RazorpayWebhookWorker.start()` (lines 257–284) implements an interruptible polling loop.
- When no events are available, the worker waits up to `poll_interval` seconds (`self._stop_event.wait(timeout=self.poll_interval)`).
- Multiple worker processes can run independently across containers or nodes. Because locking is handled via `FOR UPDATE SKIP LOCKED` and fencing tokens in PostgreSQL, workers require no distributed lock manager (e.g., Redis).

### 6.2 Identified Defect: Missing CLI Entrypoint
In `apps/api/app/workers/razorpay_webhook_worker.py`:
- Lines 21, 27, 28, 29 import `argparse`, `signal`, and `sys`.
- However, **lines 285–288 end the file without defining `main()` or `if __name__ == "__main__":`**.
- Running:
  ```bash
  python -m app.workers.razorpay_webhook_worker
  ```
  imports the module and immediately terminates with exit code 0 without executing any polling loop.
- **Reference Implementation**: `apps/api/app/workers/stripe_webhook_worker.py` (lines 287–364) defines `setup_signal_handlers()`, `run_worker()`, `main()`, and the `__main__` entrypoint with flags:
  - `--poll-interval`
  - `--batch-size`
  - `--max-attempts`
  - `--once`
  - `--log-level`

> [!WARNING]
> **Action Required Prior to Deployment**: `razorpay_webhook_worker.py` must be updated with a standard CLI entrypoint matching `stripe_webhook_worker.py` so container orchestrators (e.g. systemd, Docker, Kubernetes) can execute it directly.

---

## 7. Section F: PostgreSQL Test Evidence Classification

### 7.1 Test Suite Categorization
In `apps/api/tests/test_razorpay_webhook_worker.py`:

```
+---------------------------------------------------------------------------------------------------+
| Test Suite Architecture                                                                           |
+---------------------------------------------------------------------------------------------------+
  Tests 1-14: In-Memory Mocked Async Sessions (SharedRazorpayEventRepository)
  - Validates: Logic, state transitions, backoff math, quarantine thresholds, fencing exceptions.
  - Limitations: Uses Python dictionary and sets; does not exercise PostgreSQL Lock Manager.
  -------------------------------------------------------------------------------------------------
  Test 15: Offline Alembic Migration DDL Generation (--sql)
  - Validates: PostgreSQL SQL grammar, table schema, indexes, RLS commands, downgrade guard.
  - Limitations: Validates dialect text generation; does not execute against live PostgreSQL server.
+---------------------------------------------------------------------------------------------------+
```

1. **Tests 1–14 (In-Memory Concurrency Test Doubles)**:
   - Uses `SharedRazorpayEventRepository` and `RazorpayWorkerMockDbSession`.
   - Simulates `SKIP LOCKED` using `repo.locked_ids` and transaction rollback on `__aexit__`.
   - **Limitation**: While this proves that `webhook_engine.py` algorithms correctly branch and handle concurrency semantics, it does not validate real PostgreSQL kernel lock acquisition, connection drops, or deadlock avoidance under high-concurrency multi-process workloads.
2. **Test 15 (Offline Alembic Migration Verification)**:
   - Runs `alembic upgrade c1d2e3f4a5b7:d1e2f3a4b5c6 --sql` and `alembic downgrade d1e2f3a4b5c6:c1d2e3f4a5b7 --sql`.
   - Confirms that valid PostgreSQL SQL is produced for columns, indices, and RLS policies.
   - **Limitation**: Does not connect to a running PostgreSQL database.

### 7.2 Recommendations for Pipeline Testing
In future staging or CI environments with live database access, a live integration test should run 4 concurrent worker processes against a shared PostgreSQL database processing 100 simultaneous events to verify lock contention under real I/O.

---

## 8. Section G: Alembic Migration Review

### 8.1 Head & Lineage Verification
- **Current Head**: `d1e2f3a4b5c6 (head)`
- **Parent Revision**: `c1d2e3f4a5b7`
- Verified via `alembic heads`: Exactly one head exists. No split heads or branches.

### 8.2 Schema & Model Consistency
Migration `20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py` matches `BillingWebhookEvent` in `apps/api/app/models/payment.py`:

| Column | Migration DDL | SQLAlchemy Model | Parity |
| :--- | :--- | :--- | :---: |
| `lease_token` | `sa.UUID(), nullable=True` | `UUID(as_uuid=True), nullable=True` | **100% Match** |
| `lease_expires_at` | `sa.DateTime(timezone=True), nullable=True` | `DateTime(timezone=True), nullable=True` | **100% Match** |
| `locked_by` | `sa.String(length=64), nullable=True` | `String(64), nullable=True` | **100% Match** |

### 8.3 Claim Index and Partial Predicate
- **Index Name**: `ix_billing_webhook_events_claim`
- **Columns**: `['provider', 'status', 'next_retry_at', 'lease_expires_at', 'received_at']`
- **Partial Predicate**: `postgresql_where=sa.text("status IN ('pending', 'failed') AND signature_verified = true")`
- **Optimization**: Because processed and quarantined events are excluded by the partial predicate, the index remains small and fast even after millions of webhook events are ingested.

### 8.4 RLS and Safe Downgrade Guard
- **Security**: Upgrade explicitly re-enables RLS and revokes permissions:
  ```sql
  ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY;
  REVOKE ALL ON billing_webhook_events FROM anon, authenticated;
  ```
- **Downgrade Safety Guard**:
  ```python
  if not context.is_offline_mode():
      conn = op.get_bind()
      active_count = conn.execute(sa.text("""
          SELECT count(*) FROM billing_webhook_events
          WHERE lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at > now()
      """)).scalar()
      if active_count and active_count > 0:
          raise RuntimeError(f"Alembic Downgrade Aborted: {active_count} active unexpired webhook lease(s) exist.")
  ```
  Prevents dropping lease columns while workers actively process events in production.

---

## 9. Conditions & Recommended Next Steps

### Conditions for Step 5.2D.2.2 / Step 5.2D.3
1. **Implement CLI Entrypoint in `razorpay_webhook_worker.py`**:
   - Add `setup_signal_handlers(worker: RazorpayWebhookWorker)`.
   - Add `async def run_worker(...) -> int`.
   - Add `def main(argv: list[str] | None = None) -> None` with standard CLI argument parsing.
   - Add `if __name__ == "__main__": main()`.
2. **Enforce Idempotency in Step 5.2D.3 Handlers**:
   - Handlers for `payment.captured`, `subscription.authenticated`, `subscription.activated`, `subscription.charged`, `subscription.halted`, and `subscription.cancelled` must use database unique constraints, UPSERT semantics, and entity status guards to guarantee safe replays under crash scenarios.
3. **Staging Multi-Process Validation**:
   - Execute a multi-process concurrency test against live PostgreSQL once staging infrastructure is available.

---

## 10. Audit Conclusion

The Step 5.2D.2 implementation is **APPROVED (PASS WITH CONDITIONS)**. The underlying lease ownership engine, mathematical backoff schedule, fencing token validation, atomic database transaction boundaries, and Alembic migration are verified correct and ready for the implementation of domain event handlers.
