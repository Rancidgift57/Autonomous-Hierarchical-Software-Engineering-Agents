from __future__ import annotations

from app.db.models import TaskORM
from app.db.repositories.base import BaseRepository


class TaskRepository(BaseRepository[TaskORM]):
    model = TaskORM
    pk_attr = "task_id"

    async def list_by_project(self, project_id: str) -> list[TaskORM]:
        return await self.list_by(project_id=project_id)

    async def list_by_status(self, project_id: str, status: str) -> list[TaskORM]:
        return await self.list_by(project_id=project_id, status=status)
