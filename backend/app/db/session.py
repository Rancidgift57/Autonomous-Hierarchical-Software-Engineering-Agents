"""Async engine/session management for the persistence layer (Phase 17).

Only this module (plus `app.db.config`) knows how to construct a SQLAlchemy
engine. Everything else -- repositories, `PersistenceService`, the FastAPI
`get_db` dependency, Alembic's `env.py` -- goes through the functions
below, so there is exactly one place that decides pool settings, SQLite
connect args, etc.

Engine/sessionmaker are cached module-level singletons (mirroring
`app.llm.config.get_settings`'s `lru_cache` pattern) so repeated calls
during a process's lifetime reuse the same connection pool. Tests that
need an isolated database should call `reset_engine_cache()` after
overriding `DATABASE_URL` (or after constructing their own
`DatabaseSettings` and passing it explicitly).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.config import DatabaseSettings, get_database_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""

    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_database_settings()
    engine_kwargs: dict[str, object] = {"echo": settings.echo_sql}

    if settings.is_sqlite:
        # SQLite has no real connection pool and doesn't support
        # pool_size/max_overflow; NullPool-like defaults are fine, and
        # check_same_thread=False is required since the async driver hops
        # between the event loop and its worker thread.
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        engine_kwargs["pool_pre_ping"] = settings.pool_pre_ping
        engine_kwargs["pool_size"] = settings.pool_size
        engine_kwargs["max_overflow"] = settings.max_overflow

    _engine = create_async_engine(settings.database_url, **engine_kwargs)
    return _engine


def get_sessionmaker(
    settings: DatabaseSettings | None = None,
) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


def reset_engine_cache() -> None:
    """Drop the cached engine/sessionmaker.

    Intended for tests: call after pointing `DATABASE_URL` at a fresh
    database so the next `get_engine()`/`get_sessionmaker()` call builds a
    new engine instead of reusing a stale one.
    """

    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def init_models(engine: AsyncEngine | None = None) -> None:
    """Create all tables directly from the ORM metadata.

    Convenience for local development/tests against SQLite. Production
    deployments (PostgreSQL) should use the Alembic migrations under
    `alembic/versions/` instead, so schema changes are tracked and
    reviewable rather than inferred from the current model state.
    """

    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_models(engine: AsyncEngine | None = None) -> None:
    """Drop all tables. Test-teardown convenience only."""

    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@asynccontextmanager
async def session_scope(
    settings: DatabaseSettings | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open an `AsyncSession`, commit on success, roll back on error.

    This is the transaction boundary every repository call runs inside --
    `PersistenceService` methods each open exactly one `session_scope` per
    logical operation.
    """

    sessionmaker = get_sessionmaker(settings)
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped `AsyncSession`."""

    async with session_scope() as session:
        yield session
