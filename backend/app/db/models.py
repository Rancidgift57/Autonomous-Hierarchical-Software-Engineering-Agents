"""ORM models for the AHSEA persistence layer (Phase 17).

One table per row of the Phase 17 spec: projects, agents, tasks,
artifacts, contracts, events, errors, test_results, deployment_runs,
architecture_decisions, repair_attempts, llm_requests.

Primary keys reuse the same prefixed string ids the domain layer already
generates (`app.state.models._new_id`, e.g. ``"proj_ab12cd34ef56"``) rather
than introducing a second, database-only id scheme -- this keeps
domain <-> ORM conversion (`app.db.converters`) a straightforward 1:1
mapping instead of a lookup table.

Nested/list-valued domain fields (e.g. `Task.depends_on_task_ids`,
`ProjectMetadata.tags`) are stored as JSON columns rather than being
normalized into extra join tables -- they are read/written as a whole by
the domain layer, never queried by their inner elements, so normalizing
them would add join complexity without a corresponding query benefit.

`llm_requests` deliberately has NO column for prompt/response text.
`PersistenceService.record_llm_request` will only ever populate
`prompt_excerpt` when `DatabaseSettings.persist_llm_prompts` is true AND
the caller explicitly supplies one -- see that module's docstring.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow

# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


class ProjectORM(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    idea_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    repo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(200), default="main", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )

    agents: Mapped[list[AgentORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[TaskORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[ArtifactORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    contracts: Mapped[list[ContractORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    events: Mapped[list[EventORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    errors: Mapped[list[ErrorORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    test_results: Mapped[list[TestResultORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    deployment_runs: Mapped[list[DeploymentRunORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    architecture_decisions: Mapped[list[ArchitectureDecisionORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    repair_attempts: Mapped[list[RepairAttemptORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    llm_requests: Mapped[list[LLMRequestORM]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    memories: Mapped[list[MemoryORM]] = relationship(back_populates="project", cascade="all, delete-orphan")
    observability_events: Mapped[list[ObservabilityEventORM]] = relationship(back_populates="project", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class AgentORM(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    system_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    team_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Live runtime status, denormalized from AgentRuntimeStatus (one row per
    # agent -- the domain layer only ever tracks the *latest* status).
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    project: Mapped[ProjectORM] = relationship(back_populates="agents")


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


class TaskORM(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    assigned_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_manager: Mapped[str | None] = mapped_column(String(200), nullable=True)
    worker_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    module_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requirement_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    depends_on_task_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_outputs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    complexity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # TaskResult, denormalized onto the task row (a task has at most one
    # "current" result in the domain model).
    result_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_artifact_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    result_logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    project: Mapped[ProjectORM] = relationship(back_populates="tasks")

    __table_args__ = (Index("ix_tasks_project_status", "project_id", "status"),)


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


class ArtifactORM(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    produced_by_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    produced_by_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    project: Mapped[ProjectORM] = relationship(back_populates="artifacts")


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


class ContractORM(Base):
    __tablename__ = "contracts"

    contract_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owning_module_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consuming_module_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Sub-contract payloads (APIContract / DatabaseContract / EnvironmentContract),
    # stored as JSON since exactly one is populated depending on contract_type
    # and none of them are queried by their inner fields.
    api: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    database: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    environment: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped[ProjectORM] = relationship(back_populates="contracts")


# ---------------------------------------------------------------------------
# events (unifies AgentEvent + ProjectEvent behind one `scope` column)
# ---------------------------------------------------------------------------


class EventORM(Base):
    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, doc="'agent' or 'project'")
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)

    project: Mapped[ProjectORM] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ErrorORM(Base):
    __tablename__ = "errors"

    error_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    project: Mapped[ProjectORM] = relationship(back_populates="errors")


# ---------------------------------------------------------------------------
# test_results (QAReport is reconstructed by grouping rows by report_id)
# ---------------------------------------------------------------------------


class TestResultORM(Base):
    __tablename__ = "test_results"

    test_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    module_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    # Denormalized report-level fields (same for every row sharing a
    # report_id) so a QAReport can be rebuilt without a second table.
    report_lint_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    report_type_check_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    report_coverage_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_generated_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    project: Mapped[ProjectORM] = relationship(back_populates="test_results")


# ---------------------------------------------------------------------------
# deployment_runs (append-only history; latest row per project == current
# DeploymentState)
# ---------------------------------------------------------------------------


class DeploymentRunORM(Base):
    __tablename__ = "deployment_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), default="staging", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_deployed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    verification_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployment_log: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)

    project: Mapped[ProjectORM] = relationship(back_populates="deployment_runs")


# ---------------------------------------------------------------------------
# architecture_decisions
# ---------------------------------------------------------------------------


class ArchitectureDecisionORM(Base):
    __tablename__ = "architecture_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    consequences: Mapped[str | None] = mapped_column(Text, nullable=True)
    alternatives_considered: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    project: Mapped[ProjectORM] = relationship(back_populates="architecture_decisions")


# ---------------------------------------------------------------------------
# repair_attempts
# ---------------------------------------------------------------------------


class RepairAttemptORM(Base):
    __tablename__ = "repair_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    error_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # ErrorDiagnosis, flattened (never file contents -- see app.self_healing.schemas).
    diagnosis_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnosis_responsible_team: Mapped[str | None] = mapped_column(String(200), nullable=True)
    diagnosis_proposed_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    rework_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    project: Mapped[ProjectORM] = relationship(back_populates="repair_attempts")


# ---------------------------------------------------------------------------
# llm_requests -- metadata only, NEVER prompt/response text by default
# ---------------------------------------------------------------------------


class LLMRequestORM(Base):
    __tablename__ = "llm_requests"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, doc="'success' or 'failed'")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Only ever populated when `DatabaseSettings.persist_llm_prompts` is
    #: true AND the caller explicitly passed a prompt into
    #: `PersistenceService.record_llm_request`. Never auto-captured.
    prompt_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)

    project: Mapped[ProjectORM | None] = relationship(back_populates="llm_requests")


# ---------------------------------------------------------------------------
# Phase 21/22: curated memory and safe observability events
# ---------------------------------------------------------------------------


class MemoryORM(Base):
    __tablename__ = "memories"

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    project: Mapped[ProjectORM] = relationship(back_populates="memories")


class ObservabilityEventORM(Base):
    __tablename__ = "observability_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    task_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)

    project: Mapped[ProjectORM | None] = relationship(back_populates="observability_events")


__all__ = [
    "AgentORM",
    "ArchitectureDecisionORM",
    "ArtifactORM",
    "Base",
    "ContractORM",
    "DeploymentRunORM",
    "ErrorORM",
    "EventORM",
    "LLMRequestORM",
    "MemoryORM",
    "ObservabilityEventORM",
    "ProjectORM",
    "RepairAttemptORM",
    "TaskORM",
    "TestResultORM",
]
