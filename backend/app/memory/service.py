"""Persistent curated memory with a lexical retrieval strategy.

The interface intentionally separates scoring from storage, making a future
embedding/vector implementation a drop-in replacement without API changes.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.config import DatabaseSettings, get_database_settings
from app.db.models import MemoryORM
from app.db.session import session_scope


class MemoryType(StrEnum):
    PROJECT = "project"
    DECISION = "decision"
    CONTRACT = "contract"
    ARTIFACT = "artifact"
    FAILURE = "failure"
    REPAIR = "repair"


class StoredMemory(BaseModel):
    memory_id: str
    project_id: str
    memory_type: MemoryType
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    valid: bool = True
    created_at: datetime


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{2,}", value.lower()))


class MemoryService:
    """Explicit store only: callers choose durable facts; no chat is captured."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = settings or get_database_settings()

    async def store(self, project_id: str, memory_type: MemoryType, title: str, content: str, *, tags: list[str] | None = None, importance: float = 0.5) -> StoredMemory:
        row = MemoryORM(memory_id=f"mem_{uuid.uuid4().hex}", project_id=project_id, memory_type=memory_type.value, title=title, content=content, tags=tags or [], importance=max(0.0, min(1.0, importance)))
        async with session_scope(self.settings) as session:
            session.add(row)
            await session.flush()
            return self._to_model(row)

    async def retrieve(self, project_id: str, query: str, *, limit: int = 8, memory_types: list[MemoryType] | None = None) -> list[StoredMemory]:
        query_tokens = _tokens(query)
        async with session_scope(self.settings) as session:
            stmt = select(MemoryORM).where(MemoryORM.project_id == project_id, MemoryORM.valid.is_(True))
            if memory_types:
                stmt = stmt.where(MemoryORM.memory_type.in_([item.value for item in memory_types]))
            rows = list((await session.execute(stmt)).scalars())
            def score(row: MemoryORM) -> float:
                haystack = _tokens(f"{row.title} {row.content} {' '.join(row.tags)}")
                overlap = len(query_tokens & haystack) / max(1, len(query_tokens))
                return overlap * 0.8 + row.importance * 0.2
            return [self._to_model(row) for row in sorted(rows, key=score, reverse=True) if score(row) > 0][:limit]

    async def search(self, project_id: str, query: str, *, limit: int = 8) -> list[StoredMemory]:
        return await self.retrieve(project_id, query, limit=limit)

    async def context_for_prompt(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 5,
        memory_types: list[MemoryType] | None = None,
    ) -> str:
        """Retrieve and format memories as a ready-to-inject prompt snippet.

        This is the wiring point Phase 22 was missing: callers that build
        agent-facing prompts (CTO planning, manager delegation, self-healing
        diagnosis) call this instead of touching `retrieve`/`StoredMemory`
        directly, so the formatting stays consistent everywhere memory is
        surfaced. Returns `""` (never a header with no bullets) when there
        is nothing relevant, so call sites can safely do
        `if memory_context: prompt += memory_context`.
        """
        memories = await self.retrieve(project_id, query, limit=limit, memory_types=memory_types)
        if not memories:
            return ""
        lines = [f"- [{m.memory_type.value}] {m.title}: {m.content}" for m in memories]
        return "Relevant project memory (from earlier in this project):\n" + "\n".join(lines)

    async def summarize(self, project_id: str, *, limit: int = 20) -> str:
        async with session_scope(self.settings) as session:
            rows = list((await session.execute(select(MemoryORM).where(MemoryORM.project_id == project_id, MemoryORM.valid.is_(True)).order_by(MemoryORM.importance.desc(), MemoryORM.updated_at.desc()).limit(limit))).scalars())
        return "\n".join(f"- [{row.memory_type}] {row.title}: {row.content}" for row in rows)

    async def invalidate(self, project_id: str, memory_id: str) -> bool:
        async with session_scope(self.settings) as session:
            row = await session.get(MemoryORM, memory_id)
            if row is None or row.project_id != project_id:
                return False
            row.valid = False
            return True

    @staticmethod
    def _to_model(row: MemoryORM) -> StoredMemory:
        return StoredMemory(memory_id=row.memory_id, project_id=row.project_id, memory_type=MemoryType(row.memory_type), title=row.title, content=row.content, tags=row.tags, importance=row.importance, valid=row.valid, created_at=row.created_at)
