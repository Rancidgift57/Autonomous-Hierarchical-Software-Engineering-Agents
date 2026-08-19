from __future__ import annotations

from sqlalchemy import select

from app.db.models import ProjectORM
from app.db.repositories.base import BaseRepository


class ProjectRepository(BaseRepository[ProjectORM]):
    model = ProjectORM
    pk_attr = "project_id"

    async def list_ids(self) -> list[str]:
        result = await self.session.execute(select(ProjectORM.project_id))
        return list(result.scalars().all())
