"""Unit tests for app.self_healing (Phase 13 -- Self-Healing System)."""

from __future__ import annotations

import pytest

from app.agents.managers.base import BaseManagerAgent
from app.agents.managers.schemas import (
    ManagerAnalysis,
    ManagerReviewDecision,
    WorkerSelection,
)
from app.agents.workers.schemas import (
    WorkerFileChange,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerSelfReview,
)
from app.llm.models import TaskType
from app.self_healing.engine import DebuggingWorker, SelfHealingEngine
from app.self_healing.schemas import ErrorDiagnosis, RepairOutcome
from app.state.models import AHSEAState, ProjectMetadata, Task
from app.tools.audit import AuditLog
from app.tools.base import ToolContext, ToolExecutor, ToolResult
from app.tools.permissions import Permission
from app.tools.registry import ToolRegistry


def make_state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )


class FakeTool:
    def __init__(self, name: str, succeed: bool = True):
        self.name = name
        self.required_permission = Permission.EXECUTE if "run" in name else Permission.WRITE
        self.succeed = succeed
        self.calls: list[dict] = []

    async def __call__(self, ctx, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=self.name,
            success=self.succeed,
            output={"stdout": "ok", "stderr": ""},
        )


def make_executor(succeed: bool = True) -> ToolExecutor:
    registry = ToolRegistry(
        [
            FakeTool("write_file", succeed=succeed),
            FakeTool("edit_file", succeed=succeed),
            FakeTool("delete_file", succeed=succeed),
            FakeTool("run_pytest", succeed=succeed),
        ]
    )
    ctx = ToolContext(
        agent_id="debug-worker",
        permissions=frozenset({Permission.READ, Permission.WRITE, Permission.EXECUTE}),
        sandbox=None,
        audit_log=AuditLog(),
    )
    return ToolExecutor(registry=registry, context=ctx)


class FakeGateway:
    """Routes every call by response_model; records task_types used."""

    def __init__(self, worker_succeeds: bool = True, manager_accepts: bool = True):
        self.calls: list[TaskType] = []
        self.worker_succeeds = worker_succeeds
        self.manager_accepts = manager_accepts

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)

        if response_model is ErrorDiagnosis:
            assert task_type == TaskType.ERROR_ANALYSIS
            return ErrorDiagnosis(
                root_cause="Null pointer in login handler",
                classification="logic_error",
                responsible_team="Backend",
                proposed_solution="Add a null check before dereferencing the user object.",
                confidence=0.8,
            )
        if response_model is ManagerAnalysis:
            return ManagerAnalysis(summary="Needs a targeted fix.", key_considerations=[], risks=[])
        if response_model is WorkerSelection:
            return WorkerSelection(
                selected_worker_type="debug_worker", rationale="only option", instructions="fix it"
            )
        if response_model is WorkerPlan:
            assert task_type == TaskType.PLANNING
            return WorkerPlan(
                approach="Add null check", steps=["edit login.py"], target_files=["app/login.py"]
            )
        if response_model is WorkerImplementationOutput:
            assert task_type == TaskType.DEBUGGING  # Qwen2.5-Coder DEBUGGING, never CODING
            return WorkerImplementationOutput(
                summary="Added null check to login handler.",
                files=[
                    WorkerFileChange(
                        path="app/login.py",
                        action="edit",
                        old_str="x",
                        new_str="if x: y",
                    )
                ],
            )
        if response_model is WorkerSelfReview:
            assert task_type == TaskType.CODE_REVIEW
            if self.worker_succeeds:
                return WorkerSelfReview(decision="pass", issues=[], notes="Looks good.")
            return WorkerSelfReview(decision="needs_fix", issues=["still crashes"], notes="")
        if response_model is ManagerReviewDecision:
            if self.manager_accepts:
                return ManagerReviewDecision(decision="accept", feedback="Looks good.")
            return ManagerReviewDecision(
                decision="rework", feedback="Not sufficient.", issues=["retry"]
            )

        raise AssertionError(f"Unexpected response_model: {response_model}")


class BackendDebugManager(BaseManagerAgent):
    team_name = "Backend"
    managed_worker_types = ["debug_worker"]
    role_description = "Owns backend repair work."
    max_rework_cycles = 0  # keep tests fast/deterministic


def make_manager_factory(gateway: FakeGateway, tools: ToolExecutor):
    def factory(team_name: str):
        if team_name != "Backend":
            return None

        def worker_factory(worker_type: str):
            assert worker_type == "debug_worker"
            return DebuggingWorker(gateway=gateway, tools=tools, agent_id="debug-worker-1")

        return BackendDebugManager(
            gateway=gateway, manager_id="backend-manager", worker_factory=worker_factory
        )

    return factory


def make_failed_task() -> Task:
    return Task(title="Fix login crash", description="Login endpoint throws a 500.")


# ---------------------------------------------------------------------------
# Happy path: diagnose -> rework -> manager accepts -> SUCCESS on attempt 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heal_succeeds_on_first_attempt_and_uses_correct_model_routing():
    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway(worker_succeeds=True, manager_accepts=True)
    tools = make_executor(succeed=True)
    engine = SelfHealingEngine(
        gateway=gateway, manager_factory=make_manager_factory(gateway, tools)
    )

    result = await engine.heal(state, task, "Login endpoint throws NoneType error.")

    assert result.outcome == RepairOutcome.SUCCESS
    assert result.attempt_count == 1
    assert result.attempts[0].diagnosis is not None
    assert result.attempts[0].diagnosis.responsible_team == "Backend"
    assert result.attempts[0].rework_task_id in state.tasks
    assert state.tasks[result.attempts[0].rework_task_id].status.value == "completed"

    # Model responsibility boundary: Qwen3 only via ERROR_ANALYSIS/MANAGEMENT,
    # Qwen2.5-Coder only via DEBUGGING/CODE_REVIEW/PLANNING -- never CODING.
    assert TaskType.ERROR_ANALYSIS in gateway.calls
    assert TaskType.DEBUGGING in gateway.calls
    assert TaskType.CODING not in gateway.calls


@pytest.mark.asyncio
async def test_heal_creates_rework_task_never_edits_state_files_directly():
    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway()
    tools = make_executor(succeed=True)
    engine = SelfHealingEngine(
        gateway=gateway, manager_factory=make_manager_factory(gateway, tools)
    )

    assert not hasattr(engine, "tools")  # SelfHealingEngine itself has no tool access

    result = await engine.heal(state, task, "Login endpoint throws NoneType error.")

    rework_task_id = result.attempts[0].rework_task_id
    assert state.tasks[rework_task_id].owner_manager == "Backend"
    # The actual file edit happened through the worker's ToolExecutor, not
    # through SelfHealingEngine or ErrorDiagnosis.
    write_tool = tools.registry.get("edit_file")
    assert len(write_tool.calls) == 1


# ---------------------------------------------------------------------------
# Retry then success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heal_retries_after_manager_rejects_then_succeeds():
    state = make_state()
    task = make_failed_task()
    tools = make_executor(succeed=True)

    class FlakyGateway(FakeGateway):
        def __init__(self):
            super().__init__(worker_succeeds=True, manager_accepts=True)
            self._review_calls = 0

        async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
            if response_model is ManagerReviewDecision:
                self._review_calls += 1
                self.calls.append(task_type)
                if self._review_calls == 1:
                    return ManagerReviewDecision(
                        decision="rework", feedback="try again", issues=["x"]
                    )
                return ManagerReviewDecision(decision="accept", feedback="good")
            return await super().generate_json(task_type, prompt, response_model, metadata, **_)

    gateway = FlakyGateway()
    engine = SelfHealingEngine(
        gateway=gateway, manager_factory=make_manager_factory(gateway, tools)
    )

    result = await engine.heal(state, task, "Login endpoint throws NoneType error.")

    assert result.outcome == RepairOutcome.SUCCESS
    assert result.attempt_count == 2
    assert result.attempts[0].outcome == RepairOutcome.FAILED
    assert result.attempts[1].outcome == RepairOutcome.SUCCESS


# ---------------------------------------------------------------------------
# Escalation after MAX_REPAIR_ATTEMPTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heal_escalates_after_max_repair_attempts():
    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway(worker_succeeds=False, manager_accepts=False)
    tools = make_executor(succeed=True)
    engine = SelfHealingEngine(
        gateway=gateway, manager_factory=make_manager_factory(gateway, tools), max_attempts=3
    )

    result = await engine.heal(state, task, "Login endpoint throws NoneType error.")

    assert result.outcome == RepairOutcome.ESCALATED
    assert result.attempt_count == 3
    assert result.escalation_reason is not None
    assert all(a.outcome == RepairOutcome.FAILED for a in result.attempts)

    # An ESCALATE_TO_HUMAN error record and event must have been logged.
    assert any("ESCALATE_TO_HUMAN" in e.message for e in state.errors)
    assert any("escalated" in e.message.lower() for e in state.project_events)


@pytest.mark.asyncio
async def test_heal_never_exceeds_max_attempts_across_repeated_calls():
    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway(worker_succeeds=False, manager_accepts=False)
    tools = make_executor(succeed=True)
    engine = SelfHealingEngine(
        gateway=gateway, manager_factory=make_manager_factory(gateway, tools), max_attempts=2
    )

    first = await engine.heal(state, task, "boom")
    assert first.outcome == RepairOutcome.ESCALATED
    assert first.attempt_count == 2

    # Calling heal() again for the same task must not run any more attempts.
    second = await engine.heal(state, task, "boom again")
    assert second.outcome == RepairOutcome.ESCALATED
    assert second.attempt_count == 2
    assert engine.attempts_for(task.task_id) == second.attempts


@pytest.mark.asyncio
async def test_heal_escalates_when_no_manager_registered_for_team():
    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway()

    def empty_factory(team_name: str):
        return None

    engine = SelfHealingEngine(gateway=gateway, manager_factory=empty_factory, max_attempts=2)

    result = await engine.heal(state, task, "boom")

    assert result.outcome == RepairOutcome.ESCALATED
    assert result.attempt_count == 2
    assert all("No manager registered" in a.detail for a in result.attempts)


# ---------------------------------------------------------------------------
# DebuggingWorker in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debugging_worker_uses_debugging_task_type_not_coding():
    gateway = FakeGateway()
    tools = make_executor(succeed=True)
    worker = DebuggingWorker(gateway=gateway, tools=tools, agent_id="debug-1")

    task = make_failed_task()
    result = await worker.run(task)

    assert result.status.value == "success"
    assert TaskType.DEBUGGING in gateway.calls
    assert TaskType.CODING not in gateway.calls


# ---------------------------------------------------------------------------
# Phase 19: realtime event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heal_emits_repair_started_and_repair_completed_on_success():
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager
    from app.realtime.schemas import RealtimeEventType

    state = make_state()
    task = make_failed_task()
    gateway = FakeGateway(worker_succeeds=True, manager_accepts=True)
    tools = make_executor(succeed=True)
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    engine = SelfHealingEngine(
        gateway=gateway,
        manager_factory=make_manager_factory(gateway, tools),
        realtime=emitter,
    )

    result = await engine.heal(state, task, "Login endpoint throws NoneType error.")

    assert result.outcome == RepairOutcome.SUCCESS
    events = conn_manager.replay(state.project.project_id)
    event_types = [e.event_type for e in events]
    assert event_types == [RealtimeEventType.REPAIR_STARTED, RealtimeEventType.REPAIR_COMPLETED]
    assert all(e.task_id == task.task_id for e in events)
