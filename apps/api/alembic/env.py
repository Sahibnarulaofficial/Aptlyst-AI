"""Alembic environment configuration."""

import asyncio
from logging.config import fileConfig
import os
import sys

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

_HAD_PROCESS_ROLE_ENV = "APTLYST_PROCESS_ROLE" in os.environ
if not _HAD_PROCESS_ROLE_ENV and "app.config" not in sys.modules:
    os.environ["APTLYST_PROCESS_ROLE"] = "migration"
try:
    from app.config import settings
finally:
    if not _HAD_PROCESS_ROLE_ENV:
        os.environ.pop("APTLYST_PROCESS_ROLE", None)

settings.validate_migration_configuration()
from app.db.base import Base

# Import all models here so Alembic can detect them.
# As new models are added, import them in this block.
from app.models.organization import Organization, OrganizationMember  # noqa: F401
from app.models.chatbot import Chatbot  # noqa: F401
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.billing import OrganizationBillingPeriod, UsageReservation, LLMUsageLog  # noqa: F401
from app.models.generation import GenerationTask  # noqa: F401
from app.models.generation_attempt import GenerationTaskAttempt  # noqa: F401
from app.models.subscription import (  # noqa: F401
    BillingReconciliation,
    StripeCheckoutAttempt,
    StripeInvoice,
    StripeWebhookEvent,
    Subscription,
)
from app.models.payment import (  # noqa: F401
    BillingCheckoutAttempt,
    BillingCustomerProvision,
    BillingInvoice,
    BillingPayment,
    BillingWebhookEvent,
    BillingWorkerHeartbeat,
    CustomerProvisionStatus,
    PaymentReconciliation,
)

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
        context.execute("ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY")
        context.execute("REVOKE ALL ON alembic_version FROM anon, authenticated")


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
        context.execute("ALTER TABLE IF EXISTS alembic_version ENABLE ROW LEVEL SECURITY")
        context.execute("REVOKE ALL ON alembic_version FROM anon, authenticated")


async def run_migrations_online() -> None:
    """Run migrations with an async engine."""
    connect_args = {}
    if "asyncpg" in settings.database_url:
        connect_args["statement_cache_size"] = 0

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
