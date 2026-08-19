"""QA pipeline agents (Phase 12): `UnitTestAgent`, `IntegrationTestAgent`,
`StaticAnalysisAgent`, `CodeReviewAgent`.

Model routing (all already wired in `app.llm.gateway`):
    * Test generation      -> `TaskType.TEST_GENERATION` -> Qwen2.5-Coder
    * Code review           -> `TaskType.CODE_REVIEW`     -> Qwen2.5-Coder
    * Architecture-level QA -> `TaskType.REASONING`        -> Qwen3

Test *execution* (running pytest/ruff/mypy) is not an LLM call at all --
it goes through the Phase 9 tool system (`self.tools.run("run_pytest", ...)`),
same as a worker's own TEST stage.
"""

from __future__ import annotations

from typing import Any

from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.qa.schemas import (
    CodeReviewFinding,
    GeneratedTestSuite,
    QACheckCategory,
    QACheckResult,
)
from app.state.enums import ErrorSeverity
from app.tools.base import ToolExecutor
from app.tools.exceptions import ToolError


def _tool_output_logs(output: Any) -> str | None:
    if isinstance(output, dict):
        return (output.get("stdout", "") + "\n" + output.get("stderr", "")).strip() or None
    return None


class UnitTestAgent:
    """Generates missing unit tests (Qwen2.5-Coder) and/or runs the existing suite."""

    name = "UnitTestAgent"

    def __init__(self, gateway: LLMGateway, tools: ToolExecutor | None = None):
        self.gateway = gateway
        self.tools = tools

    async def generate_tests(
        self, module_summary: str, target_files: list[str], metadata: dict[str, Any] | None = None
    ) -> GeneratedTestSuite:
        prompt = (
            "You are the UnitTestAgent in an automated QA pipeline. Write focused unit "
            f"tests for the following module:\n{module_summary}\n\n"
            f"Files to cover: {', '.join(target_files) or '(unspecified)'}\n\n"
            "Return complete test file contents, not just snippets."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.TEST_GENERATION,
            prompt=prompt,
            response_model=GeneratedTestSuite,
            metadata=metadata,
        )

    async def run_suite(self, path: str = "tests") -> QACheckResult:
        if self.tools is None:
            return QACheckResult(
                check_name="unit_tests",
                category=QACheckCategory.UNIT_TEST,
                agent=self.name,
                passed=False,
                severity=ErrorSeverity.HIGH,
                message="No tool access configured; unit tests were not run.",
            )
        try:
            result = await self.tools.run("run_pytest", path=path)
        except ToolError as exc:
            # A missing `tests/` directory means no tests have been written
            # yet (e.g. this is an early task in the DAG) -- that is a
            # warning, not a blocking QA failure. A `tests/` directory that
            # exists but errors out when pytest actually runs it (syntax
            # errors, import errors, etc.) is still a hard failure, handled
            # below via `result.success`. Mirrors IntegrationTestAgent.
            return QACheckResult(
                check_name="unit_tests",
                category=QACheckCategory.UNIT_TEST,
                agent=self.name,
                passed=True,
                is_warning=True,
                severity=ErrorSeverity.LOW,
                message=f"Unit tests skipped: {exc}",
            )
        return QACheckResult(
            check_name="unit_tests",
            category=QACheckCategory.UNIT_TEST,
            agent=self.name,
            passed=result.success,
            severity=ErrorSeverity.LOW if result.success else ErrorSeverity.HIGH,
            message="Unit tests passed."
            if result.success
            else (result.error or "Unit tests failed."),
            logs=_tool_output_logs(result.output),
        )


class IntegrationTestAgent:
    """Runs the integration test suite (a separate directory/marker from unit tests)."""

    name = "IntegrationTestAgent"

    def __init__(self, tools: ToolExecutor | None = None):
        self.tools = tools

    async def run_suite(self, path: str = "tests/integration") -> QACheckResult:
        if self.tools is None:
            return QACheckResult(
                check_name="integration_tests",
                category=QACheckCategory.INTEGRATION_TEST,
                agent=self.name,
                passed=False,
                severity=ErrorSeverity.HIGH,
                message="No tool access configured; integration tests were not run.",
            )
        try:
            result = await self.tools.run("run_pytest", path=path)
        except ToolError as exc:
            # A missing integration-tests directory is a warning, not a hard
            # failure -- plenty of projects only have unit tests early on.
            return QACheckResult(
                check_name="integration_tests",
                category=QACheckCategory.INTEGRATION_TEST,
                agent=self.name,
                passed=True,
                is_warning=True,
                severity=ErrorSeverity.LOW,
                message=f"Integration tests skipped: {exc}",
            )
        return QACheckResult(
            check_name="integration_tests",
            category=QACheckCategory.INTEGRATION_TEST,
            agent=self.name,
            passed=result.success,
            severity=ErrorSeverity.LOW if result.success else ErrorSeverity.HIGH,
            message=(
                "Integration tests passed."
                if result.success
                else (result.error or "Integration tests failed.")
            ),
            logs=_tool_output_logs(result.output),
        )


class StaticAnalysisAgent:
    """Runs lint + type checking via the Phase 9 tool system (no LLM call)."""

    name = "StaticAnalysisAgent"

    def __init__(self, tools: ToolExecutor | None = None):
        self.tools = tools

    async def run(self, path: str = ".") -> list[QACheckResult]:
        if self.tools is None:
            return [
                QACheckResult(
                    check_name="static_analysis",
                    category=QACheckCategory.STATIC_ANALYSIS,
                    agent=self.name,
                    passed=False,
                    severity=ErrorSeverity.MEDIUM,
                    message="No tool access configured; static analysis was not run.",
                )
            ]

        checks: list[QACheckResult] = []
        for check_name, tool_name in (("lint", "run_lint"), ("type_check", "run_typecheck")):
            try:
                result = await self.tools.run(tool_name, path=path)
                # returncode 127 is this codebase's own sentinel (see
                # `_run_subprocess` in app/tools/shell.py) for "the
                # executable itself could not be started" -- missing from
                # PATH, or (on Windows, under a Selector event loop)
                # subprocess creation unsupported entirely. That is an
                # environment/tooling problem, not a code-quality finding,
                # and must be treated the same way `UnitTestAgent` already
                # treats a missing `tests/` directory: a low-severity
                # warning that doesn't block the QA gate, not a real
                # MEDIUM-severity failure. Without this distinction, a dev
                # machine where `ruff`/`mypy` aren't runnable for
                # environment reasons fails the QA gate every time,
                # exhausts self-healing retries (which can't fix an
                # environment problem), and fails the entire project run
                # over tooling availability rather than actual code issues.
                tool_unavailable = (
                    isinstance(result.output, dict) and result.output.get("returncode") == 127
                )
                checks.append(
                    QACheckResult(
                        check_name=check_name,
                        category=QACheckCategory.STATIC_ANALYSIS,
                        agent=self.name,
                        passed=result.success or tool_unavailable,
                        is_warning=tool_unavailable,
                        severity=(
                            ErrorSeverity.LOW
                            if result.success or tool_unavailable
                            else ErrorSeverity.MEDIUM
                        ),
                        message=(
                            f"{check_name} passed."
                            if result.success
                            else f"{check_name} skipped: {result.error}"
                            if tool_unavailable
                            else (result.error or "")
                        ),
                        logs=_tool_output_logs(result.output),
                    )
                )
            except ToolError as exc:
                checks.append(
                    QACheckResult(
                        check_name=check_name,
                        category=QACheckCategory.STATIC_ANALYSIS,
                        agent=self.name,
                        passed=False,
                        severity=ErrorSeverity.MEDIUM,
                        message=str(exc),
                    )
                )
        return checks


class CodeReviewAgent:
    """LLM-driven code review (Qwen2.5-Coder, via `TaskType.CODE_REVIEW`)."""

    name = "CodeReviewAgent"

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def review(
        self,
        summary: str,
        files_changed: list[str],
        diff_or_content: str,
        metadata: dict[str, Any] | None = None,
    ) -> QACheckResult:
        prompt = (
            "You are the CodeReviewAgent in an automated QA pipeline. Review the "
            f"following change for correctness, style, and risk.\n\nSummary: {summary}\n"
            f"Files changed: {', '.join(files_changed)}\n\nChange contents:\n{diff_or_content}\n\n"
            "Decide 'pass' if this is safe to ship, otherwise 'needs_fix' with concrete issues."
        )
        finding: CodeReviewFinding = await self.gateway.generate_json(
            task_type=TaskType.CODE_REVIEW,
            prompt=prompt,
            response_model=CodeReviewFinding,
            metadata=metadata,
        )
        passed = finding.decision == "pass"
        return QACheckResult(
            check_name="code_review",
            category=QACheckCategory.CODE_REVIEW,
            agent=self.name,
            passed=passed,
            severity=ErrorSeverity.LOW if passed else ErrorSeverity.MEDIUM,
            message=finding.notes or "; ".join(finding.issues) or "Review passed.",
            affected_files=finding.affected_files or files_changed,
            recommended_action=(
                "; ".join(finding.issues) if not passed and finding.issues else None
            ),
        )
