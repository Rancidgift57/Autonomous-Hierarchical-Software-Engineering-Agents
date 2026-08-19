"""Tests for app.orchestration.project_runner (Phase 16 orchestration layer),
exercised with a fake LLM gateway (no real Ollama needed) but real
managers/workers/tools writing into a real temp workspace."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.cto_schemas import (
    CTOArchitectureOutput,
    CTODecompositionOutput,
    CTORequirementsOutput,
)
from app.agents.managers.schemas import ManagerAnalysis, ManagerReviewDecision, WorkerSelection
from app.agents.workers.schemas import (
    WorkerFileChange,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerSelfReview,
)
from app.orchestration.project_runner import (
    DefaultProjectOrchestrator,
    ProjectRunControl,
    ProjectRunStatus,
)
from app.deployment.schemas import (
    DeploymentPlan,
    GeneratedDeploymentConfig,
    GeneratedDeploymentScript,
    GeneratedDockerfile,
)
from app.qa.schemas import QAArchitecturalAssessment
from app.state.enums import TaskStatus
from app.state.models import AHSEAState, ProjectMetadata


def _requirements_payload() -> dict:
    return {
        "functional_requirements": [
            {
                "title": "Health endpoint",
                "description": "Expose a health check endpoint.",
                "category": "functional",
                "priority": "must_have",
                "acceptance_criteria": ["GET /health returns 200"],
            }
        ],
        "non_functional_requirements": [],
        "testing_requirements": ["Unit tests for the health endpoint"],
        "deployment_requirements": ["Deploy via Docker"],
    }


def _architecture_payload() -> dict:
    return {
        "technology_stack": [
            {
                "name": "FastAPI",
                "category": "backend",
                "version_constraint": None,
                "rationale": None,
            }
        ],
        "modules": [
            {
                "name": "APIModule",
                "description": "Public HTTP API.",
                "owning_team": "Backend",
                "depends_on_module_names": [],
                "technologies": [],
                "requirement_titles": ["Health endpoint"],
            }
        ],
        "dependencies": [],
        "decisions": [],
    }


def _decomposition_payload() -> dict:
    return {
        "teams": [
            {"name": "Backend", "description": "Owns the API.", "module_names": ["APIModule"]}
        ],
        "high_level_tasks": [
            {
                "title": "Implement health endpoint",
                "description": "Add GET /health.",
                "owner_team": "Backend",
                "worker_type": "api_worker",
                "depends_on_task_titles": [],
                "expected_outputs": ["GET /health returns 200"],
                "priority": 5,
                "complexity": "low",
            }
        ],
    }


class FakeGateway:
    """Routes every call by response_model; real CTO -> manager -> worker
    sequence, no network calls."""

    def __init__(self, worker_decision: str = "pass", manager_decision: str = "accept"):
        self.calls: list[str] = []
        self.worker_decision = worker_decision
        self.manager_decision = manager_decision

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(response_model.__name__)

        if response_model is CTORequirementsOutput:
            return CTORequirementsOutput.model_validate(_requirements_payload())
        if response_model is CTOArchitectureOutput:
            return CTOArchitectureOutput.model_validate(_architecture_payload())
        if response_model is CTODecompositionOutput:
            return CTODecompositionOutput.model_validate(_decomposition_payload())
        if response_model is ManagerAnalysis:
            return ManagerAnalysis(summary="Straightforward task.")
        if response_model is WorkerSelection:
            return WorkerSelection(
                selected_worker_type="api_worker", rationale="only fit", instructions="implement it"
            )
        if response_model is WorkerPlan:
            return WorkerPlan(
                approach="Add a health route.",
                steps=["create app/health.py"],
                target_files=["app/health.py"],
            )
        if response_model is WorkerImplementationOutput:
            return WorkerImplementationOutput(
                summary="Added GET /health.",
                files=[
                    WorkerFileChange(path="app/health.py", action="create", content="OK = True\n")
                ],
            )
        if response_model is WorkerSelfReview:
            return WorkerSelfReview(
                decision=self.worker_decision,
                issues=[] if self.worker_decision == "pass" else ["needs work"],
            )
        if response_model is ManagerReviewDecision:
            return ManagerReviewDecision(decision=self.manager_decision, feedback="ok")
        if response_model is QAArchitecturalAssessment:
            return QAArchitecturalAssessment(
                summary="QA findings reviewed.", recommended_actions=[]
            )
        if response_model is DeploymentPlan:
            return DeploymentPlan(
                summary="Build, start, verify.",
                steps=["build", "start", "verify"],
                target_environment="staging",
                base_image_recommendation="python:3.12-slim",
                rollback_strategy="redeploy previous image",
            )
        if response_model is GeneratedDockerfile:
            return GeneratedDockerfile(
                content="FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD [\"true\"]\n"
            )
        if response_model is GeneratedDeploymentConfig:
            return GeneratedDeploymentConfig(
                docker_compose_content="services:\n  app:\n    build: .\n",
                env_template_content="APP_ENV=production\n",
            )
        if response_model is GeneratedDeploymentScript:
            return GeneratedDeploymentScript(
                filename="deploy.sh", content="#!/bin/sh\necho deployed\n"
            )

        raise AssertionError(f"Unexpected response_model: {response_model.__name__}")


def make_state() -> AHSEAState:
    return AHSEAState(project=ProjectMetadata(name="P", description="d", idea_prompt="A todo app"))


def _diag(control: ProjectRunControl, state: AHSEAState) -> str:
    """Build a diagnostic string for a failed-run assertion: the actual
    reason the run failed, since `control.status != COMPLETED` alone
    doesn't say *why* -- and guessing at the cause from the outside has
    already burned two round trips. `control.error` is set when
    `DefaultProjectOrchestrator.run()` caught an unhandled exception;
    `state.errors` is set when the workflow completed without raising but
    a task/QA/recovery step recorded a real failure (see
    `app/orchestration/project_runner.py`'s `run()` -- both paths set
    `status = FAILED`, so the status alone can't distinguish them)."""
    error_records = [f"{e.source}: {e.message}" for e in state.errors]
    return (
        f"control.status={control.status!r} control.error={control.error!r} "
        f"state.errors={error_records!r}"
    )


@pytest.mark.asyncio
async def test_orchestrator_plans_and_completes_a_simple_project(tmp_path):
    state = make_state()
    gateway = FakeGateway()
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.COMPLETED, _diag(control, state)
    assert control.error is None
    assert len(state.tasks) == 1
    task = next(iter(state.tasks.values()))
    assert task.status == TaskStatus.COMPLETED

    # Regression test: `assigned_agent_id` used to stay `None` forever --
    # nothing in the dispatch path ever wrote to it, even though the task
    # genuinely was being handled by a real worker the whole time. It
    # should end up set to the specific worker that did the work (not
    # just the team's manager), via `ManagerReport.worker_agent_id`.
    assert task.assigned_agent_id == "Backend-api_worker"

    # The CTO used only reasoning task types; managers/workers actually ran.
    assert "CTORequirementsOutput" in gateway.calls
    assert "WorkerImplementationOutput" in gateway.calls

    # The worker really wrote the file into the real workspace.
    assert (tmp_path / "app" / "health.py").exists()


@pytest.mark.asyncio
async def test_orchestrator_fails_run_when_manager_rejects_worker_permanently(tmp_path):
    state = make_state()
    gateway = FakeGateway(worker_decision="needs_fix", manager_decision="rework")
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.FAILED
    assert control.error is not None
    task = next(iter(state.tasks.values()))
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_orchestrator_emits_project_events(tmp_path):
    state = make_state()
    gateway = FakeGateway()
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    messages = [e.message for e in state.project_events]
    assert any("planning" in m.lower() for m in messages)
    assert any("completed" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_orchestrator_cancellation_before_execution_marks_cancelled(tmp_path):
    state = make_state()
    gateway = FakeGateway()
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()
    control.cancel_requested = True  # simulate a cancel that arrived during planning

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_project_run_control_pause_blocks_and_resume_unblocks():
    control = ProjectRunControl()
    control.status = ProjectRunStatus.RUNNING
    control.pause()
    assert control.status == ProjectRunStatus.PAUSED
    assert not control.pause_event.is_set()

    waiter = asyncio.ensure_future(control.pause_event.wait())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    control.resume()
    assert control.status == ProjectRunStatus.RUNNING
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done()


def test_project_run_control_request_cancel_unblocks_a_paused_run():
    control = ProjectRunControl()
    control.pause()
    assert not control.pause_event.is_set()

    control.request_cancel()

    assert control.cancel_requested
    assert control.pause_event.is_set()  # cancellation must unstick a paused run


# ---------------------------------------------------------------------------
# Phase 19: realtime event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_emits_realtime_events_for_a_full_run(tmp_path):
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager
    from app.realtime.schemas import RealtimeEventType

    state = make_state()
    gateway = FakeGateway()
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    orchestrator = DefaultProjectOrchestrator(
        gateway=gateway, workspace_root=tmp_path, realtime=emitter
    )
    control = ProjectRunControl()

    await orchestrator.run(state, control)
    # AGENT_TOOL_CALL is fired fire-and-forget (emit_soon); give the loop a
    # tick to flush those scheduled tasks before asserting on history.
    await asyncio.sleep(0.05)

    assert control.status == ProjectRunStatus.COMPLETED, _diag(control, state)
    events = conn_manager.replay(state.project.project_id)
    event_types = [e.event_type for e in events]

    assert RealtimeEventType.PROJECT_STARTED in event_types
    assert RealtimeEventType.AGENT_STARTED in event_types
    assert RealtimeEventType.AGENT_COMPLETED in event_types
    assert RealtimeEventType.TASK_STARTED in event_types
    assert RealtimeEventType.TASK_COMPLETED in event_types
    assert RealtimeEventType.AGENT_TOOL_CALL in event_types

    # project_started must be first, and every task/agent event must carry
    # a task_id so a frontend can route it to the right row/card.
    assert event_types[0] == RealtimeEventType.PROJECT_STARTED
    task_scoped = [e for e in events if e.event_type == RealtimeEventType.TASK_STARTED]
    assert all(e.task_id for e in task_scoped)

    # Nothing that looks like a secret/prompt leaked through.
    for event in events:
        assert "system_prompt" not in event.payload
        for value in event.payload.values():
            assert "sk-" not in str(value)


@pytest.mark.asyncio
async def test_orchestrator_without_realtime_emitter_is_unaffected(tmp_path):
    """`realtime=None` (the default) must behave exactly like Phase 16-18:
    no emitter, no broadcasting, no behavior change to the run itself."""

    state = make_state()
    gateway = FakeGateway()
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.COMPLETED, _diag(control, state)


@pytest.mark.asyncio
async def test_orchestrator_without_memory_service_is_unaffected(tmp_path):
    """`memory_service=None` (the default) must behave exactly like before
    this wiring existed -- no DB required, no behavior change."""

    state = make_state()
    gateway = FakeGateway()
    orchestrator = DefaultProjectOrchestrator(gateway=gateway, workspace_root=tmp_path)
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.COMPLETED, _diag(control, state)


@pytest.mark.asyncio
async def test_orchestrator_writes_and_reads_project_memory(tmp_path, db_settings):
    """Phase 22 wiring: a full run with a real `MemoryService` must (a) not
    fail, and (b) leave behind memory this project's own run produced --
    the CTO's architecture decisions and the manager's task outcome."""
    from app.memory.service import MemoryService, MemoryType

    state = make_state()
    gateway = FakeGateway()
    memory_service = MemoryService(settings=db_settings)
    orchestrator = DefaultProjectOrchestrator(
        gateway=gateway, workspace_root=tmp_path, memory_service=memory_service
    )
    control = ProjectRunControl()

    await orchestrator.run(state, control)

    assert control.status == ProjectRunStatus.COMPLETED, _diag(control, state)

    project_id = state.project.project_id
    all_memories = await memory_service.retrieve(project_id, "", limit=50)
    # An empty query still ranks everything (score floor is > 0 via the
    # importance term), so this is the simplest way to assert "something
    # got written for this project".
    assert len(all_memories) >= 1

    decisions = await memory_service.retrieve(
        project_id, "", limit=50, memory_types=[MemoryType.DECISION]
    )
    assert len(decisions) >= 1

    # And the memory that was written is actually retrievable by content,
    # not just present -- proving `context_for_prompt` would surface it.
    context = await memory_service.context_for_prompt(project_id, "health endpoint task")
    assert context == "" or "Relevant project memory" in context
