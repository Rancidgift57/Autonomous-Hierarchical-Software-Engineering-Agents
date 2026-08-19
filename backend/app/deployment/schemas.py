"""Structured types for the Deployment System (Phase 15).

Model responsibility boundary (mirrors the QA/self-healing phases):
    * `TaskType.PLANNING` (Qwen3) produces `DeploymentPlan` -- prose: what
      needs to happen, in what order, and how to roll it back. It never
      emits file contents.
    * `TaskType.CODING` / `TaskType.CONFIGURATION` / `TaskType.DOCUMENTATION`
      (Qwen2.5-Coder) produce the actual `GeneratedDockerfile`,
      `GeneratedDeploymentConfig`, and `GeneratedDeploymentScript` file
      contents that `DeploymentManager` validates and writes to disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.state.enums import ErrorSeverity
from app.state.models import _new_id  # reuse the project-wide id scheme

# ---------------------------------------------------------------------------
# Planning (Qwen3, TaskType.PLANNING)
# ---------------------------------------------------------------------------


class DeploymentPlan(BaseModel):
    """Result of `TaskType.PLANNING` -- prose only, no file contents."""

    summary: str
    steps: list[str] = Field(default_factory=list)
    target_environment: str = "staging"
    base_image_recommendation: str = ""
    risk_notes: list[str] = Field(default_factory=list)
    rollback_strategy: str = ""


# ---------------------------------------------------------------------------
# Generation (Qwen2.5-Coder, TaskType.CODING / CONFIGURATION / DOCUMENTATION)
# ---------------------------------------------------------------------------


class GeneratedDockerfile(BaseModel):
    """Result of `TaskType.CODING` -- the Dockerfile's actual contents."""

    content: str
    notes: str = ""


class GeneratedDeploymentConfig(BaseModel):
    """Result of `TaskType.CONFIGURATION` -- compose file + env template."""

    docker_compose_content: str
    env_template_content: str = ""
    notes: str = ""


class GeneratedDeploymentScript(BaseModel):
    """Result of `TaskType.DOCUMENTATION` -- an auxiliary deploy/rollback script."""

    filename: str
    content: str
    description: str = ""


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


class EnvVarSpec(BaseModel):
    """A single environment variable the deployed service needs."""

    name: str
    required: bool = True
    secret: bool = False
    description: str = ""
    default: str | None = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    field: str
    severity: ErrorSeverity
    message: str


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [
            i for i in self.issues if i.severity in (ErrorSeverity.HIGH, ErrorSeverity.CRITICAL)
        ]


# ---------------------------------------------------------------------------
# Health checks / smoke tests
# ---------------------------------------------------------------------------


class HealthCheckSpec(BaseModel):
    container_name: str
    max_attempts: int = 10
    interval_seconds: float = 2.0
    require_healthy_status: bool = True


class HealthCheckResult(BaseModel):
    passed: bool
    container_name: str
    attempts: int = 0
    last_status: str | None = None
    last_health: str | None = None
    message: str = ""


class SmokeTestCase(BaseModel):
    name: str
    command: list[str] = Field(
        default_factory=list,
        description="Argv, e.g. a curl-style health endpoint check run inside the sandbox.",
    )
    expected_returncode: int = 0


class SmokeTestResult(BaseModel):
    name: str
    passed: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Approval / rollback
# ---------------------------------------------------------------------------


class DeploymentApproval(BaseModel):
    """Human approval decision. `DeploymentManager.deploy()` refuses to run
    without one of these on record with `approved=True`."""

    approved: bool
    approved_by: str
    reason: str = ""
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RollbackPlan(BaseModel):
    """Captured immediately before `deploy()` promotes a new image, so a
    failed deploy can be reverted without re-deriving what was running
    before."""

    previous_image_tag: str | None = None
    previous_project_name: str | None = None
    compose_path: str | None = None
    instructions: str = ""
    prepared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class DeploymentEventType(str, Enum):
    QA_GATE_CHECKED = "qa_gate_checked"
    PLAN_CREATED = "plan_created"
    ARTIFACTS_GENERATED = "artifacts_generated"
    VALIDATION_COMPLETED = "validation_completed"
    BUILD_STARTED = "build_started"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    START_STARTED = "start_started"
    START_COMPLETED = "start_completed"
    START_FAILED = "start_failed"
    HEALTH_CHECK_STARTED = "health_check_started"
    HEALTH_CHECK_PASSED = "health_check_passed"
    HEALTH_CHECK_FAILED = "health_check_failed"
    SMOKE_TEST_STARTED = "smoke_test_started"
    SMOKE_TEST_PASSED = "smoke_test_passed"
    SMOKE_TEST_FAILED = "smoke_test_failed"
    DEPLOYMENT_READY = "deployment_ready"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    DEPLOY_STARTED = "deploy_started"
    DEPLOY_COMPLETED = "deploy_completed"
    DEPLOY_FAILED = "deploy_failed"
    ROLLBACK_PREPARED = "rollback_prepared"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_COMPLETED = "rollback_completed"


class DeploymentEvent(BaseModel):
    """A single, structured deployment-pipeline event.

    `data` must never contain secret values -- callers are expected to
    have already run anything user-supplied through
    `app.deployment.validator.redact_secrets` before it lands here.
    """

    event_id: str = Field(default_factory=lambda: _new_id("depevt"))
    event_type: DeploymentEventType
    stage: str = ""
    message: str = ""
    data: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Aggregated report
# ---------------------------------------------------------------------------


class DeploymentReport(BaseModel):
    """What `DeploymentManager.run_pipeline()` / `.deploy()` hand back."""

    report_id: str = Field(default_factory=lambda: _new_id("deprep"))
    service_name: str
    target_environment: str
    stage: str = "not_started"
    image_tag: str | None = None
    container_name: str | None = None
    project_name: str | None = None
    compose_path: str = "docker-compose.yml"
    dockerfile_path: str = "Dockerfile"
    plan: DeploymentPlan | None = None
    validation_results: list[ValidationResult] = Field(default_factory=list)
    health_check: HealthCheckResult | None = None
    smoke_tests: list[SmokeTestResult] = Field(default_factory=list)
    ready_for_approval: bool = False
    approval: DeploymentApproval | None = None
    rollback_plan: RollbackPlan | None = None
    deployed: bool = False
    events: list[DeploymentEvent] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    error: str | None = None

    @property
    def validation_passed(self) -> bool:
        return all(v.passed for v in self.validation_results)
