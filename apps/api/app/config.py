import ipaddress
from pathlib import Path
from typing import Any
import urllib.parse
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_env_path = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_env_path, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aptlyst"

    # Supabase project settings
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # JWT secret from Supabase Dashboard → Project Settings → API → JWT Secret
    # Used server-side only to verify access tokens. Never exposed to the browser.
    supabase_jwt_secret: str = ""
    supabase_jwt_algorithm: str = "ES256"
    supabase_jwt_audience: str = "authenticated"

    @field_validator("supabase_jwt_algorithm")
    @classmethod
    def validate_jwt_algorithm(cls, v: str) -> str:
        if isinstance(v, str) and v.lower() == "none":
            raise ValueError("Insecure JWT algorithm 'none' is not permitted")
        return v

    @field_validator("database_url", mode="before")
    @classmethod
    def assemble_async_database_url(cls, v: str) -> str:
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+asyncpg://", 1)
            elif v.startswith("postgresql://") and not v.startswith("postgresql+"):
                return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    environment: str = "development"

    # Secret used to sign anonymous visitor session tokens (HMAC-SHA256).
    # In production, VISITOR_SESSION_SECRET must be explicitly set and >= 32 characters.
    visitor_session_secret: str = ""
    # Optional secondary/previous secret for zero-downtime key rotation grace periods
    visitor_session_secret_secondary: str = ""
    visitor_session_ttl_seconds: int = 604800  # 7 days (60 * 60 * 24 * 7)

    def model_post_init(self, __context: Any) -> None:
        """Validate critical production secrets on application startup."""
        is_prod = self.environment.strip().lower() in ("production", "prod")
        if is_prod:
            secret = self.visitor_session_secret.strip()
            if not secret or secret == "dev-insecure-visitor-session-secret-change-in-production":
                raise ValueError(
                    "VISITOR_SESSION_SECRET must be securely configured in production environments. "
                    "Insecure default or empty secrets are prohibited."
                )
            if len(secret) < 32:
                raise ValueError(
                    "VISITOR_SESSION_SECRET must be at least 32 characters long in production."
                )
            if self.llm_provider.strip().lower() == "mock" and not self.llm_allow_mock_in_production:
                raise ValueError(
                    "Mock LLM provider is strictly for development and automated testing. "
                    "It is prohibited in production environments."
                )

        # Stripe Billing Configuration Validation
        if self.stripe_enabled:
            secret = self.stripe_secret_key.get_secret_value().strip()
            if not secret:
                raise ValueError(
                    "STRIPE_SECRET_KEY must be configured when Stripe billing is enabled."
                )

            webhook_secret = self.stripe_webhook_secret.get_secret_value().strip()
            if not webhook_secret:
                raise ValueError(
                    "STRIPE_WEBHOOK_SECRET must be configured when Stripe billing is enabled."
                )
            if (
                not webhook_secret.startswith("whsec_")
                or len(webhook_secret) < 15
                or not all(c.isalnum() or c == "_" for c in webhook_secret[6:])
            ):
                raise ValueError(
                    "STRIPE_WEBHOOK_SECRET format is invalid. It must start with 'whsec_' "
                    "followed by alphanumeric characters. (Note: format validation does not verify existence in Stripe.)"
                )

            pub_key = self.stripe_publishable_key.strip()
            if not pub_key:
                raise ValueError(
                    "STRIPE_PUBLISHABLE_KEY must be configured when Stripe billing is enabled."
                )

            price_id = self.stripe_pro_price_id.strip()
            if not price_id:
                raise ValueError(
                    "STRIPE_PRO_PRICE_ID must be configured when Stripe billing is enabled."
                )
            if (
                not price_id.startswith("price_")
                or len(price_id) < 14
                or not all(c.isalnum() or c == "_" for c in price_id[6:])
            ):
                raise ValueError(
                    "STRIPE_PRO_PRICE_ID format is invalid. It must start with 'price_' "
                    "followed by alphanumeric characters. (Note: format validation does not verify existence in Stripe.)"
                )

            return_url = self.stripe_billing_return_url.strip()
            if not return_url:
                raise ValueError(
                    "STRIPE_BILLING_RETURN_URL must be configured when Stripe billing is enabled."
                )

            parsed_url = urllib.parse.urlsplit(return_url)
            if parsed_url.scheme not in ("http", "https"):
                raise ValueError(
                    "STRIPE_BILLING_RETURN_URL must use an allowed scheme ('http' or 'https')."
                )
            if not parsed_url.netloc or not parsed_url.hostname or any(c.isspace() for c in parsed_url.netloc):
                raise ValueError(
                    "STRIPE_BILLING_RETURN_URL must contain a valid, non-empty host without whitespace."
                )

            # Test / Live environment separation and deployment HTTPS requirements
            is_staging = self.environment.strip().lower() in ("staging",)
            if is_prod:
                if secret.startswith(("sk_test_", "rk_test_")):
                    raise ValueError(
                        "Test Stripe secret key is prohibited in production environments."
                    )
                if not (secret.startswith("sk_live_") or secret.startswith("rk_live_")):
                    raise ValueError(
                        "Production Stripe secret key must start with 'sk_live_' or 'rk_live_'."
                    )
                if pub_key.startswith("pk_test_"):
                    raise ValueError(
                        "Test Stripe publishable key is prohibited in production environments."
                    )
                if not pub_key.startswith("pk_live_"):
                    raise ValueError(
                        "Production Stripe publishable key must start with 'pk_live_'."
                    )
                if parsed_url.scheme != "https":
                    raise ValueError(
                        "STRIPE_BILLING_RETURN_URL must use https:// in production environments."
                    )
            elif is_staging:
                if secret.startswith(("sk_live_", "rk_live_")):
                    raise ValueError(
                        "Live Stripe secret key is prohibited in non-production environments."
                    )
                if not (secret.startswith("sk_test_") or secret.startswith("rk_test_")):
                    raise ValueError(
                        "Development/test Stripe secret key must start with 'sk_test_' or 'rk_test_'."
                    )
                if pub_key.startswith("pk_live_"):
                    raise ValueError(
                        "Live Stripe publishable key is prohibited in non-production environments."
                    )
                if not pub_key.startswith("pk_test_"):
                    raise ValueError(
                        "Development/test Stripe publishable key must start with 'pk_test_'."
                    )
                if parsed_url.scheme != "https":
                    raise ValueError(
                        "STRIPE_BILLING_RETURN_URL must use https:// in staging environments."
                    )
            else:
                # Local development & automated testing
                if secret.startswith(("sk_live_", "rk_live_")):
                    raise ValueError(
                        "Live Stripe secret key is prohibited in non-production environments."
                    )
                if not (secret.startswith("sk_test_") or secret.startswith("rk_test_")):
                    raise ValueError(
                        "Development/test Stripe secret key must start with 'sk_test_' or 'rk_test_'."
                    )
                if pub_key.startswith("pk_live_"):
                    raise ValueError(
                        "Live Stripe publishable key is prohibited in non-production environments."
                    )
                if not pub_key.startswith("pk_test_"):
                    raise ValueError(
                        "Development/test Stripe publishable key must start with 'pk_test_'."
                    )
                if parsed_url.scheme == "http" and parsed_url.hostname.lower() not in (
                    "localhost",
                    "127.0.0.1",
                    "::1",
                    "0.0.0.0",
                ):
                    raise ValueError(
                        "STRIPE_BILLING_RETURN_URL scheme http:// is only permitted for localhost in development. "
                        "Remote return URLs must use https://."
                    )

    @property
    def resolved_visitor_session_secret(self) -> str:
        """Return the primary secret for HMAC-SHA256 visitor session tokens.

        In production, raises RuntimeError if VISITOR_SESSION_SECRET is missing or insecure.
        In test/dev environments, falls back to a derivation from supabase_jwt_secret
        or a deterministic fallback to prevent test crashes.
        """
        secret = self.visitor_session_secret.strip()
        is_prod = self.environment.strip().lower() in ("production", "prod")

        if is_prod:
            if not secret or secret == "dev-insecure-visitor-session-secret-change-in-production":
                raise RuntimeError(
                    "VISITOR_SESSION_SECRET must be securely configured in production environments."
                )
            if len(secret) < 32:
                raise RuntimeError(
                    "VISITOR_SESSION_SECRET must be at least 32 characters long in production."
                )
            return secret

        if secret:
            return secret
        if self.supabase_jwt_secret:
            return f"visitor-session:{self.supabase_jwt_secret}"
        return "dev-insecure-visitor-session-secret-change-in-production"

    @property
    def verification_visitor_session_secrets(self) -> list[str]:
        """Return the list of active secrets for verifying visitor tokens.

        Supports zero-downtime secret rotation:
        - Primary secret is evaluated first.
        - If secondary secret is configured, it is tried as fallback to allow
          existing valid sessions to complete their TTL.
        """
        secrets = [self.resolved_visitor_session_secret]
        secondary = self.visitor_session_secret_secondary.strip()
        if secondary:
            secrets.append(secondary)
        return secrets

    # Rate Limiting & Abuse Protection
    rate_limiting_enabled: bool = True
    rate_limit_storage: str = "memory"  # "memory" or "redis"
    redis_url: str = ""
    redis_fallback_policy: str = "memory"  # "memory", "fail_open", "fail_closed"
    redis_connect_timeout_seconds: float = 1.0
    redis_command_timeout_seconds: float = 1.0
    trusted_proxies: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    rate_limit_max_keys: int = 10000
    rate_limit_capacity_overflow_policy: str = "evict_oldest"  # "evict_oldest", "drop_new"

    # Configurable Rate Limits
    rate_limit_public_config_per_minute: int = 60
    rate_limit_conversation_create_per_minute: int = 10
    rate_limit_conversation_create_per_hour: int = 50
    rate_limit_message_create_per_visitor_minute: int = 20
    rate_limit_message_create_per_ip_minute: int = 40
    rate_limit_messages_list_per_visitor_minute: int = 30
    rate_limit_messages_list_per_ip_minute: int = 60

    @property
    def parsed_trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Parse trusted_proxies string into IPv4/IPv6 networks."""
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in self.trusted_proxies.split(","):
            raw_str = raw.strip()
            if raw_str:
                try:
                    networks.append(ipaddress.ip_network(raw_str, strict=False))
                except ValueError:
                    pass
        return networks

    # AI Generation Foundation (Milestone 3 - Phase 1)
    llm_provider: str = "disabled"  # "disabled" by default; set to "mock" for local dev/testing
    llm_model: str = "mock-gpt-4o-mini"
    llm_timeout_seconds: float = 10.0
    llm_max_output_tokens: int = 1000
    llm_temperature: float = 0.7
    llm_max_context_messages: int = 10
    llm_allow_mock_in_production: bool = False

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, v: str) -> str:
        """Validate that llm_provider is one of the supported Phase 1 options."""
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized not in ("disabled", "mock"):
                raise ValueError(
                    f"Unsupported LLM provider '{v}'. "
                    "Phase 1 accepts only 'disabled' or 'mock'."
                )
            return normalized
        raise ValueError("llm_provider must be a string")

    # Stripe Billing Configuration (Milestone 4)
    stripe_enabled: bool = False
    stripe_secret_key: SecretStr = Field(default=SecretStr(""))
    stripe_webhook_secret: SecretStr = Field(default=SecretStr(""))
    stripe_publishable_key: str = ""
    stripe_pro_price_id: str = ""
    stripe_billing_return_url: str = "http://localhost:3000/billing"
    billing_grace_period_days: int = 7
    default_free_monthly_token_quota: int = Field(
        default=0,
        description="Initial monthly token quota for Free organizations. Defaults to 0 (fail-closed) until approved.",
    )
    default_pro_monthly_token_quota: int = Field(
        default=0,
        ge=0,
        description="Initial monthly token quota for Pro organizations. Defaults to 0 (fail-closed) until configured.",
    )

    def get_public_stripe_config(self) -> dict[str, Any]:
        """Return safe, non-sensitive Stripe configuration for client-side consumption.

        Guarantees that secret keys (stripe_secret_key, stripe_webhook_secret) are never exposed.
        """
        return {
            "enabled": self.stripe_enabled,
            "publishable_key": self.stripe_publishable_key if self.stripe_enabled else "",
            "pro_price_id": self.stripe_pro_price_id if self.stripe_enabled else "",
            "billing_return_url": self.stripe_billing_return_url if self.stripe_enabled else "",
        }


settings = Settings()
