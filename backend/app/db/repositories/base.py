"""Generic base repository (Phase 17).

Provides the CRUD operations every table-specific repository needs
(`get`, `list_all`, `list_by`, `add`, `upsert`, `delete`) so subclasses
only need to declare `model` and `pk_attr`, plus any domain-specific query
helpers.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Session-scoped CRUD repository for one ORM model.

    A repository never opens or closes its own session/transaction --
    that's `app.db.session.session_scope`'s job, driven by
    `PersistenceService`. This keeps repositories composable: several
    repository calls can share one transaction.
    """

    model: type[ModelT]
    pk_attr: str = "id"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, instance: ModelT) -> ModelT:
        """Insert a brand-new row. Raises on primary-key collision."""

        self.session.add(instance)
        await self.session.flush()
        return instance

    async def get(self, pk: Any) -> ModelT | None:
        return await self.session.get(self.model, pk)

    async def list_all(self) -> list[ModelT]:
        result = await self.session.execute(select(self.model))
        return list(result.scalars().all())

    async def list_by(self, **filters: Any) -> list[ModelT]:
        stmt = select(self.model).filter_by(**filters)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, pk: Any) -> bool:
        instance = await self.get(pk)
        if instance is None:
            return False
        await self.session.delete(instance)
        await self.session.flush()
        return True

    async def upsert(self, instance: ModelT) -> ModelT:
        """Insert `instance`, or update the existing row with the same
        primary key if one already exists.

        Used for entities the domain layer mutates in place (projects,
        tasks, agents, contracts, ...); append-only entities (events,
        errors, llm_requests, ...) should use `add` instead.
        """

        pk = getattr(instance, self.pk_attr)
        existing = await self.get(pk)
        if existing is None:
            self.session.add(instance)
            await self.session.flush()
            return instance

        merged = await self.session.merge(instance)
        await self.session.flush()
        return merged
