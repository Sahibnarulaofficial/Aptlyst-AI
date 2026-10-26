"""Base definitions, schemas, and abstract interface for LLM providers."""

from abc import ABC, abstractmethod
from typing import Literal
from pydantic import BaseModel, Field


class LLMError(Exception):
    """Base exception for all LLM provider errors."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when an LLM provider request exceeds the configured timeout."""
    pass


class LLMProviderError(LLMError):
    """Raised when an LLM provider returns an upstream error or network failure."""
    pass


class LLMConfigurationError(LLMError):
    """Raised when LLM provider configuration is invalid or disallowed."""
    pass


class LLMMessage(BaseModel):
    """A single message turn in the conversation context."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1, max_length=10000)


class LLMUsage(BaseModel):
    """Token usage metadata for an LLM completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(BaseModel):
    """Structured response from an LLM provider."""

    content: str
    model: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    finish_reason: str = "stop"


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers."""

    @abstractmethod
    async def generate_reply(
        self,
        messages: list[LLMMessage],
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout_seconds: float = 10.0,
    ) -> LLMResponse:
        """Generate an AI assistant completion for the given conversation context.

        Args:
            messages: Chronological conversation turns (user and assistant messages).
            system_prompt: Optional instructions scoped to the chatbot.
            temperature: Sampling temperature (0.0 to 1.0).
            max_tokens: Hard ceiling on generated response tokens.
            timeout_seconds: Maximum wall-clock time in seconds before aborting.

        Returns:
            LLMResponse: Structured response including content and token usage.

        Raises:
            LLMTimeoutError: If generation exceeds timeout_seconds.
            LLMProviderError: If upstream provider returns an error.
            LLMConfigurationError: If configuration is invalid.
        """
        pass
