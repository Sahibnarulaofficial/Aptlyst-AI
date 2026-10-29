"""Pure, deterministic token and usage pricing calculations.

Guarantees:
1. Strict integer arithmetic: all monetary amounts are stored and calculated in micro-dollars ($1e-6 USD).
   One dollar ($1.00 USD) equals 1,000,000 micro-dollars.
2. Conservative ceiling rounding: fractional micro-dollar costs are rounded up to the next integer
   micro-dollar using pure integer math: `(tokens * rate + 999_999) // 1_000_000`.
3. Strict input validation: rejects booleans, negative numbers, floats, strings, and out-of-range values.
4. Independent domain logic: zero dependencies on FastAPI, SQLAlchemy, or external I/O.
5. Explicit mock rates: mock models use explicit test-accounting rates; real provider pricing
   remains unconfigured until official integration.
"""

from dataclasses import dataclass
from typing import Any

# Monetary constants
ONE_DOLLAR_MICROS: int = 1_000_000
MICROS_PER_MILLION: int = 1_000_000

# Safe database limits (PostgreSQL 32-bit Integer and 64-bit BigInteger)
MAX_TOKENS_LIMIT: int = 2_147_483_647  # Max signed 32-bit int
MAX_MICROS_LIMIT: int = 9_223_372_036_854_775_807  # Max signed 64-bit int

# Development and testing pricing version identifier
DEV_MOCK_PRICING_VERSION: str = "dev-mock-2026-v1"


class PricingError(Exception):
    """Base exception for all pricing calculation errors."""
    pass


class InvalidTokenCountError(PricingError, ValueError):
    """Raised when a token count is invalid, negative, or not an integer."""
    pass


class InvalidPricingRateError(PricingError, ValueError):
    """Raised when a pricing rate is invalid, negative, or not an integer."""
    pass


class PricingOverflowError(PricingError, OverflowError, ValueError):
    """Raised when calculated token cost, rate, or total cost exceeds 64-bit BigInteger capacity."""
    pass


class ModelPricingNotFoundError(PricingError, KeyError):
    """Raised when pricing configuration is not found for a requested model/version."""
    pass


class TurnTokenLimitExceededError(PricingError, ValueError):
    """Raised when token counts exceed the configured maximum ceilings for a turn."""
    pass


def validate_token_count(tokens: Any, name: str = "tokens", max_tokens: int | None = None) -> int:
    """Validate that token count is a non-negative integer and not a boolean.

    Args:
        tokens: The token count to validate.
        name: Name of the variable for descriptive error messages.
        max_tokens: Optional upper bound limit to enforce.

    Returns:
        int: The validated integer token count.

    Raises:
        InvalidTokenCountError: If value is a boolean, non-integer, negative, or exceeds limit.
    """
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise InvalidTokenCountError(f"{name} must be an integer, got {type(tokens).__name__}")
    if tokens < 0:
        raise InvalidTokenCountError(f"{name} must be non-negative, got {tokens}")
    if tokens > MAX_TOKENS_LIMIT:
        raise InvalidTokenCountError(
            f"{name} exceeds system maximum integer capacity ({MAX_TOKENS_LIMIT}), got {tokens}"
        )
    if max_tokens is not None and tokens > max_tokens:
        raise TurnTokenLimitExceededError(
            f"{name} ({tokens}) exceeds configured maximum limit of {max_tokens}"
        )
    return tokens


def validate_rate(rate: Any, name: str = "rate") -> int:
    """Validate that a pricing rate is a non-negative integer and not a boolean.

    Args:
        rate: The rate in micro-dollars per 1M tokens to validate.
        name: Name of the variable for descriptive error messages.

    Returns:
        int: The validated integer rate.

    Raises:
        InvalidPricingRateError: If value is a boolean, non-integer, negative, or overflows BigInteger.
    """
    if isinstance(rate, bool) or not isinstance(rate, int):
        raise InvalidPricingRateError(f"{name} must be an integer, got {type(rate).__name__}")
    if rate < 0:
        raise InvalidPricingRateError(f"{name} must be non-negative, got {rate}")
    if rate > MAX_MICROS_LIMIT:
        raise PricingOverflowError(
            f"{name} exceeds maximum micro-dollar capacity ({MAX_MICROS_LIMIT}), got {rate}"
        )
    return rate


def calculate_token_cost_micros(tokens: int, rate_micros_per_million: int) -> int:
    """Calculate token cost in micro-dollars using pure integer ceiling division.

    Formula:
        `(tokens * rate_micros_per_million + 999_999) // 1_000_000`

    Guarantees:
    - Zero floating-point arithmetic.
    - Zero tokens or zero rate returns 0 micro-dollars.
    - Fractional micro-dollar results are rounded up to the nearest whole micro-dollar.
    - Prevents overflow into negative values.

    Args:
        tokens: Number of tokens consumed.
        rate_micros_per_million: Rate in micro-dollars ($1e-6 USD) per 1,000,000 tokens.

    Returns:
        int: Cost in micro-dollars.
    """
    t = validate_token_count(tokens, "tokens")
    r = validate_rate(rate_micros_per_million, "rate_micros_per_million")

    if t == 0 or r == 0:
        return 0

    product = t * r
    cost = (product + 999_999) // MICROS_PER_MILLION

    if cost > MAX_MICROS_LIMIT:
        raise PricingOverflowError(f"Calculated cost {cost} exceeds 64-bit integer capacity ({MAX_MICROS_LIMIT})")

    return cost


@dataclass(frozen=True)
class ModelPricing:
    """Immutable model pricing specification with versioning and token ceilings.

    Attributes:
        model_name: Unique model identifier (e.g., 'mock-gpt-4o-mini').
        pricing_version: Pricing revision identifier (e.g., 'dev-mock-2026-v1').
        input_rate_micros_per_million: Micro-dollars per 1,000,000 prompt tokens.
        output_rate_micros_per_million: Micro-dollars per 1,000,000 completion tokens.
        max_input_tokens: Hard ceiling on permitted input tokens for a single turn.
        max_output_tokens: Hard ceiling on permitted output tokens for a single turn.
    """

    model_name: str
    pricing_version: str
    input_rate_micros_per_million: int
    output_rate_micros_per_million: int
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if not isinstance(self.pricing_version, str) or not self.pricing_version.strip():
            raise ValueError("pricing_version must be a non-empty string")

        validate_rate(self.input_rate_micros_per_million, "input_rate_micros_per_million")
        validate_rate(self.output_rate_micros_per_million, "output_rate_micros_per_million")

        validate_token_count(self.max_input_tokens, "max_input_tokens")
        if self.max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be greater than zero")

        validate_token_count(self.max_output_tokens, "max_output_tokens")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")

        # Verify that configured ceilings and rates do not exceed MAX_MICROS_LIMIT
        max_in_cost = calculate_token_cost_micros(self.max_input_tokens, self.input_rate_micros_per_million)
        max_out_cost = calculate_token_cost_micros(self.max_output_tokens, self.output_rate_micros_per_million)
        if max_in_cost + max_out_cost > MAX_MICROS_LIMIT:
            raise PricingOverflowError(
                f"Configured rates and ceilings for model '{self.model_name}' exceed maximum turn cost capacity"
            )

    @property
    def is_mock(self) -> bool:
        """Indicates whether this model pricing is an internal test-accounting configuration."""
        return self.model_name.startswith("mock-") or self.pricing_version.startswith("dev-mock")

    @property
    def max_turn_tokens(self) -> int:
        """Total maximum token limit (input + output) for a single turn."""
        return self.max_input_tokens + self.max_output_tokens


@dataclass(frozen=True)
class TurnCostEstimate:
    """Exact token cost breakdown for an AI generation turn in micro-dollars.

    Attributes:
        pricing_version: Version of pricing applied.
        model_name: Model used for inference.
        prompt_tokens: Prompt (input) tokens.
        completion_tokens: Completion (output) tokens.
        total_tokens: Total tokens (prompt + completion).
        input_cost_micros: Cost of prompt tokens in micro-dollars.
        output_cost_micros: Cost of completion tokens in micro-dollars.
        total_cost_micros: Total cost in micro-dollars.
    """

    pricing_version: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost_micros: int
    output_cost_micros: int
    total_cost_micros: int

    def __post_init__(self) -> None:
        validate_token_count(self.prompt_tokens, "prompt_tokens")
        validate_token_count(self.completion_tokens, "completion_tokens")
        validate_token_count(self.total_tokens, "total_tokens")
        validate_rate(self.input_cost_micros, "input_cost_micros")
        validate_rate(self.output_cost_micros, "output_cost_micros")
        validate_rate(self.total_cost_micros, "total_cost_micros")

        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError(
                f"total_tokens ({self.total_tokens}) must equal "
                f"prompt_tokens ({self.prompt_tokens}) + completion_tokens ({self.completion_tokens})"
            )
        if self.total_cost_micros != self.input_cost_micros + self.output_cost_micros:
            raise ValueError(
                f"total_cost_micros ({self.total_cost_micros}) must equal "
                f"input_cost_micros ({self.input_cost_micros}) + output_cost_micros ({self.output_cost_micros})"
            )


def calculate_turn_cost(
    pricing: ModelPricing,
    prompt_tokens: int,
    completion_tokens: int,
    enforce_limits: bool = False,
) -> TurnCostEstimate:
    """Calculate input, output, and total costs for a turn in micro-dollars.

    Args:
        pricing: ModelPricing configuration specifying rates and ceilings.
        prompt_tokens: Number of prompt (input) tokens consumed.
        completion_tokens: Number of completion (output) tokens generated.
        enforce_limits: If True, raises TurnTokenLimitExceededError if token counts exceed ceilings.

    Returns:
        TurnCostEstimate: Detailed token counts and micro-dollar costs.
    """
    if not isinstance(pricing, ModelPricing):
        raise TypeError(f"pricing must be an instance of ModelPricing, got {type(pricing).__name__}")

    p_tokens = validate_token_count(prompt_tokens, "prompt_tokens")
    c_tokens = validate_token_count(completion_tokens, "completion_tokens")

    if enforce_limits:
        if p_tokens > pricing.max_input_tokens:
            raise TurnTokenLimitExceededError(
                f"prompt_tokens ({p_tokens}) exceeds maximum input limit "
                f"({pricing.max_input_tokens}) for model '{pricing.model_name}'"
            )
        if c_tokens > pricing.max_output_tokens:
            raise TurnTokenLimitExceededError(
                f"completion_tokens ({c_tokens}) exceeds maximum output limit "
                f"({pricing.max_output_tokens}) for model '{pricing.model_name}'"
            )

    input_cost = calculate_token_cost_micros(p_tokens, pricing.input_rate_micros_per_million)
    output_cost = calculate_token_cost_micros(c_tokens, pricing.output_rate_micros_per_million)
    total_cost = input_cost + output_cost

    if total_cost > MAX_MICROS_LIMIT:
        raise PricingOverflowError(
            f"Total turn cost ({total_cost}) exceeds maximum micro-dollar capacity ({MAX_MICROS_LIMIT})"
        )

    total_tokens = p_tokens + c_tokens

    return TurnCostEstimate(
        pricing_version=pricing.pricing_version,
        model_name=pricing.model_name,
        prompt_tokens=p_tokens,
        completion_tokens=c_tokens,
        total_tokens=total_tokens,
        input_cost_micros=input_cost,
        output_cost_micros=output_cost,
        total_cost_micros=total_cost,
    )


def calculate_max_turn_cost(
    pricing: ModelPricing,
    custom_max_input_tokens: int | None = None,
    custom_max_output_tokens: int | None = None,
) -> TurnCostEstimate:
    """Calculate the conservative maximum possible cost for a single turn.

    Used by the usage reservation service to hold quota and spending headroom
    prior to invoking provider inference.

    Args:
        pricing: ModelPricing configuration specifying rates and ceilings.
        custom_max_input_tokens: Optional tighter ceiling on input tokens.
        custom_max_output_tokens: Optional tighter ceiling on output tokens.

    Returns:
        TurnCostEstimate: The worst-case conservative token cost estimate.
    """
    if not isinstance(pricing, ModelPricing):
        raise TypeError(f"pricing must be an instance of ModelPricing, got {type(pricing).__name__}")

    effective_input = pricing.max_input_tokens
    if custom_max_input_tokens is not None:
        c_input = validate_token_count(custom_max_input_tokens, "custom_max_input_tokens")
        if c_input > pricing.max_input_tokens:
            raise ValueError(
                f"custom_max_input_tokens ({c_input}) cannot exceed "
                f"model ceiling ({pricing.max_input_tokens})"
            )
        effective_input = c_input

    effective_output = pricing.max_output_tokens
    if custom_max_output_tokens is not None:
        c_output = validate_token_count(custom_max_output_tokens, "custom_max_output_tokens")
        if c_output > pricing.max_output_tokens:
            raise ValueError(
                f"custom_max_output_tokens ({c_output}) cannot exceed "
                f"model ceiling ({pricing.max_output_tokens})"
            )
        effective_output = c_output

    return calculate_turn_cost(
        pricing=pricing,
        prompt_tokens=effective_input,
        completion_tokens=effective_output,
        enforce_limits=False,
    )


# =============================================================================
# Development / Mock Pricing Catalog
# =============================================================================
# IMPORTANT:
# These rates are internal test-accounting rates specifically for the deterministic
# mock provider. They are NOT actual OpenAI or third-party provider prices.
# Real provider pricing is explicitly unconfigured in this phase.
#
# Rates:
#   Input: 150,000 micro-dollars / 1M tokens ($0.15 USD per 1M tokens)
#   Output: 600,000 micro-dollars / 1M tokens ($0.60 USD per 1M tokens)
# Ceilings:
#   Max Input Tokens: 4,000 tokens per turn
#   Max Output Tokens: 1,000 tokens per turn
# =============================================================================
MOCK_GPT_4O_MINI_PRICING = ModelPricing(
    model_name="mock-gpt-4o-mini",
    pricing_version=DEV_MOCK_PRICING_VERSION,
    input_rate_micros_per_million=150_000,
    output_rate_micros_per_million=600_000,
    max_input_tokens=4_000,
    max_output_tokens=1_000,
)

_PRICING_CATALOG: dict[tuple[str, str], ModelPricing] = {
    ("mock-gpt-4o-mini", DEV_MOCK_PRICING_VERSION): MOCK_GPT_4O_MINI_PRICING,
}


def get_model_pricing(
    model_name: str,
    pricing_version: str = DEV_MOCK_PRICING_VERSION,
) -> ModelPricing:
    """Retrieve pricing configuration for an explicitly registered model and version.

    Args:
        model_name: Model identifier (e.g., 'mock-gpt-4o-mini').
        pricing_version: Pricing version identifier (defaults to 'dev-mock-2026-v1').

    Returns:
        ModelPricing: The immutable model pricing configuration.

    Raises:
        ModelPricingNotFoundError: If model_name or pricing_version is not registered.
        ValueError: If model_name or pricing_version is not a valid non-empty string.
    """
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name must be a non-empty string")
    if not isinstance(pricing_version, str) or not pricing_version.strip():
        raise ValueError("pricing_version must be a non-empty string")

    key = (model_name.strip(), pricing_version.strip())
    if key not in _PRICING_CATALOG:
        raise ModelPricingNotFoundError(
            f"No pricing configuration registered for model '{model_name}' "
            f"under pricing version '{pricing_version}'. Real-provider pricing is "
            "unconfigured in this phase."
        )
    return _PRICING_CATALOG[key]
