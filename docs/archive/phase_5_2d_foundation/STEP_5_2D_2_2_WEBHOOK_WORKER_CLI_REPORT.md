# Aptlyst AI — Step 5.2D.2.2: Razorpay Webhook Worker CLI Completion Report

**Date**: 2026-11-08  
**Scope**: Razorpay Webhook Worker CLI Entry Point & Lifecycle Completion  
**Target File**: `apps/api/app/workers/razorpay_webhook_worker.py`  
**Test File**: `apps/api/tests/test_razorpay_webhook_worker.py`  

---

## 1. Executive Summary

In Step 5.2D.2, the durable database-backed lease ownership and webhook processing worker was implemented. Step 5.2D.2.1 completed a read-only audit which yielded `PASS WITH CONDITIONS`, identifying an operational defect: `apps/api/app/workers/razorpay_webhook_worker.py` omitted a functional CLI entry point (`main()` and `if __name__ == "__main__":`), preventing direct containerized execution.

Step 5.2D.2.2 completes the CLI and lifecycle management for `razorpay_webhook_worker.py` without modifying any underlying lease, claim, retry, backoff, or event-processing algorithms.

### Summary of Completed Objectives
- Implemented `build_parser()` and `main(argv)`.
- Added support for `--poll-interval`, `--batch-size`, `--max-attempts`, `--once`, and `--log-level`.
- Implemented strict argument validation rejecting non-positive intervals, batch sizes $< 1$, and attempts $< 1$.
- Preserved the default 5-attempt retry policy (`MAX_WEBHOOK_ATTEMPTS = 5`).
- Implemented cross-platform signal handlers (`setup_signal_handlers()`) supporting POSIX event loops and Windows fallbacks.
- Added duplicate signal-handler registration protection (`_signal_handlers_registered`).
- Ensured graceful termination where in-flight events complete within their lease window while rejecting new claims.
- Added resource cleanup disposing the SQLAlchemy async database engine in `finally:` blocks.
- Provided `if __name__ == "__main__": main()` enabling `python -m app.workers.razorpay_webhook_worker`.
- Verified `--help` executes offline without opening database or network connections.
- Added 8 new automated tests (23 worker tests total); 187 regression tests passing across the entire billing suite.

---

## 2. Implementation Details

### 2.1 CLI Argument Parser (`build_parser()`)
Constructed using `argparse.ArgumentParser` with explicit flags matching established worker conventions:

```python
def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for the Razorpay webhook worker."""
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.razorpay_webhook_worker",
        description="Aptlyst AI — Durable Razorpay Webhook Processing Worker",
    )
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
    return parser
```

### 2.2 CLI Entrypoint & Validation (`main()`)
In `main(argv)`:
- Validates bounds: `--poll-interval > 0`, `--batch-size >= 1`, `--max-attempts >= 1`.
- Calls `parser.error(...)` if validation fails, printing usage and exiting with code 2.
- Configures `logging.basicConfig()` based on `--log-level`.
- Runs `asyncio.run(run_worker(...))` and intercepts `KeyboardInterrupt` to exit cleanly with code 0.

```python
def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for running the Razorpay webhook worker."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.poll_interval is not None and args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.max_attempts is not None and args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        exit_code = asyncio.run(
            run_worker(
                poll_interval=args.poll_interval,
                batch_size=args.batch_size,
                max_attempts=args.max_attempts,
                run_once_only=args.once,
            )
        )
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
```

### 2.3 Cross-Platform Signal Handling (`setup_signal_handlers()`)
Handles SIGINT and SIGTERM gracefully while avoiding duplicate registration:

```python
def setup_signal_handlers(worker: RazorpayWebhookWorker) -> None:
    """Register signal handlers for graceful shutdown across POSIX and Windows environments.

    Guarantees:
    - Avoids duplicate signal-handler registration on the worker.
    - Gracefully stops accepting new work upon SIGINT/SIGTERM while allowing in-flight events to finish.
    - Handles POSIX event loop signal handlers and Windows sync signal handlers.
    """
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
        # Fallback for Windows or environments where add_signal_handler is unsupported
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

### 2.4 Worker Lifecycle Coordinator (`run_worker()`)
Coordinates execution, programmatic argument validation, single-pass `--once` execution, and connection pool cleanup:

```python
async def run_worker(
    poll_interval: float | None = None,
    batch_size: int | None = None,
    max_attempts: int | None = None,
    run_once_only: bool = False,
    session_factory: Callable[[], AsyncSession] | None = None,
    dispatcher: RazorpayWebhookDispatcher | None = None,
    environment: str | None = None,
) -> int:
    """Run the Razorpay webhook worker as an async task with signal interception.

    Args:
        poll_interval: Seconds between polls when queue is idle.
        batch_size: Maximum events claimed and executed per batch iteration.
        max_attempts: Maximum retry attempts before quarantining an unprocessable event.
        run_once_only: If True, executes a single batch pass and returns.
        session_factory: Optional custom AsyncSession factory (used for testing).
        dispatcher: Optional custom dispatcher instance.
        environment: Optional environment filter ("test" or "live").

    Returns:
        Exit code (0 on clean shutdown).
    """
    if poll_interval is not None and poll_interval <= 0:
        raise ValueError(f"poll_interval must be greater than 0, got {poll_interval}")
    if batch_size is not None and batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    if max_attempts is not None and max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

    worker = RazorpayWebhookWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        poll_interval=poll_interval,
        batch_size=batch_size,
        max_attempts=max_attempts,
        environment=environment,
    )

    setup_signal_handlers(worker)

    try:
        if run_once_only:
            count = await worker.run_once()
            logger.info("Single batch pass completed: %d events processed.", count)
            return 0

        await worker.start()
        return 0
    finally:
        if session_factory is None:
            try:
                from app.db.session import engine

                await engine.dispose()
                logger.debug("Disposed default database engine successfully.")
            except Exception as exc:
                logger.debug("Error disposing database engine: %s", exc)
```

### 2.5 `RazorpayWebhookWorker` Enhancements
1. **Added `run_once()`**:
   ```python
   async def run_once(self) -> int:
       """Process currently available eligible events in a single pass and return count."""
       results = await self.run_batch()
       return len(results)
   ```
2. **Added `run_forever()`**:
   ```python
   async def run_forever(self) -> None:
       """Continuously poll and process events until stop() is invoked."""
       await self.start()
   ```
3. **Constructor Parameter Validation**:
   ```python
   if self.poll_interval <= 0:
       raise ValueError(f"poll_interval must be greater than 0, got {self.poll_interval}")
   if self.batch_size < 1:
       raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
   if self.max_attempts < 1:
       raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")
   ```
4. **Immediate Continuous Polling for Full Batches**:
   In `start()`:
   ```python
   results = await self.run_batch()
   if results:
       if len(results) >= self.batch_size:
           continue  # Queue has pending events; immediately claim next batch
   ```

---

## 3. CLI Usage & Verification

### 3.1 Help Output (`--help`)
```bash
cd apps/api
python -m app.workers.razorpay_webhook_worker --help
```
Output:
```
usage: python -m app.workers.razorpay_webhook_worker [-h]
                                                     [--poll-interval POLL_INTERVAL]
                                                     [--batch-size BATCH_SIZE]
                                                     [--max-attempts MAX_ATTEMPTS]
                                                     [--once]
                                                     [--log-level {DEBUG,INFO,WARNING,ERROR}]

Aptlyst AI — Durable Razorpay Webhook Processing Worker

options:
  -h, --help            show this help message and exit
  --poll-interval POLL_INTERVAL
                        Seconds to wait between polling loops when queue is
                        idle (default: 2.0)
  --batch-size BATCH_SIZE
                        Maximum events to claim and execute per batch
                        iteration (default: 10)
  --max-attempts MAX_ATTEMPTS
                        Maximum attempts before quarantining an unprocessable
                        event (default: 5)
  --once                Execute a single pass over pending events and exit
                        immediately
  --log-level {DEBUG,INFO,WARNING,ERROR}
                        Logging verbosity level (default: INFO)
```

### 3.2 Daemon Execution
```bash
# Standard background polling daemon
python -m app.workers.razorpay_webhook_worker

# Custom poll interval and batch size with DEBUG logging
python -m app.workers.razorpay_webhook_worker --poll-interval 1.0 --batch-size 20 --log-level DEBUG
```

### 3.3 Single Batch Execution (`--once`)
```bash
# Processes up to 10 events (or batch size) and exits immediately with code 0
python -m app.workers.razorpay_webhook_worker --once
```

### 3.4 Invalid Argument Rejection Examples
- **Zero / Negative Poll Interval**:
  ```bash
  python -m app.workers.razorpay_webhook_worker --poll-interval -1
  # Output: python -m app.workers.razorpay_webhook_worker: error: --poll-interval must be greater than 0
  ```
- **Zero / Negative Batch Size**:
  ```bash
  python -m app.workers.razorpay_webhook_worker --batch-size 0
  # Output: python -m app.workers.razorpay_webhook_worker: error: --batch-size must be at least 1
  ```
- **Zero / Negative Max Attempts**:
  ```bash
  python -m app.workers.razorpay_webhook_worker --max-attempts 0
  # Output: python -m app.workers.razorpay_webhook_worker: error: --max-attempts must be at least 1
  ```
- **Invalid Log Level**:
  ```bash
  python -m app.workers.razorpay_webhook_worker --log-level FOO
  # Output: python -m app.workers.razorpay_webhook_worker: error: argument --log-level: invalid choice: 'FOO'
  ```

---

## 4. Automated Test Results

### 4.1 Worker Test Suite (`tests/test_razorpay_webhook_worker.py`)
All 23 unit, concurrency, and CLI lifecycle tests passed:

```
tests/test_razorpay_webhook_worker.py::test_1_successful_claim_and_lease_assignment PASSED
tests/test_razorpay_webhook_worker.py::test_2_two_workers_competing_for_same_event PASSED
tests/test_razorpay_webhook_worker.py::test_3_different_workers_claiming_different_events PASSED
tests/test_razorpay_webhook_worker.py::test_4_unexpired_lease_cannot_be_stolen PASSED
tests/test_razorpay_webhook_worker.py::test_5_expired_lease_can_be_recovered PASSED
tests/test_razorpay_webhook_worker.py::test_6_stale_worker_cannot_complete_after_lease_replacement PASSED
tests/test_razorpay_webhook_worker.py::test_7_stale_worker_cannot_increment_attempts_or_quarantine PASSED
tests/test_razorpay_webhook_worker.py::test_8_valid_owner_can_mark_event_processed PASSED
tests/test_razorpay_webhook_worker.py::test_9_valid_owner_can_schedule_retry PASSED
tests/test_razorpay_webhook_worker.py::test_10_fifth_failed_attempt_quarantines PASSED
tests/test_razorpay_webhook_worker.py::test_11_lease_recovery_does_not_increment_attempts PASSED
tests/test_razorpay_webhook_worker.py::test_12_worker_crash_and_recovery_end_to_end PASSED
tests/test_razorpay_webhook_worker.py::test_13_lease_fields_are_cleared_on_completion PASSED
tests/test_razorpay_webhook_worker.py::test_14_provider_account_environment_scoping_intact PASSED
tests/test_razorpay_webhook_worker.py::test_15_offline_alembic_ddl_and_metadata PASSED
tests/test_razorpay_webhook_worker.py::test_16_cli_argument_parsing_and_worker_propagation PASSED
tests/test_razorpay_webhook_worker.py::test_17_cli_default_maximum_attempts_remains_five PASSED
tests/test_razorpay_webhook_worker.py::test_18_cli_invalid_argument_rejection PASSED
tests/test_razorpay_webhook_worker.py::test_19_cli_help_flag_no_connections PASSED
tests/test_razorpay_webhook_worker.py::test_20_worker_run_once_execution PASSED
tests/test_razorpay_webhook_worker.py::test_21_signal_triggered_graceful_shutdown PASSED
tests/test_razorpay_webhook_worker.py::test_22_worker_startup_and_cleanup_engine_disposal PASSED
tests/test_razorpay_webhook_worker.py::test_23_worker_polling_loop_tolerates_transient_exceptions PASSED

============================= 23 passed in 5.41s ==============================
```

### 4.2 Full Billing Regression Test Suite
Executed the entire suite across worker, ingestion, checkout, customer, config, models, and migrations:
```
pytest tests/test_razorpay_webhook_worker.py \
       tests/test_razorpay_webhook_ingestion.py \
       tests/test_razorpay_checkout.py \
       tests/test_stripe_webhook_worker.py \
       tests/test_provider_neutral_billing_models.py \
       tests/test_provider_neutral_migration.py \
       tests/test_razorpay_client.py \
       tests/test_razorpay_customer.py \
       tests/test_razorpay_config.py -v
```
Result: **187 passed in 23.64s** (100% pass rate).

---

## 5. Invariant & Architecture Compliance

| Invariant / Requirement | Compliance Verification |
| :--- | :---: |
| **CLI Entry Point** | Exposes `main(argv)` and `if __name__ == "__main__": main()`. |
| **Flag Support** | `--poll-interval`, `--batch-size`, `--max-attempts`, `--once`, `--log-level`. |
| **Strict Argument Validation** | Rejects non-positive intervals, batch sizes $< 1$, and retry limits $< 1$. |
| **Retry Policy Preservation** | Default remains strictly 5 attempts (`MAX_WEBHOOK_ATTEMPTS`). |
| **Cross-Platform Signals** | Gracefully handles SIGINT/SIGTERM on POSIX and Windows. |
| **Duplicate Signal Guard** | `_signal_handlers_registered` prevents duplicate registrations. |
| **Graceful Draining** | Existing leases finish and record outcome; new work is blocked after stop. |
| **Resource Disposal** | `engine.dispose()` called in `finally:` block of `run_worker()`. |
| **Loop Error Resilience** | Unexpected batch errors log and back off; polling loop remains running. |
| **Offline Independence** | `--help` runs with 0 network calls and 0 database connections. |
| **Schema & Migration Unchanged** | Alembic head remains `d1e2f3a4b5c6 (head)`. No schema changes. |

---

## 6. Git Status

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
	modified:   apps/api/tests/test_stripe_checkout_handler.py
	modified:   apps/api/tests/test_stripe_customer.py

Untracked files:
	STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md
	STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md
	STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md
	STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md
	STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md
	STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md
	apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py
	apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py
	apps/api/alembic/versions/20261107_2000_c1d2e3f4a5b7_scope_active_checkout_attempt_uniqueness.py
	apps/api/alembic/versions/20261108_1200_d1e2f3a4b5c6_add_webhook_event_lease_ownership.py
	apps/api/app/models/payment.py
	apps/api/app/services/billing/razorpay/
	apps/api/app/workers/razorpay_webhook_worker.py
	apps/api/tests/test_provider_neutral_billing_models.py
	apps/api/tests/test_provider_neutral_migration.py
	apps/api/tests/test_razorpay_checkout.py
	apps/api/tests/test_razorpay_client.py
	apps/api/tests/test_razorpay_config.py
	apps/api/tests/test_razorpay_customer.py
	apps/api/tests/test_razorpay_webhook_ingestion.py
	apps/api/tests/test_razorpay_webhook_worker.py
```

---

## 7. Conclusion

Step 5.2D.2.2 is complete. The Razorpay webhook background worker now has a complete CLI interface, robust signal handling, resource management, and test coverage, ready for the next phase: implementing the domain event handlers in Step 5.2D.3.
