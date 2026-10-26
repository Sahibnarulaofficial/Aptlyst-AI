"""AI generation lifecycle coordinator managing context, invocation, and durable persistence."""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chatbot import Chatbot
from app.models.conversation import Message, MessageSenderType
from app.schemas.conversation import GenerationStatus
from app.services.llm.base import (
    BaseLLMProvider,
    LLMConfigurationError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.services.llm.context import build_conversation_context
from app.services.llm.mock import MockLLMProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIGenerationResult:
    """Outcome of an AI assistant reply generation cycle."""

    status: GenerationStatus
    reply_message: Message | None = None
    error: str | None = None


_global_provider: BaseLLMProvider | None = None


def get_llm_provider() -> BaseLLMProvider:
    """Resolve the active LLM provider based on application configuration."""
    global _global_provider
    if _global_provider is not None:
        return _global_provider

    provider_name = settings.llm_provider.strip().lower()
    if provider_name == "mock":
        _global_provider = MockLLMProvider(
            model_name=settings.llm_model,
            allow_in_production=settings.llm_allow_mock_in_production,
        )
        return _global_provider

    if provider_name == "disabled":
        raise LLMConfigurationError("LLM provider is explicitly disabled.")

    raise LLMConfigurationError(
        f"Unsupported LLM provider '{provider_name}'. Live providers are not enabled in Phase 1."
    )


def set_llm_provider_for_testing(provider: BaseLLMProvider | None) -> None:
    """Override the active provider for testing purposes."""
    global _global_provider
    _global_provider = provider


async def generate_and_persist_assistant_reply(
    db: AsyncSession,
    chatbot: Chatbot,
    conversation_id: uuid.UUID,
    visitor_message: Message,
    provider: BaseLLMProvider | None = None,
) -> AIGenerationResult:
    """Coordinate the generation and durable persistence of an AI assistant reply.

    Guarantees:
    1. Idempotency: Uses client_message_id = f"reply:{visitor_message.id}". If a reply
       already exists, returns it immediately without re-invoking the provider.
    2. Durable Correlation: Explicit 1:1 foreign link via client_message_id; does not
       rely on timestamps or chronological adjacency.
    3. Failure Isolation: If the provider times out or fails, the visitor message remains
       intact and committed. Generation failure is recorded unambiguously.
    4. Concurrency Safety: Handled via PostgreSQL unique constraint
       `uq_messages_conversation_client_id`. If concurrent workers generate replies,
       the loser catches IntegrityError and retrieves the winner's reply.
    """
    correlation_key = f"reply:{visitor_message.id}"

    # 1. Preliminary check: Has this visitor message already been answered?
    existing_stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.client_message_id == correlation_key,
    )
    existing_reply = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing_reply is not None:
        return AIGenerationResult(
            status=GenerationStatus.completed,
            reply_message=existing_reply,
        )

    # 2. Check if provider is disabled
    if settings.llm_provider.strip().lower() == "disabled":
        return AIGenerationResult(
            status=GenerationStatus.skipped,
            reply_message=None,
        )

    # 3. Resolve provider
    active_provider = provider or get_llm_provider()

    # 4. Build prompt context (isolated to this conversation)
    context_messages = await build_conversation_context(
        db=db,
        chatbot=chatbot,
        conversation_id=conversation_id,
        current_message=visitor_message,
        max_context_messages=settings.llm_max_context_messages,
    )

    # 5. Invoke LLM provider with explicit timeout & error handling
    try:
        llm_response = await active_provider.generate_reply(
            messages=context_messages,
            system_prompt=chatbot.instructions,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    except LLMTimeoutError as exc:
        logger.warning(
            "AI generation timed out for conversation %s, visitor message %s: %s",
            conversation_id,
            visitor_message.id,
            exc,
        )
        return AIGenerationResult(
            status=GenerationStatus.failed,
            reply_message=None,
            error="The AI assistant timed out while generating a response. Your message was saved.",
        )
    except (LLMProviderError, LLMError) as exc:
        logger.error(
            "AI provider error for conversation %s, visitor message %s: %s",
            conversation_id,
            visitor_message.id,
            exc,
        )
        return AIGenerationResult(
            status=GenerationStatus.failed,
            reply_message=None,
            error="The AI assistant is temporarily unavailable. Your message was saved.",
        )
    except Exception as exc:
        logger.exception(
            "Unexpected error during AI generation for conversation %s, visitor message %s: %s",
            conversation_id,
            visitor_message.id,
            exc,
        )
        return AIGenerationResult(
            status=GenerationStatus.failed,
            reply_message=None,
            error="An unexpected error occurred during AI generation. Your message was saved.",
        )

    # 6. Persist assistant reply with explicit correlation key
    assistant_msg = Message(
        conversation_id=conversation_id,
        sender_type=MessageSenderType.assistant,
        content=llm_response.content,
        client_message_id=correlation_key,
    )
    db.add(assistant_msg)

    try:
        await db.commit()
        await db.refresh(assistant_msg)
        return AIGenerationResult(
            status=GenerationStatus.completed,
            reply_message=assistant_msg,
        )
    except IntegrityError:
        # Concurrent insert of identical correlation_key won the race
        await db.rollback()
        winning_stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.client_message_id == correlation_key,
        )
        winning_reply = (await db.execute(winning_stmt)).scalar_one_or_none()
        if winning_reply is not None:
            return AIGenerationResult(
                status=GenerationStatus.completed,
                reply_message=winning_reply,
            )
        # Re-raise if failure was caused by another constraint
        raise
