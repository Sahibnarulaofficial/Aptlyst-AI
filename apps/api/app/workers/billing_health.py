"""Operational CLI for inspecting Razorpay worker heartbeats and billing queues.

Usage:
    python -m app.workers.billing_health
    python -m app.workers.billing_health --environment live --json --fail-on-alert

Guarantees:
- Strictly read-only database inspection (zero queue mutations).
- Zero secrets, signing keys, or customer PII output.
- Disposes database engine cleanly before exit.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import os
import sys

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

# Read-only monitoring requires DB and deployment scope only (no API ingress secrets)
_HAD_PROCESS_ROLE_ENV = "APTLYST_PROCESS_ROLE" in os.environ
if not _HAD_PROCESS_ROLE_ENV and "app.config" not in sys.modules:
    os.environ["APTLYST_PROCESS_ROLE"] = "webhook_worker"

try:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import ALLOWED_RAZORPAY_ENVIRONMENTS, settings
finally:
    if not _HAD_PROCESS_ROLE_ENV:
        os.environ.pop("APTLYST_PROCESS_ROLE", None)
from app.db.session import AsyncSessionLocal
from app.services.billing.razorpay.monitoring import (
    BillingHealthReport,
    BillingHealthThresholds,
    collect_billing_health_snapshot,
)

logger = logging.getLogger("app.workers.billing_health")


def format_human_report(report: BillingHealthReport) -> str:
    """Format a BillingHealthReport for operator terminal inspection."""
    lines: list[str] = [
        f"Aptlyst AI Billing Health Report ({report.checked_at.isoformat()})",
        f"Overall Status : {report.overall_status.upper()}",
        f"Env Filter     : {report.environment_filter or 'all'}",
        "",
        "Workers:",
    ]
    if not report.workers:
        lines.append("  (none recorded)")
    else:
        for w in report.workers:
            lines.append(
                f"  - {w.worker_id} [{w.worker_type}] status={w.status} "
                f"env={w.environment or 'any'} stale={w.is_stale} "
                f"hb_age={w.heartbeat_age_seconds:.1f}s "
                f"processed={w.processed_count} errors={w.error_count} "
                f"last_processed={w.last_processed_at.isoformat() if w.last_processed_at else 'never'}"
            )

    wh = report.webhook_queue
    lines.extend([
        "",
        "Webhook Queue:",
        f"  pending={wh.pending_count} failed={wh.failed_count} "
        f"quarantined={wh.quarantined_count} expired_leases={wh.expired_lease_count} "
        f"oldest_pending_age={f'{wh.oldest_pending_age_seconds:.1f}s' if wh.oldest_pending_age_seconds is not None else 'none'} "
        f"last_processed={wh.last_processed_at.isoformat() if wh.last_processed_at else 'never'}",
    ])

    rec = report.recovery_queue
    lines.extend([
        "",
        "Recovery Queue:",
        f"  due={rec.due_count} scheduled={rec.scheduled_count} "
        f"quarantined={rec.quarantined_count} expired_claims={rec.expired_claim_count} "
        f"repeated_lease_expiries={rec.repeated_lease_expiry_count} "
        f"oldest_overdue_age={f'{rec.oldest_overdue_age_seconds:.1f}s' if rec.oldest_overdue_age_seconds is not None else 'none'} "
        f"last_recovered={rec.last_recovered_at.isoformat() if rec.last_recovered_at else 'never'}",
    ])

    recon = report.reconciliation
    lines.extend([
        "",
        "Reconciliation:",
        f"  pending_review={recon.pending_review_count} recent_24h={recon.recent_created_24h_count} "
        f"oldest_pending_age={f'{recon.oldest_pending_age_seconds:.1f}s' if recon.oldest_pending_age_seconds is not None else 'none'}",
    ])

    lines.extend(["", f"Alerts ({len(report.alerts)}):"])
    if not report.alerts:
        lines.append("  (none)")
    else:
        for alert in report.alerts:
            lines.append(
                f"  - [{alert.severity.upper()}] {alert.code}: {alert.message} "
                f"(value={alert.metric_value}, threshold={alert.threshold})"
            )

    return "\n".join(lines)


async def run_billing_health_check(
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
    environment: str | None = None,
    thresholds: BillingHealthThresholds | None = None,
    output_json: bool = False,
    fail_on_alert: bool = False,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Execute read-only billing health check and return (exit_code, formatted_output)."""
    settings.validate_migration_configuration()
    factory = session_factory or AsyncSessionLocal

    try:
        async with factory() as db:
            report = await collect_billing_health_snapshot(
                db=db,
                environment=environment,
                now=now or datetime.now(timezone.utc),
                thresholds=thresholds,
                emit_alert_logs=True,
            )
    finally:
        if session_factory is None:
            try:
                from app.db.session import engine

                await engine.dispose()
            except Exception as exc:
                logger.debug("Error disposing database engine after health check: %s", exc)

    rendered = (
        json.dumps(report.to_dict(), indent=2, sort_keys=True)
        if output_json
        else format_human_report(report)
    )
    exit_code = 1 if (fail_on_alert and report.alerts) else 0
    return exit_code, rendered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.billing_health",
        description="Aptlyst AI — Read-Only Billing Worker & Queue Health Inspector",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=None,
        choices=sorted(ALLOWED_RAZORPAY_ENVIRONMENTS),
        help="Filter worker heartbeats and queues by Razorpay environment ('test' or 'live').",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output suitable for alerting/monitoring pipelines.",
    )
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="Exit with code 1 if any warning or critical alert condition is active.",
    )
    parser.add_argument(
        "--heartbeat-stale-seconds",
        type=int,
        default=None,
        help="Override stale worker heartbeat threshold in seconds.",
    )
    parser.add_argument(
        "--webhook-backlog-seconds",
        type=int,
        default=None,
        help="Override webhook backlog age alert threshold in seconds.",
    )
    parser.add_argument(
        "--recovery-overdue-seconds",
        type=int,
        default=None,
        help="Override recovery overdue age alert threshold in seconds.",
    )
    parser.add_argument(
        "--reconciliation-age-seconds",
        type=int,
        default=None,
        help="Override pending reconciliation age alert threshold in seconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    base = BillingHealthThresholds.from_settings()
    thresholds = BillingHealthThresholds(
        heartbeat_stale_seconds=(
            args.heartbeat_stale_seconds
            if args.heartbeat_stale_seconds is not None
            else base.heartbeat_stale_seconds
        ),
        webhook_backlog_age_seconds=(
            args.webhook_backlog_seconds
            if args.webhook_backlog_seconds is not None
            else base.webhook_backlog_age_seconds
        ),
        recovery_overdue_age_seconds=(
            args.recovery_overdue_seconds
            if args.recovery_overdue_seconds is not None
            else base.recovery_overdue_age_seconds
        ),
        reconciliation_backlog_age_seconds=(
            args.reconciliation_age_seconds
            if args.reconciliation_age_seconds is not None
            else base.reconciliation_backlog_age_seconds
        ),
        repeated_lease_expiry_threshold=base.repeated_lease_expiry_threshold,
    )

    logging.basicConfig(
        level=logging.WARNING if args.json else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    exit_code, rendered = asyncio.run(
        run_billing_health_check(
            environment=args.environment,
            thresholds=thresholds,
            output_json=args.json,
            fail_on_alert=args.fail_on_alert,
        )
    )
    print(rendered)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
