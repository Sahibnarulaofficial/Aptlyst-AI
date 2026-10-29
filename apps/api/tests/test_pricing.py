"""Unit tests for the pure, deterministic token and usage pricing module."""

import pytest

from app.services.billing.pricing import (
    DEV_MOCK_PRICING_VERSION,
    MAX_MICROS_LIMIT,
    MAX_TOKENS_LIMIT,
    MOCK_GPT_4O_MINI_PRICING,
    ONE_DOLLAR_MICROS,
    InvalidPricingRateError,
    InvalidTokenCountError,
    ModelPricing,
    ModelPricingNotFoundError,
    PricingOverflowError,
    TurnCostEstimate,
    TurnTokenLimitExceededError,
    calculate_max_turn_cost,
    calculate_token_cost_micros,
    calculate_turn_cost,
    get_model_pricing,
    validate_rate,
    validate_token_count,
)


# -----------------------------------------------------------------------------
# 1. Zero Tokens and Zero Rates
# -----------------------------------------------------------------------------

def test_calculate_token_cost_zero_tokens() -> None:
    """Zero tokens consumed must always incur exactly zero cost, regardless of rate."""
    assert calculate_token_cost_micros(0, 150_000) == 0
    assert calculate_token_cost_micros(0, 600_000) == 0
    assert calculate_token_cost_micros(0, 10_000_000) == 0
    assert calculate_token_cost_micros(0, 0) == 0


def test_calculate_token_cost_zero_rate() -> None:
    """A zero rate must always incur zero cost, regardless of tokens consumed."""
    assert calculate_token_cost_micros(100, 0) == 0
    assert calculate_token_cost_micros(1_000_000, 0) == 0
    assert calculate_token_cost_micros(MAX_TOKENS_LIMIT, 0) == 0


# -----------------------------------------------------------------------------
# 2. Exact Multiples & Integer Ceiling Rounding
# -----------------------------------------------------------------------------

def test_calculate_token_cost_exact_multiples() -> None:
    """Exact multiples of 1,000,000 tokens must compute without remainder or rounding."""
    # 1M tokens at $0.15/1M ($150,000 micros) = 150,000 micro-dollars
    assert calculate_token_cost_micros(1_000_000, 150_000) == 150_000

    # 2M tokens at $0.60/1M ($600,000 micros) = 1,200,000 micro-dollars ($1.20)
    assert calculate_token_cost_micros(2_000_000, 600_000) == 1_200_000

    # 10,000 tokens at $1.00/1M ($1,000,000 micros) = 10,000 micro-dollars ($0.01)
    assert calculate_token_cost_micros(10_000, ONE_DOLLAR_MICROS) == 10_000

    # 1M tokens at $1.00/1M = 1,000,000 micro-dollars ($1.00)
    assert calculate_token_cost_micros(1_000_000, ONE_DOLLAR_MICROS) == ONE_DOLLAR_MICROS


def test_calculate_token_cost_fractional_ceiling_rounding() -> None:
    """Any non-zero fractional micro-dollar result must round UP to the next integer micro-dollar."""
    # 1 token at $0.15/1M (rate = 150,000):
    # Mathematical cost = 0.15 micro-dollars. Ceiling must round to 1 micro-dollar.
    assert calculate_token_cost_micros(1, 150_000) == 1

    # 1 token at $0.01/1M (rate = 10,000):
    # Mathematical cost = 0.01 micro-dollars. Ceiling must round to 1 micro-dollar.
    assert calculate_token_cost_micros(1, 10_000) == 1

    # 3 tokens at $0.10/1M (rate = 100,000):
    # Mathematical cost = 0.30 micro-dollars. Ceiling must round to 1 micro-dollar.
    assert calculate_token_cost_micros(3, 100_000) == 1

    # 7 tokens at $0.20/1M (rate = 200,000):
    # Mathematical cost = 1.40 micro-dollars. Ceiling must round to 2 micro-dollars.
    assert calculate_token_cost_micros(7, 200_000) == 2

    # 1 token at $1.00/1M (rate = 1,000,000):
    # Mathematical cost = 1.00 micro-dollar (exact). Must equal 1.
    assert calculate_token_cost_micros(1, ONE_DOLLAR_MICROS) == 1

    # 999_999 tokens at rate 1 micro/1M:
    # (999_999 + 999_999) // 1_000_000 = 1_999_998 // 1_000_000 = 1 micro-dollar
    assert calculate_token_cost_micros(999_999, 1) == 1

    # 1_000_001 tokens at rate 1 micro/1M:
    # (1_000_001 + 999_999) // 1_000_000 = 2_000_000 // 1_000_000 = 2 micro-dollars
    assert calculate_token_cost_micros(1_000_001, 1) == 2


# -----------------------------------------------------------------------------
# 3. Large Valid Integers & Safety Bounds
# -----------------------------------------------------------------------------

def test_calculate_token_cost_large_valid_integers() -> None:
    """Large valid token and rate integers must compute accurately without overflow."""
    # 1 billion tokens at $10.00/1M tokens (rate = 10,000,000 micros)
    # Cost = 1,000 * 10,000,000 = 10,000,000,000 micro-dollars ($10,000 USD)
    assert calculate_token_cost_micros(1_000_000_000, 10_000_000) == 10_000_000_000

    # Max 32-bit tokens at $0.60/1M tokens
    cost = calculate_token_cost_micros(MAX_TOKENS_LIMIT, 600_000)
    expected = (MAX_TOKENS_LIMIT * 600_000 + 999_999) // 1_000_000
    assert cost == expected
    assert cost > 0
    assert cost < MAX_MICROS_LIMIT


# -----------------------------------------------------------------------------
# 4. Input Validation: Booleans, Negatives, Floats, Strings, and None
# -----------------------------------------------------------------------------

def test_validate_token_count_rejects_booleans() -> None:
    """Booleans (True/False) must be rejected because bool is a subclass of int in Python."""
    with pytest.raises(InvalidTokenCountError, match="must be an integer"):
        validate_token_count(True)
    with pytest.raises(InvalidTokenCountError, match="must be an integer"):
        validate_token_count(False)
    with pytest.raises(InvalidTokenCountError, match="must be an integer"):
        calculate_token_cost_micros(True, 150_000)
    with pytest.raises(InvalidTokenCountError, match="must be an integer"):
        calculate_token_cost_micros(False, 150_000)


def test_validate_token_count_rejects_negatives() -> None:
    """Negative token counts must be rejected."""
    with pytest.raises(InvalidTokenCountError, match="must be non-negative"):
        validate_token_count(-1)
    with pytest.raises(InvalidTokenCountError, match="must be non-negative"):
        validate_token_count(-1000)
    with pytest.raises(InvalidTokenCountError, match="must be non-negative"):
        calculate_token_cost_micros(-5, 150_000)


def test_validate_token_count_rejects_non_integers() -> None:
    """Floats, strings, None, and structures must be rejected."""
    for invalid in [1.5, 0.0, -0.1, "100", "0", None, [], {}]:
        with pytest.raises(InvalidTokenCountError, match="must be an integer"):
            validate_token_count(invalid)
        with pytest.raises(InvalidTokenCountError, match="must be an integer"):
            calculate_token_cost_micros(invalid, 150_000)  # type: ignore[arg-type]


def test_validate_token_count_rejects_overflow() -> None:
    """Token counts exceeding PostgreSQL 32-bit Integer limit must be rejected."""
    with pytest.raises(InvalidTokenCountError, match="exceeds system maximum integer capacity"):
        validate_token_count(MAX_TOKENS_LIMIT + 1)


def test_validate_rate_rejects_booleans() -> None:
    """Pricing rates must reject booleans."""
    with pytest.raises(InvalidPricingRateError, match="must be an integer"):
        validate_rate(True)
    with pytest.raises(InvalidPricingRateError, match="must be an integer"):
        validate_rate(False)
    with pytest.raises(InvalidPricingRateError, match="must be an integer"):
        calculate_token_cost_micros(100, True)
    with pytest.raises(InvalidPricingRateError, match="must be an integer"):
        calculate_token_cost_micros(100, False)


def test_validate_rate_rejects_negatives() -> None:
    """Pricing rates must reject negative numbers."""
    with pytest.raises(InvalidPricingRateError, match="must be non-negative"):
        validate_rate(-1)
    with pytest.raises(InvalidPricingRateError, match="must be non-negative"):
        calculate_token_cost_micros(100, -50_000)


def test_validate_rate_rejects_non_integers() -> None:
    """Pricing rates must reject floats, strings, and None."""
    for invalid in [0.5, 150.0, "150000", None, [100]]:
        with pytest.raises(InvalidPricingRateError, match="must be an integer"):
            validate_rate(invalid)
        with pytest.raises(InvalidPricingRateError, match="must be an integer"):
            calculate_token_cost_micros(100, invalid)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# 5. Model Pricing Configuration & Validation
# -----------------------------------------------------------------------------

def test_model_pricing_valid_construction() -> None:
    """Valid ModelPricing attributes must construct properly."""
    pricing = ModelPricing(
        model_name="test-model",
        pricing_version="v1",
        input_rate_micros_per_million=200_000,
        output_rate_micros_per_million=800_000,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
    )
    assert pricing.model_name == "test-model"
    assert pricing.pricing_version == "v1"
    assert pricing.input_rate_micros_per_million == 200_000
    assert pricing.output_rate_micros_per_million == 800_000
    assert pricing.max_input_tokens == 8_000
    assert pricing.max_output_tokens == 2_000
    assert pricing.max_turn_tokens == 10_000


def test_model_pricing_rejects_malformed_inputs() -> None:
    """ModelPricing must reject empty names, negative rates, or non-positive ceilings."""
    # Empty model name
    with pytest.raises(ValueError, match="model_name must be a non-empty string"):
        ModelPricing(
            model_name="",
            pricing_version="v1",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    # Empty pricing version
    with pytest.raises(ValueError, match="pricing_version must be a non-empty string"):
        ModelPricing(
            model_name="test-model",
            pricing_version="   ",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    # Negative rate
    with pytest.raises(InvalidPricingRateError, match="must be non-negative"):
        ModelPricing(
            model_name="test-model",
            pricing_version="v1",
            input_rate_micros_per_million=-100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=1000,
            max_output_tokens=1000,
        )

    # Zero max input tokens
    with pytest.raises(ValueError, match="max_input_tokens must be greater than zero"):
        ModelPricing(
            model_name="test-model",
            pricing_version="v1",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=0,
            max_output_tokens=1000,
        )

    # Negative max input tokens
    with pytest.raises(InvalidTokenCountError, match="max_input_tokens must be non-negative"):
        ModelPricing(
            model_name="test-model",
            pricing_version="v1",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=-5,
            max_output_tokens=1000,
        )

    # Zero max output tokens
    with pytest.raises(ValueError, match="max_output_tokens must be greater than zero"):
        ModelPricing(
            model_name="test-model",
            pricing_version="v1",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=1000,
            max_output_tokens=0,
        )

    # Negative max output tokens
    with pytest.raises(InvalidTokenCountError, match="max_output_tokens must be non-negative"):
        ModelPricing(
            model_name="test-model",
            pricing_version="v1",
            input_rate_micros_per_million=100_000,
            output_rate_micros_per_million=100_000,
            max_input_tokens=1000,
            max_output_tokens=-10,
        )


# -----------------------------------------------------------------------------
# 6. Turn Cost Calculation & Composition
# -----------------------------------------------------------------------------

def test_calculate_turn_cost_composition() -> None:
    """Turn cost calculation must compose input, output, and total costs accurately."""
    # Mock model: input = $0.15/1M (150,000 micros), output = $0.60/1M (600,000 micros)
    estimate = calculate_turn_cost(
        pricing=MOCK_GPT_4O_MINI_PRICING,
        prompt_tokens=1_000,
        completion_tokens=500,
    )
    # Input cost: (1000 * 150_000 + 999_999) // 1_000_000 = 150 micro-dollars
    # Output cost: (500 * 600_000 + 999_999) // 1_000_000 = 300 micro-dollars
    assert estimate.pricing_version == DEV_MOCK_PRICING_VERSION
    assert estimate.model_name == "mock-gpt-4o-mini"
    assert estimate.prompt_tokens == 1_000
    assert estimate.completion_tokens == 500
    assert estimate.total_tokens == 1_500
    assert estimate.input_cost_micros == 150
    assert estimate.output_cost_micros == 300
    assert estimate.total_cost_micros == 450


def test_calculate_turn_cost_with_zero_completions() -> None:
    """Prompt tokens without completion tokens (e.g. prompt evaluation only)."""
    estimate = calculate_turn_cost(
        pricing=MOCK_GPT_4O_MINI_PRICING,
        prompt_tokens=2_000,
        completion_tokens=0,
    )
    # Input cost: (2000 * 150_000 + 999_999) // 1_000_000 = 300 micros
    assert estimate.prompt_tokens == 2_000
    assert estimate.completion_tokens == 0
    assert estimate.total_tokens == 2_000
    assert estimate.input_cost_micros == 300
    assert estimate.output_cost_micros == 0
    assert estimate.total_cost_micros == 300


# -----------------------------------------------------------------------------
# 7. Maximum Turn Cost (Conservative Upper Bound)
# -----------------------------------------------------------------------------

def test_calculate_max_turn_cost_default_ceilings() -> None:
    """Conservative maximum turn cost computes worst-case expenditure at configured ceilings."""
    # Default mock ceilings: max_input_tokens = 4_000, max_output_tokens = 1_000
    # Input cost: (4000 * 150_000 + 999_999) // 1_000_000 = 600 micros
    # Output cost: (1000 * 600_000 + 999_999) // 1_000_000 = 600 micros
    # Total max cost: 1,200 micro-dollars ($0.0012 USD)
    max_estimate = calculate_max_turn_cost(MOCK_GPT_4O_MINI_PRICING)
    assert max_estimate.prompt_tokens == 4_000
    assert max_estimate.completion_tokens == 1_000
    assert max_estimate.total_tokens == 5_000
    assert max_estimate.input_cost_micros == 600
    assert max_estimate.output_cost_micros == 600
    assert max_estimate.total_cost_micros == 1_200


def test_calculate_max_turn_cost_custom_tighter_ceilings() -> None:
    """Custom ceilings allow restricting maximum turn reservation headroom."""
    # Tighter limits: 2_000 input, 500 output
    max_estimate = calculate_max_turn_cost(
        pricing=MOCK_GPT_4O_MINI_PRICING,
        custom_max_input_tokens=2_000,
        custom_max_output_tokens=500,
    )
    # Input cost: (2000 * 150_000) / 1M = 300 micros
    # Output cost: (500 * 600_000) / 1M = 300 micros
    assert max_estimate.prompt_tokens == 2_000
    assert max_estimate.completion_tokens == 500
    assert max_estimate.total_tokens == 2_500
    assert max_estimate.input_cost_micros == 300
    assert max_estimate.output_cost_micros == 300
    assert max_estimate.total_cost_micros == 600


def test_calculate_max_turn_cost_custom_exceeding_ceiling_rejected() -> None:
    """Custom turn ceilings cannot exceed the model's hard maximum ceilings."""
    with pytest.raises(ValueError, match="cannot exceed model ceiling"):
        calculate_max_turn_cost(
            pricing=MOCK_GPT_4O_MINI_PRICING,
            custom_max_input_tokens=4_001,  # Model ceiling is 4_000
        )
    with pytest.raises(ValueError, match="cannot exceed model ceiling"):
        calculate_max_turn_cost(
            pricing=MOCK_GPT_4O_MINI_PRICING,
            custom_max_output_tokens=1_001,  # Model ceiling is 1_000
        )


# -----------------------------------------------------------------------------
# 8. Boundary Conditions at Token Limits
# -----------------------------------------------------------------------------

def test_enforce_turn_token_limits_boundary_conditions() -> None:
    """Turn cost calculation with enforce_limits=True must strictly guard boundaries."""
    # At exact boundaries: must succeed
    estimate = calculate_turn_cost(
        pricing=MOCK_GPT_4O_MINI_PRICING,
        prompt_tokens=4_000,
        completion_tokens=1_000,
        enforce_limits=True,
    )
    assert estimate.prompt_tokens == 4_000
    assert estimate.completion_tokens == 1_000

    # 1 token over prompt limit
    with pytest.raises(TurnTokenLimitExceededError, match="exceeds maximum input limit"):
        calculate_turn_cost(
            pricing=MOCK_GPT_4O_MINI_PRICING,
            prompt_tokens=4_001,
            completion_tokens=1_000,
            enforce_limits=True,
        )

    # 1 token over completion limit
    with pytest.raises(TurnTokenLimitExceededError, match="exceeds maximum output limit"):
        calculate_turn_cost(
            pricing=MOCK_GPT_4O_MINI_PRICING,
            prompt_tokens=4_000,
            completion_tokens=1_001,
            enforce_limits=True,
        )


# -----------------------------------------------------------------------------
# 9. Catalog & Unconfigured Real Providers
# -----------------------------------------------------------------------------

def test_get_model_pricing_mock_success() -> None:
    """Retrieving mock model pricing returns the development pricing configuration."""
    pricing = get_model_pricing("mock-gpt-4o-mini")
    assert pricing == MOCK_GPT_4O_MINI_PRICING
    assert pricing.model_name == "mock-gpt-4o-mini"
    assert pricing.pricing_version == DEV_MOCK_PRICING_VERSION


def test_get_model_pricing_unconfigured_real_providers_rejected() -> None:
    """Real provider models must not have invented prices and must raise ModelPricingNotFoundError."""
    for real_model in ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet", "gemini-1.5-pro"]:
        with pytest.raises(ModelPricingNotFoundError, match="Real-provider pricing is unconfigured"):
            get_model_pricing(real_model)


def test_get_model_pricing_unknown_version_rejected() -> None:
    """Known model with an unknown pricing version must raise ModelPricingNotFoundError."""
    with pytest.raises(ModelPricingNotFoundError):
        get_model_pricing("mock-gpt-4o-mini", "unknown-version-2099")


def test_get_model_pricing_invalid_names_rejected() -> None:
    """Empty or non-string model names must raise ValueError."""
    with pytest.raises(ValueError, match="model_name must be a non-empty string"):
        get_model_pricing("")
    with pytest.raises(ValueError, match="model_name must be a non-empty string"):
        get_model_pricing("   ")
    with pytest.raises(ValueError, match="model_name must be a non-empty string"):
        get_model_pricing(123)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# 10. Deterministic Consistency Across Repeated Invocations
# -----------------------------------------------------------------------------

def test_deterministic_results_across_repeated_calls() -> None:
    """Repeated calls with identical parameters must yield identical results."""
    for _ in range(50):
        cost1 = calculate_token_cost_micros(733, 150_000)
        cost2 = calculate_token_cost_micros(733, 150_000)
        assert cost1 == cost2 == 110  # (733 * 150_000 + 999_999) // 1_000_000 = 109_950_000 + 999_999 = 110_949_999 // 1M = 110

        est1 = calculate_turn_cost(MOCK_GPT_4O_MINI_PRICING, 1234, 567)
        est2 = calculate_turn_cost(MOCK_GPT_4O_MINI_PRICING, 1234, 567)
        assert est1 == est2
        assert est1.total_cost_micros == est2.total_cost_micros


# -----------------------------------------------------------------------------
# 11. Focused Invariant Checks (MAX_MICROS_LIMIT Overflow, Ceilings, Mock Flags)
# -----------------------------------------------------------------------------

def test_calculated_token_cost_overflow_rejected() -> None:
    """Calculated token cost exceeding MAX_MICROS_LIMIT must raise PricingOverflowError."""
    # 10M tokens at MAX_MICROS_LIMIT rate = 10 * MAX_MICROS_LIMIT (overflows 64-bit int)
    with pytest.raises(PricingOverflowError, match="exceeds 64-bit integer capacity"):
        calculate_token_cost_micros(10_000_000, MAX_MICROS_LIMIT)


def test_total_turn_cost_overflow_rejected() -> None:
    """Total turn cost exceeding MAX_MICROS_LIMIT must raise PricingOverflowError."""
    half_limit = MAX_MICROS_LIMIT // 2
    # Model ceilings are small (500K each) so ModelPricing construction succeeds
    pricing = ModelPricing(
        model_name="edge-pricing-model",
        pricing_version="v1",
        input_rate_micros_per_million=half_limit,
        output_rate_micros_per_million=half_limit + 2,
        max_input_tokens=500_000,
        max_output_tokens=500_000,
    )
    # When tokens consumed exceed ceilings (with enforce_limits=False), sum overflows
    # 1M input tokens at half_limit = half_limit
    # 1M output tokens at half_limit + 2 = half_limit + 2
    # Total cost = 2 * half_limit + 2 = MAX_MICROS_LIMIT + 1 (overflows 64-bit int)
    with pytest.raises(PricingOverflowError, match="exceeds maximum micro-dollar capacity"):
        calculate_turn_cost(
            pricing=pricing,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            enforce_limits=False,
        )


def test_model_pricing_rejects_overflowing_ceilings_at_construction() -> None:
    """ModelPricing construction must reject rate and ceiling combinations that overflow MAX_MICROS_LIMIT."""
    with pytest.raises(PricingOverflowError):
        ModelPricing(
            model_name="overflow-model",
            pricing_version="v1",
            input_rate_micros_per_million=MAX_MICROS_LIMIT,
            output_rate_micros_per_million=MAX_MICROS_LIMIT,
            max_input_tokens=2_000_000,
            max_output_tokens=2_000_000,
        )


def test_model_pricing_is_mock_property() -> None:
    """ModelPricing.is_mock reliably distinguishes test-accounting from real models."""
    assert MOCK_GPT_4O_MINI_PRICING.is_mock is True

    custom_mock = ModelPricing(
        model_name="mock-custom-llm",
        pricing_version="dev-mock-v2",
        input_rate_micros_per_million=100_000,
        output_rate_micros_per_million=100_000,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
    )
    assert custom_mock.is_mock is True

    real_pricing = ModelPricing(
        model_name="custom-enterprise-model",
        pricing_version="2026-q4-v1",
        input_rate_micros_per_million=100_000,
        output_rate_micros_per_million=100_000,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
    )
    assert real_pricing.is_mock is False
