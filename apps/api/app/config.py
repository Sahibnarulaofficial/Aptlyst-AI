import ipaddress
from pathlib import Path
from typing import Any
import urllib.parse
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_env_path = Path(__file__).resolve().parent.parent / ".env"

PRODUCTION_DEPLOYMENT_ENVIRONMENTS: frozenset[str] = frozenset({"production", "prod"})
NON_PRODUCTION_DEPLOYMENT_ENVIRONMENTS: frozenset[str] = frozenset(
    {"development", "dev", "staging", "stage", "test", "testing", "ci", "local"}
)
ALLOWED_RAZORPAY_ENVIRONMENTS: frozenset[str] = frozenset({"test", "live"})
ALLOWED_PROCESS_ROLES: frozenset[str] = frozenset(
    {"api", "webhook_worker", "recovery_worker", "migration"}
)
PROHIBITED_PRODUCTION_MERCHANT_IDS: frozenset[str] = frozenset(
    {"default", "placeholder", "none", "null", "test", "dev", "stage", "staging", "ci", "legacy", ""}
)
LOCALHOST_ORIGIN_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def parse_and_validate_cors_origins(
    raw_origins: str,
    *,
    is_production: bool = False,
) -> list[str]:
    """Parse and validate comma-separated CORS allowed origins (fail-closed).

    Rejects wildcards ('*', '*.example.com'), non-HTTP(S) schemes, paths/queries/fragments,
    userinfo, whitespace, and non-localhost HTTP origins in production.
    """
    if not isinstance(raw_origins, str):
        raise ValueError("CORS_ALLOWED_ORIGINS must be a comma-separated string of origins.")

    origins: list[str] = []
    seen: set[str] = set()
    for part in raw_origins.split(","):
        origin = part.strip()
        if not origin:
            continue
        if "*" in origin:
            raise ValueError(
                "Wildcard CORS origins ('*') are prohibited when credentialed requests are enabled."
            )
        if any(ch.isspace() for ch in origin):
            raise ValueError(f"Invalid CORS origin '{origin}': whitespace is not permitted.")

        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Invalid CORS origin '{origin}': scheme must be 'http' or 'https'."
            )
        if not parsed.netloc or not parsed.hostname:
            raise ValueError(f"Invalid CORS origin '{origin}': missing valid host.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"Invalid CORS origin '{origin}': userinfo is prohibited.")
        if parsed.path not in ("",) or parsed.query or parsed.fragment:
            raise ValueError(
                f"Invalid CORS origin '{origin}': must be a pure origin without path, query, or fragment."
            )
        if is_production and parsed.scheme == "http" and parsed.hostname.lower() not in LOCALHOST_ORIGIN_HOSTS:
            raise ValueError(
                f"Invalid production CORS origin '{origin}': non-localhost origins must use https://."
            )

        if origin not in seen:
            seen.add(origin)
            origins.append(origin)

    return origins


def validate_deployment_environment(deployment_env: str | None = None) -> tuple[str, bool]:
    """Validate and classify the deployment environment signal (fail-closed).

    Returns:
        tuple[str, bool]: (normalized_environment, is_production)

    Raises:
        ValueError: If deployment environment is empty, non-string, or unrecognized.
    """
    raw = settings.environment if deployment_env is None else deployment_env
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "ENVIRONMENT must be explicitly configured with a recognized deployment environment."
        )
    normalized = raw.strip().lower()
    if normalized in PRODUCTION_DEPLOYMENT_ENVIRONMENTS:
        return normalized, True
    if normalized in NON_PRODUCTION_DEPLOYMENT_ENVIRONMENTS:
        return normalized, False
    raise ValueError(
        f"Unrecognized deployment ENVIRONMENT '{raw}'. "
        "Must be a recognized production ('production', 'prod') or non-production environment."
    )


def is_production_environment(deployment_env: str | None = None) -> bool:
    """Return True if deployment environment is recognized production; fail closed if invalid."""
    _, is_prod = validate_deployment_environment(deployment_env)
    return is_prod


def resolve_required_razorpay_environment(deployment_env: str | None = None) -> str:
    """Return the strictly required Razorpay environment ('live' in prod, 'test' otherwise)."""
    _, is_prod = validate_deployment_environment(deployment_env)
    return "live" if is_prod else "test"


def validate_razorpay_runtime_scope(
    *,
    deployment_env: str | None = None,
    razorpay_env: str | None = None,
    merchant_account_id: str | None = None,
) -> tuple[str, str]:
    """Validate Razorpay merchant account and environment against deployment policy (fail-closed).

    Guarantees (Option A):
    - Unrecognized or missing deployment ENVIRONMENT fails closed.
    - RAZORPAY_ENVIRONMENT must be explicitly 'test' or 'live'.
    - Production deployments ('production', 'prod') strictly require RAZORPAY_ENVIRONMENT='live'
      and an explicit non-default merchant account identifier.
    - Non-production deployments strictly require RAZORPAY_ENVIRONMENT='test'.

    Returns:
        tuple[str, str]: (merchant_account_id, razorpay_environment)
    """
    _, is_prod = validate_deployment_environment(deployment_env)

    raw_rzp_env = settings.razorpay_environment if razorpay_env is None else razorpay_env
    clean_rzp_env = (raw_rzp_env or "").strip().lower() if isinstance(raw_rzp_env, str) else ""
    if clean_rzp_env not in ALLOWED_RAZORPAY_ENVIRONMENTS:
        raise ValueError(
            f"Invalid Razorpay environment '{raw_rzp_env}'. Must be 'test' or 'live'."
        )

    raw_merchant = (
        settings.razorpay_merchant_account_id
        if merchant_account_id is None
        else merchant_account_id
    )
    clean_merchant = (raw_merchant or "").strip() if isinstance(raw_merchant, str) else ""
    if not clean_merchant:
        raise ValueError("Razorpay merchant account ID must be explicitly configured.")

    if is_prod:
        if clean_rzp_env != "live":
            raise ValueError(
                "Production RAZORPAY_ENVIRONMENT must be 'live'."
            )
        if clean_merchant.lower() in PROHIBITED_PRODUCTION_MERCHANT_IDS:
            raise ValueError(
                "Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured "
                "with a non-default account identifier."
            )
    else:
        if clean_rzp_env != "test":
            raise ValueError(
                "Non-production RAZORPAY_ENVIRONMENT must be 'test'."
            )

    return clean_merchant, clean_rzp_env


def get_canonical_razorpay_merchant_account_id(
    *,
    deployment_env: str | None = None,
    merchant_account_id: str | None = None,
) -> str:
    """Return the single authoritative Aptlyst AI Razorpay merchant account identifier.

    Guarantees:
    - Fails closed if deployment environment or merchant account ID is invalid.
    - In production, strictly requires an explicit non-prohibited live merchant identity.
    - In non-production, returns the canonical test merchant identity.
    - Customer organization IDs must NEVER be used as merchant account IDs.
    """
    clean_merchant, _ = validate_razorpay_runtime_scope(
        deployment_env=deployment_env,
        merchant_account_id=merchant_account_id,
    )
    return clean_merchant


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_env_path, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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

    process_role: str = Field(
        default="api",
        validation_alias=AliasChoices("process_role", "APTLYST_PROCESS_ROLE"),
        description="Runtime process profile controlling least-privilege startup validation.",
    )

    @field_validator("process_role")
    @classmethod
    def validate_process_role(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                f"APTLYST_PROCESS_ROLE must be one of {sorted(ALLOWED_PROCESS_ROLES)}."
            )
        normalized = v.strip().lower()
        if normalized not in ALLOWED_PROCESS_ROLES:
            raise ValueError(
                f"Unsupported APTLYST_PROCESS_ROLE '{v}'. "
                f"Must be one of {sorted(ALLOWED_PROCESS_ROLES)}."
            )
        return normalized

    environment: str = "development"
    cors_allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def parsed_cors_allowed_origins(self) -> list[str]:
        """Return validated list of configured CORS allowed origins."""
        _, is_prod = validate_deployment_environment(self.environment)
        return parse_and_validate_cors_origins(
            self.cors_allowed_origins,
            is_production=is_prod,
        )

    # Secret used to sign anonymous visitor session tokens (HMAC-SHA256).
    # In production, VISITOR_SESSION_SECRET must be explicitly set and >= 32 characters.
    visitor_session_secret: str = ""
    # Optional secondary/previous secret for zero-downtime key rotation grace periods
    visitor_session_secret_secondary: str = ""
    visitor_session_ttl_seconds: int = 604800  # 7 days (60 * 60 * 24 * 7)

    def model_post_init(self, __context: Any) -> None:
        """Validate configuration based on the active runtime process role."""
        validate_deployment_environment(self.environment)
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("DATABASE_URL must be explicitly configured.")

        role = self.process_role
        if role == "api":
            self.validate_api_configuration()
        elif role == "webhook_worker":
            self.validate_webhook_worker_configuration()
        elif role == "recovery_worker":
            self.validate_recovery_worker_configuration()
        elif role == "migration":
            self.validate_migration_configuration()

    def validate_api_configuration(self) -> None:
        """Validate full API server configuration (auth, visitor sessions, CORS, LLM, billing)."""
        _, is_prod = validate_deployment_environment(self.environment)
        parse_and_validate_cors_origins(self.cors_allowed_origins, is_production=is_prod)

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

        if self.stripe_enabled:
            self.validate_stripe_configuration()

        if self.razorpay_enabled:
            self.validate_razorpay_configuration(
                require_ingress_secret=True,
                require_api_credentials=True,
                require_plan=True,
            )

    def validate_webhook_worker_configuration(self) -> None:
        """Validate configuration required by the Razorpay webhook worker process.

        Does NOT require API-only secrets (VISITOR_SESSION_SECRET, RAZORPAY_WEBHOOK_SECRET,
        RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, or RAZORPAY_PRO_PLAN_ID).
        """
        validate_deployment_environment(self.environment)
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("DATABASE_URL must be explicitly configured.")
        if self.razorpay_enabled:
            self.validate_razorpay_configuration(
                require_ingress_secret=False,
                require_api_credentials=False,
                require_plan=False,
            )

    def validate_recovery_worker_configuration(self) -> None:
        """Validate configuration required by the Razorpay checkout recovery worker process.

        Requires Razorpay API credentials, canonical merchant scope, and plan configuration,
        but does NOT require API-only secrets (VISITOR_SESSION_SECRET or RAZORPAY_WEBHOOK_SECRET).
        """
        validate_deployment_environment(self.environment)
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("DATABASE_URL must be explicitly configured.")
        if self.razorpay_enabled:
            self.validate_razorpay_configuration(
                require_ingress_secret=False,
                require_api_credentials=True,
                require_plan=True,
            )

    def validate_migration_configuration(self) -> None:
        """Validate minimal configuration required by Alembic migrations (DB & environment)."""
        validate_deployment_environment(self.environment)
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise ValueError("DATABASE_URL must be explicitly configured for migrations.")

    def validate_stripe_configuration(self) -> None:
        """Validate legacy Stripe billing configuration when enabled."""
        _, is_prod = validate_deployment_environment(self.environment)
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

    def validate_razorpay_configuration(
        self,
        *,
        require_ingress_secret: bool = True,
        require_api_credentials: bool = True,
        require_plan: bool = True,
    ) -> None:
        """Validate Razorpay billing configuration against Option A deployment policy (fail-closed)."""
        if not self.razorpay_enabled:
            return

        _, is_prod = validate_deployment_environment(self.environment)

        rzp_env = (self.razorpay_environment or "").strip().lower()
        if rzp_env not in ALLOWED_RAZORPAY_ENVIRONMENTS:
            raise ValueError("RAZORPAY_ENVIRONMENT must be either 'test' or 'live'.")

        key_id = self.razorpay_key_id.strip()
        key_secret = self.razorpay_key_secret.get_secret_value().strip()
        webhook_secret = self.razorpay_webhook_secret.get_secret_value().strip()
        plan_id = self.razorpay_pro_plan_id.strip()
        plan_currency = self.razorpay_plan_currency.strip().upper()
        merchant_id = self.razorpay_merchant_account_id.strip()

        if require_api_credentials:
            if not key_id:
                raise ValueError(
                    "RAZORPAY_KEY_ID must be configured when Razorpay billing is enabled."
                )
            if not key_secret:
                raise ValueError(
                    "RAZORPAY_KEY_SECRET must be configured when Razorpay billing is enabled."
                )

        if require_ingress_secret and not webhook_secret:
            raise ValueError(
                "RAZORPAY_WEBHOOK_SECRET must be configured when Razorpay billing is enabled."
            )

        if require_plan:
            if not plan_id:
                raise ValueError(
                    "RAZORPAY_PRO_PLAN_ID must be configured when Razorpay billing is enabled."
                )
            if not plan_id.startswith("plan_") or len(plan_id) < 8:
                raise ValueError(
                    "RAZORPAY_PRO_PLAN_ID format is invalid. It must start with 'plan_'."
                )
            if len(plan_currency) != 3 or not plan_currency.isalpha():
                raise ValueError(
                    "RAZORPAY_PLAN_CURRENCY must be a valid 3-letter ISO currency code (e.g., 'USD', 'INR')."
                )
        elif plan_id and (not plan_id.startswith("plan_") or len(plan_id) < 8):
            raise ValueError(
                "RAZORPAY_PRO_PLAN_ID format is invalid. It must start with 'plan_'."
            )

        if is_prod:
            if rzp_env != "live":
                raise ValueError(
                    "RAZORPAY_ENVIRONMENT must be 'live' in production environments."
                )
            if key_id:
                if key_id.startswith("rzp_test_"):
                    raise ValueError(
                        "Test Razorpay key is prohibited in production environments."
                    )
                if not key_id.startswith("rzp_live_"):
                    raise ValueError(
                        "Production Razorpay key must start with 'rzp_live_'."
                    )
            if not merchant_id or merchant_id.lower() in PROHIBITED_PRODUCTION_MERCHANT_IDS:
                raise ValueError(
                    "Production RAZORPAY_MERCHANT_ACCOUNT_ID must be explicitly configured with a non-default account identifier."
                )
        else:
            # Staging & Local development / testing
            if rzp_env != "test":
                raise ValueError(
                    "RAZORPAY_ENVIRONMENT must be 'test' in non-production environments."
                )
            if key_id:
                if key_id.startswith("rzp_live_"):
                    raise ValueError(
                        "Live Razorpay key is prohibited in non-production environments."
                    )
                if not key_id.startswith("rzp_test_"):
                    raise ValueError(
                        "Development/test Razorpay key must start with 'rzp_test_'."
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

    # Razorpay Billing Configuration (Step 5.2B)
    razorpay_enabled: bool = False
    razorpay_environment: str = "test"
    razorpay_merchant_account_id: str = "default"
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = Field(default=SecretStr(""))
    razorpay_webhook_secret: SecretStr = Field(default=SecretStr(""))
    razorpay_pro_plan_id: str = ""
    razorpay_plan_currency: str = "USD"

    # Worker Heartbeat & Queue Monitoring Thresholds (Stage 2J.4 / Stage 2K-C)
    worker_heartbeat_interval_seconds: float = Field(default=30.0, gt=0.0)
    worker_heartbeat_stopped_retention_seconds: int = Field(default=604800, ge=3600)
    worker_heartbeat_abandoned_retention_seconds: int = Field(default=2592000, ge=3600)
    billing_health_heartbeat_stale_seconds: int = Field(default=90, ge=1)
    billing_health_webhook_backlog_age_seconds: int = Field(default=300, ge=1)
    billing_health_webhook_backlog_count_threshold: int = Field(default=50, ge=1)
    billing_health_webhook_failed_count_threshold: int = Field(default=5, ge=1)
    billing_health_recovery_overdue_age_seconds: int = Field(default=300, ge=1)
    billing_health_reconciliation_backlog_age_seconds: int = Field(default=3600, ge=1)
    billing_health_repeated_lease_expiry_threshold: int = Field(default=2, ge=1)

    # Operational Alert Delivery & Deduplication (Stage 2K-C, strictly opt-in)
    billing_health_alert_webhook_enabled: bool = False
    billing_health_alert_webhook_url: str = ""
    billing_health_alert_webhook_bearer_token: SecretStr = Field(default=SecretStr(""))
    billing_health_alert_webhook_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    billing_health_alert_state_file: str = ""
    billing_health_alert_renotify_seconds: int = Field(default=0, ge=0)

    def get_public_razorpay_config(self) -> dict[str, Any]:
        """Return safe, non-sensitive Razorpay configuration for client-side consumption.

        Guarantees that secret keys (razorpay_key_secret, razorpay_webhook_secret) are never exposed.
        """
        return {
            "enabled": self.razorpay_enabled,
            "environment": self.razorpay_environment if self.razorpay_enabled else "test",
            "key_id": self.razorpay_key_id if self.razorpay_enabled else "",
            "merchant_account_id": self.razorpay_merchant_account_id if self.razorpay_enabled else "",
            "pro_plan_id": self.razorpay_pro_plan_id if self.razorpay_enabled else "",
            "plan_currency": self.razorpay_plan_currency if self.razorpay_enabled else "USD",
        }


settings = Settings()
