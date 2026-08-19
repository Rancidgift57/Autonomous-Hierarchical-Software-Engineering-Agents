from __future__ import annotations

from app.db.models import ArchitectureDecisionORM
from app.db.repositories.base import BaseRepository


class ArchitectureDecisionRepository(BaseRepository[ArchitectureDecisionORM]):
    model = ArchitectureDecisionORM
    pk_attr = "decision_id"

    async def list_by_project(self, project_id: str) -> list[ArchitectureDecisionORM]:
        return await self.list_by(project_id=project_id)
