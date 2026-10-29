"""Durable AI generation orchestration service coordinating tasks, leases, attempts, and accounting.

Guarantees & Architecture:
1. Two-Phase Transaction Boundaries:
   - Transaction 1: Pre-inference quota reservation, attempt allocation ('claimed'), and
     durable provider dispatch commitment ('dispatched') are COMMITTED before any provider I/O.
   - Zero Database Locks Across Network I/O: The provider call runs outside any open DB transaction.
   - Transaction 2: Post-inference worker fencing verification, usage settlement against the
     active reservation, reply persistence with client_message_id idempotency, and terminal state transitions.
2. Worker Fencing:
   - Every attempt records the worker's unique lease_token.
   - If a lease expires and another worker acquires the task, the stale worker is fenced
     upon return (status -> 'stale_received') and cannot settle usage or persist messages.
3. Ambiguity Quarantine:
   - Once a dispatched attempt is committed, timeouts, network drops, or worker crashes
     quarantine the task to 'reconciliation_needed' and attempt to 'ambiguous'.
   - The usage reservation remains active; in-flight provider liabilities are never silently released.
4. Pre-Dispatch Safe Release:
   - If failure occurs before dispatch commitment, the attempt is marked 'failed' and the
     reservation is cleanly returned to the billing period via release_reservation.
5. Idempotent Reply Retrieval:
   - If a visitor message was already answered, returns the existing assistant message
     immediately without re-invoking the provider or creating duplicate charges.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import enum
import logging
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.billing import (
    LLMUsageLog,
    OrganizationBillingPeriod,
    ReservationStatus,
    UsageReservation,
)
from app.models.chatbot import Chatbot, ChatbotStatus
from app.models.conversation import Message, MessageSenderType
from app.models.generation import GenerationTask, GenerationTaskStatus
from app.models.generation_attempt import GenerationAttemptStatus, GenerationTaskAttempt
from app.schemas.conversation import GenerationStatus
from app.services.billing.accounting import (
    AccountingErrorCode,
    release_reservation,
    reserve_usage,
    settle_reservation,
)
from app.services.billing.pricing import (
    MOCK_GPT_4O_MINI_PRICING,
    ModelPricing,
    calculate_turn_cost,
    get_model_pricing,
)
from app.services.llm.base import (
    BaseLLMProvider,
    LLMConfigurationError,
    LLMError,
    LLMProviderError,
    LLMResponse,
    LLMTimeoutError,
)
from app.services.llm.context import build_conversation_context
from app.services.llm.service import get_llm_provider

logger = logging.getLogger(__name__)


class OrchestratorErrorCode(str, enum.Enum):
    """Categorized outcome codes for durable generation orchestration."""

    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SPENDING_CAP_EXHAUSTED = "SPENDING_CAP_EXHAUSTED"
    NO_ACTIVE_BILLING_PERIOD = "NO_ACTIVE_BILLING_PERIOD"
    TASK_ALREADY_COMPLETED = "TASK_ALREADY_COMPLETED"
    TASK_IN_PROGRESS = "TASK_IN_PROGRESS"
    TASK_QUARANTINED = "TASK_QUARANTINED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    PRE_DISPATCH_FAILURE = "PRE_DISPATCH_FAILURE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    STALE_LEASE_FENCED = "STALE_LEASE_FENCED"
    SETTLEMENT_FAILED = "SETTLEMENT_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_OWNERSHIP = "INVALID_OWNERSHIP"


@dataclass(frozen=True)
class OrchestrationResult:
    """Detailed typed outcome of an orchestrated generation turn."""

    status: GenerationStatus
    reply_message: Message | None = None
    task: GenerationTask | None = None
    attempt: GenerationTaskAttempt | None = None
    usage_log: LLMUsageLog | None = None
    tokens_used: int = 0
    cost_micros: int = 0
    error_code: OrchestratorErrorCode | None = None
    error_message: str | None = None
    is_idempotent_replay: bool = False


async def orchestrate_assistant_reply(
    db: AsyncSession,
    *,
    chatbot: Chatbot,
    conversation_id: uuid.UUID,
    visitor_message: Message,
    pricing: ModelPricing | None = None,
    provider: BaseLLMProvider | None = None,
    lease_duration_seconds: int = 60,
    max_task_attempts: int = 3,
    now: datetime | None = None,
) -> OrchestrationResult:
    """Coordinate reliable, durable, and fenced AI generation and settlement.

    Args:
        db: Active SQLAlchemy async session.
        chatbot: Chatbot associated with the conversation.
        conversation_id: Active conversation identifier.
        visitor_message: The visitor message requesting an assistant reply.
        pricing: Model pricing configuration (defaults to mock model pricing).
        provider: Provider instance (defaults to configured mock provider).
        lease_duration_seconds: Worker lease duration before failover is permitted.
        max_task_attempts: Maximum provider execution attempts per task.
        now: Optional datetime override for deterministic testing.

    Returns:
        OrchestrationResult: Complete outcome details.
    """
    current_time = now or datetime.now(timezone.utc)
    correlation_key = f"reply:{visitor_message.id}"

    # -------------------------------------------------------------------------
    # PHASE 1: Idempotency & Task Acquisition (Transaction 1)
    # -------------------------------------------------------------------------

    # 1. Preliminary Check: Has this visitor message already been answered?
    stmt_existing_reply = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.client_message_id == correlation_key,
    )
    existing_reply = (await db.execute(stmt_existing_reply)).scalar_one_or_none()
    if existing_reply is not None:
        # Load task if available
        stmt_task = select(GenerationTask).where(
            GenerationTask.visitor_message_id == visitor_message.id
        )
        task = (await db.execute(stmt_task)).scalar_one_or_none()
        return OrchestrationResult(
            status=GenerationStatus.completed,
            reply_message=existing_reply,
            task=task,
            is_idempotent_replay=True,
        )

    # 2. Provider Disabled Safeguard
    if settings.llm_provider.strip().lower() == "disabled":
        return OrchestrationResult(
            status=GenerationStatus.skipped,
            reply_message=None,
        )

    # 3. Resolve Pricing Configuration
    resolved_pricing: ModelPricing
    if pricing is not None:
        resolved_pricing = pricing
    else:
        try:
            resolved_pricing = get_model_pricing(settings.llm_model)
        except Exception:
            resolved_pricing = MOCK_GPT_4O_MINI_PRICING

    # 4. Resolve Active Billing Period for Tenant
    stmt_bp = select(OrganizationBillingPeriod).where(
        OrganizationBillingPeriod.organization_id == chatbot.organization_id,
        OrganizationBillingPeriod.is_active == True,
    )
    res_bp = await db.execute(stmt_bp)
    billing_period = res_bp.scalar_one_or_none()
    if billing_period is None:
        return OrchestrationResult(
            status=GenerationStatus.failed,
            error_code=OrchestratorErrorCode.NO_ACTIVE_BILLING_PERIOD,
            error_message="No active billing period found for organization",
        )

    # 5. Acquire or Create GenerationTask with Lease Fencing
    worker_lease_token = uuid.uuid4()
    lease_expires = current_time + timedelta(seconds=lease_duration_seconds)

    stmt_task = (
        select(GenerationTask)
        .where(GenerationTask.visitor_message_id == visitor_message.id)
        .with_for_update()
    )
    res_task = await db.execute(stmt_task)
    task = res_task.scalar_one_or_none()

    if task is None:
        task = GenerationTask(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            visitor_message_id=visitor_message.id,
            organization_id=chatbot.organization_id,
            billing_period_id=billing_period.id,
            status=GenerationTaskStatus.pending,
            lease_token=worker_lease_token,
            lease_version=1,
            lease_expires_at=lease_expires,
            attempts=0,
            max_attempts=max_task_attempts,
        )
        db.add(task)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent worker created it first: catch conflict and load winner's task
            await db.rollback()
            res_winner = await db.execute(stmt_task)
            task = res_winner.scalar_one_or_none()
            if task is None:
                raise RuntimeError("Failed to resolve concurrently created generation task")

    # If task exists, validate ownership and lease
    if task.organization_id != chatbot.organization_id or task.conversation_id != conversation_id:
        return OrchestrationResult(
            status=GenerationStatus.failed,
            error_code=OrchestratorErrorCode.INVALID_OWNERSHIP,
            error_message="Generation task tenant or conversation mismatch",
        )

    if task.status == GenerationTaskStatus.completed:
        # Task was completed by another worker; retrieve answer
        res_completed_reply = await db.execute(stmt_existing_reply)
        completed_msg = res_completed_reply.scalar_one_or_none()
        return OrchestrationResult(
            status=GenerationStatus.completed,
            reply_message=completed_msg,
            task=task,
            is_idempotent_replay=True,
        )

    if task.status == GenerationTaskStatus.reconciliation_needed:
        return OrchestrationResult(
            status=GenerationStatus.failed,
            task=task,
            error_code=OrchestratorErrorCode.TASK_QUARANTINED,
            error_message="Task is quarantined in reconciliation_needed state; provider retry blocked",
        )

    if task.status != GenerationTaskStatus.pending:
        return OrchestrationResult(
            status=GenerationStatus.failed,
            task=task,
            error_code=OrchestratorErrorCode.TASK_IN_PROGRESS,
            error_message=f"Task is in non-pending state: {task.status.value}",
        )

    # If task already exists and is pending, check lease
    if task.id != getattr(task, "_is_just_created", None) and task.lease_token != worker_lease_token:
        if task.lease_expires_at > current_time:
            # Task actively leased by another live worker
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task,
                error_code=OrchestratorErrorCode.TASK_IN_PROGRESS,
                error_message="Task is actively leased by another worker",
            )
        else:
            # Lease expired: before takeover, inspect existing attempts for ambiguous/dispatched states
            stmt_existing_attempts = (
                select(GenerationTaskAttempt)
                .where(GenerationTaskAttempt.generation_task_id == task.id)
                .with_for_update()
            )
            res_existing_attempts = await db.execute(stmt_existing_attempts)
            existing_attempts = res_existing_attempts.scalars().all()

            # MANDATORY INVARIANT: A dispatched attempt cannot become retryable merely
            # because its worker lease expired. The provider request may have been processed.
            # It is ambiguous and MUST be quarantined!
            in_flight_dispatched = next(
                (a for a in existing_attempts if a.status == GenerationAttemptStatus.dispatched),
                None,
            )
            if in_flight_dispatched is not None:
                logger.warning(
                    "Task %s has dispatched attempt %s with expired lease; quarantining to reconciliation_needed",
                    task.id,
                    in_flight_dispatched.attempt_number,
                )
                in_flight_dispatched.status = GenerationAttemptStatus.ambiguous
                in_flight_dispatched.error_message = (
                    "Worker lease expired while attempt was in dispatched state"
                )
                task.status = GenerationTaskStatus.reconciliation_needed
                task.last_error = (
                    f"Attempt {in_flight_dispatched.attempt_number} was dispatched "
                    "but worker lease expired without recorded outcome"
                )
                await db.commit()
                return OrchestrationResult(
                    status=GenerationStatus.failed,
                    task=task,
                    attempt=in_flight_dispatched,
                    error_code=OrchestratorErrorCode.TASK_QUARANTINED,
                    error_message="Task quarantined: previous worker lease expired during provider dispatch",
                )

            # Check for existing ambiguous attempt
            ambiguous_attempt = next(
                (a for a in existing_attempts if a.status == GenerationAttemptStatus.ambiguous),
                None,
            )
            if ambiguous_attempt is not None:
                task.status = GenerationTaskStatus.reconciliation_needed
                task.last_error = (
                    f"Task quarantined due to ambiguous attempt {ambiguous_attempt.attempt_number}"
                )
                await db.commit()
                return OrchestrationResult(
                    status=GenerationStatus.failed,
                    task=task,
                    attempt=ambiguous_attempt,
                    error_code=OrchestratorErrorCode.TASK_QUARANTINED,
                    error_message="Task is quarantined in reconciliation_needed state; provider retry blocked",
                )

            # Clean up any abandoned 'claimed' attempts (worker crashed before dispatch was committed)
            for a in existing_attempts:
                if a.status == GenerationAttemptStatus.claimed:
                    a.status = GenerationAttemptStatus.failed
                    a.error_message = "Previous worker crashed before dispatch commitment"

            # Safe to take over expired lease
            task.lease_token = worker_lease_token
            task.lease_version += 1
            task.lease_expires_at = lease_expires
            await db.flush()

    # -------------------------------------------------------------------------
    # PHASE 2: Quota & Turn Cost Reservation
    # -------------------------------------------------------------------------

    stmt_res = select(UsageReservation).where(
        UsageReservation.generation_task_id == task.id,
        UsageReservation.status == ReservationStatus.active,
    )
    active_reservation = (await db.execute(stmt_res)).scalar_one_or_none()

    if active_reservation is None:
        reserve_result = await reserve_usage(
            db,
            organization_id=chatbot.organization_id,
            task_id=task.id,
            billing_period_id=task.billing_period_id,
            pricing=resolved_pricing,
            now=current_time,
        )
        if not reserve_result.success:
            task.status = GenerationTaskStatus.failed
            task.last_error = f"Reservation failed: {reserve_result.message}"
            await db.commit()
            err_code = OrchestratorErrorCode.QUOTA_EXHAUSTED
            if reserve_result.error_code == AccountingErrorCode.SPENDING_CAP_EXHAUSTED:
                err_code = OrchestratorErrorCode.SPENDING_CAP_EXHAUSTED
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task,
                error_code=err_code,
                error_message=reserve_result.message,
            )
        active_reservation = reserve_result.reservation

    # -------------------------------------------------------------------------
    # PHASE 3: Attempt Allocation ('claimed')
    # -------------------------------------------------------------------------

    stmt_max_attempt = select(
        func.coalesce(func.max(GenerationTaskAttempt.attempt_number), 0)
    ).where(GenerationTaskAttempt.generation_task_id == task.id)
    current_max_num = (await db.execute(stmt_max_attempt)).scalar() or 0
    next_attempt_number = current_max_num + 1

    if next_attempt_number > task.max_attempts:
        if active_reservation is not None:
            await release_reservation(
                db,
                reservation_id=active_reservation.id,
                organization_id=chatbot.organization_id,
                now=current_time,
            )
        task.status = GenerationTaskStatus.failed
        task.last_error = f"Exceeded maximum attempt limit ({task.max_attempts})"
        await db.commit()
        return OrchestrationResult(
            status=GenerationStatus.failed,
            task=task,
            error_code=OrchestratorErrorCode.MAX_ATTEMPTS_EXCEEDED,
            error_message=f"Exceeded max attempts ({task.max_attempts})",
        )

    attempt = GenerationTaskAttempt(
        id=uuid.uuid4(),
        generation_task_id=task.id,
        attempt_number=next_attempt_number,
        lease_token=task.lease_token,
        status=GenerationAttemptStatus.claimed,
        provider=resolved_pricing.model_name.split("-")[0],
        model=resolved_pricing.model_name,
    )
    task.attempts = next_attempt_number
    db.add(attempt)
    await db.flush()

    # -------------------------------------------------------------------------
    # PHASE 4: Pre-Dispatch Context & Validation
    # -------------------------------------------------------------------------

    try:
        context_messages = await build_conversation_context(
            db=db,
            chatbot=chatbot,
            conversation_id=conversation_id,
            current_message=visitor_message,
            max_context_messages=settings.llm_max_context_messages,
        )
    except Exception as exc:
        logger.error("Pre-dispatch context building error: %s", exc)
        attempt.status = GenerationAttemptStatus.failed
        attempt.error_type = type(exc).__name__
        attempt.error_message = str(exc)
        if active_reservation is not None:
            await release_reservation(
                db,
                reservation_id=active_reservation.id,
                organization_id=chatbot.organization_id,
                now=current_time,
            )
        task.status = GenerationTaskStatus.failed
        task.last_error = f"Pre-dispatch context error: {exc}"
        await db.commit()
        return OrchestrationResult(
            status=GenerationStatus.failed,
            task=task,
            attempt=attempt,
            error_code=OrchestratorErrorCode.PRE_DISPATCH_FAILURE,
            error_message=str(exc),
        )

    # -------------------------------------------------------------------------
    # PHASE 5: Durable Dispatch Marker & Transaction 1 COMMIT
    # -------------------------------------------------------------------------

    # Fencing assertion: Ensure lease has not expired or mutated before committing dispatch
    if task.lease_token != worker_lease_token or task.lease_expires_at <= current_time:
        attempt.status = GenerationAttemptStatus.failed
        attempt.error_message = "Worker lease expired before provider dispatch commitment"
        if active_reservation is not None:
            await release_reservation(
                db,
                reservation_id=active_reservation.id,
                organization_id=chatbot.organization_id,
                now=current_time,
            )
        task.status = GenerationTaskStatus.failed
        task.last_error = "Worker lease expired prior to dispatch"
        await db.commit()
        return OrchestrationResult(
            status=GenerationStatus.failed,
            task=task,
            attempt=attempt,
            error_code=OrchestratorErrorCode.STALE_LEASE_FENCED,
            error_message="Worker lease expired before provider dispatch could be committed",
        )

    if attempt.status != GenerationAttemptStatus.claimed:
        raise RuntimeError(f"Invalid prior attempt state for dispatch: {attempt.status.value}")

    attempt.status = GenerationAttemptStatus.dispatched
    attempt.dispatched_at = current_time

    # COMMIT Transaction 1: Dispatch marker is now durably persisted in database.
    # From this point forward, the reservation CANNOT be released without reconciliation.
    await db.commit()

    # -------------------------------------------------------------------------
    # PHASE 6: LLM Provider Invocation (OUTSIDE Database Transaction)
    # -------------------------------------------------------------------------

    active_provider = provider or get_llm_provider()
    llm_response: LLMResponse | None = None
    provider_error: Exception | None = None

    try:
        llm_response = await active_provider.generate_reply(
            messages=context_messages,
            system_prompt=chatbot.instructions,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    except Exception as exc:
        provider_error = exc

    # -------------------------------------------------------------------------
    # PHASE 7: Fencing Check & Settlement (Transaction 2)
    # -------------------------------------------------------------------------

    task2 = task
    attempt2 = attempt
    actual_cost_micros = 0

    try:
        # Refresh task and attempt state in Transaction 2
        res_task2 = await db.execute(
            select(GenerationTask).where(GenerationTask.id == task.id).with_for_update()
        )
        task2 = res_task2.scalar_one_or_none() or task

        res_attempt2 = await db.execute(
            select(GenerationTaskAttempt).where(GenerationTaskAttempt.id == attempt.id).with_for_update()
        )
        attempt2 = res_attempt2.scalar_one_or_none() or attempt

        # Fencing Guard: Did another worker take over the lease while we were calling the provider?
        if task2.lease_token != worker_lease_token:
            logger.warning(
                "Worker fence: lease lost for task %s (expected %s, found %s)",
                task2.id,
                worker_lease_token,
                task2.lease_token,
            )
            attempt2.status = GenerationAttemptStatus.stale_received
            attempt2.error_message = "Worker lease expired; fenced from settlement"
            await db.commit()
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task2,
                attempt=attempt2,
                error_code=OrchestratorErrorCode.STALE_LEASE_FENCED,
                error_message="Worker was fenced due to expired lease token",
            )

        # Branch A: Provider Call Timed Out or Dropped (Ambiguous Outcome)
        if isinstance(provider_error, LLMTimeoutError):
            logger.warning("Provider timed out after dispatch commitment for task %s", task2.id)
            attempt2.status = GenerationAttemptStatus.ambiguous
            attempt2.error_type = "LLMTimeoutError"
            attempt2.error_message = str(provider_error)
            task2.status = GenerationTaskStatus.reconciliation_needed
            task2.last_error = f"Provider request timed out after dispatch: {provider_error}"
            # Reservation liability is PRESERVED; never released
            await db.commit()
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task2,
                attempt=attempt2,
                error_code=OrchestratorErrorCode.PROVIDER_TIMEOUT,
                error_message="The AI assistant timed out while generating a response. Quarantined for reconciliation.",
            )

        # Branch B: General Provider Error
        if provider_error is not None:
            is_definite = (
                isinstance(provider_error, LLMConfigurationError)
                or getattr(provider_error, "is_definite_rejection", False) is True
            )

            if is_definite:
                logger.warning(
                    "Definite provider rejection for task %s; safely releasing reservation: %s",
                    task2.id,
                    provider_error,
                )
                attempt2.status = GenerationAttemptStatus.failed
                attempt2.error_type = type(provider_error).__name__
                attempt2.error_message = str(provider_error)
                task2.status = GenerationTaskStatus.failed
                task2.last_error = str(provider_error)

                # Safe Release: Provider contract guarantees zero billable work occurred
                res_active_res = await db.execute(
                    select(UsageReservation).where(
                        UsageReservation.generation_task_id == task2.id,
                        UsageReservation.status == ReservationStatus.active,
                    )
                )
                current_reservation = res_active_res.scalar_one_or_none()
                if current_reservation is not None:
                    release_res = await release_reservation(
                        db,
                        reservation_id=current_reservation.id,
                        organization_id=chatbot.organization_id,
                        now=current_time,
                    )
                    if not release_res.success:
                        logger.error(
                            "Failed to release reservation after definite rejection for task %s: %s; quarantining",
                            task2.id,
                            release_res.message,
                        )
                        task2.status = GenerationTaskStatus.reconciliation_needed
                        task2.last_error = (
                            f"Definite provider rejection but reservation release failed: {release_res.message}"
                        )
                await db.commit()
                return OrchestrationResult(
                    status=GenerationStatus.failed,
                    task=task2,
                    attempt=attempt2,
                    error_code=OrchestratorErrorCode.PROVIDER_ERROR,
                    error_message="The AI assistant is temporarily unavailable. Your message was saved.",
                )
            else:
                # Uncertain liability: In-flight execution or token billing cannot be ruled out.
                # Retain reservation and quarantine task/attempt to reconciliation_needed/ambiguous.
                logger.warning(
                    "Uncertain provider failure for task %s after dispatch; quarantining to reconciliation_needed: %s",
                    task2.id,
                    provider_error,
                )
                attempt2.status = GenerationAttemptStatus.ambiguous
                attempt2.error_type = type(provider_error).__name__
                attempt2.error_message = str(provider_error)
                task2.status = GenerationTaskStatus.reconciliation_needed
                task2.last_error = f"Provider failure with uncertain liability: {provider_error}"
                # Reservation liability is retained; never released without reconciliation
                await db.commit()
                return OrchestrationResult(
                    status=GenerationStatus.failed,
                    task=task2,
                    attempt=attempt2,
                    error_code=OrchestratorErrorCode.TASK_QUARANTINED,
                    error_message="The AI assistant encountered an uncertain provider failure. Quarantined for reconciliation.",
                )

        # Branch C: Successful Provider Output -> Settlement & Message Persistence
        assert llm_response is not None

        p_tokens = llm_response.usage.prompt_tokens
        c_tokens = llm_response.usage.completion_tokens
        total_tokens = p_tokens + c_tokens

        cost_estimate = calculate_turn_cost(
            pricing=resolved_pricing,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
        )
        actual_cost_micros = cost_estimate.total_cost_micros

        # Re-verify active reservation
        res_active_res = await db.execute(
            select(UsageReservation).where(
                UsageReservation.generation_task_id == task2.id,
                UsageReservation.status == ReservationStatus.active,
            )
        )
        current_reservation = res_active_res.scalar_one_or_none()
        if current_reservation is None:
            task2.status = GenerationTaskStatus.reconciliation_needed
            task2.last_error = "Active usage reservation missing at post-inference settlement"
            await db.commit()
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task2,
                attempt=attempt2,
                error_code=OrchestratorErrorCode.SETTLEMENT_FAILED,
                error_message="Active reservation missing at settlement time",
            )

        # Settle Reservation
        settle_result = await settle_reservation(
            db,
            reservation_id=current_reservation.id,
            organization_id=chatbot.organization_id,
            actual_tokens=total_tokens,
            actual_cost_micros=actual_cost_micros,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            pricing=resolved_pricing,
            now=current_time,
        )

        if not settle_result.success:
            # MANDATORY INVARIANT: If settlement fails after provider success, preserve liability
            # and route task to reconciliation_needed; never repeat provider call!
            logger.error("Post-inference settlement failed for task %s: %s", task2.id, settle_result.message)
            attempt2.status = GenerationAttemptStatus.completed
            attempt2.response_content = llm_response.content
            attempt2.prompt_tokens = p_tokens
            attempt2.completion_tokens = c_tokens
            attempt2.cost_micros = actual_cost_micros
            task2.status = GenerationTaskStatus.reconciliation_needed
            task2.last_error = f"Settlement failed after provider output: {settle_result.message}"
            await db.commit()
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=task2,
                attempt=attempt2,
                error_code=OrchestratorErrorCode.SETTLEMENT_FAILED,
                error_message=f"Post-inference settlement failed: {settle_result.message}",
            )

        # Update attempt state to completed
        attempt2.status = GenerationAttemptStatus.completed
        attempt2.completed_at = current_time
        attempt2.response_content = llm_response.content
        attempt2.prompt_tokens = p_tokens
        attempt2.completion_tokens = c_tokens
        attempt2.cost_micros = actual_cost_micros
        attempt2.latency_ms = getattr(llm_response, "latency_ms", 0)
        attempt2.provider_request_id = getattr(llm_response, "request_id", None)

        # Persist Assistant Reply Message
        assistant_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            sender_type=MessageSenderType.assistant,
            content=llm_response.content,
            client_message_id=correlation_key,
        )
        db.add(assistant_msg)

        task2.status = GenerationTaskStatus.completed

        try:
            await db.commit()
            await db.refresh(assistant_msg)
            return OrchestrationResult(
                status=GenerationStatus.completed,
                reply_message=assistant_msg,
                task=task2,
                attempt=attempt2,
                usage_log=settle_result.usage_log,
                tokens_used=total_tokens,
                cost_micros=actual_cost_micros,
            )
        except IntegrityError:
            # Concurrent winner inserted client_message_id first
            await db.rollback()
            res_winner_msg = await db.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.client_message_id == correlation_key,
                )
            )
            winner_msg = res_winner_msg.scalar_one_or_none()
            return OrchestrationResult(
                status=GenerationStatus.completed,
                reply_message=winner_msg,
                task=task2,
                attempt=attempt2,
                usage_log=settle_result.usage_log,
                tokens_used=total_tokens,
                cost_micros=actual_cost_micros,
                is_idempotent_replay=True,
            )
    except Exception as exc:
        # Unexpected failure during Transaction 2 settlement / message persistence.
        # Clear aborted transaction via rollback, then in a clean transaction
        # mark task as reconciliation_needed and commit so liability is preserved!
        logger.exception("Unexpected error in Transaction 2 for task %s: %s", task.id, exc)
        try:
            await db.rollback()
            res_t_rec = await db.execute(
                select(GenerationTask).where(GenerationTask.id == task.id).with_for_update()
            )
            t_rec = res_t_rec.scalar_one_or_none() or task
            res_a_rec = await db.execute(
                select(GenerationTaskAttempt).where(GenerationTaskAttempt.id == attempt.id).with_for_update()
            )
            a_rec = res_a_rec.scalar_one_or_none() or attempt

            t_rec.status = GenerationTaskStatus.reconciliation_needed
            t_rec.last_error = f"Transaction 2 failed after provider dispatch: {exc}"
            if llm_response is not None:
                a_rec.status = GenerationAttemptStatus.completed
                a_rec.response_content = llm_response.content
                a_rec.prompt_tokens = getattr(llm_response.usage, "prompt_tokens", 0)
                a_rec.completion_tokens = getattr(llm_response.usage, "completion_tokens", 0)
                a_rec.cost_micros = actual_cost_micros
            else:
                a_rec.status = GenerationAttemptStatus.ambiguous
            a_rec.error_type = type(exc).__name__
            a_rec.error_message = str(exc)
            await db.commit()
            return OrchestrationResult(
                status=GenerationStatus.failed,
                task=t_rec,
                attempt=a_rec,
                error_code=OrchestratorErrorCode.SETTLEMENT_FAILED,
                error_message=f"Transaction 2 failed: {exc}",
            )
        except Exception as recovery_exc:
            logger.error("Failed to commit reconciliation quarantine for task %s: %s", task.id, recovery_exc)
            raise
