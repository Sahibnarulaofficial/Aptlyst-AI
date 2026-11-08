"""Focused unit tests for Razorpay settings and configuration validation.

Covers:
1. Settings defaults when Razorpay is disabled (no credentials required).
2. Pydantic SecretStr masking in repr, str, and public configs.
3. Razorpay configuration validation for test and live credentials.
4. Test credentials rejected in production environments.
5. Live credentials rejected in development and staging environments.
6. Missing, default, or placeholder merchant-account ID rejected in production.
7. Plan ID format and currency code validation.
8. Zero remote Razorpay API calls. Pure offline mock-based tests.
"""

import pytest
from pydantic import SecretStr

from app.config import Settings


def test_razorpay_settings_defaults_when_disabled() -> None:
    """Settings should load cleanly with Razorpay disabled by default without credentials."""
    s = Settings(
        environment="development",
        razorpay_enabled=False,
    )
    assert s.razorpay_enabled is False
    assert s.razorpay_environment == "test"
    assert s.razorpay_merchant_account_id == "default"
    assert s.razorpay_key_id == ""
    assert isinstance(s.razorpay_key_secret, SecretStr)
    assert s.razorpay_key_secret.get_secret_value() == ""
    assert isinstance(s.razorpay_webhook_secret, SecretStr)
    assert s.razorpay_webhook_secret.get_secret_value() == ""
    assert s.razorpay_pro_plan_id == ""
    assert s.razorpay_plan_currency == "USD"

    public_conf = s.get_public_razorpay_config()
    assert public_conf == {
        "enabled": False,
        "environment": "test",
        "key_id": "",
        "merchant_account_id": "",
        "pro_plan_id": "",
        "plan_currency": "USD",
    }
    assert "key_secret" not in public_conf
    assert "webhook_secret" not in public_conf


def test_razorpay_valid_test_configuration() -> None:
    """Valid test credentials in development should pass validation."""
    s = Settings(
        environment="development",
        razorpay_enabled=True,
        razorpay_environment="test",
        razorpay_merchant_account_id="acc_dev123",
        razorpay_key_id="rzp_test_1234567890",
        razorpay_key_secret=SecretStr("sec_test_secret_abc"),
        razorpay_webhook_secret=SecretStr("whsec_test_secret_xyz"),
        razorpay_pro_plan_id="plan_12345678",
        razorpay_plan_currency="USD",
    )
    assert s.razorpay_enabled is True
    assert s.razorpay_environment == "test"
    assert s.razorpay_key_id == "rzp_test_1234567890"

    public_conf = s.get_public_razorpay_config()
    assert public_conf["enabled"] is True
    assert public_conf["environment"] == "test"
    assert public_conf["key_id"] == "rzp_test_1234567890"
    assert public_conf["merchant_account_id"] == "acc_dev123"
    assert "key_secret" not in public_conf


def test_razorpay_valid_live_configuration() -> None:
    """Valid live credentials in production should pass validation."""
    s = Settings(
        environment="production",
        visitor_session_secret="prod-super-secure-visitor-session-secret-32chars",
        razorpay_enabled=True,
        razorpay_environment="live",
        razorpay_merchant_account_id="acc_live_merchant_99",
        razorpay_key_id="rzp_live_0987654321",
        razorpay_key_secret=SecretStr("sec_live_secret_real"),
        razorpay_webhook_secret=SecretStr("whsec_live_secret_real"),
        razorpay_pro_plan_id="plan_live_pro_plan",
        razorpay_plan_currency="USD",
    )
    assert s.razorpay_enabled is True
    assert s.razorpay_environment == "live"
    assert s.razorpay_key_id == "rzp_live_0987654321"


def test_razorpay_test_key_rejected_in_production() -> None:
    """Production environment must strictly reject rzp_test_ keys."""
    with pytest.raises(ValueError, match="Test Razorpay key is prohibited in production environments"):
        Settings(
            environment="production",
            visitor_session_secret="prod-super-secure-visitor-session-secret-32chars",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="acc_prod_001",
            razorpay_key_id="rzp_test_should_fail",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="plan_12345678",
            razorpay_plan_currency="USD",
        )


def test_razorpay_live_key_rejected_in_development() -> None:
    """Development environment must strictly reject rzp_live_ keys."""
    with pytest.raises(ValueError, match="Live Razorpay key is prohibited in non-production environments"):
        Settings(
            environment="development",
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id="acc_dev_001",
            razorpay_key_id="rzp_live_should_fail",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="plan_12345678",
            razorpay_plan_currency="USD",
        )


def test_razorpay_production_rejects_default_or_placeholder_merchant_account() -> None:
    """Production environment must reject 'default', empty, or placeholder merchant account ID."""
    with pytest.raises(ValueError, match="Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured"):
        Settings(
            environment="production",
            visitor_session_secret="prod-super-secure-visitor-session-secret-32chars",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="default",  # Prohibited default!
            razorpay_key_id="rzp_live_1234567890",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="plan_12345678",
            razorpay_plan_currency="USD",
        )

    with pytest.raises(ValueError, match="Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured"):
        Settings(
            environment="production",
            visitor_session_secret="prod-super-secure-visitor-session-secret-32chars",
            razorpay_enabled=True,
            razorpay_environment="live",
            razorpay_merchant_account_id="",  # Empty prohibited!
            razorpay_key_id="rzp_live_1234567890",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="plan_12345678",
            razorpay_plan_currency="USD",
        )


def test_razorpay_secrets_masked_in_repr_and_str() -> None:
    """SecretStr fields must never expose key_secret or webhook_secret in repr or str."""
    raw_key_secret = "super_secret_rzp_key_999"
    raw_wh_secret = "super_secret_rzp_webhook_888"

    s = Settings(
        environment="development",
        razorpay_enabled=True,
        razorpay_environment="test",
        razorpay_merchant_account_id="acc_dev",
        razorpay_key_id="rzp_test_1234567890",
        razorpay_key_secret=SecretStr(raw_key_secret),
        razorpay_webhook_secret=SecretStr(raw_wh_secret),
        razorpay_pro_plan_id="plan_12345678",
        razorpay_plan_currency="USD",
    )

    s_repr = repr(s)
    s_str = str(s)

    assert raw_key_secret not in s_repr
    assert raw_wh_secret not in s_repr
    assert raw_key_secret not in s_str
    assert raw_wh_secret not in s_str
    assert "**********" in s_repr


def test_razorpay_invalid_plan_id_and_currency_format() -> None:
    """Plan ID must start with 'plan_' and currency must be a 3-letter ISO code."""
    with pytest.raises(ValueError, match="RAZORPAY_PRO_PLAN_ID format is invalid"):
        Settings(
            environment="development",
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id="acc_dev",
            razorpay_key_id="rzp_test_1234567890",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="invalid_prefix_plan",
            razorpay_plan_currency="USD",
        )

    with pytest.raises(ValueError, match="RAZORPAY_PLAN_CURRENCY must be a valid 3-letter ISO currency code"):
        Settings(
            environment="development",
            razorpay_enabled=True,
            razorpay_environment="test",
            razorpay_merchant_account_id="acc_dev",
            razorpay_key_id="rzp_test_1234567890",
            razorpay_key_secret=SecretStr("sec_secret"),
            razorpay_webhook_secret=SecretStr("whsec_secret"),
            razorpay_pro_plan_id="plan_12345678",
            razorpay_plan_currency="DOLLARS",  # Invalid > 3 chars
        )
