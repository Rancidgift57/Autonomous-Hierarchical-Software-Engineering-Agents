"""Shared declarative base for every ORM model (Phase 17).

A single `MetaData` with an explicit naming convention is used so that
constraint/index names are deterministic -- this makes Alembic
autogeneration stable across SQLite and PostgreSQL (SQLite in particular
tends to leave constraints unnamed otherwise, which breaks autogenerate
diffing).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all AHSEA ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp, used as a Python-side column default.

    Matches `app.state.models._now` so domain <-> ORM round-trips don't
    drift between naive/aware datetimes.
    """

    return datetime.now(UTC)
