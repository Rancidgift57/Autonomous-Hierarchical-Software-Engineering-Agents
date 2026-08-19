"""Pydantic v2 models for the AHSEA shared project state.

This module defines every structured data type that flows through the
agent hierarchy: project metadata, requirements, architecture, the task
DAG, artifacts, contracts, QA results, deployment state, errors, and
events. These models are the single source of truth for inter-agent
communication and MUST be used instead of ad-hoc dicts or free-form
conversation history.

No database persistence is implemented here (see Phase 4+). Everything in
this module is an in-memory, JSON-serializable representation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.state.enums import (
    AgentStatus,
    AgentType,
    ArtifactType,
    ContractType,
    DeploymentStage,
    ErrorSeverity,
    EventLevel,
    RequirementPriority,
    RequirementStatus,
    SystemAgentKind,
    TaskComplexity,
    TaskStatus,
    TestOutcome,
)


def _now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    """Generate a short, prefixed unique identifier."""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AHSEABaseModel(BaseModel):
    """Common configuration shared by every state model."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
    )


# ---------------------------------------------------------------------------
# Project metadata & requirements
# ---------------------------------------------------------------------------


class ProjectMetadata(AHSEABaseModel):
    """Top-level descriptive information about the project being built."""

    project_id: str = Field(default_factory=lambda: _new_id("proj"))
    name: str
    description: str
    idea_prompt: str = Field(
        description="Original natural-language project idea supplied by the user."
    )
    workspace_path: str | None = Field(
        default=None, description="Filesystem path of the project workspace."
    )
    repo_url: str | None = None
    default_branch: str = "main"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    tags: list[str] = Field(default_factory=list)


class Requirement(AHSEABaseModel):
    """A single functional or non-functional requirement."""

    requirement_id: str = Field(default_factory=lambda: _new_id("req"))
    title: str
    description: str
    priority: RequirementPriority = RequirementPriority.MUST_HAVE
    status: RequirementStatus = RequirementStatus.PROPOSED
    acceptance_criteria: list[str] = Field(default_factory=list)
    related_module_ids: list[str] = Field(default_factory=list)
    source: str = Field(
        default="user", description="Origin of the requirement, e.g. user, cto, manager."
    )
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------


class Technology(AHSEABaseModel):
    """A technology/library/framework chosen for the project."""

    name: str
    category: str = Field(
        description="e.g. backend, frontend, database, llm, testing, infra"
    )
    version_constraint: str | None = None
    rationale: str | None = None


class Module(AHSEABaseModel):
    """A logical subsystem/component of the architecture."""

    module_id: str = Field(default_factory=lambda: _new_id("mod"))
    name: str
    description: str
    owning_team: str | None = Field(
        default=None, description="Team name expected to own this module."
    )
    depends_on_module_ids: list[str] = Field(default_factory=list)
    technologies: list[Technology] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)


class ArchitectureDecision(AHSEABaseModel):
    """A recorded architecture decision record (ADR)."""

    decision_id: str = Field(default_factory=lambda: _new_id("adr"))
    title: str
    context: str
    decision: str
    consequences: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class ArchitectureState(AHSEABaseModel):
    """The evolving architecture of the project."""

    modules: list[Module] = Field(default_factory=list)
    technologies: list[Technology] = Field(default_factory=list)
    decisions: list[ArchitectureDecision] = Field(default_factory=list)
    diagram_artifact_ids: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Tasks (dependency-aware DAG)
# ---------------------------------------------------------------------------


class TaskDependency(AHSEABaseModel):
    """A directed dependency edge: `task_id` depends on `depends_on_task_id`."""

    task_id: str
    depends_on_task_id: str


class TaskResult(AHSEABaseModel):
    """Outcome of executing a task."""

    task_id: str
    success: bool
    summary: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    logs: str | None = None
    error_message: str | None = None
    duration_seconds: float | None = None
    completed_at: datetime = Field(default_factory=_now)


class Task(AHSEABaseModel):
    """A unit of work in the dependency-aware task DAG.

    `owner_manager`, `worker_type`, `expected_outputs`, and `complexity`
    are populated at planning time (see Phase 5's CTO agent / Phase 6's
    task DAG) to describe *who* should pick up the task and *what* a
    complete result looks like, ahead of actually assigning a concrete
    `assigned_agent_id`.
    """

    task_id: str = Field(default_factory=lambda: _new_id("task"))
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: str | None = None
    owner_manager: str | None = Field(
        default=None,
        description="Team/manager expected to own this task, e.g. 'Backend', 'Frontend'.",
    )
    worker_type: str | None = Field(
        default=None,
        description="Kind of worker expected to execute this task, e.g. 'api_worker'.",
    )
    module_id: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    depends_on_task_ids: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(
        default_factory=list,
        description="Human-readable description of artifacts/outcomes this task should produce.",
    )
    priority: int = Field(default=0, description="Higher runs first among ready tasks.")
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    retries: int = 0
    max_retries: int = 3
    result: TaskResult | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentDefinition(AHSEABaseModel):
    """Static definition of an agent within the hierarchy."""

    agent_id: str = Field(default_factory=lambda: _new_id("agent"))
    name: str
    agent_type: AgentType
    system_kind: SystemAgentKind | None = Field(
        default=None,
        description="Set only when agent_type == SYSTEM_AGENT.",
    )
    parent_agent_id: str | None = None
    team_name: str | None = None
    role_description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class AgentRuntimeStatus(AHSEABaseModel):
    """Live status of an agent instance during execution."""

    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: str | None = None
    last_heartbeat: datetime = Field(default_factory=_now)
    message: str | None = None


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class Artifact(AHSEABaseModel):
    """A concrete file or output produced by an agent."""

    artifact_id: str = Field(default_factory=lambda: _new_id("art"))
    artifact_type: ArtifactType
    path: str
    description: str | None = None
    produced_by_agent_id: str | None = None
    produced_by_task_id: str | None = None
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class APIContract(AHSEABaseModel):
    """Contract describing an HTTP/RPC API surface between subsystems."""

    endpoint: str
    method: str
    request_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None


class DatabaseContract(AHSEABaseModel):
    """Contract describing a shared database schema element."""

    table_name: str
    columns: dict[str, str] = Field(
        default_factory=dict, description="column_name -> type description"
    )
    description: str | None = None


class EnvironmentContract(AHSEABaseModel):
    """Contract describing a required environment variable or config value."""

    key: str
    required: bool = True
    description: str | None = None
    default_value: str | None = None


class Contract(AHSEABaseModel):
    """A contract between two or more subsystems/modules."""

    contract_id: str = Field(default_factory=lambda: _new_id("contract"))
    contract_type: ContractType
    name: str
    owning_module_id: str | None = None
    consuming_module_ids: list[str] = Field(default_factory=list)
    api: APIContract | None = None
    database: DatabaseContract | None = None
    environment: EnvironmentContract | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------


class TestResult(AHSEABaseModel):
    """Result of a single test case execution."""

    test_id: str = Field(default_factory=lambda: _new_id("test"))
    name: str
    outcome: TestOutcome
    module_id: str | None = None
    task_id: str | None = None
    duration_seconds: float | None = None
    message: str | None = None
    ran_at: datetime = Field(default_factory=_now)


class QAReport(AHSEABaseModel):
    """Aggregated quality-assurance report for a run."""

    report_id: str = Field(default_factory=lambda: _new_id("qa"))
    test_results: list[TestResult] = Field(default_factory=list)
    lint_passed: bool | None = None
    type_check_passed: bool | None = None
    coverage_percent: float | None = None
    summary: str | None = None
    generated_at: datetime = Field(default_factory=_now)

    @property
    def passed_count(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.PASSED)

    @property
    def failed_count(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.FAILED)

    @property
    def all_passed(self) -> bool:
        if not self.test_results:
            return False
        return all(t.outcome == TestOutcome.PASSED for t in self.test_results)


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


class DeploymentState(AHSEABaseModel):
    """Tracks the status of deployment for the project."""

    stage: DeploymentStage = DeploymentStage.NOT_STARTED
    environment: str = "staging"
    approved_by: str | None = None
    approved_at: datetime | None = None
    last_deployed_at: datetime | None = None
    deployment_log: list[str] = Field(default_factory=list)
    verification_passed: bool | None = None
    rollback_reason: str | None = None


# ---------------------------------------------------------------------------
# Errors & events
# ---------------------------------------------------------------------------


class ErrorRecord(AHSEABaseModel):
    """A recorded failure/error surfaced anywhere in the system."""

    error_id: str = Field(default_factory=lambda: _new_id("err"))
    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    source: str = Field(description="Component/agent that raised the error.")
    task_id: str | None = None
    agent_id: str | None = None
    message: str
    traceback: str | None = None
    resolved: bool = False
    resolution_summary: str | None = None
    created_at: datetime = Field(default_factory=_now)
    resolved_at: datetime | None = None


class AgentEvent(AHSEABaseModel):
    """An event emitted by a specific agent (for observability/tracing)."""

    event_id: str = Field(default_factory=lambda: _new_id("aevt"))
    agent_id: str
    level: EventLevel = EventLevel.INFO
    message: str
    task_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class ProjectEvent(AHSEABaseModel):
    """A project-wide event not tied to a single agent."""

    event_id: str = Field(default_factory=lambda: _new_id("pevt"))
    level: EventLevel = EventLevel.INFO
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Root state
# ---------------------------------------------------------------------------


class AHSEAState(AHSEABaseModel):
    """Root shared state object for a single AHSEA project run.

    This is the single object that gets threaded through the LangGraph
    orchestration graph. Agents should only ever be given the slices of
    this state that are relevant to them (see context hierarchy in
    `app.state.context`), never the raw object with full history.
    """

    project: ProjectMetadata
    requirements: list[Requirement] = Field(default_factory=list)
    architecture: ArchitectureState = Field(default_factory=ArchitectureState)

    agents: dict[str, AgentDefinition] = Field(default_factory=dict)
    agent_statuses: dict[str, AgentRuntimeStatus] = Field(default_factory=dict)

    tasks: dict[str, Task] = Field(default_factory=dict)
    task_dependencies: list[TaskDependency] = Field(default_factory=list)

    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    contracts: dict[str, Contract] = Field(default_factory=dict)

    qa_reports: list[QAReport] = Field(default_factory=list)
    deployment: DeploymentState = Field(default_factory=DeploymentState)

    errors: list[ErrorRecord] = Field(default_factory=list)
    agent_events: list[AgentEvent] = Field(default_factory=list)
    project_events: list[ProjectEvent] = Field(default_factory=list)

    shared_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form global context accessible to all agents (GLOBAL CONTEXT).",
    )
    team_context: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-team context, keyed by team name (TEAM CONTEXT).",
    )

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
