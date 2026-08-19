"""`PersistenceService` -- the Service layer of Phase 17's

    API  ->  Service  ->  Repository  ->  Database

This is the *only* class `app.api.services.project_service.ProjectService`
(or any other caller) should depend on for durability. It never leaks a
`Session` or an ORM row -- every public method takes and returns plain
domain objects (`app.state.models`, `app.llm.models.LLMTelemetryRecord`,
`app.self_healing.schemas.RepairAttempt`).

Each public method opens exactly one `session_scope()` (one transaction).
Callers that need several of these to happen atomically should compose at
a higher level rather than this service exposing a leaky transaction
object.

Sensitive-data policy: `record_llm_request` never stores prompt/response
text unless `DatabaseSettings.persist_llm_prompts` is explicitly enabled
*and* the caller passes `prompt` in. Nothing else in this module ever
touches prompt/response content or secrets -- `AHSEAState` and the other
domain objects it persists don't carry any.
"""

from __future__ import annotations

from app.db import converters
from app.db.config import DatabaseSettings, get_database_settings
from app.db.repositories import (
    AgentRepository,
    ArchitectureDecisionRepository,
    ArtifactRepository,
    ContractRepository,
    DeploymentRunRepository,
    ErrorRepository,
    EventRepository,
    LLMRequestRepository,
    ProjectRepository,
    RepairAttemptRepository,
    TaskRepository,
    TestResultRepository,
)
from app.db.session import session_scope
from app.llm.models import LLMTelemetryRecord
from app.self_healing.schemas import RepairAttempt
from app.state.models import (
    AgentEvent,
    AHSEAState,
    ArchitectureDecision,
    ProjectEvent,
)


class PersistenceService:
    """Durable storage for `AHSEAState` and the auxiliary records
    (LLM telemetry, repair attempts) that accumulate around a run."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = settings or get_database_settings()

    # ------------------------------------------------------------------
    # Whole-state snapshot: save / load
    # ------------------------------------------------------------------

    async def save_state(self, state: AHSEAState) -> None:
        """Upsert every part of `state` into the database.

        Safe to call repeatedly (e.g. after every orchestration step, or
        once at the end of a run) -- every child entity is keyed by its own
        domain id, so re-saving only updates rows that actually changed.
        """

        async with session_scope(self.settings) as session:
            projects = ProjectRepository(session)
            await projects.upsert(converters.project_to_orm(state.project))

            agents = AgentRepository(session)
            for agent_id, agent in state.agents.items():
                runtime_status = state.agent_statuses.get(agent_id)
                await agents.upsert(
                    converters.agent_to_orm(state.project.project_id, agent, runtime_status)
                )

            tasks = TaskRepository(session)
            for task in state.tasks.values():
                await tasks.upsert(converters.task_to_orm(state.project.project_id, task))

            artifacts = ArtifactRepository(session)
            for artifact in state.artifacts.values():
                await artifacts.upsert(
                    converters.artifact_to_orm(state.project.project_id, artifact)
                )

            contracts = ContractRepository(session)
            for contract in state.contracts.values():
                await contracts.upsert(
                    converters.contract_to_orm(state.project.project_id, contract)
                )

            errors = ErrorRepository(session)
            for error in state.errors:
                await errors.upsert(converters.error_to_orm(state.project.project_id, error))

            events = EventRepository(session)
            for agent_event in state.agent_events:
                await events.upsert(
                    converters.agent_event_to_orm(state.project.project_id, agent_event)
                )
            for project_event in state.project_events:
                await events.upsert(
                    converters.project_event_to_orm(state.project.project_id, project_event)
                )

            test_results = TestResultRepository(session)
            for report in state.qa_reports:
                for row in converters.qa_report_to_orm_rows(state.project.project_id, report):
                    await test_results.upsert(row)

            architecture = ArchitectureDecisionRepository(session)
            for decision in state.architecture.decisions:
                await architecture.upsert(
                    converters.architecture_decision_to_orm(state.project.project_id, decision)
                )

            # deployment_runs is append-only history; only add a new row if
            # the deployment state actually changed since the last save.
            deployments = DeploymentRunRepository(session)
            latest = await deployments.latest_for_project(state.project.project_id)
            if latest is None or latest.stage != state.deployment.stage.value:
                existing_runs = await deployments.list_by_project(state.project.project_id)
                run_id = f"deployrun_{state.project.project_id}_{len(existing_runs) + 1}"
                await deployments.add(
                    converters.deployment_state_to_orm(
                        state.project.project_id, run_id, state.deployment
                    )
                )

    async def load_state(self, project_id: str) -> AHSEAState | None:
        """Rebuild an `AHSEAState` from the database, or `None` if the
        project doesn't exist. Used to resume a project across a process
        restart."""

        async with session_scope(self.settings) as session:
            projects = ProjectRepository(session)
            project_row = await projects.get(project_id)
            if project_row is None:
                return None

            state = AHSEAState(project=converters.project_from_orm(project_row))

            agents = AgentRepository(session)
            for row in await agents.list_by_project(project_id):
                definition, status = converters.agent_from_orm(row)
                state.agents[definition.agent_id] = definition
                state.agent_statuses[definition.agent_id] = status

            tasks = TaskRepository(session)
            for row in await tasks.list_by_project(project_id):
                task = converters.task_from_orm(row)
                state.tasks[task.task_id] = task

            artifacts = ArtifactRepository(session)
            for row in await artifacts.list_by_project(project_id):
                artifact = converters.artifact_from_orm(row)
                state.artifacts[artifact.artifact_id] = artifact

            contracts = ContractRepository(session)
            for row in await contracts.list_by_project(project_id):
                contract = converters.contract_from_orm(row)
                state.contracts[contract.contract_id] = contract

            errors = ErrorRepository(session)
            state.errors = [
                converters.error_from_orm(row) for row in await errors.list_by_project(project_id)
            ]

            events = EventRepository(session)
            agent_events: list[AgentEvent] = []
            project_events: list[ProjectEvent] = []
            for row in await events.list_by_project(project_id):
                event = converters.event_from_orm(row)
                if isinstance(event, AgentEvent):
                    agent_events.append(event)
                else:
                    project_events.append(event)
            state.agent_events = agent_events
            state.project_events = project_events

            test_results = TestResultRepository(session)
            state.qa_reports = converters.qa_reports_from_orm_rows(
                await test_results.list_by_project(project_id)
            )

            architecture = ArchitectureDecisionRepository(session)
            state.architecture.decisions = [
                converters.architecture_decision_from_orm(row)
                for row in await architecture.list_by_project(project_id)
            ]

            deployments = DeploymentRunRepository(session)
            latest_run = await deployments.latest_for_project(project_id)
            if latest_run is not None:
                state.deployment = converters.deployment_state_from_orm(latest_run)

            return state

    async def list_project_ids(self) -> list[str]:
        async with session_scope(self.settings) as session:
            return await ProjectRepository(session).list_ids()

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project and everything that cascades from it."""

        async with session_scope(self.settings) as session:
            return await ProjectRepository(session).delete(project_id)

    # ------------------------------------------------------------------
    # Fine-grained, single-record writes (used outside a full save_state,
    # e.g. streamed straight out of the LLM gateway / self-healing engine)
    # ------------------------------------------------------------------

    async def record_llm_request(
        self,
        record: LLMTelemetryRecord,
        *,
        prompt: str | None = None,
    ) -> None:
        """Persist LLM call metadata: model, task_type, duration, status.

        `prompt` is accepted but discarded unless
        `DatabaseSettings.persist_llm_prompts` is explicitly true --
        matching the Phase 17 requirement to never persist sensitive
        prompts/secrets unless explicitly configured.
        """

        should_store = prompt is not None and self.settings.persist_llm_prompts
        prompt_excerpt = prompt if should_store else None
        async with session_scope(self.settings) as session:
            repo = LLMRequestRepository(session)
            await repo.upsert(
                converters.llm_telemetry_to_orm(record, prompt_excerpt=prompt_excerpt)
            )

    async def list_llm_requests(self, project_id: str) -> list[LLMTelemetryRecord]:
        async with session_scope(self.settings) as session:
            rows = await LLMRequestRepository(session).list_by_project(project_id)
            return [converters.llm_request_from_orm(row) for row in rows]

    async def record_repair_attempt(self, project_id: str, attempt: RepairAttempt) -> None:
        async with session_scope(self.settings) as session:
            repo = RepairAttemptRepository(session)
            await repo.upsert(converters.repair_attempt_to_orm(project_id, attempt))

    async def list_repair_attempts(self, project_id: str) -> list[RepairAttempt]:
        async with session_scope(self.settings) as session:
            rows = await RepairAttemptRepository(session).list_by_project(project_id)
            return [converters.repair_attempt_from_orm(row) for row in rows]

    async def record_architecture_decision(
        self, project_id: str, decision: ArchitectureDecision
    ) -> None:
        async with session_scope(self.settings) as session:
            repo = ArchitectureDecisionRepository(session)
            await repo.upsert(converters.architecture_decision_to_orm(project_id, decision))
