from __future__ import annotations

from app.db.models import ArtifactORM
from app.db.repositories.base import BaseRepository


class ArtifactRepository(BaseRepository[ArtifactORM]):
    model = ArtifactORM
    pk_attr = "artifact_id"

    async def list_by_project(self, project_id: str) -> list[ArtifactORM]:
        return await self.list_by(project_id=project_id)
