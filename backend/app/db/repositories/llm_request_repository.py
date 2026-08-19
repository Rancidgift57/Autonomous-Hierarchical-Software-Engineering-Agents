from __future__ import annotations

from app.db.models import LLMRequestORM
from app.db.repositories.base import BaseRepository


class LLMRequestRepository(BaseRepository[LLMRequestORM]):
    model = LLMRequestORM
    pk_attr = "request_id"

    async def list_by_project(self, project_id: str) -> list[LLMRequestORM]:
        return await self.list_by(project_id=project_id)

    async def list_by_task_type(self, task_type: str) -> list[LLMRequestORM]:
        return await self.list_by(task_type=task_type)
