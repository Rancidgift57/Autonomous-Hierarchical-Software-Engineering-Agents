"""`QAManager` (Phase 12): runs the full QA pipeline and applies quality gates.

Distinct from `app.agents.managers.concrete.QAManager` (Phase 7), which is
a *team* manager that delegates rework tasks to a worker when something
needs fixing. This `QAManager` is the *pipeline orchestrator* that decides
whether something needs fixing in the first place: it runs

    unit tests -> static analysis -> code review -> integration tests ->
    contract validation -> final QA judgment

against declared `QualityGate`s and produces one aggregated
`QAPipelineReport`.
"""

from __future__ import annotations

from typing import Any

from app.agents.system.integration import IntegrationAgent, IntegrationReport
from app.agents.system.integration_schemas import ContractRegistry
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.qa.agents import CodeReviewAgent, IntegrationTestAgent, StaticAnalysisAgent, UnitTestAgent
from app.qa.schemas import (
    QAArchitecturalAssessment,
    QACheckCategory,
    QACheckResult,
    QAGateResult,
    QAPipelineReport,
    QualityGate,
)
from app.realtime.emitter import RealtimeEmitter
from app.realtime.schemas import RealtimeEventType
from app.state.enums import ErrorSeverity
from app.state.models import AHSEAState
from app.tools.base import ToolExecutor

#: Severity order, low to high, used to compare against a gate's max_severity.
_SEVERITY_RANK: dict[ErrorSeverity, int] = {
    ErrorSeverity.LOW: 0,
    ErrorSeverity.MEDIUM: 1,
    ErrorSeverity.HIGH: 2,
    ErrorSeverity.CRITICAL: 3,
}

#: Sensible defaults: nothing above MEDIUM severity may fail, zero
#: tolerance for failed unit tests, everything else best-effort/non-blocking.
DEFAULT_QUALITY_GATES: list[QualityGate] = [
    QualityGate(
        name="unit_tests_must_pass",
        blocking=True,
        max_failed_checks=0,
        categories=[QACheckCategory.UNIT_TEST],
    ),
    QualityGate(
        name="no_critical_or_high_findings",
        blocking=True,
        max_failed_checks=0,
        max_severity=ErrorSeverity.MEDIUM,
        categories=[],
    ),
    QualityGate(
        name="contract_validation_must_pass",
        blocking=True,
        max_failed_checks=0,
        categories=[QACheckCategory.CONTRACT_VALIDATION],
    ),
]


class QAManager:
    """Orchestrates the QA pipeline: unit -> static -> review -> integration ->
    contracts -> final."""

    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolExecutor | None = None,
        gates: list[QualityGate] | None = None,
        integration_agent: IntegrationAgent | None = None,
        realtime: RealtimeEmitter | None = None,
    ):
        self.gateway = gateway
        self.tools = tools
        self.gates = gates if gates is not None else list(DEFAULT_QUALITY_GATES)
        self.realtime = realtime
        self.unit_test_agent = UnitTestAgent(gateway=gateway, tools=tools)
        self.static_analysis_agent = StaticAnalysisAgent(tools=tools)
        self.code_review_agent = CodeReviewAgent(gateway=gateway)
        self.integration_test_agent = IntegrationTestAgent(tools=tools)
        self.integration_agent = integration_agent or IntegrationAgent(
            gateway=gateway, realtime=realtime
        )

    # ------------------------------------------------------------------
    # Quality gates
    # ------------------------------------------------------------------

    def _evaluate_gates(self, checks: list[QACheckResult]) -> list[QAGateResult]:
        results: list[QAGateResult] = []
        for gate in self.gates:
            relevant = [c for c in checks if not gate.categories or c.category in gate.categories]
            failed = [c for c in relevant if not c.passed]
            over_severity = [
                c for c in failed if _SEVERITY_RANK[c.severity] > _SEVERITY_RANK[gate.max_severity]
            ]
            gate_failed = len(failed) > gate.max_failed_checks or bool(over_severity)

            reason = ""
            if gate_failed:
                names = ", ".join(c.check_name for c in (over_severity or failed))
                reason = f"{len(failed)} failed check(s) exceeding gate policy: {names}"

            results.append(
                QAGateResult(
                    gate_name=gate.name,
                    passed=not gate_failed,
                    blocking=gate.blocking,
                    reason=reason,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Final QA judgment (Qwen3, architecture-level reasoning)
    # ------------------------------------------------------------------

    async def _final_assessment(
        self, checks: list[QACheckResult], metadata: dict[str, Any] | None
    ) -> QAArchitecturalAssessment:
        failed = [c for c in checks if not c.passed]
        if not failed:
            return QAArchitecturalAssessment(summary="All QA checks passed.")

        findings_text = "\n".join(f"- [{c.category.value}/{c.agent}] {c.message}" for c in failed)
        prompt = (
            "You are performing final architecture-level QA judgment for an automated "
            f"pipeline. The following checks failed:\n{findings_text}\n\n"
            "Summarize the overall quality risk and recommend concrete next actions. "
            "Do not propose code."
        )
        try:
            return await self.gateway.generate_json(
                task_type=TaskType.REASONING,
                prompt=prompt,
                response_model=QAArchitecturalAssessment,
                metadata=metadata,
            )
        except LLMError as exc:
            return QAArchitecturalAssessment(
                summary=f"(LLM assessment unavailable: {exc})",
                recommended_actions=[c.recommended_action or c.message for c in failed],
            )

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def run_pipeline(
        self,
        state: AHSEAState,
        contract_registry: ContractRegistry | None = None,
        code_summary: str = "",
        files_changed: list[str] | None = None,
        diff_or_content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> QAPipelineReport:
        """Run unit tests -> static analysis -> code review -> integration
        tests -> contract validation -> final QA, applying quality gates."""

        checks: list[QACheckResult] = []
        files_changed = files_changed or []

        if self.realtime is not None:
            await self.realtime.emit(
                RealtimeEventType.QA_STARTED,
                payload={"files_changed": files_changed, "code_summary": code_summary},
            )

        # 1. Unit tests
        checks.append(await self.unit_test_agent.run_suite())

        # 2. Static analysis
        checks.extend(await self.static_analysis_agent.run())

        # 3. Code review (only meaningful if there's something to review)
        if files_changed or diff_or_content:
            checks.append(
                await self.code_review_agent.review(
                    summary=code_summary,
                    files_changed=files_changed,
                    diff_or_content=diff_or_content,
                    metadata=metadata,
                )
            )

        # 4. Integration tests
        checks.append(await self.integration_test_agent.run_suite())

        # 5. Contract validation (delegates to the Phase 11 Integration Agent)
        integration_report: IntegrationReport | None = None
        if contract_registry is not None:
            integration_report = await self.integration_agent.run(
                state=state,
                registry=contract_registry,
                create_rework_tasks=False,  # QAManager reports findings; doesn't dispatch rework
                metadata=metadata,
            )
            checks.append(
                QACheckResult(
                    check_name="contract_validation",
                    category=QACheckCategory.CONTRACT_VALIDATION,
                    agent="IntegrationAgent",
                    passed=integration_report.passed,
                    severity=ErrorSeverity.HIGH
                    if not integration_report.passed
                    else ErrorSeverity.LOW,
                    message=integration_report.summary,
                    affected_files=[],
                    recommended_action=(
                        "; ".join(m.recommended_fix for m in integration_report.mismatches) or None
                    ),
                )
            )

        # 6. Final QA judgment (Qwen3, architecture-level reasoning)
        assessment = await self._final_assessment(checks, metadata)

        gate_results = self._evaluate_gates(checks)
        gate_passed = all(g.passed for g in gate_results if g.blocking)

        if not gate_passed and self.realtime is not None:
            failed_gates = [g.gate_name for g in gate_results if g.blocking and not g.passed]
            await self.realtime.emit(
                RealtimeEventType.QA_FAILED,
                payload={
                    "failed_gates": failed_gates,
                    "failed_check_count": len([c for c in checks if not c.passed]),
                    "summary": assessment.summary,
                },
            )

        passed_checks = [c for c in checks if c.passed and not c.is_warning]
        failed_checks = [c for c in checks if not c.passed]
        warnings = [c for c in checks if c.passed and c.is_warning]

        affected_files = sorted({f for c in checks for f in c.affected_files})
        logs = [c.logs for c in checks if c.logs]
        recommended_actions = [assess for assess in assessment.recommended_actions]
        recommended_actions += [c.recommended_action for c in failed_checks if c.recommended_action]

        return QAPipelineReport(
            passed_checks=passed_checks,
            failed_checks=failed_checks,
            warnings=warnings,
            logs=logs,
            affected_files=affected_files,
            recommended_actions=recommended_actions,
            gate_results=gate_results,
            gate_passed=gate_passed,
        )
