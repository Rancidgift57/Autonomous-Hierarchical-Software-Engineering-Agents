from __future__ import annotations

from app.db.models import ContractORM
from app.db.repositories.base import BaseRepository


class ContractRepository(BaseRepository[ContractORM]):
    model = ContractORM
    pk_attr = "contract_id"

    async def list_by_project(self, project_id: str) -> list[ContractORM]:
        return await self.list_by(project_id=project_id)
