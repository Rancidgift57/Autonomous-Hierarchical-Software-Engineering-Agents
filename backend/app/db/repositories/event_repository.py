from __future__ import annotations

from sqlalchemy import select

from app.db.models import EventORM
from app.db.repositories.base import BaseRepository


class EventRepository(BaseRepository[EventORM]):
    model = EventORM
    pk_attr = "event_id"

    async def list_by_project(self, project_id: str) -> list[EventORM]:
        stmt = (
            select(EventORM)
            .filter_by(project_id=project_id)
            .order_by(EventORM.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
