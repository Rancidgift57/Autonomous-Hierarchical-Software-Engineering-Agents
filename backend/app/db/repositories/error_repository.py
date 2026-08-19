from __future__ import annotations

from app.db.models import ErrorORM
from app.db.repositories.base import BaseRepository


class ErrorRepository(BaseRepository[ErrorORM]):
    model = ErrorORM
    pk_attr = "error_id"

    async def list_by_project(self, project_id: str) -> list[ErrorORM]:
        return await self.list_by(project_id=project_id)

    async def list_unresolved(self, project_id: str) -> list[ErrorORM]:
        return await self.list_by(project_id=project_id, resolved=False)
