"""Tests for app.db.persistence_service.PersistenceService (Phase 17).

The important property under test: `save_state` -> `load_state` round-trips
an `AHSEAState` without losing/corrupting data, for every table in the
Phase 17 spec. Also covers the LLM-prompt safety switch and repair-attempt
recording, since those bypass `save_state`.
"""

from __future__ import annotations

import pytest

from app.db.config import DatabaseSettings
from app.db.persistence_service import PersistenceService
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
    TaskStatus,
    TestOutcome,
)
from app.state.models import (
    AgentDefinition,
    AgentEvent,
    AgentRuntimeStatus,
    AHSEAState,
    ArchitectureDecision,
    Artifact,
    Contract,
    EnvironmentContract,
    ErrorRecord,
    ProjectEvent,
    ProjectMetadata,
    QAReport,
    Task,
    TestResult,
)


def _build_full_state() -> AHSEAState:
    project = ProjectMetadata(
        project_id="proj_roundtrip",
        name="Roundtrip Co",
        description="A project used to test persistence round-tripping.",
        idea_prompt="Build a todo app.",
        tags=["demo", "phase17"],
    )
    state = AHSEAState(project=project)

    agent = AgentDefinition(
        agent_id="agent_cto",
        name="CTO",
        agent_type=AgentType.CTO,
        role_description="Owns architecture.",
        capabilities=["planning"],
        allowed_tools=["read_file"],
    )
    state.agents[agent.agent_id] = agent
    state.agent_statuses[agent.agent_id] = AgentRuntimeStatus(
        agent_id=agent.agent_id, status=AgentStatus.WORKING, message="on it"
    )

    task = Task(
        task_id="task_1",
        title="Design schema",
        description="Design the initial database schema.",
        status=TaskStatus.RUNNING,
        assigned_agent_id=agent.agent_id,
        depends_on_task_ids=[],
        priority=5,
    )
    state.tasks[task.task_id] = task

    artifact = Artifact(
        artifact_id="art_1",
        artifact_type=ArtifactType.SOURCE_FILE,
        path="backend/app/main.py",
        description="Entry point.",
        produced_by_task_id=task.task_id,
    )
    state.artifacts[artifact.artifact_id] = artifact

    contract = Contract(
        contract_id="contract_1",
        contract_type=ContractType.ENVIRONMENT,
        name="DATABASE_URL",
        environment=EnvironmentContract(
            key="DATABASE_URL", required=True, description="Postgres DSN"
        ),
    )
    state.contracts[contract.contract_id] = contract

    state.errors.append(
        ErrorRecord(
            error_id="err_1",
            severity=ErrorSeverity.HIGH,
            source="worker_backend",
            task_id=task.task_id,
            message="Migration failed.",
        )
    )

    state.agent_events.append(
        AgentEvent(
            event_id="aevt_1",
            agent_id=agent.agent_id,
            level=EventLevel.INFO,
            message="Started task.",
            task_id=task.task_id,
        )
    )
    state.project_events.append(
        ProjectEvent(event_id="pevt_1", level=EventLevel.WARNING, message="Retry scheduled.")
    )

    state.qa_reports.append(
        QAReport(
            report_id="qa_1",
            test_results=[
                TestResult(test_id="test_1", name="test_health", outcome=TestOutcome.PASSED),
                TestResult(test_id="test_2", name="test_users", outcome=TestOutcome.FAILED),
            ],
            lint_passed=True,
            type_check_passed=False,
            coverage_percent=83.5,
            summary="2 tests, 1 failed.",
        )
    )

    state.architecture.decisions.append(
        ArchitectureDecision(
            decision_id="adr_1",
            title="Use PostgreSQL",
            context="Need durable relational storage.",
            decision="Use PostgreSQL in production, SQLite for dev.",
            alternatives_considered=["MongoDB", "DynamoDB"],
        )
    )

    state.deployment.stage = DeploymentStage.VERIFYING
    state.deployment.environment = "staging"
    state.deployment.deployment_log.append("Deployed to staging.")

    return state


@pytest.mark.asyncio
async def test_save_and_load_state_round_trips_every_table(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    original = _build_full_state()

    await service.save_state(original)
    loaded = await service.load_state(original.project.project_id)

    assert loaded is not None
    assert loaded.project.project_id == original.project.project_id
    assert loaded.project.name == original.project.name
    assert loaded.project.tags == original.project.tags

    assert set(loaded.agents) == {"agent_cto"}
    assert loaded.agents["agent_cto"].agent_type == AgentType.CTO
    assert loaded.agent_statuses["agent_cto"].status == AgentStatus.WORKING

    assert set(loaded.tasks) == {"task_1"}
    assert loaded.tasks["task_1"].status == TaskStatus.RUNNING
    assert loaded.tasks["task_1"].priority == 5

    assert set(loaded.artifacts) == {"art_1"}
    assert loaded.artifacts["art_1"].path == "backend/app/main.py"

    assert set(loaded.contracts) == {"contract_1"}
    assert loaded.contracts["contract_1"].environment.key == "DATABASE_URL"

    assert len(loaded.errors) == 1
    assert loaded.errors[0].severity == ErrorSeverity.HIGH

    assert len(loaded.agent_events) == 1
    assert loaded.agent_events[0].agent_id == "agent_cto"
    assert len(loaded.project_events) == 1

    assert len(loaded.qa_reports) == 1
    assert len(loaded.qa_reports[0].test_results) == 2
    assert loaded.qa_reports[0].coverage_percent == 83.5

    assert len(loaded.architecture.decisions) == 1
    assert loaded.architecture.decisions[0].title == "Use PostgreSQL"

    assert loaded.deployment.stage == DeploymentStage.VERIFYING


@pytest.mark.asyncio
async def test_load_state_returns_none_for_unknown_project(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    assert await service.load_state("proj_does_not_exist") is None


@pytest.mark.asyncio
async def test_save_state_is_idempotent(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    state = _build_full_state()

    await service.save_state(state)
    state.tasks["task_1"].status = TaskStatus.COMPLETED
    await service.save_state(state)

    loaded = await service.load_state(state.project.project_id)
    assert loaded.tasks["task_1"].status == TaskStatus.COMPLETED
    # Re-saving shouldn't duplicate the agent/task/artifact rows.
    assert len(loaded.tasks) == 1
    assert len(loaded.artifacts) == 1


@pytest.mark.asyncio
async def test_deployment_runs_append_only_history(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    state = _build_full_state()

    await service.save_state(state)
    state.deployment.stage = DeploymentStage.DEPLOYED
    await service.save_state(state)
    # Saving again with no stage change shouldn't add a third row.
    await service.save_state(state)

    from app.db.repositories import DeploymentRunRepository
    from app.db.session import session_scope

    async with session_scope(db_settings) as session:
        runs = await DeploymentRunRepository(session).list_by_project(state.project.project_id)

    assert [r.stage for r in runs] == ["verifying", "deployed"]


@pytest.mark.asyncio
async def test_list_project_ids_and_delete_project(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    state = _build_full_state()
    await service.save_state(state)

    assert await service.list_project_ids() == [state.project.project_id]

    assert await service.delete_project(state.project.project_id) is True
    assert await service.list_project_ids() == []
    assert await service.load_state(state.project.project_id) is None


@pytest.mark.asyncio
async def test_record_llm_request_omits_prompt_by_default(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    project = ProjectMetadata(
        project_id="proj_llm_default", name="P", description="d", idea_prompt="idea"
    )
    await service.save_state(AHSEAState(project=project))

    record = LLMTelemetryRecord(
        request_id="req_1",
        project_id="proj_llm_default",
        task_type=TaskType.CODING,
        selected_model="qwen2.5-coder:7b",
        start_time=0.0,
        end_time=1.5,
        duration=1.5,
        success=True,
    )

    await service.record_llm_request(record, prompt="SELECT * FROM secrets;")

    stored = await service.list_llm_requests("proj_llm_default")
    assert len(stored) == 1
    assert stored[0].selected_model == "qwen2.5-coder:7b"
    assert stored[0].duration == 1.5

    from app.db.repositories import LLMRequestRepository
    from app.db.session import session_scope

    async with session_scope(db_settings) as session:
        rows = await LLMRequestRepository(session).list_by_project("proj_llm_default")
    # Prompt is discarded because persist_llm_prompts defaults to False.
    assert rows[0].prompt_excerpt is None


@pytest.mark.asyncio
async def test_record_llm_request_respects_persist_prompts_flag(
    persist_prompts_settings: DatabaseSettings,
):
    service = PersistenceService(persist_prompts_settings)
    project = ProjectMetadata(project_id="proj_llm", name="P", description="d", idea_prompt="idea")
    state = AHSEAState(project=project)
    await service.save_state(state)

    record = LLMTelemetryRecord(
        request_id="req_2",
        project_id="proj_llm",
        task_type=TaskType.CODING,
        selected_model="qwen2.5-coder:7b",
        start_time=0.0,
        end_time=0.4,
        duration=0.4,
        success=False,
        error_message="timeout",
    )
    await service.record_llm_request(record, prompt="do not leak me")

    stored = await service.list_llm_requests("proj_llm")
    assert len(stored) == 1
    assert stored[0].success is False
    assert stored[0].error_message == "timeout"

    from app.db.repositories import LLMRequestRepository
    from app.db.session import session_scope

    async with session_scope(persist_prompts_settings) as session:
        rows = await LLMRequestRepository(session).list_by_project("proj_llm")
    assert rows[0].prompt_excerpt == "do not leak me"


@pytest.mark.asyncio
async def test_record_and_list_repair_attempts(db_settings: DatabaseSettings):
    service = PersistenceService(db_settings)
    project = ProjectMetadata(
        project_id="proj_repair", name="P", description="d", idea_prompt="idea"
    )
    await service.save_state(AHSEAState(project=project))

    attempt = RepairAttempt(
        attempt_id="repair_1",
        task_id="task_1",
        error_id="err_1",
        attempt_number=1,
        diagnosis=ErrorDiagnosis(
            root_cause="Null pointer in handler.",
            classification="logic_error",
            responsible_team="backend",
            proposed_solution="Add a null check.",
            confidence=0.8,
        ),
        outcome=RepairOutcome.SUCCESS,
        detail="Fixed on first attempt.",
    )
    await service.record_repair_attempt("proj_repair", attempt)

    attempts = await service.list_repair_attempts("proj_repair")
    assert len(attempts) == 1
    assert attempts[0].diagnosis.classification == "logic_error"
    assert attempts[0].outcome == RepairOutcome.SUCCESS
