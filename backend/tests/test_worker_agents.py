"""Unit tests for app.agents.workers (Phase 8 -- coding worker agents)."""

from __future__ import annotations

import pytest

from app.agents.workers import (
    WORKER_CLASSES,
    APIWorker,
    WorkerContext,
    WorkerFileChange,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerScope,
    WorkerScopeError,
    WorkerSelfReview,
    WorkerStatus,
)
from app.llm.exceptions import InvalidJSONError
from app.llm.models import TaskType
from app.state.models import Task
from app.tools.permissions import WORKER_DEFAULT
from app.tools.registry import make_executor


class FakeGateway:
    """Stand-in for LLMGateway: returns canned structured outputs per task_type."""

    def __init__(
        self, plan=None, implementation=None, review=None, fail_on: TaskType | None = None
    ):
        self.plan = plan
        self.implementation = implementation
        self.review = review
        self.fail_on = fail_on
        self.calls: list[TaskType] = []

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        if self.fail_on is not None and task_type == self.fail_on:
            raise InvalidJSONError("simulated LLM failure")
        if task_type == TaskType.PLANNING:
            return self.plan
        if task_type == TaskType.CODING:
            return self.implementation
        if task_type == TaskType.CODE_REVIEW:
            return self.review
        raise AssertionError(f"Unexpected task_type routed to worker: {task_type}")


def make_task(**overrides) -> Task:
    defaults = dict(
        title="Add /health endpoint",
        description="Implement a GET /health endpoint returning {'status': 'ok'}.",
        worker_type="api_worker",
        expected_outputs=["backend/app/api/health.py"],
    )
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "backend" / "app" / "api").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def executor(workspace):
    return make_executor(
        agent_id="api_worker_1", workspace_root=workspace, permissions=WORKER_DEFAULT
    )


def default_plan() -> WorkerPlan:
    return WorkerPlan(
        approach="Add a health check route.",
        steps=["Create health.py", "Register router"],
        target_files=["backend/app/api/health.py"],
    )


def default_implementation() -> WorkerImplementationOutput:
    return WorkerImplementationOutput(
        summary="Added GET /health returning status ok.",
        files=[
            WorkerFileChange(
                path="backend/app/api/health.py",
                action="create",
                content="def health():\n    return {'status': 'ok'}\n",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_workflow_success(executor):
    gateway = FakeGateway(
        plan=default_plan(),
        implementation=default_implementation(),
        review=WorkerSelfReview(decision="pass"),
    )
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")
    task = make_task()

    result = await worker.run(task, WorkerContext(module_summary="Backend API module."))

    assert result.status == WorkerStatus.SUCCESS
    assert result.task_id == task.task_id
    assert result.agent_id == "api_worker_1"
    assert "backend/app/api/health.py" in result.files_changed
    assert result.errors == []

    read_result = await executor.run("read_file", path="backend/app/api/health.py")
    assert "status" in read_result.output


@pytest.mark.asyncio
async def test_worker_routes_each_stage_to_correct_task_type(executor):
    gateway = FakeGateway(
        plan=default_plan(),
        implementation=default_implementation(),
        review=WorkerSelfReview(decision="pass"),
    )
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")
    await worker.run(make_task())

    assert gateway.calls == [TaskType.PLANNING, TaskType.CODING, TaskType.CODE_REVIEW]


# ---------------------------------------------------------------------------
# Self-review loop / partial status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_fix_review_yields_partial_status_with_issues(executor):
    gateway = FakeGateway(
        plan=default_plan(),
        implementation=default_implementation(),
        review=WorkerSelfReview(decision="needs_fix", issues=["Missing docstring"]),
    )
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

    result = await worker.run(make_task())

    assert result.status == WorkerStatus.PARTIAL
    assert "Missing docstring" in result.errors
    # The file was still written -- self-review found a quality issue, not
    # that nothing was produced.
    assert "backend/app/api/health.py" in result.files_changed


# ---------------------------------------------------------------------------
# Scope controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_scope_path_fails_the_task(executor):
    implementation = WorkerImplementationOutput(
        summary="oops",
        files=[WorkerFileChange(path="frontend/app/page.tsx", action="create", content="x")],
    )
    gateway = FakeGateway(
        plan=default_plan(), implementation=implementation, review=WorkerSelfReview(decision="pass")
    )
    scope = WorkerScope(allowed_path_prefixes=["backend/app/api/"])
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1", scope=scope)

    result = await worker.run(make_task())

    assert result.status == WorkerStatus.FAILED
    assert result.files_changed == []
    assert any("outside this worker's allowed scope" in e for e in result.errors)


@pytest.mark.asyncio
async def test_max_files_changed_scope_enforced(executor):
    implementation = WorkerImplementationOutput(
        summary="too many files",
        files=[
            WorkerFileChange(path=f"backend/app/api/f{i}.py", action="create", content="x")
            for i in range(5)
        ],
    )
    gateway = FakeGateway(
        plan=default_plan(), implementation=implementation, review=WorkerSelfReview(decision="pass")
    )
    scope = WorkerScope(allowed_path_prefixes=["backend/app/api/"], max_files_changed=2)
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1", scope=scope)

    result = await worker.run(make_task())

    assert result.status == WorkerStatus.FAILED
    assert result.files_changed == []


def test_validate_scope_raises_directly():
    scope = WorkerScope(allowed_path_prefixes=["backend/"])
    worker = APIWorker.__new__(APIWorker)  # bypass __init__, only need scope
    worker.scope = scope
    with pytest.raises(WorkerScopeError):
        worker._validate_scope([WorkerFileChange(path="frontend/x.py", action="create")])


# ---------------------------------------------------------------------------
# LLM failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_failure_yields_failed_status(executor):
    gateway = FakeGateway(fail_on=TaskType.PLANNING)
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

    result = await worker.run(make_task())

    assert result.status == WorkerStatus.FAILED
    assert result.errors
    assert result.files_changed == []


@pytest.mark.asyncio
async def test_coding_failure_yields_failed_status(executor):
    gateway = FakeGateway(plan=default_plan(), fail_on=TaskType.CODING)
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

    result = await worker.run(make_task())

    assert result.status == WorkerStatus.FAILED


# ---------------------------------------------------------------------------
# Worker registry / identity
# ---------------------------------------------------------------------------


def test_all_nine_workers_registered():
    expected = {
        "api_worker", "auth_worker", "service_worker", "ui_worker",
        "component_worker", "schema_worker", "migration_worker",
        "model_worker", "evaluation_worker",
    }
    assert set(WORKER_CLASSES) == expected


@pytest.mark.asyncio
async def test_worker_never_touches_llm_provider_directly(executor):
    # BaseWorkerAgent only ever holds `self.gateway` -- there is no
    # provider/model attribute to reach into, and the fake gateway proves
    # every call went through `generate_json(task_type=...)`.
    gateway = FakeGateway(
        plan=default_plan(),
        implementation=default_implementation(),
        review=WorkerSelfReview(decision="pass"),
    )
    worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")
    assert not hasattr(worker, "provider")
    assert not hasattr(worker, "model")
    await worker.run(make_task())
    assert all(isinstance(t, TaskType) for t in gateway.calls)
