"""Deterministic mock LLM provider for development and automated testing.

Identified strictly as a development and test implementation. Must never silently
run in production environments.
"""

import asyncio
from typing import Any

from app.config import settings
from app.services.llm.base import (
    BaseLLMProvider,
    LLMConfigurationError,
    LLMMessage,
    LLMProviderError,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
)


class MockLLMProvider(BaseLLMProvider):
    """Deterministic, configurable mock LLM provider for testing and local dev.

    Guarantees:
    - Zero external network requests.
    - Zero API keys required.
    - Deterministic replies for reproducible integration tests.
    - Strict rejection if instantiated in production unless explicitly allowed.
    """

    def __init__(
        self,
        model_name: str = "mock-gpt-4o-mini",
        canned_response: str | None = None,
        force_timeout: bool = False,
        force_error: str | None = None,
        simulated_delay: float = 0.0,
        allow_in_production: bool = False,
    ):
        is_prod = settings.environment.strip().lower() in ("production", "prod")
        if is_prod and not allow_in_production and not settings.llm_allow_mock_in_production:
            raise LLMConfigurationError(
                "MockLLMProvider is strictly for development and automated testing. "
                "It is prohibited from running in production environments."
            )

        self.model_name = model_name
        self.canned_response = canned_response
        self.force_timeout = force_timeout
        self.force_error = force_error
        self.simulated_delay = simulated_delay
        self.call_count = 0
        self.last_prompt_messages: list[LLMMessage] = []
        self.last_system_prompt: str | None = None

    async def generate_reply(
        self,
        messages: list[LLMMessage],
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout_seconds: float = 10.0,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_prompt_messages = list(messages)
        self.last_system_prompt = system_prompt

        # 1. Simulated delay
        if self.simulated_delay > 0:
            if self.simulated_delay >= timeout_seconds or self.force_timeout:
                await asyncio.sleep(min(self.simulated_delay, 0.05))  # Keep tests fast
                raise LLMTimeoutError(
                    f"Mock generation exceeded timeout of {timeout_seconds}s"
                )
            await asyncio.sleep(self.simulated_delay)

        # 2. Simulated explicit timeout
        if self.force_timeout:
            raise LLMTimeoutError(
                f"Mock generation forcibly timed out after {timeout_seconds}s"
            )

        # 3. Simulated upstream provider error
        if self.force_error:
            raise LLMProviderError(f"Mock upstream failure: {self.force_error}")

        # 4. Extract latest user query for context
        user_queries = [m.content for m in messages if m.role == "user"]
        last_user_query = user_queries[-1] if user_queries else "Hello"

        # 5. Build response content
        if self.canned_response:
            content = self.canned_response
        else:
            truncated_query = last_user_query[:80].strip()
            content = f"[MOCK AI] I received your inquiry: \"{truncated_query}\". How can I assist you further?"

        # 6. Bound output to max_tokens approximation (approx 4 chars per token)
        max_chars = max_tokens * 4
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"

        # 7. Token accounting approximation
        prompt_chars = sum(len(m.content) for m in messages) + len(system_prompt or "")
        prompt_tokens = max(1, prompt_chars // 4)
        completion_tokens = max(1, len(content) // 4)

        return LLMResponse(
            content=content,
            model=self.model_name,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
        )
