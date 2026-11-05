"""Durable background worker for Stripe webhook event processing.

Architecture & Invariants:
1. Queue Authority: Polls the durable `stripe_webhook_events` table populated by the
   FastAPI ingestion endpoint `/api/v1/billing/webhook`.
2. Concurrency Safety: Uses `claim_next_webhook_event()`, leveraging PostgreSQL
   `SELECT ... FOR UPDATE SKIP LOCKED` to guarantee multiple worker processes or instances
   cannot claim or process the same event concurrently.
3. Crash & Lease Recovery: If a worker process terminates or loses connectivity before
   committing, PostgreSQL automatically terminates the backend connection, rolling back
   the active transaction and releasing the row lock. The event remains in its last
   committed state (`pending` or `failed`) and is eligible for immediate re-claiming upon restart.
4. Safe Bounded Retries: Transient failures increment `attempts` and remain `failed` for retry
   up to `max_attempts`. Permanent errors or exhausted attempts are marked `quarantined` and
   will not be re-polled automatically.
5. Domain Decoupling: Reuses `execute_claimed_webhook_event()` and the registered
   `default_webhook_dispatcher`. Does not independently fabricate entitlements or business rules.
6. Graceful Shutdown: Listens for SIGINT and SIGTERM, allowing any in-flight event to complete
   and commit before terminating cleanly.
7. Zero Secret Leakage: Logs event IDs, event types, processing status, and error summaries,
   never customer credentials, card information, or sensitive payloads.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any, Callable

# Ensure apps/api directory is in sys.path when executed directly as a script
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.services.billing.stripe_webhook_engine import (
    MAX_WEBHOOK_ATTEMPTS,
    WEBHOOK_RETRY_BACKOFF_SCHEDULE,
    ClaimStatus,
    ProcessResult,
    WebhookDispatcher,
    claim_next_webhook_event,
    default_webhook_dispatcher,
    execute_claimed_webhook_event,
    get_retry_backoff_seconds,
    is_webhook_event_eligible,
)

logger = logging.getLogger("app.workers.stripe_webhook_worker")

# Default worker configuration
DEFAULT_POLL_INTERVAL_SECONDS: float = 2.0
DEFAULT_BATCH_SIZE: int = 10


class StripeWebhookWorker:
    """Dedicated background worker that continuously claims and processes Stripe webhook events."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
        dispatcher: WebhookDispatcher | None = None,
        poll_interval: float | None = None,
        batch_size: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        """Initialize the Stripe webhook worker.

        Args:
            session_factory: Callable returning an AsyncSession (defaults to AsyncSessionLocal).
            dispatcher: WebhookDispatcher instance (defaults to default_webhook_dispatcher).
            poll_interval: Seconds to wait between polling loops when queue is empty.
            batch_size: Maximum number of events to claim per batch iteration.
            max_attempts: Maximum retry threshold before quarantine.
        """
        self.session_factory = session_factory or AsyncSessionLocal
        self.dispatcher = dispatcher or default_webhook_dispatcher

        env_poll = os.getenv("STRIPE_WEBHOOK_WORKER_POLL_INTERVAL")
        env_batch = os.getenv("STRIPE_WEBHOOK_WORKER_BATCH_SIZE")

        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else (float(env_poll) if env_poll else DEFAULT_POLL_INTERVAL_SECONDS)
        )
        self.batch_size = (
            batch_size
            if batch_size is not None
            else (int(env_batch) if env_batch else DEFAULT_BATCH_SIZE)
        )
        self.max_attempts = (
            max_attempts if max_attempts is not None else MAX_WEBHOOK_ATTEMPTS
        )

        self._stop_event = asyncio.Event()
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Return True if the worker run loop is active."""
        return self._is_running

    def stop(self) -> None:
        """Signal the worker to gracefully cease polling and exit."""
        logger.info("Graceful shutdown requested for Stripe webhook worker...")
        self._stop_event.set()

    async def process_one(self) -> ProcessResult | None:
        """Claim and execute the next eligible Stripe webhook event.

        Uses PostgreSQL SELECT ... FOR UPDATE SKIP LOCKED via claim_next_webhook_event()
        to serialize concurrent workers safely.

        Returns:
            ProcessResult if an event was claimed and executed, or None if no event was available.
        """
        async with self.session_factory() as db:
            try:
                claim_res = await claim_next_webhook_event(
                    db=db,
                    max_attempts=self.max_attempts,
                )
            except Exception as exc:
                logger.error("Database error while claiming next webhook event: %s", exc)
                return None

            if claim_res.status != ClaimStatus.CLAIMED:
                return None

            event = claim_res.event
            assert event is not None

            handler = self.dispatcher.get_handler(event.event_type)

            try:
                result = await execute_claimed_webhook_event(
                    db=db,
                    event=event,
                    handler=handler,
                    max_attempts=self.max_attempts,
                )
                logger.info(
                    "Processed webhook event %s (type=%s, outcome_status=%s, attempts=%d)",
                    result.event_id,
                    event.event_type,
                    result.status.value,
                    result.attempts,
                )
                return result
            except Exception as exc:
                logger.error(
                    "Unexpected error during webhook execution for event %s (type=%s): %s",
                    event.id,
                    event.event_type,
                    exc,
                    exc_info=True,
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                return None

    async def run_batch(self, max_count: int | None = None) -> list[ProcessResult]:
        """Claim and process a batch of eligible events up to max_count or batch_size.

        Args:
            max_count: Optional override for batch limit.

        Returns:
            List of ProcessResult instances for successfully claimed events.
        """
        limit = max_count if max_count is not None else self.batch_size
        results: list[ProcessResult] = []

        for _ in range(limit):
            if self._stop_event.is_set():
                break

            result = await self.process_one()
            if result is None:
                # No more events ready or all active rows locked by competing workers
                break

            results.append(result)

        return results

    async def run_once(self) -> int:
        """Process currently available eligible events in a single pass and return count.

        Returns:
            Number of events processed.
        """
        results = await self.run_batch()
        return len(results)

    async def run_forever(self) -> None:
        """Continuously poll and process events until stop() is invoked."""
        self._is_running = True
        logger.info(
            "Stripe webhook worker started (poll_interval=%.2fs, batch_size=%d, max_attempts=%d).",
            self.poll_interval,
            self.batch_size,
            self.max_attempts,
        )

        try:
            while not self._stop_event.is_set():
                try:
                    results = await self.run_batch()

                    if results:
                        logger.debug("Processed %d webhook events in batch.", len(results))
                        # If a full batch was processed, immediately poll again without delay
                        if len(results) >= self.batch_size:
                            continue

                    # Queue is empty or partially filled; wait for poll_interval or until stop signaled
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self.poll_interval,
                        )
                    except asyncio.TimeoutError:
                        pass
                except asyncio.CancelledError:
                    logger.info("Worker task received cancellation signal.")
                    break
                except Exception as exc:
                    logger.error(
                        "Unhandled exception in Stripe webhook worker loop: %s",
                        exc,
                        exc_info=True,
                    )
                    # Backoff briefly on unexpected top-level loop errors
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=min(5.0, self.poll_interval),
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._is_running = False
            logger.info("Stripe webhook worker terminated cleanly.")


def setup_signal_handlers(worker: StripeWebhookWorker) -> None:
    """Register signal handlers for graceful shutdown across POSIX and Windows environments."""
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
    except (NotImplementedError, AttributeError):
        # Fallback for Windows or non-main thread where add_signal_handler is unsupported
        def _sync_signal_handler(signum: int, frame: Any) -> None:
            worker.stop()

        signal.signal(signal.SIGINT, _sync_signal_handler)
        signal.signal(signal.SIGTERM, _sync_signal_handler)


async def run_worker(
    poll_interval: float | None = None,
    batch_size: int | None = None,
    max_attempts: int | None = None,
    run_once_only: bool = False,
) -> int:
    """Run the Stripe webhook worker as an async task with signal interception.

    Args:
        poll_interval: Seconds between polls.
        batch_size: Max events per batch.
        max_attempts: Max retry attempts before quarantine.
        run_once_only: If True, executes a single batch and returns.

    Returns:
        Exit code (0 on clean shutdown).
    """
    worker = StripeWebhookWorker(
        poll_interval=poll_interval,
        batch_size=batch_size,
        max_attempts=max_attempts,
    )

    setup_signal_handlers(worker)

    if run_once_only:
        count = await worker.run_once()
        logger.info("Single batch pass completed: %d events processed.", count)
        return 0

    await worker.run_forever()
    return 0


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for running the Stripe webhook worker."""
    parser = argparse.ArgumentParser(
        description="Aptlyst AI — Durable Stripe Webhook Processing Worker",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds to wait between polling loops when queue is idle (default: 2.0)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Maximum events to claim and execute per batch iteration (default: 10)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum attempts before quarantining an unprocessable event (default: 5)",
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

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        sys.exit(
            asyncio.run(
                run_worker(
                    poll_interval=args.poll_interval,
                    batch_size=args.batch_size,
                    max_attempts=args.max_attempts,
                    run_once_only=args.once,
                )
            )
        )
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
