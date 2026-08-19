"""Unit tests for app.qa (Phase 12 -- QA System)."""

from __future__ import annotations

import pytest

from app.agents.system.integration_schemas import ContractRegistry
from app.llm.models import TaskType
from app.qa.agents import CodeReviewAgent, IntegrationTestAgent, StaticAnalysisAgent, UnitTestAgent
from app.qa.manager import QAManager
from app.qa.schemas import (
    CodeReviewFinding,
    GeneratedTestSuite,
    QAArchitecturalAssessment,
    QACheckCategory,
    QualityGate,
)
from app.state.enums import ErrorSeverity
from app.state.models import AHSEAState, ProjectMetadata
from app.tools.audit import AuditLog
from app.tools.base import ToolContext, ToolExecutor, ToolResult
from app.tools.permissions import Permission
from app.tools.registry import ToolRegistry


def make_state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )


class FakeTool:
    """Stand-in BaseTool: returns a scripted ToolResult, records calls."""

    def __init__(self, name: str, success: bool = True, error: str | None = None):
        self.name = name
        self.required_permission = Permission.EXECUTE
        self._success = success
        self._error = error
        self.calls: list[dict] = []

    async def __call__(self, ctx, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=self.name,
            success=self._success,
            output={"stdout": "ok" if self._success else "", "stderr": self._error or ""},
            error=self._error,
        )


def make_executor(tool_results: dict[str, bool]) -> ToolExecutor:
    registry = ToolRegistry([FakeTool(name, success=ok) for name, ok in tool_results.items()])
    ctx = ToolContext(
        agent_id="qa-agent",
        permissions=frozenset({Permission.READ, Permission.EXECUTE}),
        sandbox=None,  # not touched by FakeTool
        audit_log=AuditLog(),
    )
    return ToolExecutor(registry=registry, context=ctx)


class FakeToolWithReturncode:
    """Like `FakeTool`, but lets a test script the exact `returncode` the
    real shell tools put in `ToolResult.output` -- needed to simulate the
    "executable could not be started" sentinel (127) that
    `app/tools/shell.py::_run_subprocess` returns for a missing/unusable
    `ruff`/`mypy` binary, as distinct from an ordinary non-zero exit from
    a tool that actually ran and found real issues."""

    def __init__(self, name: str, returncode: int, error: str | None = None):
        self.name = name
        self.required_permission = Permission.EXECUTE
        self.returncode = returncode
        self.error = error

    async def __call__(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=self.returncode == 0,
            output={"stdout": "", "stderr": self.error or "", "returncode": self.returncode},
            error=self.error,
        )


def make_executor_with_returncodes(tool_returncodes: dict[str, int]) -> ToolExecutor:
    registry = ToolRegistry(
        [
            FakeToolWithReturncode(name, returncode=rc, error=f"{name} exited {rc}")
            for name, rc in tool_returncodes.items()
        ]
    )
    ctx = ToolContext(
        agent_id="qa-agent",
        permissions=frozenset({Permission.READ, Permission.EXECUTE}),
        sandbox=None,
        audit_log=AuditLog(),
    )
    return ToolExecutor(registry=registry, context=ctx)


class FakeGateway:
    """Routes by response_model since QA agents use several task types."""

    def __init__(self, code_review_decision: str = "pass", reasoning_summary: str = "ok"):
        self.calls: list[TaskType] = []
        self.code_review_decision = code_review_decision
        self.reasoning_summary = reasoning_summary

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        if response_model is GeneratedTestSuite:
            assert task_type == TaskType.TEST_GENERATION
            return GeneratedTestSuite(summary="tests written", test_files=[])
        if response_model is CodeReviewFinding:
            assert task_type == TaskType.CODE_REVIEW
            issues = [] if self.code_review_decision == "pass" else ["needs a docstring"]
            return CodeReviewFinding(decision=self.code_review_decision, issues=issues)
        if response_model is QAArchitecturalAssessment:
            assert task_type == TaskType.REASONING
            return QAArchitecturalAssessment(
                summary=self.reasoning_summary, recommended_actions=["fix the failing suite"]
            )
        raise AssertionError(f"Unexpected response_model {response_model}")


# ---------------------------------------------------------------------------
# Individual agents + model routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_test_agent_generation_uses_test_generation_routing():
    gateway = FakeGateway()
    agent = UnitTestAgent(gateway=gateway)
    suite = await agent.generate_tests("a service module", ["app/service.py"])
    assert suite.summary == "tests written"
    assert gateway.calls == [TaskType.TEST_GENERATION]


@pytest.mark.asyncio
async def test_unit_test_agent_run_suite_reports_pass():
    executor = make_executor({"run_pytest": True})
    agent = UnitTestAgent(gateway=FakeGateway(), tools=executor)
    check = await agent.run_suite()
    assert check.passed
    assert check.category == QACheckCategory.UNIT_TEST


@pytest.mark.asyncio
async def test_unit_test_agent_run_suite_reports_failure():
    executor = make_executor({"run_pytest": False})
    executor.registry.get("run_pytest")._error = "2 failed"
    agent = UnitTestAgent(gateway=FakeGateway(), tools=executor)
    check = await agent.run_suite()
    assert not check.passed
    assert check.severity == ErrorSeverity.HIGH


@pytest.mark.asyncio
async def test_static_analysis_agent_runs_lint_and_typecheck():
    executor = make_executor({"run_lint": True, "run_typecheck": False})
    agent = StaticAnalysisAgent(tools=executor)
    checks = await agent.run()
    assert len(checks) == 2
    assert all(c.category == QACheckCategory.STATIC_ANALYSIS for c in checks)
    passed = {c.check_name: c.passed for c in checks}
    assert passed == {"lint": True, "type_check": False}


@pytest.mark.asyncio
async def test_static_analysis_agent_treats_missing_tool_as_warning_not_failure():
    """Regression test: a `returncode == 127` (the shell-tool sentinel for
    "the executable could not be started at all" -- missing from PATH, or
    Windows subprocess support unavailable) must be treated like
    `UnitTestAgent` treats a missing `tests/` directory: a low-severity,
    non-blocking warning. Before this fix it was scored identically to a
    real lint/typecheck failure (MEDIUM severity, `passed=False`), which
    fails the QA gate and can escalate to failing the entire project run
    over environment/tooling availability rather than an actual code
    issue."""
    executor = make_executor_with_returncodes({"run_lint": 127, "run_typecheck": 127})
    agent = StaticAnalysisAgent(tools=executor)
    checks = await agent.run()

    assert len(checks) == 2
    assert all(c.passed for c in checks)
    assert all(c.is_warning for c in checks)
    assert all(c.severity == ErrorSeverity.LOW for c in checks)


@pytest.mark.asyncio
async def test_static_analysis_agent_still_fails_on_real_lint_findings():
    """A real, non-127 non-zero exit code (the tool ran and found actual
    issues) must still be scored as a genuine MEDIUM-severity failure --
    the fix above must not accidentally silence real findings."""
    executor = make_executor_with_returncodes({"run_lint": 1, "run_typecheck": 0})
    agent = StaticAnalysisAgent(tools=executor)
    checks = await agent.run()

    by_name = {c.check_name: c for c in checks}
    assert by_name["lint"].passed is False
    assert by_name["lint"].is_warning is False
    assert by_name["lint"].severity == ErrorSeverity.MEDIUM
    assert by_name["type_check"].passed is True


@pytest.mark.asyncio
async def test_code_review_agent_uses_code_review_routing():
    gateway = FakeGateway(code_review_decision="needs_fix")
    agent = CodeReviewAgent(gateway=gateway)
    check = await agent.review("summary", ["app/x.py"], "diff contents")
    assert not check.passed
    assert gateway.calls == [TaskType.CODE_REVIEW]


@pytest.mark.asyncio
async def test_integration_test_agent_treats_missing_suite_as_warning():
    executor = make_executor({})  # no run_pytest tool registered -> ToolNotFoundError
    agent = IntegrationTestAgent(tools=executor)
    check = await agent.run_suite()
    assert check.passed
    assert check.is_warning


# ---------------------------------------------------------------------------
# QAManager pipeline + quality gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_all_green_passes_gates():
    executor = make_executor({"run_pytest": True, "run_lint": True, "run_typecheck": True})
    state = make_state()
    manager = QAManager(gateway=FakeGateway(), tools=executor)

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert report.gate_passed
    assert report.failed_checks == []
    assert len(report.passed_checks) >= 3  # unit + lint + typecheck (+ contract + integration warn)


@pytest.mark.asyncio
async def test_pipeline_passes_gates_when_lint_typecheck_tools_are_unavailable():
    """End-to-end version of the StaticAnalysisAgent regression above: a
    dev/CI environment where `ruff`/`mypy` can't be started (missing from
    PATH, or -- the actual Windows failure this reproduces -- subprocess
    creation unsupported on the current event loop) must not fail the QA
    gate, and therefore must not cascade into failing the whole project
    run. Unit tests still genuinely pass, so the gate should pass."""
    tool_executor = make_executor_with_returncodes(
        {"run_pytest": 0, "run_lint": 127, "run_typecheck": 127}
    )
    state = make_state()
    manager = QAManager(gateway=FakeGateway(), tools=tool_executor)

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert report.gate_passed
    assert report.failed_checks == []


@pytest.mark.asyncio
async def test_pipeline_failing_unit_tests_blocks_gate():
    executor = make_executor({"run_pytest": False, "run_lint": True, "run_typecheck": True})
    state = make_state()
    manager = QAManager(gateway=FakeGateway(), tools=executor)

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert not report.gate_passed
    assert any(c.check_name == "unit_tests" for c in report.failed_checks)
    blocking_gate = next(g for g in report.gate_results if g.gate_name == "unit_tests_must_pass")
    assert not blocking_gate.passed


@pytest.mark.asyncio
async def test_pipeline_runs_full_sequence_including_code_review_and_contracts():
    # run_typecheck fails so the final architecture-level assessment (Qwen3,
    # REASONING) actually has something to reason about.
    executor = make_executor({"run_pytest": True, "run_lint": True, "run_typecheck": False})
    gateway = FakeGateway()
    state = make_state()
    manager = QAManager(gateway=gateway, tools=executor)

    report = await manager.run_pipeline(
        state,
        contract_registry=ContractRegistry([]),
        code_summary="added login endpoint",
        files_changed=["app/api/auth.py"],
        diff_or_content="+ def login(): ...",
    )

    categories = {c.category for c in report.all_checks}
    assert QACheckCategory.UNIT_TEST in categories
    assert QACheckCategory.STATIC_ANALYSIS in categories
    assert QACheckCategory.CODE_REVIEW in categories
    assert QACheckCategory.INTEGRATION_TEST in categories
    assert QACheckCategory.CONTRACT_VALIDATION in categories
    # Test gen / code review must have gone to Qwen2.5-Coder task types,
    # final assessment to Qwen3's REASONING task type.
    assert TaskType.CODE_REVIEW in gateway.calls
    assert TaskType.REASONING in gateway.calls


@pytest.mark.asyncio
async def test_non_blocking_gate_does_not_fail_pipeline():
    executor = make_executor({"run_pytest": True, "run_lint": True, "run_typecheck": True})
    state = make_state()
    non_blocking_gate = QualityGate(
        name="lint_advisory",
        blocking=False,
        max_failed_checks=999,
        categories=[QACheckCategory.STATIC_ANALYSIS],
    )
    manager = QAManager(gateway=FakeGateway(), tools=executor, gates=[non_blocking_gate])

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert report.gate_passed
    assert report.gate_results[0].blocking is False


# ---------------------------------------------------------------------------
# Phase 19: realtime event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_emits_qa_started_and_qa_failed_when_gate_fails():
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager
    from app.realtime.schemas import RealtimeEventType

    executor = make_executor({"run_pytest": False, "run_lint": True, "run_typecheck": True})
    state = make_state()
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    manager = QAManager(gateway=FakeGateway(), tools=executor, realtime=emitter)

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert not report.gate_passed
    events = conn_manager.replay(state.project.project_id)
    event_types = [e.event_type for e in events]
    assert event_types[0] == RealtimeEventType.QA_STARTED
    assert RealtimeEventType.QA_FAILED in event_types


@pytest.mark.asyncio
async def test_pipeline_emits_only_qa_started_when_gate_passes():
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager
    from app.realtime.schemas import RealtimeEventType

    executor = make_executor({"run_pytest": True, "run_lint": True, "run_typecheck": True})
    state = make_state()
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    manager = QAManager(gateway=FakeGateway(), tools=executor, realtime=emitter)

    report = await manager.run_pipeline(state, contract_registry=ContractRegistry([]))

    assert report.gate_passed
    events = conn_manager.replay(state.project.project_id)
    event_types = [e.event_type for e in events]
    assert RealtimeEventType.QA_STARTED in event_types
    assert RealtimeEventType.QA_FAILED not in event_types
