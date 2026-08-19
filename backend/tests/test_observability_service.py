"""Unit tests for app.observability.service (Phase 23 -- agent performance
tracking).

`metrics()` (the pre-existing global aggregation) already had informal
coverage via API integration tests; these focus on what was actually
missing: `agent_scorecards()` (per-agent success rate/duration/trend) and
`task_type_model_scorecards()` (per task_type+model breakdown), plus basic
record/metrics correctness now that this module has its own test file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import ObservabilityEventORM
from app.db.session import session_scope
from app.observability.service import ObservabilityService, TraceContext


async def _insert_event(
    settings,
    *,
    event_id: str,
    project_id: str = "proj-1",
    agent_id: str | None = "backend-manager",
    task_type: str | None = "coding",
    model: str | None = "qwen2.5-coder:7b-instruct-q4_K_M",
    success: bool | None = True,
    duration_seconds: float | None = 1.0,
    created_at: datetime | None = None,
) -> None:
    """Insert an observability event directly, optionally backdating
    `created_at` -- `ObservabilityService.record()` always stamps "now",
    so trend tests need a lower-level way to simulate history."""
    row = ObservabilityEventORM(
        event_id=event_id,
        project_id=project_id,
        agent_id=agent_id,
        task_id=None,
        request_id=None,
        event_type="llm_call",
        task_type=task_type,
        model=model,
        duration_seconds=duration_seconds,
        success=success,
        attributes={},
    )
    if created_at is not None:
        row.created_at = created_at
    async with session_scope(settings) as session:
        session.add(row)


@pytest.mark.asyncio
async def test_record_and_metrics_basic_aggregation(db_settings):
    service = ObservabilityService(settings=db_settings)
    await service.record(
        "llm_call",
        TraceContext(project_id="proj-1", agent_id="backend-manager"),
        task_type="coding",
        model="qwen2.5-coder:7b-instruct-q4_K_M",
        duration_seconds=2.5,
        success=True,
    )
    await service.record(
        "llm_call",
        TraceContext(project_id="proj-1", agent_id="backend-manager"),
        task_type="coding",
        model="qwen2.5-coder:7b-instruct-q4_K_M",
        duration_seconds=1.5,
        success=False,
    )

    metrics = await service.metrics("proj-1")
    assert metrics["event_count"] == 2
    assert metrics["by_event_type"]["llm_call"]["count"] == 2
    assert metrics["by_event_type"]["llm_call"]["success"] == 1
    assert metrics["by_event_type"]["llm_call"]["failed"] == 1


@pytest.mark.asyncio
async def test_agent_scorecards_aggregates_per_agent(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(db_settings, event_id="e1", agent_id="backend-manager", success=True, duration_seconds=2.0)
    await _insert_event(db_settings, event_id="e2", agent_id="backend-manager", success=False, duration_seconds=4.0)
    await _insert_event(db_settings, event_id="e3", agent_id="frontend-manager", success=True, duration_seconds=1.0)

    cards = await service.agent_scorecards("proj-1")
    by_agent = {c["agent_id"]: c for c in cards}

    assert by_agent["backend-manager"]["event_count"] == 2
    assert by_agent["backend-manager"]["success_count"] == 1
    assert by_agent["backend-manager"]["failure_count"] == 1
    assert by_agent["backend-manager"]["success_rate"] == 0.5
    assert by_agent["backend-manager"]["avg_duration_seconds"] == 3.0

    assert by_agent["frontend-manager"]["event_count"] == 1
    assert by_agent["frontend-manager"]["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_agent_scorecards_excludes_events_without_agent_id(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(db_settings, event_id="e1", agent_id=None)
    await _insert_event(db_settings, event_id="e2", agent_id="cto")

    cards = await service.agent_scorecards("proj-1")
    assert [c["agent_id"] for c in cards] == ["cto"]


@pytest.mark.asyncio
async def test_agent_scorecards_scoped_to_project(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(db_settings, event_id="e1", project_id="proj-1", agent_id="cto")
    await _insert_event(db_settings, event_id="e2", project_id="proj-2", agent_id="cto")

    cards = await service.agent_scorecards("proj-1")
    assert len(cards) == 1
    assert cards[0]["event_count"] == 1


@pytest.mark.asyncio
async def test_agent_scorecards_reports_models_and_task_types(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(
        db_settings, event_id="e1", agent_id="cto", task_type="architecture", model="qwen3:4b"
    )
    await _insert_event(
        db_settings, event_id="e2", agent_id="cto", task_type="decomposition", model="qwen3:4b"
    )

    cards = await service.agent_scorecards("proj-1")
    assert cards[0]["models_used"] == ["qwen3:4b"]
    assert cards[0]["task_types"] == ["architecture", "decomposition"]


@pytest.mark.asyncio
async def test_agent_scorecards_trend_insufficient_data_by_default(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(db_settings, event_id="e1", agent_id="cto", success=True)

    cards = await service.agent_scorecards("proj-1")
    assert cards[0]["trend"] == "insufficient_data"


@pytest.mark.asyncio
async def test_agent_scorecards_trend_declining(db_settings):
    service = ObservabilityService(settings=db_settings)
    now = datetime.utcnow()
    old = now - timedelta(days=20)

    # Prior window: all successes.
    for i in range(4):
        await _insert_event(
            db_settings, event_id=f"old-{i}", agent_id="cto", success=True, created_at=old
        )
    # Recent window (last 7 days): all failures.
    for i in range(4):
        await _insert_event(
            db_settings, event_id=f"new-{i}", agent_id="cto", success=False, created_at=now
        )

    cards = await service.agent_scorecards("proj-1", recent_days=7)
    assert cards[0]["trend"] == "declining"


@pytest.mark.asyncio
async def test_agent_scorecards_trend_improving(db_settings):
    service = ObservabilityService(settings=db_settings)
    now = datetime.utcnow()
    old = now - timedelta(days=20)

    for i in range(4):
        await _insert_event(
            db_settings, event_id=f"old-{i}", agent_id="cto", success=False, created_at=old
        )
    for i in range(4):
        await _insert_event(
            db_settings, event_id=f"new-{i}", agent_id="cto", success=True, created_at=now
        )

    cards = await service.agent_scorecards("proj-1", recent_days=7)
    assert cards[0]["trend"] == "improving"


@pytest.mark.asyncio
async def test_task_type_model_scorecards_breaks_down_correctly(db_settings):
    service = ObservabilityService(settings=db_settings)
    await _insert_event(
        db_settings, event_id="e1", task_type="coding", model="model-a", success=True
    )
    await _insert_event(
        db_settings, event_id="e2", task_type="coding", model="model-a", success=False
    )
    await _insert_event(
        db_settings, event_id="e3", task_type="coding", model="model-b", success=True
    )

    cards = await service.task_type_model_scorecards("proj-1")
    by_key = {(c["task_type"], c["model"]): c for c in cards}

    assert by_key[("coding", "model-a")]["event_count"] == 2
    assert by_key[("coding", "model-a")]["success_rate"] == 0.5
    assert by_key[("coding", "model-b")]["success_rate"] == 1.0
