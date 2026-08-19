"""Alembic migration environment (Phase 17).

Uses SQLAlchemy 2.x's async engine, matching how the application itself
connects (`app.db.session`). The database URL is read from
`app.db.config.DatabaseSettings` (i.e. `DATABASE_URL` / `.env`) rather than
`alembic.ini`, so migrations and the running application can never point at
different databases by accident.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import every ORM model module so `Base.metadata` is fully populated
# before Alembic compares it against the database for autogeneration.
from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.db.config import get_database_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return get_database_settings().database_url


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live DB connection (`alembic upgrade
    --sql`)."""

    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live, async database connection."""

    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", future=True
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
