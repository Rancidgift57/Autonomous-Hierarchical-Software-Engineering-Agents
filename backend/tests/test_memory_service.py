"""Unit tests for app.memory.service (Phase 22 -- project memory).

These exercise the previously-untested `MemoryService` directly: storing,
scoped/typed retrieval, ranking, invalidation, and the `context_for_prompt`
helper that's now the wiring point CTO/manager/self-healing prompts use.
"""

from __future__ import annotations

import pytest

from app.memory.service import MemoryService, MemoryType


@pytest.mark.asyncio
async def test_store_and_retrieve_roundtrip(db_settings):
    service = MemoryService(settings=db_settings)
    stored = await service.store(
        "proj-1",
        MemoryType.DECISION,
        title="Use PostgreSQL",
        content="Chose Postgres over MySQL for JSONB support.",
        tags=["database"],
        importance=0.8,
    )
    assert stored.project_id == "proj-1"
    assert stored.memory_type == MemoryType.DECISION

    results = await service.retrieve("proj-1", "postgres database choice")
    assert any(m.memory_id == stored.memory_id for m in results)


@pytest.mark.asyncio
async def test_retrieve_is_scoped_to_project(db_settings):
    service = MemoryService(settings=db_settings)
    await service.store(
        "proj-1", MemoryType.DECISION, title="A", content="Postgres decision for project one"
    )
    await service.store(
        "proj-2", MemoryType.DECISION, title="B", content="Postgres decision for project two"
    )

    results = await service.retrieve("proj-1", "postgres")
    assert all(m.project_id == "proj-1" for m in results)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_retrieve_filters_by_memory_type(db_settings):
    service = MemoryService(settings=db_settings)
    await service.store("proj-1", MemoryType.DECISION, title="D", content="a decision about auth")
    await service.store("proj-1", MemoryType.FAILURE, title="F", content="a failure about auth")

    decisions_only = await service.retrieve(
        "proj-1", "auth", memory_types=[MemoryType.DECISION]
    )
    assert all(m.memory_type == MemoryType.DECISION for m in decisions_only)
    assert len(decisions_only) == 1


@pytest.mark.asyncio
async def test_retrieve_ranks_more_relevant_memories_first(db_settings):
    service = MemoryService(settings=db_settings)
    await service.store(
        "proj-1", MemoryType.DECISION, title="Auth module", content="JWT-based authentication"
    )
    await service.store(
        "proj-1", MemoryType.DECISION, title="Unrelated", content="Frontend button color"
    )

    results = await service.retrieve("proj-1", "authentication jwt", limit=5)
    assert results
    assert results[0].title == "Auth module"


@pytest.mark.asyncio
async def test_invalidate_removes_memory_from_retrieval(db_settings):
    service = MemoryService(settings=db_settings)
    stored = await service.store(
        "proj-1", MemoryType.FAILURE, title="Flaky test", content="test_foo flaked once"
    )

    ok = await service.invalidate("proj-1", stored.memory_id)
    assert ok is True

    results = await service.retrieve("proj-1", "flaky test")
    assert all(m.memory_id != stored.memory_id for m in results)


@pytest.mark.asyncio
async def test_invalidate_wrong_project_is_noop(db_settings):
    service = MemoryService(settings=db_settings)
    stored = await service.store("proj-1", MemoryType.FAILURE, title="X", content="Y")

    ok = await service.invalidate("proj-2", stored.memory_id)
    assert ok is False

    results = await service.retrieve("proj-1", "x y", memory_types=[MemoryType.FAILURE])
    assert any(m.memory_id == stored.memory_id for m in results)


@pytest.mark.asyncio
async def test_context_for_prompt_empty_when_nothing_relevant(db_settings):
    service = MemoryService(settings=db_settings)
    context = await service.context_for_prompt("empty-project", "anything")
    assert context == ""


@pytest.mark.asyncio
async def test_context_for_prompt_formats_relevant_memories(db_settings):
    service = MemoryService(settings=db_settings)
    await service.store(
        "proj-1",
        MemoryType.REPAIR,
        title="Fixed flaky retry logic",
        content="Added exponential backoff to the HTTP client.",
        importance=0.9,
    )

    context = await service.context_for_prompt("proj-1", "retry logic http client")
    assert "Relevant project memory" in context
    assert "Fixed flaky retry logic" in context
    assert "exponential backoff" in context


@pytest.mark.asyncio
async def test_summarize_orders_by_importance(db_settings):
    service = MemoryService(settings=db_settings)
    await service.store("proj-1", MemoryType.PROJECT, title="Low", content="c", importance=0.1)
    await service.store("proj-1", MemoryType.PROJECT, title="High", content="c", importance=0.9)

    summary = await service.summarize("proj-1")
    assert summary.index("High") < summary.index("Low")
