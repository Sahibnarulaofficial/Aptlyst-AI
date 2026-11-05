"""Centralized Stripe SDK client initialization and configuration management.

Guarantees:
1. Server-side only: uses server-side secret credentials; never exposes secrets.
2. Lazy/Guarded initialization: avoids initializing Stripe when billing is disabled.
3. Test isolation: provides hooks for injecting mock clients during unit testing.
4. Thread/context safety: uses modern StripeClient instances without mutating global state.
5. Explicit credential lifecycle: following standard 12-factor application architecture,
   credentials are loaded at application process startup. In production deployments,
   credential rotation requires an application process restart (or explicit invocation
   of reset_stripe_client() in maintenance tasks).
"""

from typing import Any
import stripe

from app.config import settings


class StripeBillingDisabledError(RuntimeError):
    """Raised when attempting to access Stripe client while Stripe billing is disabled."""


class StripeConfigurationError(ValueError):
    """Raised when Stripe billing is enabled but required credentials are missing or invalid."""


_TEST_STRIPE_CLIENT: Any | None = None
_CACHED_STRIPE_CLIENT: stripe.StripeClient | None = None


def is_stripe_enabled() -> bool:
    """Return True if Stripe billing is enabled and configured."""
    return bool(settings.stripe_enabled and settings.stripe_secret_key.get_secret_value().strip())


def get_stripe_client() -> stripe.StripeClient:
    """Retrieve or initialize the centralized StripeClient instance.

    Raises:
        StripeBillingDisabledError: If settings.stripe_enabled is False.
        StripeConfigurationError: If settings.stripe_secret_key is empty.

    Returns:
        stripe.StripeClient: Configured modern Stripe client.
    """
    global _TEST_STRIPE_CLIENT, _CACHED_STRIPE_CLIENT

    if _TEST_STRIPE_CLIENT is not None:
        return _TEST_STRIPE_CLIENT

    if not settings.stripe_enabled:
        raise StripeBillingDisabledError(
            "Stripe billing is currently disabled. Set STRIPE_ENABLED=true to enable."
        )

    secret_key = settings.stripe_secret_key.get_secret_value().strip()
    if not secret_key:
        raise StripeConfigurationError(
            "Stripe secret key is not configured. Set STRIPE_SECRET_KEY."
        )

    if _CACHED_STRIPE_CLIENT is None:
        _CACHED_STRIPE_CLIENT = stripe.StripeClient(
            api_key=secret_key,
            max_network_retries=2,
        )

    return _CACHED_STRIPE_CLIENT


def set_stripe_client_for_testing(client: Any | None) -> None:
    """Inject a test or mock StripeClient to isolate unit tests from external API calls.

    Pass None to clear the test client override.
    """
    global _TEST_STRIPE_CLIENT
    _TEST_STRIPE_CLIENT = client


def reset_stripe_client() -> None:
    """Reset cached Stripe client instances (useful between tests)."""
    global _TEST_STRIPE_CLIENT, _CACHED_STRIPE_CLIENT
    _TEST_STRIPE_CLIENT = None
    _CACHED_STRIPE_CLIENT = None
