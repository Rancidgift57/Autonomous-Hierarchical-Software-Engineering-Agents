from __future__ import annotations

from app.db.models import AgentORM
from app.db.repositories.base import BaseRepository


class AgentRepository(BaseRepository[AgentORM]):
    model = AgentORM
    pk_attr = "agent_id"

    async def list_by_project(self, project_id: str) -> list[AgentORM]:
        return await self.list_by(project_id=project_id)
