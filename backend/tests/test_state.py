"""Unit tests for app.state (Phase 2 — shared project state)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.state import (
    AgentDefinition,
    AgentStatus,
    AgentType,
    AHSEAState,
    Artifact,
    ArtifactType,
    Contract,
    ContractType,
    EnvironmentContract,
    ErrorRecord,
    ProjectMetadata,
    StateError,
    Task,
    TaskStatus,
    add_agent,
    add_artifact,
    add_contract,
    add_error,
    add_task,
    get_blocked_tasks,
    get_ready_tasks,
    mark_task_completed,
    mark_task_failed,
    update_agent_status,
    update_task,
)


@pytest.fixture
def state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(
            name="Test Project",
            description="A project used for testing.",
            idea_prompt="Build a todo app.",
        )
    )


# ---------------------------------------------------------------------------
# Model validation / serialization
# ---------------------------------------------------------------------------


def test_project_metadata_defaults():
    meta = ProjectMetadata(
        name="X", description="Y", idea_prompt="Z"
    )
    assert meta.project_id.startswith("proj_")
    assert meta.default_branch == "main"
    assert meta.tags == []


def test_ahseastate_round_trips_to_json(state: AHSEAState):
    raw = state.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["project"]["name"] == "Test Project"
    restored = AHSEAState.model_validate_json(raw)
    assert restored.project.project_id == state.project.project_id


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        ProjectMetadata(
            name="X", description="Y", idea_prompt="Z", not_a_real_field=True
        )


# ---------------------------------------------------------------------------
# Task DAG operations
# ---------------------------------------------------------------------------


def test_add_task_with_no_deps_is_ready(state: AHSEAState):
    task = Task(title="Set up repo", description="Init the repo.")
    add_task(state, task)
    assert state.tasks[task.task_id].status == TaskStatus.READY
    assert task in get_ready_tasks(state)


def test_add_task_with_unmet_deps_is_blocked(state: AHSEAState):
    upstream = Task(title="Design schema", description="...")
    add_task(state, upstream)
    downstream = Task(
        title="Write migration",
        description="...",
        depends_on_task_ids=[upstream.task_id],
    )
    add_task(state, downstream)

    assert state.tasks[downstream.task_id].status == TaskStatus.BLOCKED
    assert downstream in get_blocked_tasks(state)
    assert downstream not in get_ready_tasks(state)


def test_completing_task_unblocks_dependents(state: AHSEAState):
    upstream = Task(title="Design schema", description="...")
    add_task(state, upstream)
    downstream = Task(
        title="Write migration",
        description="...",
        depends_on_task_ids=[upstream.task_id],
    )
    add_task(state, downstream)

    mark_task_completed(state, upstream.task_id)

    assert state.tasks[upstream.task_id].status == TaskStatus.COMPLETED
    assert state.tasks[downstream.task_id].status == TaskStatus.READY


def test_self_dependency_raises(state: AHSEAState):
    task = Task(title="Bad task", description="...")
    task.depends_on_task_ids = [task.task_id]
    with pytest.raises(StateError):
        add_task(state, task)


def test_duplicate_task_id_raises(state: AHSEAState):
    task = Task(title="Dup", description="...")
    add_task(state, task)
    with pytest.raises(StateError):
        add_task(state, task)


def test_mark_task_failed_retries_then_fails(state: AHSEAState):
    task = Task(title="Flaky", description="...", max_retries=1)
    add_task(state, task)

    mark_task_failed(state, task.task_id, "boom", retry=True)
    assert state.tasks[task.task_id].status == TaskStatus.RETRYING
    assert state.tasks[task.task_id].retries == 1

    mark_task_failed(state, task.task_id, "boom again", retry=True)
    assert state.tasks[task.task_id].status == TaskStatus.FAILED


def test_mark_task_failed_blocks_dependents(state: AHSEAState):
    upstream = Task(title="Upstream", description="...", max_retries=0)
    add_task(state, upstream)
    downstream = Task(
        title="Downstream", description="...", depends_on_task_ids=[upstream.task_id]
    )
    add_task(state, downstream)

    mark_task_failed(state, upstream.task_id, "fatal", retry=False)

    assert state.tasks[upstream.task_id].status == TaskStatus.FAILED
    assert state.tasks[downstream.task_id].status == TaskStatus.BLOCKED


def test_update_task_partial_update(state: AHSEAState):
    task = Task(title="Original", description="...")
    add_task(state, task)
    updated = update_task(state, task.task_id, title="Renamed")
    assert updated.title == "Renamed"
    assert state.tasks[task.task_id].title == "Renamed"


def test_update_nonexistent_task_raises(state: AHSEAState):
    with pytest.raises(StateError):
        update_task(state, "does-not-exist", title="X")


def test_get_ready_tasks_orders_by_priority(state: AHSEAState):
    low = Task(title="Low", description="...", priority=0)
    high = Task(title="High", description="...", priority=10)
    add_task(state, low)
    add_task(state, high)
    ready = get_ready_tasks(state)
    assert ready[0].task_id == high.task_id


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_add_agent_and_update_status(state: AHSEAState):
    cto = AgentDefinition(agent_id="cto", name="CTO Agent", agent_type=AgentType.CTO)
    add_agent(state, cto)
    assert "cto" in state.agents
    assert state.agent_statuses["cto"].status == AgentStatus.IDLE

    update_agent_status(state, "cto", AgentStatus.WORKING, current_task_id="task_1")
    assert state.agent_statuses["cto"].status == AgentStatus.WORKING
    assert state.agent_statuses["cto"].current_task_id == "task_1"


def test_add_agent_missing_parent_raises(state: AHSEAState):
    worker = AgentDefinition(
        agent_id="w1",
        name="Worker",
        agent_type=AgentType.WORKER,
        parent_agent_id="missing_manager",
    )
    with pytest.raises(StateError):
        add_agent(state, worker)


def test_update_status_unknown_agent_raises(state: AHSEAState):
    with pytest.raises(StateError):
        update_agent_status(state, "unknown", AgentStatus.WORKING)


# ---------------------------------------------------------------------------
# Artifacts, contracts, errors
# ---------------------------------------------------------------------------


def test_add_artifact(state: AHSEAState):
    artifact = Artifact(artifact_type=ArtifactType.SOURCE_FILE, path="app/main.py")
    add_artifact(state, artifact)
    assert artifact.artifact_id in state.artifacts


def test_add_contract(state: AHSEAState):
    contract = Contract(
        contract_type=ContractType.ENVIRONMENT,
        name="Ollama base URL",
        environment=EnvironmentContract(key="OLLAMA_BASE_URL"),
    )
    add_contract(state, contract)
    assert contract.contract_id in state.contracts


def test_add_error(state: AHSEAState):
    error = ErrorRecord(source="worker:api_worker", message="Type error in routes.py")
    add_error(state, error)
    assert state.errors[-1].message == "Type error in routes.py"
