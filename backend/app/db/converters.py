"""Domain (Pydantic) <-> ORM converters (Phase 17).

Pure functions only -- no session/IO here. `app.db.persistence_service`
calls these to translate `app.state.models` (plus the small schema modules
in `app.llm`, `app.self_healing`, `app.deployment`) into ORM rows and back.

Enum fields are stored as their `.value` string and re-hydrated through the
corresponding domain enum's constructor, since ORM columns are plain
`String`, not database-level enum types (keeps SQLite and PostgreSQL
schemas identical and makes adding a new enum member a data-only change).
"""

from __future__ import annotations

from app.db.models import (
    AgentORM,
    ArchitectureDecisionORM,
    ArtifactORM,
    ContractORM,
    DeploymentRunORM,
    ErrorORM,
    EventORM,
    LLMRequestORM,
    ProjectORM,
    RepairAttemptORM,
    TaskORM,
    TestResultORM,
)
from app.llm.models import LLMTelemetryRecord, TaskType
from app.self_healing.schemas import ErrorDiagnosis, RepairAttempt, RepairOutcome
from app.state.enums import (
    AgentStatus,
    AgentType,
    ArtifactType,
    ContractType,
    DeploymentStage,
    ErrorSeverity,
    EventLevel,
    SystemAgentKind,
    TaskComplexity,
    TaskStatus,
    TestOutcome,
)
from app.state.models import (
    AgentDefinition,
    AgentEvent,
    AgentRuntimeStatus,
    APIContract,
    ArchitectureDecision,
    Artifact,
    Contract,
    DatabaseContract,
    DeploymentState,
    EnvironmentContract,
    ErrorRecord,
    ProjectEvent,
    ProjectMetadata,
    QAReport,
    Task,
    TestResult,
)

# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


def project_to_orm(project: ProjectMetadata) -> ProjectORM:
    return ProjectORM(
        project_id=project.project_id,
        name=project.name,
        description=project.description,
        idea_prompt=project.idea_prompt,
        workspace_path=project.workspace_path,
        repo_url=project.repo_url,
        default_branch=project.default_branch,
        tags=list(project.tags),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_from_orm(row: ProjectORM) -> ProjectMetadata:
    return ProjectMetadata(
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        idea_prompt=row.idea_prompt,
        workspace_path=row.workspace_path,
        repo_url=row.repo_url,
        default_branch=row.default_branch,
        tags=list(row.tags or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def agent_to_orm(
    project_id: str,
    agent: AgentDefinition,
    runtime_status: AgentRuntimeStatus | None = None,
) -> AgentORM:
    return AgentORM(
        agent_id=agent.agent_id,
        project_id=project_id,
        name=agent.name,
        agent_type=agent.agent_type.value,
        system_kind=agent.system_kind.value if agent.system_kind else None,
        parent_agent_id=agent.parent_agent_id,
        team_name=agent.team_name,
        role_description=agent.role_description,
        capabilities=list(agent.capabilities),
        allowed_tools=list(agent.allowed_tools),
        status=(runtime_status.status.value if runtime_status else AgentStatus.IDLE.value),
        current_task_id=(runtime_status.current_task_id if runtime_status else None),
        status_message=(runtime_status.message if runtime_status else None),
        last_heartbeat=(runtime_status.last_heartbeat if runtime_status else agent.created_at),
        created_at=agent.created_at,
    )


def agent_from_orm(row: AgentORM) -> tuple[AgentDefinition, AgentRuntimeStatus]:
    definition = AgentDefinition(
        agent_id=row.agent_id,
        name=row.name,
        agent_type=AgentType(row.agent_type),
        system_kind=SystemAgentKind(row.system_kind) if row.system_kind else None,
        parent_agent_id=row.parent_agent_id,
        team_name=row.team_name,
        role_description=row.role_description,
        capabilities=list(row.capabilities or []),
        allowed_tools=list(row.allowed_tools or []),
        created_at=row.created_at,
    )
    status = AgentRuntimeStatus(
        agent_id=row.agent_id,
        status=AgentStatus(row.status),
        current_task_id=row.current_task_id,
        last_heartbeat=row.last_heartbeat,
        message=row.status_message,
    )
    return definition, status


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def task_to_orm(project_id: str, task: Task) -> TaskORM:
    result = task.result
    return TaskORM(
        task_id=task.task_id,
        project_id=project_id,
        title=task.title,
        description=task.description,
        status=task.status.value,
        assigned_agent_id=task.assigned_agent_id,
        owner_manager=task.owner_manager,
        worker_type=task.worker_type,
        module_id=task.module_id,
        requirement_ids=list(task.requirement_ids),
        depends_on_task_ids=list(task.depends_on_task_ids),
        expected_outputs=list(task.expected_outputs),
        priority=task.priority,
        complexity=task.complexity.value,
        retries=task.retries,
        max_retries=task.max_retries,
        result_success=result.success if result else None,
        result_summary=result.summary if result else None,
        result_artifact_ids=list(result.artifact_ids) if result else [],
        result_logs=result.logs if result else None,
        result_error_message=result.error_message if result else None,
        result_duration_seconds=result.duration_seconds if result else None,
        result_completed_at=result.completed_at if result else None,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


def task_from_orm(row: TaskORM) -> Task:
    from app.state.models import TaskResult

    result = None
    if row.result_success is not None:
        result = TaskResult(
            task_id=row.task_id,
            success=row.result_success,
            summary=row.result_summary,
            artifact_ids=list(row.result_artifact_ids or []),
            logs=row.result_logs,
            error_message=row.result_error_message,
            duration_seconds=row.result_duration_seconds,
            completed_at=row.result_completed_at or row.updated_at,
        )
    return Task(
        task_id=row.task_id,
        title=row.title,
        description=row.description,
        status=TaskStatus(row.status),
        assigned_agent_id=row.assigned_agent_id,
        owner_manager=row.owner_manager,
        worker_type=row.worker_type,
        module_id=row.module_id,
        requirement_ids=list(row.requirement_ids or []),
        depends_on_task_ids=list(row.depends_on_task_ids or []),
        expected_outputs=list(row.expected_outputs or []),
        priority=row.priority,
        complexity=TaskComplexity(row.complexity),
        retries=row.retries,
        max_retries=row.max_retries,
        result=result,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def artifact_to_orm(project_id: str, artifact: Artifact) -> ArtifactORM:
    return ArtifactORM(
        artifact_id=artifact.artifact_id,
        project_id=project_id,
        artifact_type=artifact.artifact_type.value,
        path=artifact.path,
        description=artifact.description,
        produced_by_agent_id=artifact.produced_by_agent_id,
        produced_by_task_id=artifact.produced_by_task_id,
        content_hash=artifact.content_hash,
        created_at=artifact.created_at,
    )


def artifact_from_orm(row: ArtifactORM) -> Artifact:
    return Artifact(
        artifact_id=row.artifact_id,
        artifact_type=ArtifactType(row.artifact_type),
        path=row.path,
        description=row.description,
        produced_by_agent_id=row.produced_by_agent_id,
        produced_by_task_id=row.produced_by_task_id,
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def contract_to_orm(project_id: str, contract: Contract) -> ContractORM:
    return ContractORM(
        contract_id=contract.contract_id,
        project_id=project_id,
        contract_type=contract.contract_type.value,
        name=contract.name,
        owning_module_id=contract.owning_module_id,
        consuming_module_ids=list(contract.consuming_module_ids),
        api=contract.api.model_dump(mode="json") if contract.api else None,
        database=contract.database.model_dump(mode="json") if contract.database else None,
        environment=(
            contract.environment.model_dump(mode="json") if contract.environment else None
        ),
        version=contract.version,
        created_at=contract.created_at,
        updated_at=contract.updated_at,
    )


def contract_from_orm(row: ContractORM) -> Contract:
    return Contract(
        contract_id=row.contract_id,
        contract_type=ContractType(row.contract_type),
        name=row.name,
        owning_module_id=row.owning_module_id,
        consuming_module_ids=list(row.consuming_module_ids or []),
        api=APIContract.model_validate(row.api) if row.api else None,
        database=DatabaseContract.model_validate(row.database) if row.database else None,
        environment=(
            EnvironmentContract.model_validate(row.environment) if row.environment else None
        ),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def agent_event_to_orm(project_id: str, event: AgentEvent) -> EventORM:
    return EventORM(
        event_id=event.event_id,
        project_id=project_id,
        scope="agent",
        agent_id=event.agent_id,
        level=event.level.value,
        message=event.message,
        task_id=event.task_id,
        data=dict(event.data),
        created_at=event.created_at,
    )


def project_event_to_orm(project_id: str, event: ProjectEvent) -> EventORM:
    return EventORM(
        event_id=event.event_id,
        project_id=project_id,
        scope="project",
        agent_id=None,
        level=event.level.value,
        message=event.message,
        task_id=None,
        data=dict(event.data),
        created_at=event.created_at,
    )


def event_from_orm(row: EventORM) -> AgentEvent | ProjectEvent:
    if row.scope == "agent":
        return AgentEvent(
            event_id=row.event_id,
            agent_id=row.agent_id or "",
            level=EventLevel(row.level),
            message=row.message,
            task_id=row.task_id,
            data=dict(row.data or {}),
            created_at=row.created_at,
        )
    return ProjectEvent(
        event_id=row.event_id,
        level=EventLevel(row.level),
        message=row.message,
        data=dict(row.data or {}),
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def error_to_orm(project_id: str, error: ErrorRecord) -> ErrorORM:
    return ErrorORM(
        error_id=error.error_id,
        project_id=project_id,
        severity=error.severity.value,
        source=error.source,
        task_id=error.task_id,
        agent_id=error.agent_id,
        message=error.message,
        traceback=error.traceback,
        resolved=error.resolved,
        resolution_summary=error.resolution_summary,
        created_at=error.created_at,
        resolved_at=error.resolved_at,
    )


def error_from_orm(row: ErrorORM) -> ErrorRecord:
    return ErrorRecord(
        error_id=row.error_id,
        severity=ErrorSeverity(row.severity),
        source=row.source,
        task_id=row.task_id,
        agent_id=row.agent_id,
        message=row.message,
        traceback=row.traceback,
        resolved=row.resolved,
        resolution_summary=row.resolution_summary,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


# ---------------------------------------------------------------------------
# test_results / QAReport
# ---------------------------------------------------------------------------


def qa_report_to_orm_rows(project_id: str, report: QAReport) -> list[TestResultORM]:
    if not report.test_results:
        # A report with no individual test cases still needs to be
        # representable; store a single sentinel row carrying only the
        # report-level fields.
        return [
            TestResultORM(
                test_id=f"{report.report_id}_summary",
                project_id=project_id,
                report_id=report.report_id,
                name="__report_summary__",
                outcome=TestOutcome.SKIPPED.value,
                report_lint_passed=report.lint_passed,
                report_type_check_passed=report.type_check_passed,
                report_coverage_percent=report.coverage_percent,
                report_summary=report.summary,
                report_generated_at=report.generated_at,
                ran_at=report.generated_at,
            )
        ]
    return [
        TestResultORM(
            test_id=t.test_id,
            project_id=project_id,
            report_id=report.report_id,
            name=t.name,
            outcome=t.outcome.value,
            module_id=t.module_id,
            task_id=t.task_id,
            duration_seconds=t.duration_seconds,
            message=t.message,
            ran_at=t.ran_at,
            report_lint_passed=report.lint_passed,
            report_type_check_passed=report.type_check_passed,
            report_coverage_percent=report.coverage_percent,
            report_summary=report.summary,
            report_generated_at=report.generated_at,
        )
        for t in report.test_results
    ]


def qa_reports_from_orm_rows(rows: list[TestResultORM]) -> list[QAReport]:
    """Group flat `test_results` rows back into `QAReport`s by `report_id`."""

    by_report: dict[str, list[TestResultORM]] = {}
    for row in rows:
        by_report.setdefault(row.report_id, []).append(row)

    reports: list[QAReport] = []
    for report_id, report_rows in by_report.items():
        first = report_rows[0]
        test_results = [
            TestResult(
                test_id=r.test_id,
                name=r.name,
                outcome=TestOutcome(r.outcome),
                module_id=r.module_id,
                task_id=r.task_id,
                duration_seconds=r.duration_seconds,
                message=r.message,
                ran_at=r.ran_at,
            )
            for r in report_rows
            if r.name != "__report_summary__"
        ]
        reports.append(
            QAReport(
                report_id=report_id,
                test_results=test_results,
                lint_passed=first.report_lint_passed,
                type_check_passed=first.report_type_check_passed,
                coverage_percent=first.report_coverage_percent,
                summary=first.report_summary,
                generated_at=first.report_generated_at,
            )
        )
    return reports


# ---------------------------------------------------------------------------
# deployment_runs
# ---------------------------------------------------------------------------


def deployment_state_to_orm(
    project_id: str, run_id: str, deployment: DeploymentState
) -> DeploymentRunORM:
    return DeploymentRunORM(
        run_id=run_id,
        project_id=project_id,
        stage=deployment.stage.value,
        environment=deployment.environment,
        approved_by=deployment.approved_by,
        approved_at=deployment.approved_at,
        last_deployed_at=deployment.last_deployed_at,
        verification_passed=deployment.verification_passed,
        rollback_reason=deployment.rollback_reason,
        deployment_log=list(deployment.deployment_log),
    )


def deployment_state_from_orm(row: DeploymentRunORM) -> DeploymentState:
    return DeploymentState(
        stage=DeploymentStage(row.stage),
        environment=row.environment,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        last_deployed_at=row.last_deployed_at,
        deployment_log=list(row.deployment_log or []),
        verification_passed=row.verification_passed,
        rollback_reason=row.rollback_reason,
    )


# ---------------------------------------------------------------------------
# architecture_decisions
# ---------------------------------------------------------------------------


def architecture_decision_to_orm(
    project_id: str, decision: ArchitectureDecision
) -> ArchitectureDecisionORM:
    return ArchitectureDecisionORM(
        decision_id=decision.decision_id,
        project_id=project_id,
        title=decision.title,
        context=decision.context,
        decision=decision.decision,
        consequences=decision.consequences,
        alternatives_considered=list(decision.alternatives_considered),
        created_at=decision.created_at,
    )


def architecture_decision_from_orm(row: ArchitectureDecisionORM) -> ArchitectureDecision:
    return ArchitectureDecision(
        decision_id=row.decision_id,
        title=row.title,
        context=row.context,
        decision=row.decision,
        consequences=row.consequences,
        alternatives_considered=list(row.alternatives_considered or []),
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# repair_attempts
# ---------------------------------------------------------------------------


def repair_attempt_to_orm(project_id: str, attempt: RepairAttempt) -> RepairAttemptORM:
    diagnosis = attempt.diagnosis
    return RepairAttemptORM(
        attempt_id=attempt.attempt_id,
        project_id=project_id,
        task_id=attempt.task_id,
        error_id=attempt.error_id,
        attempt_number=attempt.attempt_number,
        diagnosis_root_cause=diagnosis.root_cause if diagnosis else None,
        diagnosis_classification=diagnosis.classification if diagnosis else None,
        diagnosis_responsible_team=diagnosis.responsible_team if diagnosis else None,
        diagnosis_proposed_solution=diagnosis.proposed_solution if diagnosis else None,
        diagnosis_confidence=diagnosis.confidence if diagnosis else None,
        rework_task_id=attempt.rework_task_id,
        outcome=attempt.outcome.value if attempt.outcome else None,
        detail=attempt.detail,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
    )


def repair_attempt_from_orm(row: RepairAttemptORM) -> RepairAttempt:
    diagnosis = None
    if row.diagnosis_root_cause is not None:
        diagnosis = ErrorDiagnosis(
            root_cause=row.diagnosis_root_cause,
            classification=row.diagnosis_classification or "",
            responsible_team=row.diagnosis_responsible_team or "",
            proposed_solution=row.diagnosis_proposed_solution or "",
            confidence=row.diagnosis_confidence if row.diagnosis_confidence is not None else 0.5,
        )
    return RepairAttempt(
        attempt_id=row.attempt_id,
        task_id=row.task_id,
        error_id=row.error_id,
        attempt_number=row.attempt_number,
        diagnosis=diagnosis,
        rework_task_id=row.rework_task_id,
        outcome=RepairOutcome(row.outcome) if row.outcome else None,
        detail=row.detail,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


# ---------------------------------------------------------------------------
# llm_requests
# ---------------------------------------------------------------------------


def llm_telemetry_to_orm(
    record: LLMTelemetryRecord,
    *,
    prompt_excerpt: str | None = None,
) -> LLMRequestORM:
    return LLMRequestORM(
        request_id=record.request_id,
        project_id=record.project_id,
        agent_id=record.agent_id,
        task_id=record.task_id,
        task_type=record.task_type.value,
        model=record.selected_model,
        duration=record.duration,
        status="success" if record.success else "failed",
        error_message=record.error_message,
        prompt_excerpt=prompt_excerpt,
    )


def llm_request_from_orm(row: LLMRequestORM) -> LLMTelemetryRecord:
    return LLMTelemetryRecord(
        request_id=row.request_id,
        project_id=row.project_id,
        agent_id=row.agent_id,
        task_id=row.task_id,
        task_type=TaskType(row.task_type),
        selected_model=row.model,
        start_time=0.0,
        end_time=row.duration,
        duration=row.duration,
        success=row.status == "success",
        error_message=row.error_message,
    )
