from pathlib import Path
from typing import Any
from pydantic import field_validator
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

    # Add future settings here as the product grows, e.g.:
    # openai_api_key: str = ""
    # redis_url: str = ""
    # stripe_secret_key: str = ""


settings = Settings()
