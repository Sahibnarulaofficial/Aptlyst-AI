"""Operator CLI for inspecting and resolving Aptlyst AI billing reconciliations.

Usage:
    python -m app.workers.billing_reconciliation list --environment live --status pending_review
    python -m app.workers.billing_reconciliation inspect --case-id <UUID> --environment live
    python -m app.workers.billing_reconciliation resolve --case-id <UUID> --environment live \\
        --status resolved_ignored --operator-id <UUID> --notes "..." --confirm
    python -m app.workers.billing_reconciliation list-quarantined-webhooks --environment live
    python -m app.workers.billing_reconciliation inspect-webhook --event-id <UUID> --environment live
    python -m app.workers.billing_reconciliation list-exhausted-recoveries --environment live
    python -m app.workers.billing_reconciliation inspect-recovery --attempt-id <UUID> --environment live
    python -m app.workers.billing_reconciliation list-legacy-stripe

Safety Guarantees:
- Sets APTLYST_PROCESS_ROLE='migration' so only DATABASE_URL + ENVIRONMENT are validated;
  never requires Razorpay API secrets, webhook secrets, or external network calls.
- Never initiates Razorpay refunds, webhook re-drives, or blind recovery retries.
- Sanitizes all diagnostic output to strip credentials and PII.
- Fails closed on invalid environment, missing operator UUID, or insufficient notes.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
import json
import logging
import os
import sys
import uuid

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

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
from app.models.subscription import ReconciliationStatus

register_all_models()

from app.services.billing.reconciliation_ops import (
    DEFAULT_PAGE_LIMIT,
    ReconciliationOpsError,
    inspect_exhausted_recovery_attempt,
    inspect_payment_reconciliation,
    inspect_quarantined_webhook,
    list_exhausted_recovery_attempts,
    list_legacy_stripe_reconciliations,
    list_payment_reconciliations,
    list_quarantined_webhooks,
    resolve_payment_reconciliation,
)
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.workers.billing_reconciliation")


def _parse_optional_uuid(raw: str | None, field_name: str) -> uuid.UUID | None:
    if raw is None:
        return None
    clean = raw.strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty.")
    try:
        return uuid.UUID(clean)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def _parse_status_filter(raw: str | None) -> ReconciliationStatus | None:
    if raw is None or raw.strip().lower() == "all":
        return None
    try:
        return ReconciliationStatus(raw.strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unsupported reconciliation status '{raw}'.") from exc


def _render_human_output(command: str, payload: dict[str, object]) -> str:
    if command == "list":
        lines = [
            f"Payment Reconciliations (env={payload['environment']}, total={payload['total_count']}, "
            f"limit={payload['limit']}, offset={payload['offset']})"
        ]
        items = payload.get("items", [])
        if not items:
            lines.append("  (none)")
        for item in items:  # type: ignore[attr-defined]
            lines.append(
                f"  - id={item['id']} status={item['status']} reason={item['reason']} "
                f"case_key={item['case_key']} org={item['organization_id']} "
                f"created_at={item['created_at']}"
            )
        return "\n".join(lines)

    if command == "inspect":
        return json.dumps(payload, indent=2, sort_keys=True)

    if command == "resolve":
        case = payload["case"]  # type: ignore[index]
        return (
            f"Reconciliation Resolution Result:\n"
            f"  case_id          : {case['id']}\n"
            f"  environment      : {case['environment']}\n"
            f"  status           : {case['status']}\n"
            f"  resolved_by      : {case['resolved_by']}\n"
            f"  resolved_at      : {case['resolved_at']}\n"
            f"  changed          : {payload['changed']}\n"
            f"  idempotent_replay: {payload['idempotent_replay']}\n"
            f"  notice           : {payload['notice']}"
        )

    if command == "list-quarantined-webhooks":
        lines = [
            f"Quarantined Webhooks (env={payload['environment']}, total={payload['total_count']}, "
            f"limit={payload['limit']}, offset={payload['offset']})",
            f"Note: {payload['redrive_limitation']}",
        ]
        items = payload.get("items", [])
        if not items:
            lines.append("  (none)")
        for item in items:  # type: ignore[attr-defined]
            lines.append(
                f"  - id={item['id']} type={item['event_type']} "
                f"provider_event_id={item['provider_event_id']} attempts={item['attempts']} "
                f"sig_verified={item['signature_verified']} reason={item['quarantine_reason']}"
            )
        return "\n".join(lines)

    if command == "list-exhausted-recoveries":
        lines = [
            f"Exhausted / Quarantined Recovery Attempts (env={payload['environment']}, "
            f"total={payload['total_count']}, limit={payload['limit']}, offset={payload['offset']})",
            f"Note: {payload['retry_limitation']}",
        ]
        items = payload.get("items", [])
        if not items:
            lines.append("  (none)")
        for item in items:  # type: ignore[attr-defined]
            lines.append(
                f"  - id={item['id']} org={item['organization_id']} "
                f"attempts={item['recovery_attempts']} transient={item['transient_failures']} "
                f"expired_leases={item['expired_lease_count']} "
                f"reconciliation_id={item['linked_reconciliation_id']}"
            )
        return "\n".join(lines)

    return json.dumps(payload, indent=2, sort_keys=True)


async def execute_reconciliation_cli(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Execute one subcommand against the database and return (exit_code, formatted_output)."""
    settings.validate_migration_configuration()
    factory = session_factory or AsyncSessionLocal
    output_json = bool(getattr(args, "json", False))

    try:
        async with factory() as db:
            if args.command == "list":
                res = await list_payment_reconciliations(
                    db,
                    environment=args.environment,
                    status=_parse_status_filter(args.status),
                    reason=args.reason,
                    min_age_seconds=args.min_age_seconds,
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    merchant_account_id=args.merchant_account_id,
                    limit=args.limit,
                    offset=args.offset,
                    now=now,
                )
                payload = res.to_dict()
            elif args.command == "inspect":
                case_uuid = _parse_optional_uuid(args.case_id, "case_id")
                assert case_uuid is not None
                res_detail = await inspect_payment_reconciliation(
                    db,
                    case_id=case_uuid,
                    environment=args.environment,
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    merchant_account_id=args.merchant_account_id,
                )
                payload = res_detail.to_dict()
            elif args.command == "resolve":
                if not args.confirm:
                    raise ValueError(
                        "Explicit --confirm flag is required to record an audited reconciliation resolution."
                    )
                case_uuid = _parse_optional_uuid(args.case_id, "case_id")
                assert case_uuid is not None
                target = ReconciliationStatus(args.status)
                res_resolve = await resolve_payment_reconciliation(
                    db,
                    case_id=case_uuid,
                    environment=args.environment,
                    target_status=target,
                    operator_id=args.operator_id,
                    resolution_notes=args.notes,
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    merchant_account_id=args.merchant_account_id,
                    now=now,
                )
                payload = res_resolve.to_dict()
            elif args.command == "list-quarantined-webhooks":
                res_wh = await list_quarantined_webhooks(
                    db,
                    environment=args.environment,
                    event_type=args.event_type,
                    min_age_seconds=args.min_age_seconds,
                    merchant_account_id=args.merchant_account_id,
                    limit=args.limit,
                    offset=args.offset,
                    now=now,
                )
                payload = res_wh.to_dict()
            elif args.command == "inspect-webhook":
                event_uuid = _parse_optional_uuid(args.event_id, "event_id")
                assert event_uuid is not None
                res_wh_detail = await inspect_quarantined_webhook(
                    db,
                    event_id=event_uuid,
                    environment=args.environment,
                    merchant_account_id=args.merchant_account_id,
                )
                payload = {
                    **res_wh_detail.to_dict(),
                    "redrive_supported": False,
                }
            elif args.command == "list-exhausted-recoveries":
                res_rec = await list_exhausted_recovery_attempts(
                    db,
                    environment=args.environment,
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    merchant_account_id=args.merchant_account_id,
                    min_age_seconds=args.min_age_seconds,
                    limit=args.limit,
                    offset=args.offset,
                    now=now,
                )
                payload = res_rec.to_dict()
            elif args.command == "inspect-recovery":
                attempt_uuid = _parse_optional_uuid(args.attempt_id, "attempt_id")
                assert attempt_uuid is not None
                res_rec_detail = await inspect_exhausted_recovery_attempt(
                    db,
                    attempt_id=attempt_uuid,
                    environment=args.environment,
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    merchant_account_id=args.merchant_account_id,
                )
                payload = {
                    **res_rec_detail.to_dict(),
                    "retry_supported": False,
                }
            elif args.command == "list-legacy-stripe":
                res_legacy = await list_legacy_stripe_reconciliations(
                    db,
                    status=_parse_status_filter(args.status),
                    organization_id=_parse_optional_uuid(args.organization_id, "organization_id"),
                    limit=args.limit,
                    offset=args.offset,
                )
                payload = res_legacy.to_dict()
            else:
                raise ValueError(f"Unsupported command '{args.command}'.")
    except (ReconciliationOpsError, ValueError) as exc:
        safe_msg = sanitize_billing_error_message(str(exc), max_chars=512)
        if output_json:
            return 2, json.dumps({"error": safe_msg}, indent=2, sort_keys=True)
        return 2, f"ERROR: {safe_msg}"
    finally:
        if session_factory is None:
            try:
                from app.db.session import engine

                await engine.dispose()
            except Exception as exc:
                logger.debug("Error disposing database engine: %s", exc)

    rendered = (
        json.dumps(payload, indent=2, sort_keys=True)
        if output_json
        else _render_human_output(args.command, payload)
    )
    return 0, rendered


def _add_shared_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")


def _add_pagination_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=DEFAULT_PAGE_LIMIT, help="Page size (1..100).")
    parser.add_argument("--offset", type=int, default=0, help="Non-negative offset.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.billing_reconciliation",
        description="Aptlyst AI — Audited Operator Billing Reconciliation CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    env_choices = sorted(ALLOWED_RAZORPAY_ENVIRONMENTS)

    p_list = sub.add_parser("list", help="Read-only list of PaymentReconciliation cases.")
    p_list.add_argument("--environment", choices=env_choices, default=None)
    p_list.add_argument(
        "--status",
        choices=["pending_review", "resolved_refunded", "resolved_ignored", "all"],
        default="pending_review",
    )
    p_list.add_argument("--reason", type=str, default=None)
    p_list.add_argument("--min-age-seconds", type=int, default=None)
    p_list.add_argument("--organization-id", type=str, default=None)
    p_list.add_argument("--merchant-account-id", type=str, default=None)
    _add_pagination_flags(p_list)
    _add_shared_output_flags(p_list)

    p_inspect = sub.add_parser("inspect", help="Read-only inspection of one reconciliation case.")
    p_inspect.add_argument("--case-id", required=True, type=str)
    p_inspect.add_argument("--environment", required=True, choices=env_choices)
    p_inspect.add_argument("--organization-id", type=str, default=None)
    p_inspect.add_argument("--merchant-account-id", type=str, default=None)
    _add_shared_output_flags(p_inspect)

    p_resolve = sub.add_parser(
        "resolve",
        help="Audited resolution of a pending_review PaymentReconciliation case (no refund API calls).",
    )
    p_resolve.add_argument("--case-id", required=True, type=str)
    p_resolve.add_argument("--environment", required=True, choices=env_choices)
    p_resolve.add_argument(
        "--status",
        required=True,
        choices=["resolved_refunded", "resolved_ignored"],
        help=(
            "Target resolution status. Note: 'resolved_refunded' records only an operator label "
            "and does NOT initiate or independently verify a Razorpay refund."
        ),
    )
    p_resolve.add_argument("--operator-id", required=True, type=str)
    p_resolve.add_argument("--notes", required=True, type=str)
    p_resolve.add_argument("--organization-id", type=str, default=None)
    p_resolve.add_argument("--merchant-account-id", type=str, default=None)
    p_resolve.add_argument(
        "--confirm",
        action="store_true",
        help="Required explicit confirmation to persist audited resolution.",
    )
    _add_shared_output_flags(p_resolve)

    p_wh_list = sub.add_parser(
        "list-quarantined-webhooks",
        help="Read-only list of quarantined webhook events (redrive disabled by design).",
    )
    p_wh_list.add_argument("--environment", choices=env_choices, default=None)
    p_wh_list.add_argument("--event-type", type=str, default=None)
    p_wh_list.add_argument("--min-age-seconds", type=int, default=None)
    p_wh_list.add_argument("--merchant-account-id", type=str, default=None)
    _add_pagination_flags(p_wh_list)
    _add_shared_output_flags(p_wh_list)

    p_wh_inspect = sub.add_parser(
        "inspect-webhook",
        help="Read-only sanitized inspection of a webhook event.",
    )
    p_wh_inspect.add_argument("--event-id", required=True, type=str)
    p_wh_inspect.add_argument("--environment", required=True, choices=env_choices)
    p_wh_inspect.add_argument("--merchant-account-id", type=str, default=None)
    _add_shared_output_flags(p_wh_inspect)

    p_rec_list = sub.add_parser(
        "list-exhausted-recoveries",
        help="Read-only list of exhausted checkout recovery attempts (retry disabled by design).",
    )
    p_rec_list.add_argument("--environment", choices=env_choices, default=None)
    p_rec_list.add_argument("--organization-id", type=str, default=None)
    p_rec_list.add_argument("--merchant-account-id", type=str, default=None)
    p_rec_list.add_argument("--min-age-seconds", type=int, default=None)
    _add_pagination_flags(p_rec_list)
    _add_shared_output_flags(p_rec_list)

    p_rec_inspect = sub.add_parser(
        "inspect-recovery",
        help="Read-only sanitized inspection of a checkout recovery attempt.",
    )
    p_rec_inspect.add_argument("--attempt-id", required=True, type=str)
    p_rec_inspect.add_argument("--environment", required=True, choices=env_choices)
    p_rec_inspect.add_argument("--organization-id", type=str, default=None)
    p_rec_inspect.add_argument("--merchant-account-id", type=str, default=None)
    _add_shared_output_flags(p_rec_inspect)

    p_legacy = sub.add_parser(
        "list-legacy-stripe",
        help="Read-only inspection of legacy Stripe BillingReconciliation rows.",
    )
    p_legacy.add_argument(
        "--status",
        choices=["pending_review", "resolved_refunded", "resolved_ignored", "all"],
        default="all",
    )
    p_legacy.add_argument("--organization-id", type=str, default=None)
    _add_pagination_flags(p_legacy)
    _add_shared_output_flags(p_legacy)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if getattr(args, "json", False) else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    exit_code, rendered = asyncio.run(execute_reconciliation_cli(args))
    print(rendered)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
