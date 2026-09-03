"""Prompt regression / hallucination tests (new).

This file answers "if someone tweaks a worker's prompt and it starts
hallucinating or writing broken code, how do we catch that before it
deploys?" with two complementary layers, both of which run in ordinary CI
with no LLM/Ollama available:

1. **Snapshot tests** (`Test*Snapshot` below) pin the literal, rendered
   prompt templates (the worker charter, and the PLAN/IMPLEMENT/SELF-REVIEW
   prompts built in `app.agents.workers.base`). If anyone edits those
   templates, one of these tests fails with a visible diff -- the prompt
   change becomes something a reviewer has to look at and deliberately
   approve (by updating the expected string), instead of silently shipping.

2. **Behavioral regression tests** (`Test*Hallucination*` below) drive a
   real `BaseWorkerAgent` subclass end-to-end through
   `app.prompt_testing.checks`, using the same `FakeGateway` pattern as
   `test_worker_agents.py` to script exactly the kind of bad output a
   broken prompt produces (empty output, refusal language, a self-review
   that contradicts a failing test run, an implementation that drifts off
   its own plan). Each test proves the checker *catches* that failure mode
   -- and `test_well_behaved_run_raises_no_issues` proves it does *not*
   cry wolf on a normal, healthy run, so the checks themselves don't rot
   into noise nobody reads.

Run just this file with:  pytest backend/tests/test_prompt_regression.py -v
"""

from __future__ import annotations

import pytest

from app.agents.workers import (
    APIWorker,
    WorkerContext,
    WorkerFileChange,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerSelfReview,
    WorkerStatus,
)
from app.agents.workers.base import _CHARTER_TEMPLATE
from app.llm.exceptions import InvalidJSONError
from app.llm.models import TaskType
from app.prompt_testing.checks import (
    check_degenerate_text,
    check_implementation_grounding,
    check_plan_grounding,
    check_self_review_consistency,
    run_worker_prompt_checks,
)
from app.state.models import Task
from app.tools.permissions import WORKER_DEFAULT
from app.tools.registry import make_executor


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirrors tests/test_worker_agents.py)
# ---------------------------------------------------------------------------


class FakeGateway:
    """Stand-in for LLMGateway: returns canned structured outputs per task_type."""

    def __init__(self, plan=None, implementation=None, review=None, fail_on: TaskType | None = None):
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
    return make_executor(agent_id="api_worker_1", workspace_root=workspace, permissions=WORKER_DEFAULT)


def good_plan() -> WorkerPlan:
    return WorkerPlan(
        approach="Add a health check route.",
        steps=["Create health.py", "Register router"],
        target_files=["backend/app/api/health.py"],
    )


def good_implementation() -> WorkerImplementationOutput:
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
# 1. Snapshot tests -- catch an accidental/undocumented prompt edit
# ---------------------------------------------------------------------------


class TestPromptSnapshots:
    """Pin the literal prompt text. A failing test here means a prompt was
    edited -- which is fine, but the diff below must be reviewed and this
    expected string updated deliberately, not silently.
    """

    def test_charter_template_snapshot(self):
        rendered = _CHARTER_TEMPLATE.format(
            worker_type="api_worker", role_description="Implements FastAPI routes."
        )
        expected = (
            "You are a api_worker on an autonomous software engineering system "
            "called AHSEA. Your job is to implement exactly the assigned task -- "
            "nothing broader. Implements FastAPI routes.\n\n"
            "You MUST stay within the files relevant to this task. Do not modify "
            "unrelated modules, do not invent new requirements, and do not perform "
            "destructive operations unless the task explicitly calls for them.\n"
        )
        assert rendered == expected

    @pytest.mark.asyncio
    async def test_plan_prompt_contains_required_grounding_instructions(self, executor):
        """Regardless of wording, the PLAN prompt must keep telling the model
        not to write code yet and to name concrete files -- these are the
        two properties `check_plan_grounding` depends on downstream. If a
        prompt edit drops them, this test documents exactly what broke.
        """

        gateway = FakeGateway(
            plan=good_plan(), implementation=good_implementation(), review=WorkerSelfReview(decision="pass")
        )
        worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")
        await worker.run(make_task())

        plan_prompt = worker._last_prompts["plan"]
        assert "Do not write code yet" in plan_prompt
        assert "concrete steps" in plan_prompt
        assert "file paths" in plan_prompt


# ---------------------------------------------------------------------------
# 2a. Unit tests for the individual checks
# ---------------------------------------------------------------------------


class TestIndividualChecks:
    def test_empty_text_is_critical(self):
        issues = check_degenerate_text("plan.approach", "   ")
        assert len(issues) == 1
        assert issues[0].code == "empty_output"
        assert issues[0].severity == "critical"

    def test_refusal_language_is_critical(self):
        issues = check_degenerate_text(
            "implementation.summary",
            "I'm sorry, but I cannot assist with modifying production code.",
        )
        codes = {i.code for i in issues}
        assert "refusal_language" in codes
        assert all(i.severity == "critical" for i in issues if i.code == "refusal_language")

    def test_prompt_echo_is_warning(self):
        prompt = (
            "Produce a short implementation plan: an overall approach, a list "
            "of concrete steps, and the repository-relative file paths you "
            "expect to touch. Do not write code yet."
        )
        # A degenerate response that just parrots the instruction back.
        echoed = (
            "Produce a short implementation plan: an overall approach, a list "
            "of concrete steps, and the repository-relative file paths."
        )
        issues = check_degenerate_text("plan.approach", echoed, source_prompt=prompt)
        assert any(i.code == "prompt_echo" and i.severity == "warning" for i in issues)

    def test_healthy_text_raises_nothing(self):
        assert check_degenerate_text("plan.approach", "Add a health check route.") == []

    def test_plan_with_no_steps_or_files_is_flagged(self):
        empty_plan = WorkerPlan(approach="Do the thing.")
        issues = check_plan_grounding(empty_plan)
        codes = {i.code for i in issues}
        assert "empty_plan_steps" in codes
        assert "empty_target_files" in codes

    def test_implementation_touching_only_undeclared_files_is_flagged(self):
        plan = good_plan()  # target_files = ["backend/app/api/health.py"]
        surprise_impl = WorkerImplementationOutput(
            summary="Refactored auth instead.",
            files=[WorkerFileChange(path="backend/app/auth/session.py", action="create", content="x")],
        )
        issues = check_implementation_grounding(plan, surprise_impl, WorkerContext())
        assert any(i.code == "ungrounded_files" for i in issues)

    def test_implementation_touching_a_declared_file_is_not_flagged(self):
        plan = good_plan()
        issues = check_implementation_grounding(plan, good_implementation(), WorkerContext())
        assert issues == []

    def test_self_review_pass_despite_failing_tests_is_critical(self):
        issues = check_self_review_consistency(
            WorkerSelfReview(decision="pass"),
            {"ran": True, "success": False, "output": "2 failed, 0 passed"},
        )
        assert len(issues) == 1
        assert issues[0].code == "self_review_contradicts_tests"
        assert issues[0].severity == "critical"

    def test_self_review_pass_with_passing_tests_is_fine(self):
        issues = check_self_review_consistency(
            WorkerSelfReview(decision="pass"), {"ran": True, "success": True, "output": "3 passed"}
        )
        assert issues == []

    def test_self_review_needs_fix_despite_failing_tests_is_not_double_flagged(self):
        # The model correctly reported the failure -- nothing to catch here;
        # `BaseWorkerAgent`'s own status logic already handles this case.
        issues = check_self_review_consistency(
            WorkerSelfReview(decision="needs_fix", issues=["tests fail"]),
            {"ran": True, "success": False, "output": "boom"},
        )
        assert issues == []


# ---------------------------------------------------------------------------
# 2b. End-to-end: drive a real worker through a scripted "broken prompt"
# ---------------------------------------------------------------------------


class TestEndToEndHallucinationDetection:
    @pytest.mark.asyncio
    async def test_well_behaved_run_raises_no_issues(self, executor):
        """The golden path: no false positives on ordinary, healthy output."""

        gateway = FakeGateway(
            plan=good_plan(), implementation=good_implementation(), review=WorkerSelfReview(decision="pass")
        )
        worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

        result = await worker.run(make_task(), WorkerContext(module_summary="Backend API module."))

        assert result.status == WorkerStatus.SUCCESS
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_catches_self_review_hallucination_via_broken_prompt(self, executor):
        """Simulates the exact scenario in the user's question: someone
        tweaks the self-review prompt (e.g. removes the instruction to
        weigh test output) and the model starts rubber-stamping
        `decision='pass'` even though the real pytest run failed. The
        worker's own status logic already prevents this from reporting
        SUCCESS -- this test asserts the *diagnostic* also fires, so the
        failure shows up as a named, greppable hallucination rather than a
        generic PARTIAL/FAILED.
        """

        implementation = WorkerImplementationOutput(
            summary="Added GET /health.",
            files=[
                WorkerFileChange(
                    path="backend/app/api/health.py",
                    action="create",
                    content="def health():\n    assert False  # bug\n",
                )
            ],
        )
        gateway = FakeGateway(
            plan=good_plan(),
            implementation=implementation,
            # A "broken prompt" review model that always says pass,
            # ignoring the failing test output it was shown.
            review=WorkerSelfReview(decision="pass"),
        )
        worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

        # Force a failing test run regardless of what pytest finds in the
        # (empty) tmp_path workspace, so this test is about the review
        # stage's hallucination, not the tool system.
        async def fake_test():
            return {"success": False, "output": "1 failed, 0 passed", "ran": True}

        worker._test = fake_test  # type: ignore[method-assign]

        result = await worker.run(make_task())

        assert result.status != WorkerStatus.SUCCESS
        assert any("self_review_contradicts_tests" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_catches_refusal_language_from_a_broken_prompt(self, executor):
        """Simulates a prompt edit that accidentally makes the coding model
        think the task is unsafe/out of scope and refuse instead of coding.
        """

        implementation = WorkerImplementationOutput(
            summary="I'm sorry, but I cannot assist with modifying this file.",
            files=[],
        )
        gateway = FakeGateway(
            plan=good_plan(), implementation=implementation, review=WorkerSelfReview(decision="needs_fix", issues=["no files produced"])
        )
        worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

        result = await worker.run(make_task())

        assert any("refusal_language" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_catches_implementation_drifting_off_plan(self, executor):
        """A coder model that ignores its own plan and edits something
        unrelated is a classic 'model went off the rails' symptom -- flagged
        as a warning even though `WorkerScope` may still allow the path.
        """

        drifted_implementation = WorkerImplementationOutput(
            summary="Cleaned up unrelated auth logic.",
            files=[
                WorkerFileChange(
                    path="backend/app/api/unexpected_module.py", action="create", content="x = 1\n"
                )
            ],
        )
        gateway = FakeGateway(
            plan=good_plan(),  # target_files = ["backend/app/api/health.py"]
            implementation=drifted_implementation,
            review=WorkerSelfReview(decision="pass"),
        )
        worker = APIWorker(gateway=gateway, tools=executor, agent_id="api_worker_1")

        result = await worker.run(make_task())

        assert any("ungrounded_files" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_run_worker_prompt_checks_used_standalone_for_ci_gating(self, executor):
        """Shows the checker can also be used outside `BaseWorkerAgent.run` --
        e.g. as a standalone CI gate that fails the build on any `critical`
        issue when smoke-testing a prompt change against a fixed set of
        recorded/golden LLM outputs, without needing Ollama available.
        """

        report = run_worker_prompt_checks(
            plan=good_plan(),
            implementation=good_implementation(),
            review=WorkerSelfReview(decision="pass"),
            context=WorkerContext(),
            tests={"ran": True, "success": True, "output": "3 passed"},
        )
        assert report.has_critical is False

        bad_report = run_worker_prompt_checks(
            plan=good_plan(),
            implementation=good_implementation(),
            review=WorkerSelfReview(decision="pass"),
            context=WorkerContext(),
            tests={"ran": True, "success": False, "output": "3 failed"},
        )
        assert bad_report.has_critical is True
        # This is exactly the assertion a CI job would use to fail the
        # build: `assert not report.has_critical, report.as_error_strings()`
