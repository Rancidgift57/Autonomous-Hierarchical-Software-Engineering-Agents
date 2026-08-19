"""Integration test for app.orchestration (Phase 5 -- CTO wired into LangGraph)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agents.cto_schemas import (
    CTOArchitectureOutput,
    CTODecompositionOutput,
    CTORequirementsOutput,
)
from app.orchestration.graph import build_graph, run_cto_planning
from app.state.enums import TaskStatus
from app.state.models import ProjectMetadata
from app.tasks.dag import create_graph, get_ready_tasks, validate_graph
from tests.test_cto_agent import (
    _architecture_payload,
    _decomposition_payload,
    _requirements_payload,
)


@pytest.fixture
def mock_gateway():
    gateway = AsyncMock()
    gateway.generate_json = AsyncMock(
        side_effect=[
            CTORequirementsOutput.model_validate(_requirements_payload()),
            CTOArchitectureOutput.model_validate(_architecture_payload()),
            CTODecompositionOutput.model_validate(_decomposition_payload()),
        ]
    )
    return gateway


@pytest.mark.asyncio
async def test_run_cto_planning_populates_state(mock_gateway):
    project = ProjectMetadata(
        name="TodoApp",
        description="A simple todo app",
        idea_prompt="Build a todo app with user accounts.",
    )

    state = await run_cto_planning(mock_gateway, project)

    assert len(state.requirements) == 2
    assert len(state.architecture.modules) == 2
    assert len(state.tasks) == 2
    assert state.shared_context["cto_teams"][0]["name"] == "Backend"
    assert "Unit tests for all services" in state.shared_context["testing_requirements"]


@pytest.mark.asyncio
async def test_cto_tasks_form_valid_dag_and_respect_readiness(mock_gateway):
    project = ProjectMetadata(
        name="TodoApp",
        description="A simple todo app",
        idea_prompt="Build a todo app with user accounts.",
    )

    state = await run_cto_planning(mock_gateway, project)

    graph = create_graph(list(state.tasks.values()))
    validate_graph(graph)  # should not raise

    ready = get_ready_tasks(graph)
    ready_titles = {t.title for t in ready}
    # "Implement public API" depends on "Implement auth module", so only
    # the auth task should be immediately ready.
    assert ready_titles == {"Implement auth module"}

    auth_task_id = next(
        t.task_id for t in state.tasks.values() if t.title == "Implement auth module"
    )
    ready_after_auth = get_ready_tasks(graph, completed_task_ids=[auth_task_id])
    assert {t.title for t in ready_after_auth} == {"Implement public API"}


@pytest.mark.asyncio
async def test_build_graph_compiles_and_runs(mock_gateway):
    from app.state.models import AHSEAState

    project = ProjectMetadata(
        name="TodoApp",
        description="A simple todo app",
        idea_prompt="Build a todo app with user accounts.",
    )
    graph = build_graph(mock_gateway)
    result = await graph.ainvoke(AHSEAState(project=project))
    assert "tasks" in result
    assert len(result["tasks"]) == 2


@pytest.mark.asyncio
async def test_tasks_get_valid_status_from_add_task(mock_gateway):
    project = ProjectMetadata(
        name="TodoApp", description="A simple todo app", idea_prompt="Build a todo app."
    )
    state = await run_cto_planning(mock_gateway, project)
    # add_task() promotes tasks with no unmet deps to READY, and tasks with
    # an unmet dependency to BLOCKED -- both are valid outcomes here.
    assert all(
        t.status in (TaskStatus.READY, TaskStatus.PENDING, TaskStatus.BLOCKED)
        for t in state.tasks.values()
    )
