"""Transactional usage reservations, accounting, and settlement service.

Guarantees:
1. Strict integer arithmetic: micro-dollars ($1e-6 USD) exclusively, zero floating-point math.
2. Deadlock avoidance: consistent global lock order (OrganizationBillingPeriod FIRST,
   Subscription SECOND, UsageReservation THIRD).
3. Concurrent reservation fencing: SELECT ... FOR UPDATE serializes quota and spending-cap checks per billing period.
4. Single active reservation: partial unique index (uq_task_active_reservation) and explicit transaction checks
   prevent a generation task from acquiring multiple concurrent reservations.
5. Idempotent settlement: identical settlement replays return success without double-charging or corrupting counters.
6. Conflicting settlement rejection: divergent actual usage on an already settled reservation is explicitly rejected.
7. Ambiguity quarantine: never silently releases or zero-settles an ambiguous or dispatched reservation.
8. Defense-in-depth: strict ownership validation for organization, task, reservation, and billing period.
9. Subscription entitlement enforcement: Pro billing periods require an active, entitled Subscription record.
   - past_due within settings.billing_grace_period_days: access retained.
   - past_due beyond grace window, or canceled/unpaid/paused: denied.
   - Missing subscription for a Pro period: fail closed.
   - Free billing periods proceed without requiring a Subscription row.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import enum
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing.pricing import (
    MAX_MICROS_LIMIT,
    MAX_TOKENS_LIMIT,
    ModelPricing,
    PricingError,
    PricingOverflowError,
    calculate_max_turn_cost,
    validate_rate,
    validate_token_count,
)


class AccountingErrorCode(str, enum.Enum):
    """Categorized failure codes for accounting and reservation operations."""

    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SPENDING_CAP_EXHAUSTED = "SPENDING_CAP_EXHAUSTED"
    DUPLICATE_ACTIVE_RESERVATION = "DUPLICATE_ACTIVE_RESERVATION"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    BILLING_PERIOD_NOT_FOUND = "BILLING_PERIOD_NOT_FOUND"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    INVALID_OWNERSHIP = "INVALID_OWNERSHIP"
    INVALID_TASK_STATE = "INVALID_TASK_STATE"
    BILLING_PERIOD_INACTIVE = "BILLING_PERIOD_INACTIVE"
    BILLING_PERIOD_BLOCKED = "BILLING_PERIOD_BLOCKED"
    BILLING_PERIOD_EXPIRED = "BILLING_PERIOD_EXPIRED"
    INVALID_INPUT = "INVALID_INPUT"
    CONFLICTING_SETTLEMENT = "CONFLICTING_SETTLEMENT"
    EXCEEDS_TURN_LIMITS = "EXCEEDS_TURN_LIMITS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE = "AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE"
    INTEGER_OVERFLOW = "INTEGER_OVERFLOW"
    # Subscription entitlement errors (Pro billing periods only)
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    SUBSCRIPTION_INACTIVE = "SUBSCRIPTION_INACTIVE"
    GRACE_PERIOD_EXPIRED = "GRACE_PERIOD_EXPIRED"


@dataclass(frozen=True)
class ReservationResult:
    """Outcome of an attempt to acquire a pre-inference usage reservation."""

    success: bool
    reservation: UsageReservation | None = None
    tokens_reserved: int = 0
    cost_micros_reserved: int = 0
    error_code: AccountingErrorCode | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of an attempt to settle or release a usage reservation."""

    success: bool
    reservation: UsageReservation | None = None
    actual_tokens: int = 0
    actual_cost_micros: int = 0
    tokens_released: int = 0
    cost_micros_released: int = 0
    is_idempotent_replay: bool = False
    usage_log: LLMUsageLog | None = None
    error_code: AccountingErrorCode | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _validate_uuid(val: Any, name: str) -> uuid.UUID:
    """Validate that value is a genuine uuid.UUID instance and not a boolean or string."""
    if isinstance(val, bool) or not isinstance(val, uuid.UUID):
        raise TypeError(f"{name} must be a valid uuid.UUID instance, got {type(val).__name__}")
    return val


async def reserve_usage(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    billing_period_id: uuid.UUID,
    pricing: ModelPricing,
    custom_max_input_tokens: int | None = None,
    custom_max_output_tokens: int | None = None,
    now: datetime | None = None,
) -> ReservationResult:
    """Reserve quota and spending headroom before initiating LLM inference.

    Executes within the caller's transaction context. Locks the OrganizationBillingPeriod
    row using SELECT ... FOR UPDATE to serialize concurrent quota evaluations.

    For Pro billing periods, additionally locks the Subscription row (SECOND in global lock order
    after OrganizationBillingPeriod) and enforces the subscription entitlement policy:
    - active: allowed.
    - past_due within settings.billing_grace_period_days: allowed.
    - past_due beyond grace window: denied (GRACE_PERIOD_EXPIRED).
    - canceled, unpaid, paused, incomplete, incomplete_expired, trialing: denied (SUBSCRIPTION_INACTIVE).
    - Missing subscription for a Pro period: denied (SUBSCRIPTION_NOT_FOUND).
    Free billing periods proceed without requiring a Subscription row.

    Lock order (deadlock-safe):
      1. OrganizationBillingPeriod  (SELECT FOR UPDATE)
      2. Subscription               (SELECT FOR UPDATE, Pro periods only)
      3. UsageReservation           (SELECT, no explicit lock on dup check)

    Args:
        session: Active SQLAlchemy async session.
        organization_id: Organization owning the task and billing period.
        task_id: GenerationTask requiring quota reservation.
        billing_period_id: Billing period to deduct reserved headroom from.
        pricing: Trusted ModelPricing configuration for ceiling calculations.
        custom_max_input_tokens: Optional tighter ceiling on input tokens.
        custom_max_output_tokens: Optional tighter ceiling on output tokens.
        now: Optional override for current timestamp (for deterministic test dates).

    Returns:
        ReservationResult: Typed outcome distinguishing success from specific failures.
    """
    # 1. Strict Input Validation
    try:
        org_id = _validate_uuid(organization_id, "organization_id")
        t_id = _validate_uuid(task_id, "task_id")
        bp_id = _validate_uuid(billing_period_id, "billing_period_id")

        if not isinstance(pricing, ModelPricing):
            raise TypeError(f"pricing must be an instance of ModelPricing, got {type(pricing).__name__}")

        if custom_max_input_tokens is not None:
            validate_token_count(custom_max_input_tokens, "custom_max_input_tokens")
        if custom_max_output_tokens is not None:
            validate_token_count(custom_max_output_tokens, "custom_max_output_tokens")
    except (TypeError, ValueError, PricingError) as err:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_INPUT,
            message=f"Input validation error: {err}",
        )

    # 2. Compute Conservative Maximum Turn Cost
    try:
        max_turn_estimate = calculate_max_turn_cost(
            pricing=pricing,
            custom_max_input_tokens=custom_max_input_tokens,
            custom_max_output_tokens=custom_max_output_tokens,
        )
    except (ValueError, PricingError) as err:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_INPUT,
            message=f"Pricing ceiling calculation error: {err}",
        )

    tokens_to_reserve = max_turn_estimate.total_tokens
    cost_to_reserve = max_turn_estimate.total_cost_micros

    # 3. Lock Billing Period (Consistent Lock Order: Billing Period FIRST)
    stmt_period = (
        select(OrganizationBillingPeriod)
        .where(OrganizationBillingPeriod.id == bp_id)
        .with_for_update()
    )
    res_period = await session.execute(stmt_period)
    period = res_period.scalar_one_or_none()
    if period is None:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_NOT_FOUND,
            message=f"Billing period '{bp_id}' not found",
        )

    # 4. Verify Billing Period Eligibility
    if period.organization_id != org_id:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Billing period does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "period_org_id": str(period.organization_id)},
        )

    if not period.is_active:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_INACTIVE,
            message="Billing period is not active",
        )

    if period.is_blocked:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_BLOCKED,
            message=f"Billing period is blocked: {period.blocked_reason or 'No reason provided'}",
            details={"blocked_reason": period.blocked_reason},
        )

    current_time = now or datetime.now(timezone.utc)
    if current_time < period.period_start or current_time >= period.period_end:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_EXPIRED,
            message="Current time is outside the active billing period date range",
            details={
                "period_start": period.period_start.isoformat(),
                "period_end": period.period_end.isoformat(),
                "now": current_time.isoformat(),
            },
        )

    # 4b. Subscription Entitlement Check (Pro billing periods only)
    #
    # Lock order: OrganizationBillingPeriod FIRST (already held), Subscription SECOND.
    # Subscription webhook handlers lock Subscription only and never lock
    # OrganizationBillingPeriod, so this ordering is globally deadlock-safe.
    #
    # Free billing periods bypass this check; they do not require a Subscription row.
    is_paid_period = bool(period.plan_tier and period.plan_tier.strip().lower() != "free")
    if is_paid_period:
        # Select subscriptions for the organization that are eligible to authorize Pro usage.
        # Eligible statuses: active, past_due (subject to grace period evaluation).
        # We query only eligible statuses to safely ignore historical canceled/incomplete rows
        # while leveraging the partial unique index uq_org_active_subscription.
        eligible_statuses = (SubscriptionStatus.active, SubscriptionStatus.past_due)
        stmt_sub = (
            select(Subscription)
            .where(
                Subscription.organization_id == org_id,
                Subscription.status.in_(eligible_statuses),
            )
            .with_for_update()
        )
        res_sub = await session.execute(stmt_sub)
        eligible_subs = res_sub.scalars().all()

        if len(eligible_subs) > 1:
            logger.warning(
                "Organization %s has multiple (%d) eligible subscriptions. Failing closed.",
                org_id,
                len(eligible_subs),
            )
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.RECONCILIATION_REQUIRED,
                message=f"Multiple eligible subscriptions ({len(eligible_subs)}) found for organization '{org_id}'; reconciliation required",
                details={
                    "eligible_subscription_count": len(eligible_subs),
                    "subscription_ids": [str(s.id) for s in eligible_subs],
                },
            )

        if len(eligible_subs) == 0:
            # Check if a non-entitled subscription row exists for diagnostic reporting
            stmt_any = (
                select(Subscription)
                .where(Subscription.organization_id == org_id)
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
            res_any = await session.execute(stmt_any)
            latest_sub = res_any.scalar_one_or_none()

            if latest_sub is not None:
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
                    message="Subscription is not in an entitled state for Pro access",
                    details={"subscription_status": latest_sub.status.value},
                )

            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_NOT_FOUND,
                message="Pro billing period requires an active Stripe subscription record, but none was found",
            )

        subscription = eligible_subs[0]
        sub_status = subscription.status

        # States that are never entitled for Pro access
        _non_entitled_statuses = frozenset({
            SubscriptionStatus.canceled,
            SubscriptionStatus.unpaid,
            SubscriptionStatus.paused,
            SubscriptionStatus.incomplete,
            SubscriptionStatus.incomplete_expired,
            SubscriptionStatus.trialing,
        })

        if sub_status in _non_entitled_statuses:
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SUBSCRIPTION_INACTIVE,
                message="Subscription is not in an entitled state for Pro access",
                details={"subscription_status": sub_status.value},
            )

        if sub_status == SubscriptionStatus.past_due:
            # Grace-period enforcement.
            # past_due_since must be present; absence fails closed.
            past_due_since = subscription.past_due_since
            if past_due_since is None:
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                    message="Subscription is past_due but past_due_since anchor is missing; failing closed",
                )
            # Ensure timezone-aware comparison
            if past_due_since.tzinfo is None:
                past_due_since = past_due_since.replace(tzinfo=timezone.utc)
            grace_deadline = past_due_since + timedelta(days=settings.billing_grace_period_days)
            if current_time >= grace_deadline:
                return ReservationResult(
                    success=False,
                    error_code=AccountingErrorCode.GRACE_PERIOD_EXPIRED,
                    message="Subscription past-due grace period has expired; Pro access denied",
                )
            # Within grace window: allow usage to continue within the existing paid period.

        # cancel_at_period_end=True alone does NOT revoke entitlement while status is active.
        # The status transition (to canceled) is the authoritative revocation trigger.

    # 5. Verify Generation Task Eligibility
    stmt_task = select(GenerationTask).where(GenerationTask.id == t_id)
    res_task = await session.execute(stmt_task)
    task = res_task.scalar_one_or_none()
    if task is None:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.TASK_NOT_FOUND,
            message=f"Generation task '{t_id}' not found",
        )

    if task.organization_id != org_id:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Generation task does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "task_org_id": str(task.organization_id)},
        )

    if task.billing_period_id != bp_id:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Generation task is bound to a different billing period",
            details={"task_period_id": str(task.billing_period_id), "requested_period_id": str(bp_id)},
        )

    if task.status != GenerationTaskStatus.pending:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_TASK_STATE,
            message=f"Generation task is in state '{task.status.value}', expected 'pending'",
            details={"task_status": task.status.value},
        )

    # 6. Check for Duplicate Active Reservation on this Task
    stmt_dup = (
        select(UsageReservation)
        .where(
            UsageReservation.generation_task_id == t_id,
            UsageReservation.status == ReservationStatus.active,
        )
    )
    res_dup = await session.execute(stmt_dup)
    existing_active = res_dup.scalar_one_or_none()
    if existing_active is not None:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.DUPLICATE_ACTIVE_RESERVATION,
            message="Generation task already has an active usage reservation",
            details={"existing_reservation_id": str(existing_active.id)},
        )

    # 7. Quota Availability Check (Pure Integer Arithmetic)
    committed_tokens = period.tokens_used + period.tokens_reserved
    if committed_tokens + tokens_to_reserve > period.monthly_token_quota:
        available_tokens = max(0, period.monthly_token_quota - committed_tokens)
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.QUOTA_EXHAUSTED,
            message="Monthly token quota exhausted",
            details={
                "monthly_token_quota": period.monthly_token_quota,
                "tokens_used": period.tokens_used,
                "tokens_reserved": period.tokens_reserved,
                "committed_tokens": committed_tokens,
                "available_tokens": available_tokens,
                "requested_tokens": tokens_to_reserve,
            },
        )

    # 8. Hard Spending Cap Check (Pure Integer Arithmetic)
    if period.hard_spending_cap_micros is not None:
        committed_micros = period.cost_micros_used + period.cost_micros_reserved
        if committed_micros + cost_to_reserve > period.hard_spending_cap_micros:
            available_micros = max(0, period.hard_spending_cap_micros - committed_micros)
            return ReservationResult(
                success=False,
                error_code=AccountingErrorCode.SPENDING_CAP_EXHAUSTED,
                message="Hard spending cap exhausted",
                details={
                    "hard_spending_cap_micros": period.hard_spending_cap_micros,
                    "cost_micros_used": period.cost_micros_used,
                    "cost_micros_reserved": period.cost_micros_reserved,
                    "committed_micros": committed_micros,
                    "available_micros": available_micros,
                    "requested_micros": cost_to_reserve,
                },
            )

    # 9. Guard Against 64-bit Integer Overflow
    if period.tokens_reserved + tokens_to_reserve > MAX_MICROS_LIMIT:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INTEGER_OVERFLOW,
            message="Reserved tokens would exceed maximum integer capacity",
        )
    if period.cost_micros_reserved + cost_to_reserve > MAX_MICROS_LIMIT:
        return ReservationResult(
            success=False,
            error_code=AccountingErrorCode.INTEGER_OVERFLOW,
            message="Reserved micro-dollars would exceed maximum integer capacity",
        )

    # 10. Atomic Reservation & Headroom Deduction
    period.tokens_reserved += tokens_to_reserve
    period.cost_micros_reserved += cost_to_reserve

    reservation = UsageReservation(
        id=uuid.uuid4(),
        generation_task_id=task.id,
        organization_id=org_id,
        billing_period_id=period.id,
        lease_token=task.lease_token,
        tokens_reserved=tokens_to_reserve,
        cost_micros_reserved=cost_to_reserve,
        status=ReservationStatus.active,
    )
    session.add(reservation)
    await session.flush()

    return ReservationResult(
        success=True,
        reservation=reservation,
        tokens_reserved=tokens_to_reserve,
        cost_micros_reserved=cost_to_reserve,
    )


async def settle_reservation(
    session: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    organization_id: uuid.UUID,
    actual_tokens: int,
    actual_cost_micros: int,
    pricing: ModelPricing | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    pricing_version: str | None = None,
    latency_ms: int = 0,
    now: datetime | None = None,
) -> SettlementResult:
    """Settle an active usage reservation against actual inference expenditure.

    Reconciles reserved tokens/costs against actual amounts, releases the unused headroom
    back to the billing period, transitions the reservation to 'settled', and records an
    immutable LLMUsageLog for regulatory auditing.

    Guarantees:
    - Deadlock-safe row locking: OrganizationBillingPeriod locked FIRST, UsageReservation SECOND.
    - Idempotent replay: identical re-settlement returns success without double-charging.
    - Conflicting settlement rejection: conflicting usage on settled reservations is rejected.
    - Ambiguity safety: never zero-settles an ambiguous or dispatched reservation.

    Args:
        session: Active SQLAlchemy async session.
        reservation_id: Identifier of the UsageReservation to settle.
        organization_id: Organization owning the reservation and billing period.
        actual_tokens: Total tokens consumed (prompt + completion).
        actual_cost_micros: Total cost incurred in micro-dollars ($1e-6 USD).
        pricing: Optional ModelPricing to enforce model turn ceiling.
        prompt_tokens: Optional prompt token breakdown.
        completion_tokens: Optional completion token breakdown.
        provider: Provider name for usage audit log.
        model: Model name for usage audit log.
        pricing_version: Version identifier for usage audit log.
        latency_ms: Measured provider latency in milliseconds.
        now: Optional override for settlement timestamp.

    Returns:
        SettlementResult: Detailed outcome including actual usage and released amounts.
    """
    # 1. Strict Input Validation
    try:
        r_id = _validate_uuid(reservation_id, "reservation_id")
        org_id = _validate_uuid(organization_id, "organization_id")
        a_tokens = validate_token_count(actual_tokens, "actual_tokens")
        a_cost = validate_rate(actual_cost_micros, "actual_cost_micros")

        p_tokens: int | None = None
        c_tokens: int | None = None
        if prompt_tokens is not None:
            p_tokens = validate_token_count(prompt_tokens, "prompt_tokens")
        if completion_tokens is not None:
            c_tokens = validate_token_count(completion_tokens, "completion_tokens")

        if p_tokens is not None and p_tokens > a_tokens:
            return SettlementResult(
                success=False,
                error_code=AccountingErrorCode.INVALID_INPUT,
                message=f"prompt_tokens ({p_tokens}) cannot exceed actual_tokens ({a_tokens})",
            )
        if c_tokens is not None and c_tokens > a_tokens:
            return SettlementResult(
                success=False,
                error_code=AccountingErrorCode.INVALID_INPUT,
                message=f"completion_tokens ({c_tokens}) cannot exceed actual_tokens ({a_tokens})",
            )
        if p_tokens is not None and c_tokens is not None:
            if p_tokens + c_tokens != a_tokens:
                return SettlementResult(
                    success=False,
                    error_code=AccountingErrorCode.INVALID_INPUT,
                    message=f"prompt_tokens ({p_tokens}) + completion_tokens ({c_tokens}) must equal actual_tokens ({a_tokens})",
                )
    except (TypeError, ValueError, PricingError) as err:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_INPUT,
            message=f"Input validation error: {err}",
        )

    # 2. Consistent Row Locking Order (Billing Period FIRST, Reservation SECOND)
    stmt_pre = select(UsageReservation.billing_period_id).where(UsageReservation.id == r_id)
    res_pre = await session.execute(stmt_pre)
    b_period_id = res_pre.scalar_one_or_none()
    if b_period_id is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.RESERVATION_NOT_FOUND,
            message=f"Usage reservation '{r_id}' not found",
        )

    # Lock Billing Period FIRST
    stmt_period = (
        select(OrganizationBillingPeriod)
        .where(OrganizationBillingPeriod.id == b_period_id)
        .with_for_update()
    )
    res_period = await session.execute(stmt_period)
    period = res_period.scalar_one_or_none()
    if period is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_NOT_FOUND,
            message=f"Billing period '{b_period_id}' not found",
        )

    # Lock Reservation SECOND
    stmt_res = (
        select(UsageReservation)
        .where(UsageReservation.id == r_id)
        .with_for_update()
    )
    res_res = await session.execute(stmt_res)
    reservation = res_res.scalar_one_or_none()
    if reservation is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.RESERVATION_NOT_FOUND,
            message=f"Usage reservation '{r_id}' not found",
        )

    # 3. Ownership Validation
    if reservation.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Reservation does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "reservation_org_id": str(reservation.organization_id)},
        )

    if period.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Billing period does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "period_org_id": str(period.organization_id)},
        )

    # Validate parent task ownership if task is present
    stmt_task = select(GenerationTask).where(GenerationTask.id == reservation.generation_task_id)
    res_task = await session.execute(stmt_task)
    task = res_task.scalar_one_or_none()
    if task is not None and task.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Generation task does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "task_org_id": str(task.organization_id)},
        )

    # 4. Idempotency & Conflicting Settlement Check
    if reservation.status == ReservationStatus.settled:
        stmt_log = (
            select(LLMUsageLog)
            .where(LLMUsageLog.generation_task_id == reservation.generation_task_id)
        )
        res_log = await session.execute(stmt_log)
        existing_log = res_log.scalar_one_or_none()

        settled_tokens = existing_log.total_tokens if existing_log is not None else 0
        settled_cost = existing_log.cost_micros if existing_log is not None else 0

        if settled_tokens == a_tokens and settled_cost == a_cost:
            if existing_log is not None:
                if (p_tokens is not None and existing_log.prompt_tokens != p_tokens) or (
                    c_tokens is not None and existing_log.completion_tokens != c_tokens
                ):
                    return SettlementResult(
                        success=False,
                        reservation=reservation,
                        error_code=AccountingErrorCode.CONFLICTING_SETTLEMENT,
                        message="Settlement replay token breakdown conflicts with previously settled usage log",
                        details={
                            "settled_prompt_tokens": existing_log.prompt_tokens,
                            "settled_completion_tokens": existing_log.completion_tokens,
                            "attempted_prompt_tokens": p_tokens,
                            "attempted_completion_tokens": c_tokens,
                        },
                    )
            return SettlementResult(
                success=True,
                reservation=reservation,
                actual_tokens=a_tokens,
                actual_cost_micros=a_cost,
                tokens_released=0,
                cost_micros_released=0,
                is_idempotent_replay=True,
                usage_log=existing_log,
                message="Reservation already settled with identical usage (idempotent replay)",
            )
        else:
            return SettlementResult(
                success=False,
                reservation=reservation,
                error_code=AccountingErrorCode.CONFLICTING_SETTLEMENT,
                message="Reservation was already settled with different token or cost values",
                details={
                    "settled_tokens": settled_tokens,
                    "settled_cost_micros": settled_cost,
                    "attempted_tokens": a_tokens,
                    "attempted_cost_micros": a_cost,
                },
            )

    if reservation.status == ReservationStatus.released:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INVALID_TASK_STATE,
            message="Reservation has already been released and cannot be settled",
        )

    # 5. Reservation Bound Enforcement & Reconciliation
    if a_tokens > reservation.tokens_reserved or a_cost > reservation.cost_micros_reserved:
        # Provider actual usage exceeded reservation: preserve liability and quarantine task
        if task is not None:
            task.status = GenerationTaskStatus.reconciliation_needed
            task.last_error = (
                f"Settlement exceeded reservation: actual_tokens={a_tokens} "
                f"(reserved={reservation.tokens_reserved}), actual_cost_micros={a_cost} "
                f"(reserved={reservation.cost_micros_reserved})"
            )
            await session.flush()

        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.RECONCILIATION_REQUIRED,
            message=(
                f"Actual provider usage ({a_tokens} tokens, {a_cost} micros) exceeded reserved limits "
                f"({reservation.tokens_reserved} tokens, {reservation.cost_micros_reserved} micros). "
                "Liability preserved; task quarantined for reconciliation."
            ),
            details={
                "actual_tokens": a_tokens,
                "reserved_tokens": reservation.tokens_reserved,
                "actual_cost_micros": a_cost,
                "reserved_cost_micros": reservation.cost_micros_reserved,
            },
        )

    if pricing is not None:
        if a_tokens > pricing.max_turn_tokens:
            return SettlementResult(
                success=False,
                reservation=reservation,
                error_code=AccountingErrorCode.EXCEEDS_TURN_LIMITS,
                message=f"Actual tokens ({a_tokens}) exceed model turn ceiling ({pricing.max_turn_tokens})",
                details={"actual_tokens": a_tokens, "max_turn_tokens": pricing.max_turn_tokens},
            )
        if p_tokens is not None and p_tokens > pricing.max_input_tokens:
            return SettlementResult(
                success=False,
                reservation=reservation,
                error_code=AccountingErrorCode.EXCEEDS_TURN_LIMITS,
                message=f"Prompt tokens ({p_tokens}) exceed model input ceiling ({pricing.max_input_tokens})",
                details={"prompt_tokens": p_tokens, "max_input_tokens": pricing.max_input_tokens},
            )
        if c_tokens is not None and c_tokens > pricing.max_output_tokens:
            return SettlementResult(
                success=False,
                reservation=reservation,
                error_code=AccountingErrorCode.EXCEEDS_TURN_LIMITS,
                message=f"Completion tokens ({c_tokens}) exceed model output ceiling ({pricing.max_output_tokens})",
                details={"completion_tokens": c_tokens, "max_output_tokens": pricing.max_output_tokens},
            )

    # 6. Ambiguity & Dispatched Safety Rule: Never silently zero-settle an ambiguous request
    if a_tokens == 0 and a_cost == 0:
        if task is not None:
            if task.status in (GenerationTaskStatus.completed, GenerationTaskStatus.reconciliation_needed):
                return SettlementResult(
                    success=False,
                    reservation=reservation,
                    error_code=AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE,
                    message=f"Task is in '{task.status.value}' state; cannot zero-settle ambiguous reservation",
                    details={"task_status": task.status.value},
                )
            stmt_attempts = (
                select(GenerationTaskAttempt)
                .where(GenerationTaskAttempt.generation_task_id == task.id)
            )
            res_attempts = await session.execute(stmt_attempts)
            attempts = res_attempts.scalars().all()
            for att in attempts:
                if att.status != GenerationAttemptStatus.failed:
                    return SettlementResult(
                        success=False,
                        reservation=reservation,
                        error_code=AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE,
                        message=(
                            f"Attempt #{att.attempt_number} is in '{att.status.value}' state; "
                            "cannot release or zero-settle reservation without definitive provider reconciliation"
                        ),
                        details={"attempt_number": att.attempt_number, "attempt_status": att.status.value},
                    )

    # 7. Reconciliation & Atomic Counter Updates
    if period.tokens_reserved < reservation.tokens_reserved or period.cost_micros_reserved < reservation.cost_micros_reserved:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INVALID_TASK_STATE,
            message=(
                f"Billing period reserved headroom (tokens={period.tokens_reserved}, cost={period.cost_micros_reserved}) "
                f"is less than reservation being settled (tokens={reservation.tokens_reserved}, cost={reservation.cost_micros_reserved})"
            ),
        )

    tokens_released = reservation.tokens_reserved - a_tokens
    cost_micros_released = reservation.cost_micros_reserved - a_cost

    # Check for overflow on cumulative counters
    if period.tokens_used + a_tokens > MAX_MICROS_LIMIT:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INTEGER_OVERFLOW,
            message="Cumulative tokens used would exceed maximum integer capacity",
        )
    if period.cost_micros_used + a_cost > MAX_MICROS_LIMIT:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INTEGER_OVERFLOW,
            message="Cumulative cost micro-dollars would exceed maximum integer capacity",
        )

    # Reconcile billing period
    period.tokens_reserved -= reservation.tokens_reserved
    period.cost_micros_reserved -= reservation.cost_micros_reserved
    period.tokens_used += a_tokens
    period.cost_micros_used += a_cost

    settled_time = now or datetime.now(timezone.utc)
    reservation.status = ReservationStatus.settled
    reservation.settled_at = settled_time

    # 8. Create Immutable Audit LLMUsageLog
    usage_log: LLMUsageLog | None = None
    if a_tokens > 0 or a_cost > 0:
        p_name = provider or (pricing.model_name.split("-")[0] if pricing else "mock")
        m_name = model or (pricing.model_name if pricing else "mock-gpt-4o-mini")
        v_name = pricing_version or (pricing.pricing_version if pricing else "dev-mock-2026-v1")
        if p_tokens is not None and c_tokens is not None:
            prompt_cnt = p_tokens
            comp_cnt = c_tokens
        elif p_tokens is not None:
            prompt_cnt = p_tokens
            comp_cnt = a_tokens - p_tokens
        elif c_tokens is not None:
            comp_cnt = c_tokens
            prompt_cnt = a_tokens - c_tokens
        else:
            prompt_cnt = 0
            comp_cnt = a_tokens

        usage_log = LLMUsageLog(
            id=uuid.uuid4(),
            organization_id=org_id,
            billing_period_id=period.id,
            conversation_id=task.conversation_id if task else None,
            visitor_message_id=task.visitor_message_id if task else None,
            generation_task_id=task.id if task else None,
            provider=p_name,
            model=m_name,
            pricing_version=v_name,
            prompt_tokens=prompt_cnt,
            completion_tokens=comp_cnt,
            total_tokens=a_tokens,
            cost_micros=a_cost,
            latency_ms=max(0, latency_ms),
        )
        session.add(usage_log)

    await session.flush()

    return SettlementResult(
        success=True,
        reservation=reservation,
        actual_tokens=a_tokens,
        actual_cost_micros=a_cost,
        tokens_released=tokens_released,
        cost_micros_released=cost_micros_released,
        is_idempotent_replay=False,
        usage_log=usage_log,
    )


async def release_reservation(
    session: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    organization_id: uuid.UUID,
    now: datetime | None = None,
) -> SettlementResult:
    """Release an active reservation when a task fails before provider dispatch.

    Safeguards:
    - Verifies that the task and its attempts are NOT in a dispatched or ambiguous state.
    - Transitions reservation status to 'released'.
    - Returns all reserved tokens and micro-dollars to the billing period.

    Args:
        session: Active SQLAlchemy async session.
        reservation_id: Identifier of the UsageReservation to release.
        organization_id: Organization owning the reservation.
        now: Optional override for timestamp.

    Returns:
        SettlementResult: Outcome of the release operation.
    """
    try:
        r_id = _validate_uuid(reservation_id, "reservation_id")
        org_id = _validate_uuid(organization_id, "organization_id")
    except (TypeError, ValueError) as err:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_INPUT,
            message=f"Input validation error: {err}",
        )

    # Consistent Row Locking Order
    stmt_pre = select(UsageReservation.billing_period_id).where(UsageReservation.id == r_id)
    res_pre = await session.execute(stmt_pre)
    b_period_id = res_pre.scalar_one_or_none()
    if b_period_id is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.RESERVATION_NOT_FOUND,
            message=f"Usage reservation '{r_id}' not found",
        )

    stmt_period = (
        select(OrganizationBillingPeriod)
        .where(OrganizationBillingPeriod.id == b_period_id)
        .with_for_update()
    )
    res_period = await session.execute(stmt_period)
    period = res_period.scalar_one_or_none()
    if period is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.BILLING_PERIOD_NOT_FOUND,
            message=f"Billing period '{b_period_id}' not found",
        )

    stmt_res = (
        select(UsageReservation)
        .where(UsageReservation.id == r_id)
        .with_for_update()
    )
    res_res = await session.execute(stmt_res)
    reservation = res_res.scalar_one_or_none()
    if reservation is None:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.RESERVATION_NOT_FOUND,
            message=f"Usage reservation '{r_id}' not found",
        )

    if reservation.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Reservation does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "reservation_org_id": str(reservation.organization_id)},
        )

    if period.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Billing period does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "period_org_id": str(period.organization_id)},
        )

    stmt_task = select(GenerationTask).where(GenerationTask.id == reservation.generation_task_id)
    res_task = await session.execute(stmt_task)
    task = res_task.scalar_one_or_none()
    if task is not None and task.organization_id != org_id:
        return SettlementResult(
            success=False,
            error_code=AccountingErrorCode.INVALID_OWNERSHIP,
            message="Generation task does not belong to the specified organization",
            details={"expected_org_id": str(org_id), "task_org_id": str(task.organization_id)},
        )

    if reservation.status == ReservationStatus.released:
        return SettlementResult(
            success=True,
            reservation=reservation,
            tokens_released=0,
            cost_micros_released=0,
            is_idempotent_replay=True,
            message="Reservation already marked released",
        )

    if reservation.status == ReservationStatus.settled:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INVALID_TASK_STATE,
            message="Reservation is already settled and cannot be released",
        )

    # Ambiguity Check: Ensure task is NOT in completed/reconciliation_needed or attempts in non-failed state
    if task is not None:
        if task.status in (GenerationTaskStatus.completed, GenerationTaskStatus.reconciliation_needed):
            return SettlementResult(
                success=False,
                reservation=reservation,
                error_code=AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE,
                message=f"Task is in '{task.status.value}' state; cannot release reservation without reconciliation",
                details={"task_status": task.status.value},
            )
        stmt_attempts = (
            select(GenerationTaskAttempt)
            .where(GenerationTaskAttempt.generation_task_id == task.id)
        )
        res_attempts = await session.execute(stmt_attempts)
        attempts = res_attempts.scalars().all()
        for att in attempts:
            if att.status != GenerationAttemptStatus.failed:
                return SettlementResult(
                    success=False,
                    reservation=reservation,
                    error_code=AccountingErrorCode.AMBIGUOUS_STATE_CANNOT_ZERO_SETTLE,
                    message=(
                        f"Attempt #{att.attempt_number} is in '{att.status.value}' state; "
                        "cannot release reservation without definitive evidence that dispatch did not occur"
                    ),
                    details={"attempt_number": att.attempt_number, "attempt_status": att.status.value},
                )

    if period.tokens_reserved < reservation.tokens_reserved or period.cost_micros_reserved < reservation.cost_micros_reserved:
        return SettlementResult(
            success=False,
            reservation=reservation,
            error_code=AccountingErrorCode.INVALID_TASK_STATE,
            message=(
                f"Billing period reserved headroom (tokens={period.tokens_reserved}, cost={period.cost_micros_reserved}) "
                f"is less than reservation being released (tokens={reservation.tokens_reserved}, cost={reservation.cost_micros_reserved})"
            ),
        )

    tokens_released = reservation.tokens_reserved
    cost_micros_released = reservation.cost_micros_reserved

    period.tokens_reserved -= reservation.tokens_reserved
    period.cost_micros_reserved -= reservation.cost_micros_reserved
    if period.tokens_reserved < 0:
        period.tokens_reserved = 0
    if period.cost_micros_reserved < 0:
        period.cost_micros_reserved = 0

    release_time = now or datetime.now(timezone.utc)
    reservation.status = ReservationStatus.released
    reservation.settled_at = release_time

    await session.flush()

    return SettlementResult(
        success=True,
        reservation=reservation,
        actual_tokens=0,
        actual_cost_micros=0,
        tokens_released=tokens_released,
        cost_micros_released=cost_micros_released,
        is_idempotent_replay=False,
    )


# =============================================================================
# Free Billing Period Rollover Service
# =============================================================================

FIXED_FREE_PERIOD_DURATION = timedelta(days=30)
FIXED_FREE_PERIOD_SECONDS = int(FIXED_FREE_PERIOD_DURATION.total_seconds())  # 2,592,000s


def evaluate_free_period_rollover(
    current_period_start: datetime,
    current_period_end: datetime,
    now: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """Evaluate whether a Free billing period requires rollover.

    Guarantees:
    1. Active Period Guard: If now < current_period_end, returns None (no rollover required).
    2. Exact Boundary Rollover: At or after expiry, calculates the currently applicable 30-day window
       anchored to the original anchor boundary without date drift.
    3. Intermediate Period Skipping: If dormant for multiple cycles, intermediate inactive periods
       are not created.
    4. Timezone Awareness: Operates strictly with timezone-aware UTC timestamps.

    Args:
        current_period_start: UTC timestamp of current active period start.
        current_period_end: UTC timestamp of current active period end.
        now: Optional current UTC timestamp (defaults to datetime.now(timezone.utc)).

    Returns:
        tuple[datetime, datetime]: (new_period_start, new_period_end) if rollover is required.
        None: If current period is still active (no rollover required).
    """
    now = now or datetime.now(timezone.utc)

    # 1. Active Period Guard: No rollover if current period has not ended
    if now < current_period_end:
        return None

    # 2. At or after boundary: calculate elapsed seconds past current_period_end
    elapsed_seconds = (now - current_period_end).total_seconds()

    # 3. Calculate integer cycles skipped since current_period_end
    #    k = 0 when now is within the immediate next cycle [current_period_end, current_period_end + 30d)
    #    k > 0 when one or more full 30-day cycles were dormant
    cycles_skipped = int(elapsed_seconds // FIXED_FREE_PERIOD_SECONDS)

    # 4. Anchor next period start to the original anchor boundary
    new_period_start = current_period_end + timedelta(seconds=cycles_skipped * FIXED_FREE_PERIOD_SECONDS)
    new_period_end = new_period_start + FIXED_FREE_PERIOD_DURATION

    return new_period_start, new_period_end


async def rollover_free_billing_period_if_needed(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    now: datetime | None = None,
) -> OrganizationBillingPeriod | None:
    """Atomically roll over an expired Free billing period under database row lock.

    Guarantees:
    1. Free-Tier Scoped: Never modifies or rolls over paid Pro subscription periods.
    2. Concurrency Safety: Serializes concurrent rollover attempts using SELECT ... FOR UPDATE.
    3. Double-Rollover Prevention: Re-evaluates rollover eligibility under lock.
    4. Historical Immutability: Marks previous period inactive without modifying its usage counters.
    5. In-flight Reservation Safety: Existing reservations retain their immutable billing_period_id.

    Args:
        session: Active SQLAlchemy async session.
        organization_id: Organization owning the billing period.
        now: Optional override for current timestamp.

    Returns:
        OrganizationBillingPeriod: The currently active billing period (newly rolled over or existing),
        or None if no active billing period exists.
    """
    now = now or datetime.now(timezone.utc)

    # 1. Acquire row lock on active period with bounded retry for concurrent deactivations.
    # Concurrency consideration (PostgreSQL READ COMMITTED row locking):
    # If Worker A and Worker B concurrently attempt to lock an expired active period P1:
    # - Worker A acquires the lock on P1, evaluates rollover, sets P1.is_active = False,
    #   inserts new active period P2, flushes, and commits.
    # - Worker B was blocked waiting for P1's lock. When Worker A commits, Worker B unblocks.
    #   Under PostgreSQL READ COMMITTED row-level locking, Worker B re-evaluates the query's
    #   WHERE clause against the updated P1. Since P1.is_active is now False, P1 no longer
    #   matches the filter. PostgreSQL skips the row and does NOT automatically re-scan for
    #   newly inserted rows like P2. Thus, Worker B's initial query returns 0 rows (None).
    #
    # To prevent dropping valid requests, we use a bounded 2-attempt loop:
    # If the first attempt returns None, we re-execute the fresh SELECT query under lock.
    # In READ COMMITTED mode, this second attempt takes a fresh command snapshot, locating
    # the newly committed active period P2. Worker B locks P2, determines that P2 is fresh
    # (evaluate_free_period_rollover returns None), and returns P2 safely.
    #
    # Lock Ordering: Maintains global lock order (OrganizationBillingPeriod FIRST).
    # Avoids introducing an Organization row lock to prevent deadlock with other workflows.
    current_period = None
    for _ in range(2):
        stmt = (
            select(OrganizationBillingPeriod)
            .where(
                OrganizationBillingPeriod.organization_id == organization_id,
                OrganizationBillingPeriod.is_active == True,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        current_period = result.scalar_one_or_none()
        if current_period is not None:
            break

    if not current_period:
        return None

    # Paid Pro periods are renewed strictly via Stripe webhooks, not Free rolling logic
    if current_period.plan_tier != "free":
        return current_period

    # 2. Evaluate rollover under lock (guards against concurrent worker races)
    boundaries = evaluate_free_period_rollover(
        current_period.period_start, current_period.period_end, now
    )
    if boundaries is None:
        # Current period is still active or another concurrent worker already rolled it over
        return current_period

    new_start, new_end = boundaries

    # 3. Deactivate expired period (historical record preserved)
    current_period.is_active = False

    # 4. Insert currently applicable active period
    new_period = OrganizationBillingPeriod(
        organization_id=organization_id,
        period_start=new_start,
        period_end=new_end,
        plan_tier="free",
        monthly_token_quota=settings.default_free_monthly_token_quota,
        tokens_used=0,
        tokens_reserved=0,
        cost_micros_used=0,
        cost_micros_reserved=0,
        is_active=True,
        is_blocked=False,
    )
    session.add(new_period)
    await session.flush()

    return new_period
