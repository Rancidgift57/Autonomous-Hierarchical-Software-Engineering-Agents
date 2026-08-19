"""Shared fixtures for the persistence-layer tests (Phase 17).

Each test gets a fresh, isolated SQLite database file under `tmp_path` --
`app.db.session` caches its engine/sessionmaker at module scope (mirroring
`app.llm.config.get_settings`'s `lru_cache`), so tests must call
`reset_engine_cache()` before/after pointing it at a new database, which
`db_settings` below does automatically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.db import session as db_session
from app.db.config import DatabaseSettings


@pytest_asyncio.fixture
async def db_settings(tmp_path) -> AsyncIterator[DatabaseSettings]:
    settings = DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    db_session.reset_engine_cache()
    engine = db_session.get_engine(settings)
    await db_session.init_models(engine)
    try:
        yield settings
    finally:
        await db_session.drop_models(engine)
        # `reset_engine_cache()` only drops the module-level cache
        # reference -- it never closes the pooled connections themselves.
        # Each `aiosqlite` connection runs a background worker thread that
        # calls back into the event loop it was created on
        # (`call_soon_threadsafe`); without an explicit `dispose()` here,
        # that thread outlives this test's function-scoped event loop
        # (which pytest-asyncio tears down immediately after the test),
        # so the callback fires against an already-closed loop and pytest
        # reports a `PytestUnhandledThreadExceptionWarning` /
        # `RuntimeError: Event loop is closed` -- harmless to test
        # correctness, but noisy, and a sign of a real leak (unclosed
        # engine) that this call fixes properly rather than papering over.
        await engine.dispose()
        db_session.reset_engine_cache()


@pytest.fixture
def persist_prompts_settings(db_settings: DatabaseSettings) -> DatabaseSettings:
    """Same database as `db_settings`, but with `persist_llm_prompts=True`."""

    return db_settings.model_copy(update={"persist_llm_prompts": True})
