from __future__ import annotations

from sqlalchemy import select

from app.db.models import DeploymentRunORM
from app.db.repositories.base import BaseRepository


class DeploymentRunRepository(BaseRepository[DeploymentRunORM]):
    model = DeploymentRunORM
    pk_attr = "run_id"

    async def list_by_project(self, project_id: str) -> list[DeploymentRunORM]:
        stmt = (
            select(DeploymentRunORM)
            .filter_by(project_id=project_id)
            .order_by(DeploymentRunORM.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def latest_for_project(self, project_id: str) -> DeploymentRunORM | None:
        stmt = (
            select(DeploymentRunORM)
            .filter_by(project_id=project_id)
            .order_by(DeploymentRunORM.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
