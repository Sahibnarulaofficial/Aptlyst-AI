"""Tests for configuration and database infrastructure."""

import pytest

from app.config import Settings
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine


def test_settings_loads_defaults() -> None:
    """Settings should load with defaults when no .env is present."""
    s = Settings()
    assert s.database_url.startswith("postgresql")


def test_engine_is_created() -> None:
    """SQLAlchemy async engine should be instantiated without error."""
    assert engine is not None


def test_session_factory_is_created() -> None:
    """AsyncSessionLocal should be a valid session factory."""
    assert AsyncSessionLocal is not None


def test_base_metadata_is_empty_at_milestone_2a() -> None:
    """
    At milestone 2A no application tables exist yet.
    Alembic will manage schema; Base is the future model registry.
    """
    assert Base.metadata is not None


def test_alembic_ini_exists() -> None:
    """alembic.ini must exist so `alembic` commands work."""
    import os
    ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    assert os.path.isfile(ini), "alembic.ini not found"
