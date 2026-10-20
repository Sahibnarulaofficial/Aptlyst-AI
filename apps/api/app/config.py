from pathlib import Path
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

    # Add future settings here as the product grows, e.g.:
    # openai_api_key: str = ""
    # redis_url: str = ""
    # stripe_secret_key: str = ""


settings = Settings()
