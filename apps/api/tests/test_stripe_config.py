"""Focused unit tests for Stripe SDK integration, settings, and client foundation.

Covers:
1. Settings defaults when Stripe is not configured.
2. Pydantic SecretStr masking in repr, str, model_dump, and model_dump_json.
3. Actual secret extraction at the StripeClient boundary.
4. Stripe configuration validation for missing and stub credentials.
5. Strict URL validation using urllib.parse.urlsplit (localhost HTTP in dev, HTTPS in staging/production).
6. Correct test/live environment separation across development, staging, and production.
7. No accidental secret exposure in public client configuration.
8. Stripe client lazy initialization, error guards when disabled, and test mock injection.

Zero remote Stripe API calls. Pure offline mock-based tests.
"""

from unittest.mock import MagicMock
from pydantic import SecretStr
import pytest
import stripe

from app.config import Settings
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
    get_stripe_client,
    is_stripe_enabled,
    reset_stripe_client,
    set_stripe_client_for_testing,
)


@pytest.fixture(autouse=True)
def cleanup_stripe_client() -> None:
    """Ensure test mock overrides and cached clients are reset before and after each test."""
    reset_stripe_client()
    yield
    reset_stripe_client()


def test_stripe_settings_defaults_when_disabled() -> None:
    """Settings should load cleanly with Stripe disabled by default and no credentials required."""
    s = Settings(
        environment="development",
        stripe_enabled=False,
    )
    assert s.stripe_enabled is False
    assert isinstance(s.stripe_secret_key, SecretStr)
    assert s.stripe_secret_key.get_secret_value() == ""
    assert isinstance(s.stripe_webhook_secret, SecretStr)
    assert s.stripe_webhook_secret.get_secret_value() == ""
    assert s.stripe_publishable_key == ""
    assert s.stripe_pro_price_id == ""
    assert s.stripe_billing_return_url == "http://localhost:3000/billing"

    public_conf = s.get_public_stripe_config()
    assert public_conf == {
        "enabled": False,
        "publishable_key": "",
        "pro_price_id": "",
        "billing_return_url": "",
    }


def test_stripe_secretstr_masking_comprehensive() -> None:
    """SecretStr must mask sensitive credentials in repr, str, model_dump, and model_dump_json."""
    raw_secret = "sk_test_super_secret_key_12345"
    raw_whsec = "whsec_super_secret_webhook_12345"

    s = Settings(
        environment="development",
        stripe_enabled=True,
        stripe_secret_key=raw_secret,
        stripe_webhook_secret=raw_whsec,
        stripe_publishable_key="pk_test_public_key_12345",
        stripe_pro_price_id="price_pro_monthly_12345",
        stripe_billing_return_url="http://localhost:3000/billing",
    )

    # 1. Masking in repr()
    rendered_repr = repr(s)
    assert raw_secret not in rendered_repr
    assert raw_whsec not in rendered_repr
    assert "SecretStr('**********')" in rendered_repr

    # 2. Masking in str()
    rendered_str = str(s)
    assert raw_secret not in rendered_str
    assert raw_whsec not in rendered_str

    # 3. Masking in model_dump()
    dumped_dict = s.model_dump()
    assert isinstance(dumped_dict["stripe_secret_key"], SecretStr)
    assert raw_secret not in str(dumped_dict["stripe_secret_key"])
    assert isinstance(dumped_dict["stripe_webhook_secret"], SecretStr)
    assert raw_whsec not in str(dumped_dict["stripe_webhook_secret"])

    # 4. Masking in model_dump_json()
    dumped_json = s.model_dump_json()
    assert raw_secret not in dumped_json
    assert raw_whsec not in dumped_json
    assert '"stripe_secret_key":"**********"' in dumped_json
    assert '"stripe_webhook_secret":"**********"' in dumped_json

    # 5. Public configuration excludes both secrets completely
    public_conf = s.get_public_stripe_config()
    assert "stripe_secret_key" not in public_conf
    assert "stripe_webhook_secret" not in public_conf
    assert raw_secret not in str(public_conf)
    assert raw_whsec not in str(public_conf)
    assert public_conf["enabled"] is True
    assert public_conf["publishable_key"] == "pk_test_public_key_12345"
    assert public_conf["pro_price_id"] == "price_pro_monthly_12345"
    assert public_conf["billing_return_url"] == "http://localhost:3000/billing"


def test_stripe_validation_missing_and_stub_credentials() -> None:
    """Enabling Stripe with missing or stub credentials must fail fast with descriptive errors."""
    valid_key = "sk_test_valid_key_12345"
    valid_whsec = "whsec_valid_webhook_12345"
    valid_pub = "pk_test_valid_pub_12345"
    valid_price = "price_pro_monthly_12345"

    # 1. Missing secret key
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY must be configured"):
        Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key="",
        )

    # 2. Missing webhook secret
    with pytest.raises(ValueError, match="STRIPE_WEBHOOK_SECRET must be configured"):
        Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret="",
        )

    # 3. Stub or invalid webhook secret format
    for bad_whsec in ["whsec_", "whsec_short", "whsec_invalid!@#"]:
        with pytest.raises(ValueError, match="STRIPE_WEBHOOK_SECRET format is invalid"):
            Settings(
                environment="development",
                stripe_enabled=True,
                stripe_secret_key=valid_key,
                stripe_webhook_secret=bad_whsec,
            )

    # 4. Missing publishable key
    with pytest.raises(ValueError, match="STRIPE_PUBLISHABLE_KEY must be configured"):
        Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key="",
        )

    # 5. Missing price ID
    with pytest.raises(ValueError, match="STRIPE_PRO_PRICE_ID must be configured"):
        Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key=valid_pub,
            stripe_pro_price_id="",
        )

    # 6. Stub or invalid price ID format
    for bad_price in ["price_", "price_short", "price_invalid!@#"]:
        with pytest.raises(ValueError, match="STRIPE_PRO_PRICE_ID format is invalid"):
            Settings(
                environment="development",
                stripe_enabled=True,
                stripe_secret_key=valid_key,
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key=valid_pub,
                stripe_pro_price_id=bad_price,
            )


def test_stripe_billing_return_url_validation() -> None:
    """Return URL validation must require valid schemes, valid hosts, and enforce HTTPS in staging/prod."""
    valid_key = "sk_test_valid_key_12345"
    valid_whsec = "whsec_valid_webhook_12345"
    valid_pub = "pk_test_valid_pub_12345"
    valid_price = "price_pro_monthly_12345"

    # Disallowed scheme
    for bad_url in ["ftp://localhost:3000/billing", "javascript:alert(1)"]:
        with pytest.raises(ValueError, match="STRIPE_BILLING_RETURN_URL must use an allowed scheme"):
            Settings(
                environment="development",
                stripe_enabled=True,
                stripe_secret_key=valid_key,
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key=valid_pub,
                stripe_pro_price_id=valid_price,
                stripe_billing_return_url=bad_url,
            )

    # Missing or empty host / malformed URL
    for empty_host in ["http://", "https://", "http://  ", "http://invalid host with spaces/billing"]:
        with pytest.raises(ValueError, match="STRIPE_BILLING_RETURN_URL must contain a valid, non-empty host"):
            Settings(
                environment="development",
                stripe_enabled=True,
                stripe_secret_key=valid_key,
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key=valid_pub,
                stripe_pro_price_id=valid_price,
                stripe_billing_return_url=empty_host,
            )

    # Development allows localhost HTTP
    for local_url in [
        "http://localhost:3000/billing",
        "http://127.0.0.1:3000/billing",
        "http://localhost/billing",
    ]:
        s = Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key=valid_pub,
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url=local_url,
        )
        assert s.stripe_billing_return_url == local_url

    # Development rejects non-localhost HTTP
    with pytest.raises(ValueError, match="scheme http:// is only permitted for localhost"):
        Settings(
            environment="development",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key=valid_pub,
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url="http://remote-server.com/billing",
        )

    # Staging requires HTTPS (even for localhost)
    with pytest.raises(ValueError, match="STRIPE_BILLING_RETURN_URL must use https:// in staging environments"):
        Settings(
            environment="staging",
            stripe_enabled=True,
            stripe_secret_key=valid_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key=valid_pub,
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url="http://localhost:3000/billing",
        )

    # Production requires HTTPS
    with pytest.raises(ValueError, match="STRIPE_BILLING_RETURN_URL must use https:// in production environments"):
        Settings(
            environment="production",
            visitor_session_secret="a" * 32,
            stripe_enabled=True,
            stripe_secret_key="sk_live_valid_key_12345",
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key="pk_live_valid_pub_12345",
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url="http://localhost:3000/billing",
        )


def test_stripe_environment_separation_development_and_staging() -> None:
    """Development and staging environments must reject live keys and accept test/restricted keys."""
    valid_whsec = "whsec_valid_webhook_12345"
    valid_price = "price_pro_monthly_12345"

    for env in ["development", "staging"]:
        return_url = (
            "https://staging.aptlyst.com/billing"
            if env == "staging"
            else "http://localhost:3000/billing"
        )

        # Reject live secret keys
        for live_key in ["sk_live_production_key_123", "rk_live_production_key_123"]:
            with pytest.raises(ValueError, match="Live Stripe secret key is prohibited"):
                Settings(
                    environment=env,
                    stripe_enabled=True,
                    stripe_secret_key=live_key,
                    stripe_webhook_secret=valid_whsec,
                    stripe_publishable_key="pk_test_valid_pub_123",
                    stripe_pro_price_id=valid_price,
                    stripe_billing_return_url=return_url,
                )

        # Reject live publishable key
        with pytest.raises(ValueError, match="Live Stripe publishable key is prohibited"):
            Settings(
                environment=env,
                stripe_enabled=True,
                stripe_secret_key="sk_test_valid_key_123",
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key="pk_live_production_pub_123",
                stripe_pro_price_id=valid_price,
                stripe_billing_return_url=return_url,
            )

        # Accept valid test and restricted test keys
        for test_key in ["sk_test_valid_key_123", "rk_test_valid_key_123"]:
            s = Settings(
                environment=env,
                stripe_enabled=True,
                stripe_secret_key=test_key,
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key="pk_test_valid_pub_123",
                stripe_pro_price_id=valid_price,
                stripe_billing_return_url=return_url,
            )
            assert s.stripe_enabled is True
            assert s.stripe_secret_key.get_secret_value() == test_key


def test_stripe_environment_separation_production() -> None:
    """Production environments must reject test keys and require live keys and https return URL."""
    prod_visitor_secret = "a" * 32
    valid_whsec = "whsec_production_secret_123"
    valid_price = "price_production_pro_123"
    return_url = "https://app.aptlyst.com/billing"

    # Reject test secret keys
    for test_key in ["sk_test_accidental_test_key", "rk_test_accidental_test_key"]:
        with pytest.raises(ValueError, match="Test Stripe secret key is prohibited in production"):
            Settings(
                environment="production",
                visitor_session_secret=prod_visitor_secret,
                stripe_enabled=True,
                stripe_secret_key=test_key,
                stripe_webhook_secret=valid_whsec,
                stripe_publishable_key="pk_live_valid_123",
                stripe_pro_price_id=valid_price,
                stripe_billing_return_url=return_url,
            )

    # Reject test publishable key
    with pytest.raises(ValueError, match="Test Stripe publishable key is prohibited in production"):
        Settings(
            environment="production",
            visitor_session_secret=prod_visitor_secret,
            stripe_enabled=True,
            stripe_secret_key="sk_live_production_key_123",
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key="pk_test_accidental_test_key",
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url=return_url,
        )

    # Accept valid live and restricted live keys
    for live_key in ["sk_live_production_key_123", "rk_live_production_key_123"]:
        s = Settings(
            environment="production",
            visitor_session_secret=prod_visitor_secret,
            stripe_enabled=True,
            stripe_secret_key=live_key,
            stripe_webhook_secret=valid_whsec,
            stripe_publishable_key="pk_live_production_pub_123",
            stripe_pro_price_id=valid_price,
            stripe_billing_return_url=return_url,
        )
        assert s.stripe_enabled is True
        assert s.stripe_secret_key.get_secret_value() == live_key


def test_stripe_client_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripe client access should be blocked when Stripe billing is disabled."""
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_enabled", False)
    assert is_stripe_enabled() is False

    with pytest.raises(StripeBillingDisabledError, match="Stripe billing is currently disabled"):
        get_stripe_client()


def test_stripe_client_initialization_and_secret_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled, get_stripe_client extracts raw secret at boundary and caches instance."""
    from app.config import settings

    mock_secret = "sk_test_mock_secret_key_12345"
    monkeypatch.setattr(settings, "stripe_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr(mock_secret))

    assert is_stripe_enabled() is True

    client1 = get_stripe_client()
    assert isinstance(client1, stripe.StripeClient)

    # Verify actual secret was correctly passed to the StripeClient
    assert client1._requestor.api_key == mock_secret

    # Verify singleton caching
    client2 = get_stripe_client()
    assert client1 is client2


def test_stripe_client_mock_injection_and_reset() -> None:
    """set_stripe_client_for_testing should allow injecting mock clients even if disabled."""
    mock_client = MagicMock(spec=stripe.StripeClient)
    set_stripe_client_for_testing(mock_client)

    # Test client override returns mock regardless of settings
    assert get_stripe_client() is mock_client

    # Resetting restores normal behavior
    reset_stripe_client()
    with pytest.raises(StripeBillingDisabledError):
        get_stripe_client()