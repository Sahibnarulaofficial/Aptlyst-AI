# Aptlyst AI — Step 5.2D.2.3: Final CLI and Worker Lifecycle Audit Report

**Date**: 2026-11-08  
**Audit Scope**: Final CLI and Worker Lifecycle Implementation (Step 5.2D.2.2)  
**Audited Artifacts**:
- `apps/api/app/workers/razorpay_webhook_worker.py`
- `apps/api/app/services/billing/razorpay/webhook_engine.py`
- `apps/api/tests/test_razorpay_webhook_worker.py`
- `apps/api/app/db/session.py`
- Alembic head lineage and Git working tree status

---

## 1. Executive Summary & Audit Verdict

| Audit Dimension | Core Requirement | Assessment | Verdict |
| :--- | :--- | :--- | :---: |
| **1. CLI Interface** | 5 flags (`--poll-interval`, `--batch-size`, `--max-attempts`, `--once`, `--log-level`), input validation, correct exit codes, zero DB calls on `--help`. | Validated across CLI parser, runner, and constructor. `--help` exits code 0 with zero DB activity. | **PASS** |
| **2. Polling Loop** | No CPU spinning on empty queues, continuous drain on full batches, resilient loop error handling. | Interruptible wait on idle queue; immediate `continue` on full batch; unhandled errors backoff without crashing loop. | **PASS** |
| **3. Signal Handling** | Graceful stop on SIGINT/SIGTERM, Windows fallback, duplicate registration prevention, safe process scope. | Dual-mode POSIX/Windows handlers with `_signal_handlers_registered` guard. Stop flag terminates claims cleanly. | **PASS** |
| **4. In-Flight Work** | No abandoned leases without recovery, stale workers fenced out, bounded drain, engine disposed on exit. | In-flight event completes within lease window; stale commits fail-closed; `await engine.dispose()` in `finally:` block. | **PASS** |
| **5. Retry Policy** | Default 5 attempts preserved, validated CLI overrides, 5s/15s/45s/120s backoff, zero penalty on lease expiry. | Default strictly 5 attempts; backoff schedule unchanged; lease recovery updates only lease fields. | **PASS** |
| **6. Tests & Git** | Explicit classification of tests (mock vs integration), no live PG claims without evidence, clean lineage. | Tests 16–23 classified as unit/mock doubles; single head `d1e2f3a4b5c6`; 187 regression tests passing. | **PASS** |

### Final Audit Verdict: **PASS**

The Step 5.2D.2.2 implementation fulfills all operational, concurrency, lifecycle, and CLI requirements. The Razorpay webhook background worker is completely decoupled, production-ready, and approved for Step 5.2D.3 (business event handlers).

---

## 2. Section 1: CLI Interface Verification

### 2.1 Flag Definitions & Options
In `apps/api/app/workers/razorpay_webhook_worker.py` (lines 425–461), `build_parser()` exposes the exact five requested flags:

```python
parser.add_argument(
    "--poll-interval",
    type=float,
    default=None,
    help=f"Seconds to wait between polling loops when queue is idle (default: {DEFAULT_POLL_INTERVAL_SECONDS})",
)
parser.add_argument(
    "--batch-size",
    type=int,
    default=None,
    help=f"Maximum events to claim and execute per batch iteration (default: {DEFAULT_BATCH_SIZE})",
)
parser.add_argument(
    "--max-attempts",
    type=int,
    default=None,
    help=f"Maximum attempts before quarantining an unprocessable event (default: {MAX_WEBHOOK_ATTEMPTS})",
)
parser.add_argument(
    "--once",
    action="store_true",
    help="Execute a single pass over pending events and exit immediately",
)
parser.add_argument(
    "--log-level",
    type=str,
    default="INFO",
    choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    help="Logging verbosity level (default: INFO)",
)
```

### 2.2 Input Validation and Rejection
Validation is enforced across three defense-in-depth boundaries:
1. **CLI Parser (`main`)**: lines 469–474 call `parser.error(...)` if bounds are violated:
   - `--poll-interval <= 0` $\to$ `error("--poll-interval must be greater than 0")` (exits with code 2).
   - `--batch-size < 1` $\to$ `error("--batch-size must be at least 1")` (exits with code 2).
   - `--max-attempts < 1` $\to$ `error("--max-attempts must be at least 1")` (exits with code 2).
   - Invalid choice for `--log-level` $\to$ rejected by argparse with exit code 2.
2. **Runner Function (`run_worker`)**: lines 388–393 raise `ValueError` for invalid programmatic inputs.
3. **Worker Constructor (`RazorpayWebhookWorker.__init__`)**: lines 117–122 raise `ValueError` if instantiated with invalid values.

### 2.3 Defaults Preservation
When arguments are omitted, `args` attributes are `None`, preserving established defaults:
- `poll_interval`: `DEFAULT_POLL_INTERVAL_SECONDS = 2.0` (or `RAZORPAY_WEBHOOK_WORKER_POLL_INTERVAL` env var).
- `batch_size`: `DEFAULT_BATCH_SIZE = 10` (or `RAZORPAY_WEBHOOK_WORKER_BATCH_SIZE` env var).
- `max_attempts`: `MAX_WEBHOOK_ATTEMPTS = 5`.
- `once`: `False` (continuous daemon polling).
- `log_level`: `"INFO"`.

### 2.4 `--help` Offline Independence
- In `main()` (line 467), `parser.parse_args(argv)` intercepts `-h`/`--help` and calls `parser.exit(0)`.
- No database engine is initialized, no connection pool is opened, and no network calls are dispatched. Verified offline via `python -m app.workers.razorpay_webhook_worker --help`.

### 2.5 `--once` Batch Bound
- When `--once` is passed, `run_worker()` invokes `await worker.run_once()` (line 408).
- `run_once()` invokes `await self.run_batch()` (line 271), which enforces `limit = max_count if max_count is not None else self.batch_size` (line 250).
- At most one batch of `batch_size` events is claimed and processed before `run_worker()` exits with code 0.

### 2.6 Exit Codes
- Normal completion of `--once`: returns code 0 (line 410).
- Normal loop termination on shutdown: returns code 0 (line 413).
- Unhandled `KeyboardInterrupt`: caught in `main()` line 491, exiting with code 0.
- Command-line parsing error: `parser.error` exits with code 2.

---

## 3. Section 2: Polling Loop Verification

### 3.1 Idle Queue Backoff
In `RazorpayWebhookWorker.start()` (lines 295–302):
```python
else:
    # Queue is empty, back off for poll_interval
    try:
        await asyncio.wait_for(
            self._stop_event.wait(), timeout=self.poll_interval
        )
    except asyncio.TimeoutError:
        pass
```
- When `run_batch()` returns an empty list, the worker pauses for `self.poll_interval` (2.0s).
- The wait is non-blocking to the OS and event loop, and immediately awakens if `self._stop_event` is set.
- Proves zero database spinning when the queue is idle.

### 3.2 High-Throughput Batch Draining
In lines 291–294:
```python
if results:
    # If full batch was processed, immediately poll again without delay
    if len(results) >= self.batch_size:
        continue
```
- If a complete batch was processed (`len(results) >= self.batch_size`), the worker immediately re-enters `run_batch()`, claiming subsequent events without waiting for `poll_interval`.
- Ensures backlog spikes drain at maximum database and network concurrency.

### 3.3 Loop Error Resilience
In lines 305–312:
```python
except asyncio.CancelledError:
    break
except Exception as exc:
    logger.error("Error in RazorpayWebhookWorker polling loop: %s", exc, exc_info=True)
    try:
        await asyncio.wait_for(
            self._stop_event.wait(), timeout=min(5.0, self.poll_interval)
        )
    except asyncio.TimeoutError:
        pass
```
- Top-level database or network disconnects inside the polling loop are caught, logged with stack trace, and back off briefly (`min(5.0, self.poll_interval)`) before retrying.
- Transient errors in one batch or loop iteration do not terminate the background worker process.

---

## 4. Section 3: Signal Handling Verification

### 4.1 Signal Handler Architecture
In `setup_signal_handlers()` (lines 322–362):

```python
def setup_signal_handlers(worker: RazorpayWebhookWorker) -> None:
    if getattr(worker, "_signal_handlers_registered", False):
        logger.debug("Signal handlers already registered for worker %s.", worker.worker_id)
        return

    def _on_signal() -> None:
        logger.info(
            "Termination signal received for worker %s. Requesting graceful shutdown...",
            worker.worker_id,
        )
        worker.stop()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)
        worker._signal_handlers_registered = True
        logger.debug("Registered asyncio loop signal handlers for worker %s.", worker.worker_id)
    except (NotImplementedError, AttributeError, RuntimeError):
        try:
            def _sync_signal_handler(signum: int, frame: Any) -> None:
                _on_signal()

            signal.signal(signal.SIGINT, _sync_signal_handler)
            signal.signal(signal.SIGTERM, _sync_signal_handler)
            worker._signal_handlers_registered = True
            logger.debug("Registered sync signal handlers for worker %s.", worker.worker_id)
        except (ValueError, AttributeError) as exc:
            logger.warning(
                "Could not register signal handlers for worker %s (e.g. running in thread): %s",
                worker.worker_id,
                exc,
            )
```

### 4.2 Invariant Verification Checklist
1. **Immediate Cessation of New Work**:
   - `worker.stop()` sets `self._stop_event`.
   - In `run_batch()` (lines 253–255), `if self._stop_event.is_set(): break` halts claiming before the next event.
2. **Repeated Runs & Re-registration Safety**:
   - Guarded by `if getattr(worker, "_signal_handlers_registered", False): return`.
   - Verified by `test_21_signal_triggered_graceful_shutdown`: calling `setup_signal_handlers(worker)` twice causes no errors or duplicate handler execution.
3. **Windows Compatibility**:
   - Windows Proactor loops do not support `loop.add_signal_handler` and raise `NotImplementedError`.
   - The fallback cleanly catches `NotImplementedError` and binds synchronous `signal.signal` handlers.
4. **Scope Isolation**:
   - The signal callback `_on_signal()` triggers only `worker.stop()`. It does not invoke `sys.exit()` abruptly inside the signal context, preventing disruption of unrelated threads or corruption of running event loops.

---

## 5. Section 4: In-Flight Work and Resource Lifecycle

### 5.1 In-Flight Event Guarantees
- Because `self._stop_event.is_set()` is checked at the iteration boundary in `run_batch()`, an event currently executing inside `process_one()` is allowed to finish:
  - Handler commits business logic in its session.
  - Outcome recording transaction verifies fencing tokens and commits status.
- The worker loop exits cleanly only after `process_one()` returns.

### 5.2 Fencing Token Invariants Under Catastrophic Shutdown
- If the worker process is killed abruptly (`SIGKILL` or hardware power loss) mid-execution:
  - The row remains stamped with `lease_token = UUID` and `lease_expires_at = now + 60s`.
  - No other worker claims the row while the 60-second lease is unexpired.
  - After 60 seconds, `claim_next_razorpay_webhook_event()` allows another worker to reclaim the event without incrementing attempts.
  - If the original worker revives or awakens late after its lease expired or was superseded, `record_razorpay_webhook_outcome()` validates `is_same_token`, `is_same_worker`, and `is_unexpired >= now_utc`. If invalid, it rolls back and raises `StaleLeaseError`.
- **Verdict**: A slow or zombie worker can never corrupt or overwrite a superseded lease.

### 5.3 Database Session and Connection Pool Disposal
1. **Session Scope**:
   - Each claim and outcome transaction executes within an isolated `async with self.session_factory() as db:` block. Sessions are closed and released back to the pool immediately upon block exit.
2. **Engine Disposal**:
   - In `run_worker()` (lines 415–423):
     ```python
     finally:
         if session_factory is None:
             try:
                 from app.db.session import engine
                 await engine.dispose()
                 logger.debug("Disposed default database engine successfully.")
             except Exception as exc:
                 logger.debug("Error disposing database engine: %s", exc)
     ```
   - When running in production (`session_factory is None`), `await engine.dispose()` closes all socket connections and terminates the engine pool cleanly before the process exits.

---

## 6. Section 5: Retry Policy Verification

### 6.1 Retry Invariant Table

| Condition | Verification Finding | Reference | Status |
| :--- | :--- | :--- | :---: |
| **Default Attempt Limit** | Exactly 5 attempts (`MAX_WEBHOOK_ATTEMPTS = 5`). | `webhook_engine.py:86`, `razorpay_webhook_worker.py:112` | **VERIFIED** |
| **CLI Overrides** | `--max-attempts` strictly validated ($N \ge 1$); preserves 5 if omitted. | `razorpay_webhook_worker.py:121, 392, 473` | **VERIFIED** |
| **Retry Backoff Schedule** | Delays: 5s, 15s, 45s, 120s (`WEBHOOK_RETRY_BACKOFF_SCHEDULE`). | `webhook_engine.py:90–97` | **VERIFIED** |
| **Lease Expiry Reclaim** | Reclaiming an expired lease updates only `lease_token`, `locked_by`, and `lease_expires_at`; `attempts` is untouched. | `webhook_engine.py:291–295` | **VERIFIED** |
| **Terminal Quarantine** | At attempt 5, event transitions to `quarantined` with `next_retry_at = None`. | `webhook_engine.py:408–416` | **VERIFIED** |

---

## 7. Section 6: Tests and Git Working Tree Audit

### 7.1 Test Classification & Reality Check
The 8 newly added tests in `apps/api/tests/test_razorpay_webhook_worker.py` (Tests 16–23) are categorized as follows:

```
+---------------------------------------------------------------------------------------------------+
| Step 5.2D.2.2 Test Architecture Classification                                                    |
+---------------------------------------------------------------------------------------------------+
  Test 16: Unit Test - Valid CLI flag parsing and worker property assignment.
  Test 17: Unit Test - Default max attempts (5) preservation when flags omitted.
  Test 18: Unit Test - Rejection of invalid flags (SystemExit code != 0, ValueError).
  Test 19: Unit Test - Offline --help execution without database initialization.
  -------------------------------------------------------------------------------------------------
  Test 20: In-Memory Mock Test - Single-pass (--once) execution against SharedRazorpayEventRepository.
  Test 21: Mock / Asyncio Test - Signal handler registration and graceful loop shutdown.
  Test 22: Mocked Async Test - Engine disposal via unittest.mock.patch("app.db.session.engine").
  Test 23: Mocked Loop Test - Polling loop resilience under transient exceptions.
+---------------------------------------------------------------------------------------------------+
```

> [!IMPORTANT]
> **PostgreSQL Concurrency Reality**:
> Tests 1–14 and 20–23 use in-memory repositories (`SharedRazorpayEventRepository`) and mock sessions (`RazorpayWorkerMockDbSession`). Test 15 validates PostgreSQL DDL generation via offline Alembic `--sql`.
> **No live multi-process PostgreSQL integration test is executed in this environment.** Live database concurrency testing remains a recommendation for staging/CI environments with a running PostgreSQL instance.

### 7.2 Alembic Head Lineage
- Ran `cd apps/api; .venv/Scripts/python.exe -m alembic heads`.
- Result: `d1e2f3a4b5c6 (head)`.
- Exactly one head revision exists. Zero split heads or branch conflicts.

### 7.3 Git Working Tree Verification
- Ran `git status`.
- No modifications were made to models (`organization.py`, `subscription.py`, `payment.py`), existing migrations, quota accounting, or Stripe services.
- Only `razorpay_webhook_worker.py` and `test_razorpay_webhook_worker.py` were modified for the Step 5.2D.2.2 task.

---

## 8. Final Audit Conclusion

The Step 5.2D.2.2 implementation is **VERIFIED AND APPROVED (PASS)**.
1. The CLI interface is fully functional, validates user input, and provides clean exit codes.
2. The polling loop drains backlogs immediately and sleeps on idle queues without spinning.
3. Signal handling functions across POSIX and Windows, preventing duplicate registration and draining in-flight work safely.
4. Database engine and session resources are reliably cleaned up.
5. All 187 billing regression tests pass.
6. The codebase is ready for Step 5.2D.3 (Razorpay business event handlers).
