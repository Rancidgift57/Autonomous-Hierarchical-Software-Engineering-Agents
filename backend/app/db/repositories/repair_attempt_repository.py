from __future__ import annotations

from app.db.models import RepairAttemptORM
from app.db.repositories.base import BaseRepository


class RepairAttemptRepository(BaseRepository[RepairAttemptORM]):
    model = RepairAttemptORM
    pk_attr = "attempt_id"

    async def list_by_project(self, project_id: str) -> list[RepairAttemptORM]:
        return await self.list_by(project_id=project_id)

    async def list_by_task(self, task_id: str) -> list[RepairAttemptORM]:
        return await self.list_by(task_id=task_id)
