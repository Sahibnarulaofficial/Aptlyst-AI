"""Alembic environment configuration."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db.base import Base

# Import all models here so Alembic can detect them.
# As new models are added, import them in this block.
from app.models.organization import Organization, OrganizationMember  # noqa: F401
from app.models.chatbot import Chatbot  # noqa: F401
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.billing import OrganizationBillingPeriod, UsageReservation, LLMUsageLog  # noqa: F401
from app.models.generation import GenerationTask  # noqa: F401
from app.models.subscription import Subscription, StripeWebhookEvent  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in offline mode (no DB connection needed)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations with an async engine."""
    connect_args = {}
    if "asyncpg" in settings.database_url:
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0

    connectable = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
