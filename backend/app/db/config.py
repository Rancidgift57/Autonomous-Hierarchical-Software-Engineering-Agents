"""Environment-driven configuration for the persistence layer (Phase 17).

Mirrors the style of `app.llm.config.LLMSettings`: every knob is read from
the environment (or a local `.env` file), nothing is hard-coded into the
session/engine machinery.

`database_url` accepts any SQLAlchemy 2.x *async* URL. The two officially
supported backends are:

* PostgreSQL (production): ``postgresql+asyncpg://user:pass@host:5432/db``
* SQLite (local development / tests): ``sqlite+aiosqlite:///./ahsea.db``
  or ``sqlite+aiosqlite:///:memory:``

Alembic (`alembic/env.py`) reads the same `DatabaseSettings`, so migrations
always target the same database the application would connect to.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Runtime configuration for the SQLAlchemy engine/session and for what
    the persistence layer is allowed to store."""

    database_url: str = Field(
        default="sqlite+aiosqlite:///./ahsea.db",
        validation_alias="DATABASE_URL",
        description=(
            "Async SQLAlchemy URL. Use postgresql+asyncpg://... in "
            "production and sqlite+aiosqlite://... for local dev/tests."
        ),
    )
    echo_sql: bool = Field(default=False, validation_alias="DATABASE_ECHO")
    pool_size: int = Field(default=5, validation_alias="DATABASE_POOL_SIZE")
    max_overflow: int = Field(default=10, validation_alias="DATABASE_MAX_OVERFLOW")
    pool_pre_ping: bool = Field(default=True, validation_alias="DATABASE_POOL_PRE_PING")

    #: Safety switch (see module docstring / Phase 17 spec): LLM prompt text
    #: and other potentially sensitive request payloads are NEVER persisted
    #: unless this is explicitly turned on.
    persist_llm_prompts: bool = Field(
        default=False, validation_alias="AHSEA_PERSIST_LLM_PROMPTS"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """Return process-wide cached settings, read once from the environment.

    Tests that need different settings should construct
    `DatabaseSettings(...)` directly instead of relying on this cache (see
    `app.db.session.reset_engine_cache` to also drop any cached engine).
    """

    return DatabaseSettings()
