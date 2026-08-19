"""Structured types for the QA System (Phase 12).

Distinct from (but convertible to) `app.state.models.QAReport`: that model
is the compact, persisted-in-`AHSEAState` summary; `QAPipelineReport` here
is the richer, in-flight report the pipeline itself builds -- passed
checks, failed checks, warnings, logs, affected files, and recommended
actions, exactly as the Phase 12 spec asks for.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.agents.workers.schemas import WorkerFileChange
from app.state.enums import ErrorSeverity
from app.state.models import _new_id  # reuse the project-wide id scheme


class QACheckCategory(str, Enum):
    UNIT_TEST = "unit_test"
    STATIC_ANALYSIS = "static_analysis"
    CODE_REVIEW = "code_review"
    INTEGRATION_TEST = "integration_test"
    CONTRACT_VALIDATION = "contract_validation"


class QACheckResult(BaseModel):
    """The outcome of a single QA check (one lint run, one test suite run,
    one code review pass, ...)."""

    check_name: str
    category: QACheckCategory
    agent: str = Field(
        description="Name of the agent that produced this check, e.g. 'UnitTestAgent'."
    )
    passed: bool
    is_warning: bool = Field(
        default=False,
        description="True for a non-blocking finding (reported, but doesn't fail the check).",
    )
    severity: ErrorSeverity = ErrorSeverity.LOW
    message: str = ""
    affected_files: list[str] = Field(default_factory=list)
    logs: str | None = None
    recommended_action: str | None = None


# ---------------------------------------------------------------------------
# LLM-facing schemas
# ---------------------------------------------------------------------------


class GeneratedTestSuite(BaseModel):
    """Result of `TaskType.TEST_GENERATION` (routed to Qwen2.5-Coder)."""

    summary: str
    test_files: list[WorkerFileChange] = Field(default_factory=list)


class CodeReviewFinding(BaseModel):
    """Result of `TaskType.CODE_REVIEW` (routed to Qwen2.5-Coder)."""

    decision: str = Field(description="'pass' or 'needs_fix'.")
    issues: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    notes: str = ""


class QAArchitecturalAssessment(BaseModel):
    """Result of `TaskType.REASONING` (routed to Qwen3) -- architecture-level
    QA judgment the mechanical checks can't make on their own, e.g. "these
    failures share a root cause" or "this is an acceptable trade-off"."""

    summary: str
    concerns: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------


class QualityGate(BaseModel):
    """A named pass/fail rule evaluated against the accumulated `QACheckResult`s."""

    name: str
    blocking: bool = Field(
        default=True, description="If True, failing this gate fails the whole pipeline."
    )
    max_failed_checks: int = 0
    max_severity: ErrorSeverity = ErrorSeverity.MEDIUM
    categories: list[QACheckCategory] = Field(
        default_factory=list, description="Empty = applies across all categories."
    )


class QAGateResult(BaseModel):
    gate_name: str
    passed: bool
    blocking: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------


class QAPipelineReport(BaseModel):
    """Aggregated output of one `QAManager.run_pipeline()` call."""

    report_id: str = Field(default_factory=lambda: _new_id("qapipe"))
    passed_checks: list[QACheckResult] = Field(default_factory=list)
    failed_checks: list[QACheckResult] = Field(default_factory=list)
    warnings: list[QACheckResult] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    gate_results: list[QAGateResult] = Field(default_factory=list)
    gate_passed: bool = True

    @property
    def all_checks(self) -> list[QACheckResult]:
        return self.passed_checks + self.failed_checks + self.warnings
