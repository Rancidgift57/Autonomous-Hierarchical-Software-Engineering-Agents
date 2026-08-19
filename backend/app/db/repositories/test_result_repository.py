from __future__ import annotations

from app.db.models import TestResultORM
from app.db.repositories.base import BaseRepository


class TestResultRepository(BaseRepository[TestResultORM]):
    model = TestResultORM
    pk_attr = "test_id"

    async def list_by_project(self, project_id: str) -> list[TestResultORM]:
        return await self.list_by(project_id=project_id)

    async def list_by_report(self, report_id: str) -> list[TestResultORM]:
        return await self.list_by(report_id=report_id)
