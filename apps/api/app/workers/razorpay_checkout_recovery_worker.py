"""Durable background worker for Razorpay checkout attempt recovery.

Architecture & Invariants:
1. Leased Atomic Claiming:
   - Claims eligible 'recovery_required' checkout attempts using SELECT ... FOR UPDATE SKIP LOCKED.
   - Assigns a fresh UUID recovery_claim_id and stamps recovery_claim_expires_at.
   - Commits the claim transaction immediately, releasing row locks before gateway inquiry.
2. Fencing Token & Stale Worker Rejection:
   - Verifies recovery_claim_id before outcome persistence under write row lock.
   - Stale workers (whose claim was superseded or expired) abort without mutating attempt state.
3. Bounded Backoff Schedule & Permanent Quarantine:
   - Inconclusive business outcomes follow [60s, 300s, 900s, 1800s, 3600s] +/- 15% backoff.
   - Gateway 5xx/network timeouts trigger transient backoff without incrementing attempt count.
   - Attempt budget capped at 5 attempts; exhausts to next_recovery_retry_at = NULL and stages
     PaymentReconciliation while remaining quarantined in recovery_required.
4. Graceful Shutdown & Environment Safety:
   - Intercepts SIGINT / SIGTERM cleanly across POSIX and Windows.
   - Production workers strictly enforce environment='live'.
"""

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import logging
import os
import signal
import sys
from typing import Any
import uuid

# Ensure apps/api directory is in sys.path when executed directly as a script
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    ALLOWED_RAZORPAY_ENVIRONMENTS,
    PROHIBITED_PRODUCTION_MERCHANT_IDS,
    get_canonical_razorpay_merchant_account_id,
    resolve_required_razorpay_environment,
    settings,
    validate_deployment_environment,
    validate_razorpay_runtime_scope,
)

PROHIBITED_PRODUCTION_MERCHANT_NAMES: frozenset[str] = PROHIBITED_PRODUCTION_MERCHANT_IDS | {
    "test",
    "dev",
    "stage",
    "staging",
    "ci",
}
from app.db.session import AsyncSessionLocal
from app.services.billing.razorpay.client import (
    RazorpayClient,
    is_razorpay_enabled,
)
from app.services.billing.razorpay.recovery_engine import (
    DEFAULT_RECOVERY_LEASE_DURATION_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    ClaimedRecoveryAttempt,
    RecoveryExecutionResult,
    RecoveryOutcome,
    StaleRecoveryClaimError,
    claim_next_recovery_attempt,
    execute_claimed_recovery_attempt,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.workers.razorpay_checkout_recovery_worker")

DEFAULT_POLL_INTERVAL_SECONDS: float = 2.0
DEFAULT_BATCH_SIZE: int = 10


class RazorpayCheckoutRecoveryWorker:
    """Dedicated background worker that continuously claims and recovers ambiguous checkout attempts."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
        worker_id: str | None = None,
        lease_duration: int | None = None,
        poll_interval: float | None = None,
        batch_size: int | None = None,
        max_attempts: int | None = None,
        environment: str | None = None,
        merchant_account_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        client: RazorpayClient | None = None,
        apply_jitter: bool = True,
        jitter_fn: Callable[[float], float] | None = None,
    ) -> None:
        """Initialize the Razorpay checkout recovery worker.

        Args:
            session_factory: Callable returning an AsyncSession (defaults to AsyncSessionLocal).
            worker_id: Unique worker identifier.
            lease_duration: Seconds a recovery claim remains valid (defaults to 60s).
            poll_interval: Seconds between polls when queue is empty.
            batch_size: Maximum attempts claimed and executed per batch iteration.
            max_attempts: Maximum retry threshold before quarantine.
            environment: Optional environment filter ("test" or "live").
            merchant_account_id: Optional expected merchant account identifier.
            clock: Optional callable returning current UTC datetime.
            client: Optional RazorpayClient instance.
            apply_jitter: Whether to apply +/- 15% jitter to backoff calculation.
            jitter_fn: Optional callable for deterministic test jitter injection.
        """
        self.session_factory = session_factory or AsyncSessionLocal
        self.worker_id = worker_id or f"worker_rec_{uuid.uuid4().hex[:12]}"
        self.lease_duration = (
            lease_duration if lease_duration is not None else DEFAULT_RECOVERY_LEASE_DURATION_SECONDS
        )

        env_poll = os.getenv("RAZORPAY_RECOVERY_WORKER_POLL_INTERVAL")
        env_batch = os.getenv("RAZORPAY_RECOVERY_WORKER_BATCH_SIZE")
        env_merchant = os.getenv("RAZORPAY_MERCHANT_ACCOUNT_ID")

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
            max_attempts if max_attempts is not None else MAX_RECOVERY_ATTEMPTS
        )

        required_env = resolve_required_razorpay_environment(getattr(settings, "environment", None))
        _, is_prod = validate_deployment_environment(getattr(settings, "environment", None))
        if environment is not None:
            clean_env = environment.strip().lower() if isinstance(environment, str) else ""
            if clean_env not in ALLOWED_RAZORPAY_ENVIRONMENTS:
                raise ValueError(
                    f"Invalid Razorpay worker environment '{environment}'. Must be 'test' or 'live'."
                )
            if clean_env != required_env:
                raise ValueError(
                    f"{'Production' if is_prod else 'Non-production'} RazorpayCheckoutRecoveryWorker "
                    f"strictly requires environment='{required_env}'; got '{clean_env}'."
                )
            self.environment: str = clean_env
        else:
            self.environment = required_env

        cfg_merchant = getattr(settings, "razorpay_merchant_account_id", None)
        is_cfg_prohibited = not cfg_merchant or cfg_merchant.strip().lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES

        if is_prod:
            cfg_env = (getattr(settings, "razorpay_environment", None) or "").strip().lower()
            if cfg_env != "live":
                raise ValueError(
                    "Production RazorpayCheckoutRecoveryWorker strictly requires RAZORPAY_ENVIRONMENT='live'; "
                    f"got '{getattr(settings, 'razorpay_environment', None)}'."
                )
            if merchant_account_id is not None:
                clean_merchant = merchant_account_id.strip() if isinstance(merchant_account_id, str) else ""
                if not clean_merchant or clean_merchant.lower() in PROHIBITED_PRODUCTION_MERCHANT_NAMES:
                    raise ValueError(
                        f"Production RazorpayCheckoutRecoveryWorker rejects prohibited merchant account '{merchant_account_id}'. "
                        "Must be explicitly configured with a non-default account identifier."
                    )
                self.merchant_account_id: str | None = clean_merchant
            else:
                raw_merchant = env_merchant or getattr(settings, "razorpay_merchant_account_id", None)
                clean_merchant = (raw_merchant or "").strip() if isinstance(raw_merchant, str) else ""
                self.merchant_account_id = clean_merchant if clean_merchant and clean_merchant.lower() not in PROHIBITED_PRODUCTION_MERCHANT_NAMES else None
        else:
            if merchant_account_id is not None:
                clean_merchant = merchant_account_id.strip() if isinstance(merchant_account_id, str) else ""
                if not clean_merchant:
                    raise ValueError("Razorpay merchant_account_id cannot be an empty string.")
                self.merchant_account_id = clean_merchant
            else:
                raw_merchant = env_merchant or (None if is_cfg_prohibited else cfg_merchant)
                self.merchant_account_id = (
                    raw_merchant.strip() if isinstance(raw_merchant, str) and raw_merchant.strip() else None
                )

        if client is not None:
            client_merchant = getattr(client, "merchant_account_id", None)
            if client_merchant is not None and self.merchant_account_id is not None and client_merchant != self.merchant_account_id:
                raise ValueError(
                    f"Injected client merchant_account_id '{client_merchant}' does not match worker canonical merchant '{self.merchant_account_id}'."
                )
            client_env = getattr(client, "environment", None)
            if client_env is not None and client_env != self.environment:
                raise ValueError(
                    f"Injected client environment '{client_env}' does not match worker environment '{self.environment}'."
                )

        self.clock = clock
        self.client = client
        self.apply_jitter = apply_jitter
        self.jitter_fn = jitter_fn

        if self.poll_interval <= 0:
            raise ValueError(f"poll_interval must be greater than 0, got {self.poll_interval}")
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")
        if self.lease_duration < 1:
            raise ValueError(f"lease_duration must be at least 1, got {self.lease_duration}")

        self._stop_event = asyncio.Event()
        self._signal_handlers_registered = False

    def _get_now(self) -> datetime:
        """Get current UTC timestamp, using clock callable if configured."""
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    async def claim_one(self) -> ClaimedRecoveryAttempt | None:
        """Claim a single eligible recovery attempt under a fresh lease.

        Returns:
            ClaimedRecoveryAttempt if successfully claimed, or None if queue is empty.
        """
        now = self._get_now()
        async with self.session_factory() as db:
            try:
                claimed = await claim_next_recovery_attempt(
                    db=db,
                    worker_id=self.worker_id,
                    lease_duration_seconds=self.lease_duration,
                    max_attempts=self.max_attempts,
                    environment=self.environment,
                    merchant_account_id=self.merchant_account_id,
                    now=now,
                )
                return claimed
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Error while worker %s claimed recovery attempt: %s",
                    self.worker_id,
                    sanitize_billing_error_message(str(exc)),
                )
                return None

    async def process_one(self) -> RecoveryExecutionResult | None:
        """Claim and execute recovery for a single attempt.

        Guarantees:
        - Atomic claiming in short DB transaction.
        - Remote inquiries strictly outside DB transactions.
        - Fencing token verification before persistence.
        - Stale workers rejected cleanly.
        """
        if not is_razorpay_enabled():
            logger.debug("Razorpay billing disabled. Worker %s will not process recovery.", self.worker_id)
            return None

        claimed = await self.claim_one()
        if claimed is None:
            return None

        try:
            return await execute_claimed_recovery_attempt(
                session_factory=self.session_factory,
                claimed=claimed,
                client=self.client,
                max_attempts=self.max_attempts,
                now_fn=self._get_now,
                apply_jitter=self.apply_jitter,
                jitter_fn=self.jitter_fn,
                expected_environment=self.environment,
                expected_merchant_id=self.merchant_account_id,
            )
        except asyncio.CancelledError:
            raise
        except StaleRecoveryClaimError as exc:
            logger.warning(
                "Stale worker %s superseded on attempt %s: %s",
                self.worker_id,
                claimed.attempt_id,
                exc,
            )
            return RecoveryExecutionResult(
                attempt_id=claimed.attempt_id,
                outcome=RecoveryOutcome.SUPERSEDED,
                error_message=str(exc),
            )
        except Exception as exc:
            logger.error(
                "Unexpected error during recovery execution for attempt %s: %s",
                claimed.attempt_id,
                sanitize_billing_error_message(str(exc)),
            )
            return None

    async def run_batch(self, max_count: int | None = None) -> list[RecoveryExecutionResult]:
        """Claim and process a batch of eligible recovery attempts up to max_count or batch_size.

        Args:
            max_count: Optional override for batch limit.

        Returns:
            List of RecoveryExecutionResult instances.
        """
        limit = max_count if max_count is not None else self.batch_size
        results: list[RecoveryExecutionResult] = []

        for _ in range(limit):
            if self._stop_event.is_set():
                break

            result = await self.process_one()
            if result is None:
                # No more attempts ready or all active rows locked
                break
            results.append(result)

        return results

    async def run_once(self) -> int:
        """Process currently available eligible recovery attempts in a single pass.

        Returns:
            Number of attempts processed.
        """
        results = await self.run_batch()
        return len(results)

    async def run_forever(self) -> None:
        """Continuously poll and process recovery attempts until stop() is invoked."""
        await self.start()

    async def start(self) -> None:
        """Run the worker polling loop until stopped."""
        logger.info(
            "Starting RazorpayCheckoutRecoveryWorker %s (poll_interval=%.2fs, batch_size=%d, lease_duration=%ds, max_attempts=%d)",
            self.worker_id,
            self.poll_interval,
            self.batch_size,
            self.lease_duration,
            self.max_attempts,
        )
        try:
            while not self._stop_event.is_set():
                if not is_razorpay_enabled():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=min(5.0, self.poll_interval)
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    results = await self.run_batch()
                    if results:
                        if len(results) >= self.batch_size:
                            # Full batch claimed, immediately loop for more without delay
                            continue
                    else:
                        # Queue is empty, back off for poll_interval
                        try:
                            await asyncio.wait_for(
                                self._stop_event.wait(), timeout=self.poll_interval
                            )
                        except asyncio.TimeoutError:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Error in RazorpayCheckoutRecoveryWorker polling loop: %s",
                        sanitize_billing_error_message(str(exc)),
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=min(5.0, self.poll_interval)
                        )
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            logger.info("RazorpayCheckoutRecoveryWorker %s cancelled during execution.", self.worker_id)
            raise
        finally:
            logger.info("RazorpayCheckoutRecoveryWorker %s stopped cleanly.", self.worker_id)

    def stop(self) -> None:
        """Signal the worker polling loop to stop gracefully."""
        logger.info("Stopping RazorpayCheckoutRecoveryWorker %s...", self.worker_id)
        self._stop_event.set()


def setup_signal_handlers(worker: RazorpayCheckoutRecoveryWorker) -> None:
    """Register signal handlers for graceful shutdown across POSIX and Windows environments."""
    if getattr(worker, "_signal_handlers_registered", False):
        logger.debug("Signal handlers already registered for worker %s.", worker.worker_id)
        return

    def _on_signal() -> None:
        logger.info(
            "Termination signal received for recovery worker %s. Requesting graceful shutdown...",
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


async def run_recovery_worker(
    poll_interval: float | None = None,
    batch_size: int | None = None,
    lease_duration: int | None = None,
    max_attempts: int | None = None,
    run_once_only: bool = False,
    session_factory: Callable[[], AsyncSession] | None = None,
    environment: str | None = None,
    merchant_account_id: str | None = None,
    client: RazorpayClient | None = None,
) -> int:
    """Run the Razorpay checkout recovery worker as an async task with signal interception."""
    if poll_interval is not None and poll_interval <= 0:
        raise ValueError(f"poll_interval must be greater than 0, got {poll_interval}")
    if batch_size is not None and batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    if max_attempts is not None and max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    if lease_duration is not None and lease_duration < 1:
        raise ValueError(f"lease_duration must be at least 1, got {lease_duration}")

    worker = RazorpayCheckoutRecoveryWorker(
        session_factory=session_factory,
        poll_interval=poll_interval,
        batch_size=batch_size,
        lease_duration=lease_duration,
        max_attempts=max_attempts,
        environment=environment,
        merchant_account_id=merchant_account_id,
        client=client,
    )

    setup_signal_handlers(worker)

    try:
        if run_once_only:
            count = await worker.run_once()
            logger.info("Single recovery batch pass completed: %d attempts processed.", count)
            return 0

        await worker.start()
        return 0
    except asyncio.CancelledError:
        logger.info("Recovery worker task cancelled cleanly.")
        raise
    finally:
        if session_factory is None:
            try:
                from app.db.session import engine

                await engine.dispose()
                logger.debug("Disposed default database engine successfully.")
            except Exception as exc:
                logger.debug("Error disposing database engine: %s", exc)


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for the Razorpay checkout recovery worker."""
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.razorpay_checkout_recovery_worker",
        description="Aptlyst AI — Durable Razorpay Checkout Recovery Worker",
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
        help=f"Maximum attempts to claim and execute per batch iteration (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--lease-duration",
        type=int,
        default=None,
        help=f"Seconds before a recovery claim expires (default: {DEFAULT_RECOVERY_LEASE_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help=f"Maximum attempts before quarantining an unprocessable attempt (default: {MAX_RECOVERY_ATTEMPTS})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single pass over pending recovery attempts and exit immediately",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=None,
        choices=["test", "live"],
        help="Target Razorpay environment ('test' or 'live'). Defaults to deployment environment policy.",
    )
    parser.add_argument(
        "--merchant-account-id",
        type=str,
        default=None,
        help="Target merchant account identifier. Defaults to configured RAZORPAY_MERCHANT_ACCOUNT_ID.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity level (default: INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for running the Razorpay checkout recovery worker."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.poll_interval is not None and args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.max_attempts is not None and args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
    if args.lease_duration is not None and args.lease_duration < 1:
        parser.error("--lease-duration must be at least 1")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        exit_code = asyncio.run(
            run_recovery_worker(
                poll_interval=args.poll_interval,
                batch_size=args.batch_size,
                lease_duration=args.lease_duration,
                max_attempts=args.max_attempts,
                run_once_only=args.once,
                environment=args.environment,
                merchant_account_id=args.merchant_account_id,
            )
        )
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
