"""Unit tests for app.agents.managers (Phase 7 -- manager agents)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agents.managers import (
    MANAGER_CLASSES,
    AIManager,
    BackendManager,
    DatabaseManager,
    DeploymentManager,
    FrontendManager,
    ManagerAnalysis,
    ManagerContext,
    ManagerReportStatus,
    ManagerReviewDecision,
    QAManager,
    WorkerSelection,
    make_manager_tools,
    make_worker_factory,
)
from app.agents.workers.schemas import WorkerResult, WorkerStatus
from app.llm.exceptions import InvalidJSONError
from app.llm.models import TaskType
from app.state.models import Task
from app.tools.permissions import Permission


class FakeGateway:
    """Stand-in for LLMGateway: only accepts task_type=MANAGEMENT calls."""

    def __init__(
        self, analysis=None, selection=None, reviews=None, fail_on: TaskType | None = None
    ):
        self.analysis = analysis
        self.selection = selection
        self.reviews = list(reviews or [])
        self.fail_on = fail_on
        self.calls: list[TaskType] = []

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        if task_type != TaskType.MANAGEMENT:
            raise AssertionError(
                f"Manager requested non-MANAGEMENT task_type: {task_type}. Managers must "
                "only ever request task_type=MANAGEMENT."
            )
        if self.fail_on is not None and task_type == self.fail_on:
            raise InvalidJSONError("simulated LLM failure")

        if response_model is ManagerAnalysis:
            return self.analysis
        if response_model is WorkerSelection:
            return self.selection
        if response_model is ManagerReviewDecision:
            return self.reviews.pop(0)
        raise AssertionError(f"Unexpected response_model: {response_model}")


class FakeWorker:
    """Stand-in for a Phase 8 BaseWorkerAgent."""

    def __init__(self, agent_id: str, results: list[WorkerResult]):
        self.agent_id = agent_id
        self._results = list(results)
        self.calls = 0

    async def run(self, task, context, metadata=None):
        self.calls += 1
        return self._results.pop(0)


def make_task(**overrides) -> Task:
    defaults = dict(
        title="Add /health endpoint",
        description="Implement a GET /health endpoint.",
        worker_type="api_worker",
    )
    defaults.update(overrides)
    return Task(**defaults)


def make_context(team_name="Backend") -> ManagerContext:
    return ManagerContext(team_name=team_name, global_summary="Building a task tracker app.")


def success_result(task_id: str) -> WorkerResult:
    return WorkerResult(
        task_id=task_id,
        agent_id="api_worker_1",
        status=WorkerStatus.SUCCESS,
        summary="Implemented the endpoint.",
        files_changed=["backend/app/api/health.py"],
    )


# ---------------------------------------------------------------------------
# Happy path: accept on first review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_accepts_on_first_review():
    task = make_task()
    gateway = FakeGateway(
        analysis=ManagerAnalysis(summary="Straightforward CRUD-ish endpoint."),
        selection=WorkerSelection(selected_worker_type="api_worker", instructions="Do it."),
        reviews=[ManagerReviewDecision(decision="accept", feedback="Looks good.")],
    )
    worker = FakeWorker("api_worker_1", [success_result(task.task_id)])
    manager = BackendManager(
        gateway=gateway, manager_id="backend_manager", worker_factory=lambda wt: worker
    )

    report = await manager.handle_task(task, make_context())

    assert report.status == ManagerReportStatus.ACCEPTED
    assert report.selected_worker_type == "api_worker"
    assert report.worker_agent_id == "api_worker_1"
    assert report.rework_cycles == 0
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_manager_only_ever_requests_management_task_type():
    task = make_task()
    gateway = FakeGateway(
        analysis=ManagerAnalysis(summary="s"),
        selection=WorkerSelection(selected_worker_type="api_worker"),
        reviews=[ManagerReviewDecision(decision="accept")],
    )
    worker = FakeWorker("api_worker_1", [success_result(task.task_id)])
    manager = BackendManager(gateway=gateway, manager_id="bm", worker_factory=lambda wt: worker)

    await manager.handle_task(task, make_context())

    assert gateway.calls == [TaskType.MANAGEMENT, TaskType.MANAGEMENT, TaskType.MANAGEMENT]


# ---------------------------------------------------------------------------
# Rework loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_reworks_then_accepts():
    task = make_task()
    gateway = FakeGateway(
        analysis=ManagerAnalysis(summary="s"),
        selection=WorkerSelection(selected_worker_type="api_worker"),
        reviews=[
            ManagerReviewDecision(
                decision="rework", feedback="Missing tests.", issues=["No tests"]
            ),
            ManagerReviewDecision(decision="accept"),
        ],
    )
    worker = FakeWorker(
        "api_worker_1", [success_result(task.task_id), success_result(task.task_id)]
    )
    manager = BackendManager(gateway=gateway, manager_id="bm", worker_factory=lambda wt: worker)

    report = await manager.handle_task(task, make_context())

    assert report.status == ManagerReportStatus.ACCEPTED
    assert report.rework_cycles == 1
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_manager_exhausts_rework_budget():
    task = make_task()
    gateway = FakeGateway(
        analysis=ManagerAnalysis(summary="s"),
        selection=WorkerSelection(selected_worker_type="api_worker"),
        reviews=[
            ManagerReviewDecision(decision="rework", issues=["bad"]),
            ManagerReviewDecision(decision="rework", issues=["still bad"]),
            ManagerReviewDecision(decision="rework", issues=["still bad again"]),
        ],
    )
    worker = FakeWorker(
        "api_worker_1",
        [success_result(task.task_id) for _ in range(3)],
    )
    manager = BackendManager(
        gateway=gateway, manager_id="bm", worker_factory=lambda wt: worker, tools=None
    )
    manager.max_rework_cycles = 2

    report = await manager.handle_task(task, make_context())

    assert report.status == ManagerReportStatus.REWORK_EXHAUSTED
    assert report.rework_cycles == 2
    assert worker.calls == 3


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_rejects_untrusted_worker_selection_outside_scope():
    task = make_task()
    gateway = FakeGateway(
        analysis=ManagerAnalysis(summary="s"),
        # The LLM "hallucinates" a worker type this manager doesn't own.
        selection=WorkerSelection(selected_worker_type="ui_worker"),
    )
    factory = AsyncMock()
    manager = BackendManager(gateway=gateway, manager_id="bm", worker_factory=factory)

    report = await manager.handle_task(task, make_context())

    assert report.status == ManagerReportStatus.NO_ELIGIBLE_WORKER
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_manager_context_team_mismatch_raises_failed_status():
    task = make_task()
    gateway = FakeGateway()
    manager = BackendManager(gateway=gateway, manager_id="bm", worker_factory=lambda wt: None)

    report = await manager.handle_task(task, make_context(team_name="Frontend"))

    assert report.status == ManagerReportStatus.FAILED
    assert report.errors


@pytest.mark.asyncio
async def test_manager_llm_failure_is_reported_not_raised():
    task = make_task()
    gateway = FakeGateway(fail_on=TaskType.MANAGEMENT)
    manager = BackendManager(gateway=gateway, manager_id="bm", worker_factory=lambda wt: None)

    report = await manager.handle_task(task, make_context())

    assert report.status == ManagerReportStatus.FAILED
    assert report.errors


def test_manager_has_no_provider_or_model_attribute():
    gateway = FakeGateway()
    manager = BackendManager(gateway=gateway, manager_id="bm")
    assert not hasattr(manager, "provider")
    assert not hasattr(manager, "model")
    assert not hasattr(manager, "ollama")


# ---------------------------------------------------------------------------
# DeploymentManager: no delegatable workers -> direct execution via read-only tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_manager_has_no_managed_workers():
    assert DeploymentManager.managed_worker_types == []


@pytest.mark.asyncio
async def test_deployment_manager_direct_execution_uses_readonly_tools(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    gateway = FakeGateway(analysis=ManagerAnalysis(summary="Ready to check deployment state."))
    tools = make_manager_tools("deployment_manager", tmp_path)
    manager = DeploymentManager(gateway=gateway, manager_id="deployment_manager", tools=tools)

    task = make_task(title="Check deploy readiness", description="Inspect repo state.")
    report = await manager.handle_task(task, make_context(team_name="Deployment"))

    assert report.status == ManagerReportStatus.ACCEPTED
    assert "Ready to check" in report.summary
    # Only MANAGEMENT-routed analysis was requested -- no worker selection
    # step, since there's nothing to delegate to.
    assert gateway.calls == [TaskType.MANAGEMENT]


@pytest.mark.asyncio
async def test_deployment_manager_tools_are_read_only(tmp_path):
    gateway = FakeGateway()
    tools = make_manager_tools("deployment_manager", tmp_path)
    assert tools.permissions == frozenset({Permission.READ})
    manager = DeploymentManager(gateway=gateway, manager_id="deployment_manager", tools=tools)
    assert manager.tools is tools


# ---------------------------------------------------------------------------
# Registry / identity
# ---------------------------------------------------------------------------


def test_all_six_managers_registered():
    assert set(MANAGER_CLASSES) == {
        "Backend", "Frontend", "Database", "AI", "QA", "Deployment",
    }


@pytest.mark.parametrize(
    "manager_cls,expected_workers",
    [
        (BackendManager, {"api_worker", "auth_worker", "service_worker"}),
        (FrontendManager, {"ui_worker", "component_worker"}),
        (DatabaseManager, {"schema_worker", "migration_worker"}),
        (AIManager, {"model_worker", "evaluation_worker"}),
    ],
)
def test_manager_worker_scopes_are_disjoint_by_team(manager_cls, expected_workers):
    assert set(manager_cls.managed_worker_types) == expected_workers


def test_qa_manager_can_reach_every_worker_type():
    from app.agents.workers.concrete import WORKER_CLASSES

    assert set(QAManager.managed_worker_types) == set(WORKER_CLASSES)


# ---------------------------------------------------------------------------
# make_worker_factory end-to-end wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_worker_factory_builds_real_worker_with_least_privilege_tools(tmp_path):
    from app.agents.workers.base import BaseWorkerAgent
    from app.tools.permissions import WORKER_DEFAULT

    gateway = FakeGateway()
    factory = make_worker_factory(gateway=gateway, workspace_root=tmp_path)
    worker = factory("api_worker")

    assert isinstance(worker, BaseWorkerAgent)
    assert worker.worker_type == "api_worker"
    assert worker.tools.permissions == WORKER_DEFAULT


def test_make_worker_factory_rejects_unknown_worker_type(tmp_path):
    gateway = FakeGateway()
    factory = make_worker_factory(gateway=gateway, workspace_root=tmp_path)
    with pytest.raises(ValueError):
        factory("nonexistent_worker")
