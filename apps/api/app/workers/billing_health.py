"""Operational CLI for inspecting Razorpay worker heartbeats, billing queues, and alerts.

Usage:
    python -m app.workers.billing_health
    python -m app.workers.billing_health --environment live --json --fail-on-alert
    python -m app.workers.billing_health --environment live --json --fail-on-alert --notify --state-file /var/lib/aptlyst/alerts.json

Exit Codes (suitable for external cron/schedulers):
    0: Healthy, OR alerts active without ``--fail-on-alert`` (and alert delivery succeeded if ``--notify``).
    1: Active alert condition(s) present under ``--fail-on-alert``, OR database/monitoring evaluation failed.
    2: Alert delivery or notification configuration failed when ``--notify`` is enabled.

Guarantees:
- Strictly read-only database inspection (zero queue, reconciliation, or payment mutations).
- Alert delivery executes strictly outside any database session or transaction.
- Zero secrets, signing keys, bearer tokens, or customer PII output or logged.
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
from typing import Any

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

# Read-only monitoring requires DB and deployment scope only (no API ingress secrets)
_HAD_PROCESS_ROLE_ENV = "APTLYST_PROCESS_ROLE" in os.environ
if not _HAD_PROCESS_ROLE_ENV and "app.config" not in sys.modules:
    os.environ["APTLYST_PROCESS_ROLE"] = "migration"

try:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import ALLOWED_RAZORPAY_ENVIRONMENTS, settings
finally:
    if not _HAD_PROCESS_ROLE_ENV:
        os.environ.pop("APTLYST_PROCESS_ROLE", None)
from app.db.session import AsyncSessionLocal
from app.models import register_all_models
from app.services.billing.razorpay.alerting import (
    AlertConfigurationError,
    AlertSink,
    AlertStateStore,
    build_evaluation_failure_report,
    dispatch_billing_alerts,
    resolve_alert_sink_from_settings,
    resolve_alert_state_store_from_settings,
)
from app.services.billing.razorpay.monitoring import (
    BillingHealthReport,
    BillingHealthThresholds,
    collect_billing_health_snapshot,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message

register_all_models()

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
    notify: bool = False,
    alert_sink: AlertSink | None = None,
    state_store: AlertStateStore | None = None,
    state_file: str | None = None,
    renotify_seconds: int | None = None,
    notify_on_recovery: bool = True,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Execute read-only billing health check + optional alerting and return (exit_code, output)."""
    checked_at = now or datetime.now(timezone.utc)
    try:
        settings.validate_migration_configuration()
    except Exception as exc:
        report = build_evaluation_failure_report(
            exc=exc, environment=environment, checked_at=checked_at
        )
        rendered = (
            json.dumps(report.to_dict(), indent=2, sort_keys=True)
            if output_json
            else format_human_report(report)
        )
        return 1, rendered

    resolved_sink: AlertSink | None = alert_sink
    if notify and resolved_sink is None:
        try:
            resolved_sink = resolve_alert_sink_from_settings(notify=True)
        except AlertConfigurationError as exc:
            err_msg = sanitize_billing_error_message(exc, max_chars=256)
            logger.error("Invalid alert delivery configuration: %s", err_msg)
            rendered = (
                json.dumps({"error": err_msg, "delivery_succeeded": False}, indent=2, sort_keys=True)
                if output_json
                else f"ERROR: {err_msg}"
            )
            return 2, rendered

    factory = session_factory or AsyncSessionLocal
    eval_failed = False

    try:
        async with factory() as db:
            report = await collect_billing_health_snapshot(
                db=db,
                environment=environment,
                now=checked_at,
                thresholds=thresholds,
                emit_alert_logs=True,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        eval_failed = True
        safe_err = sanitize_billing_error_message(exc, max_chars=256)
        logger.error("Billing health check evaluation failed: %s", safe_err)
        report = build_evaluation_failure_report(
            exc=exc,
            environment=environment,
            checked_at=checked_at,
        )
    finally:
        if session_factory is None:
            try:
                from app.db.session import engine

                await asyncio.wait_for(engine.dispose(), timeout=5.0)
            except Exception as exc:
                logger.debug("Error disposing database engine after health check: %s", exc)

    # Alert delivery occurs strictly outside any DB session or transaction
    dispatch_dict: dict[str, Any] | None = None
    delivery_failed = False
    if notify and resolved_sink is not None:
        resolved_store = state_store or resolve_alert_state_store_from_settings(
            state_file=state_file
        )
        effective_renotify = int(
            renotify_seconds
            if renotify_seconds is not None
            else getattr(settings, "billing_health_alert_renotify_seconds", 0)
        )
        dispatch_result = await dispatch_billing_alerts(
            report,
            sink=resolved_sink,
            state_store=resolved_store,
            renotify_interval_seconds=effective_renotify,
            notify_on_recovery=notify_on_recovery,
        )
        dispatch_dict = dispatch_result.to_dict()
        delivery_failed = not dispatch_result.delivery_succeeded

    if output_json:
        payload = report.to_dict()
        if dispatch_dict is not None:
            payload["alert_delivery"] = dispatch_dict
        rendered = json.dumps(payload, indent=2, sort_keys=True)
    else:
        rendered = format_human_report(report)
        if dispatch_dict is not None:
            rendered += (
                f"\n\nAlert Delivery ({dispatch_dict['sink_name']}): "
                f"attempted={dispatch_dict['attempted_delivery']} "
                f"succeeded={dispatch_dict['delivery_succeeded']} "
                f"emitted={dispatch_dict['emitted_count']} "
                f"suppressed={dispatch_dict['suppressed_count']}"
            )
            if dispatch_dict.get("error_message"):
                rendered += f" error={dispatch_dict['error_message']}"

    if delivery_failed:
        exit_code = 2
    elif eval_failed or (fail_on_alert and report.alerts):
        exit_code = 1
    else:
        exit_code = 0
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
        "--notify",
        action="store_true",
        help="Dispatch deduplicated alert transitions via configured sink (webhook if enabled in env, else structured logs).",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Optional local JSON state file path for edge-triggered alert deduplication.",
    )
    parser.add_argument(
        "--renotify-seconds",
        type=int,
        default=None,
        help="Optional reminder interval (seconds) for continuing firing alerts (default: 0 = edge-triggered only).",
    )
    parser.add_argument(
        "--no-recovery-notify",
        action="store_true",
        help="Suppress resolved recovery notifications when active alerts clear.",
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
        "--webhook-backlog-count",
        type=int,
        default=None,
        help="Override webhook backlog count alert threshold.",
    )
    parser.add_argument(
        "--webhook-failed-count",
        type=int,
        default=None,
        help="Override retrying failed webhook count alert threshold.",
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
        webhook_backlog_count_threshold=(
            args.webhook_backlog_count
            if args.webhook_backlog_count is not None
            else base.webhook_backlog_count_threshold
        ),
        webhook_failed_count_threshold=(
            args.webhook_failed_count
            if args.webhook_failed_count is not None
            else base.webhook_failed_count_threshold
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
            notify=args.notify,
            state_file=args.state_file,
            renotify_seconds=args.renotify_seconds,
            notify_on_recovery=not args.no_recovery_notify,
        )
    )
    print(rendered)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
