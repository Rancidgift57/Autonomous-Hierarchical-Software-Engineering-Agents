"""Service layer for the FastAPI control plane (Phase 16).

This is where "agent logic" actually lives for the API: routers
(`app.api.routers.projects`) only ever parse a request, call one method
here, and shape the response. Every state mutation goes through
`app.state.operations`; every long-running run goes through
`app.orchestration.project_runner.ProjectOrchestrator` -- this module
wires the two together and owns the in-memory project store.

`ProjectStore` is intentionally a plain in-memory dict: this is a
reference implementation of the control-plane *architecture*
(router -> service -> orchestration), not a production datastore. Swapping
in a real database only means changing `ProjectStore`'s internals -- the
service's public methods, and everything above them, stay the same.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.orchestration.project_runner import (
    DefaultProjectOrchestrator,
    ProjectOrchestrator,
    ProjectRunControl,
    ProjectRunStatus,
)
from app.realtime.emitter import RealtimeEmitter
from app.realtime.manager import ConnectionManager
from app.state.enums import TaskStatus
from app.state.models import AHSEAState, ProjectMetadata
from app.state.operations import record_deployment_approval

if TYPE_CHECKING:
    from app.db.persistence_service import PersistenceService

logger = logging.getLogger("ahsea.api.project_service")


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: str):
        super().__init__(f"Project '{project_id}' not found.")
        self.project_id = project_id


class InvalidProjectStateError(Exception):
    """Raised for a lifecycle transition that doesn't make sense right now
    (e.g. pausing a project that was never started)."""


OrchestratorFactory = Callable[[AHSEAState], ProjectOrchestrator]


@dataclass
class ProjectRecord:
    state: AHSEAState
    control: ProjectRunControl = field(default_factory=ProjectRunControl)
    workspace_root: Path | None = None


class ProjectStore:
    """In-memory `project_id -> ProjectRecord` map."""

    def __init__(self) -> None:
        self._projects: dict[str, ProjectRecord] = {}

    def add(self, record: ProjectRecord) -> None:
        self._projects[record.state.project.project_id] = record

    def get(self, project_id: str) -> ProjectRecord:
        record = self._projects.get(project_id)
        if record is None:
            raise ProjectNotFoundError(project_id)
        return record

    def list(self) -> list[ProjectRecord]:
        return sorted(self._projects.values(), key=lambda r: r.state.project.created_at)


def default_orchestrator_factory(
    base_workspace_dir: str | Path | None = None,
    persistence: PersistenceService | None = None,
    realtime_manager: ConnectionManager | None = None,
) -> OrchestratorFactory:
    """Builds a `DefaultProjectOrchestrator` per project, each in its own
    workspace directory under `base_workspace_dir` (a fresh temp dir if
    not given).

    When `persistence` is given, every LLM call the resulting gateway
    makes also has its (prompt-free) telemetry persisted via
    `PersistenceService.record_llm_request` -- see `LLMGateway`'s
    `telemetry_sink` hook.

    When `realtime_manager` is given (Phase 19), the orchestrator is
    handed a `RealtimeEmitter` bound to that project's id, so its run
    broadcasts `project_started`/`agent_started`/`agent_completed`/
    `agent_tool_call`/`task_started`/`task_completed`/`task_failed`
    events to any `/ws/projects/{project_id}` subscribers as it goes.
    """

    base_dir = (
        Path(base_workspace_dir)
        if base_workspace_dir
        else Path(tempfile.mkdtemp(prefix="ahsea-runs-"))
    )
    base_dir.mkdir(parents=True, exist_ok=True)

    def factory(state: AHSEAState) -> ProjectOrchestrator:
        from app.llm.config import get_settings
        from app.llm.gateway import LLMGateway
        from app.llm.provider import OllamaProvider

        workspace_root = base_dir / state.project.project_id
        workspace_root.mkdir(parents=True, exist_ok=True)
        settings = get_settings()

        telemetry_sink = None
        if persistence is not None:

            async def telemetry_sink(record):  # noqa: ANN001 - LLMTelemetryRecord
                await persistence.record_llm_request(record)

        async def observability_sink(record):  # noqa: ANN001 - LLMTelemetryRecord
            from app.observability.service import ObservabilityService, TraceContext
            await ObservabilityService().record(
                "llm_call",
                TraceContext(
                    project_id=record.project_id,
                    agent_id=record.agent_id,
                    task_id=record.task_id,
                    request_id=record.request_id,
                ),
                task_type=record.task_type.value,
                model=record.selected_model,
                duration_seconds=record.duration,
                success=record.success,
            )

        gateway = LLMGateway(
            provider=OllamaProvider(base_url=settings.ollama_base_url),
            settings=settings,
            telemetry_sink=telemetry_sink,
            observability_sink=observability_sink,
        )
        realtime = (
            RealtimeEmitter(realtime_manager, state.project.project_id)
            if realtime_manager is not None
            else None
        )
        from app.memory.service import MemoryService

        return DefaultProjectOrchestrator(
            gateway=gateway,
            workspace_root=workspace_root,
            realtime=realtime,
            persistence=persistence,
            memory_service=MemoryService(),
        )

    return factory


class ProjectService:
    """Everything the API needs, expressed as plain async methods with no
    HTTP concepts (status codes, request/response objects) leaking in."""

    def __init__(
        self,
        orchestrator_factory: OrchestratorFactory,
        store: ProjectStore | None = None,
        persistence: PersistenceService | None = None,
        realtime_manager: ConnectionManager | None = None,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.store = store or ProjectStore()
        # Phase 17: optional durable persistence. `None` (the default)
        # keeps this class's existing in-memory-only behavior exactly as
        # it was in Phase 16 -- callers that want durability construct a
        # `app.db.persistence_service.PersistenceService` and pass it in.
        self.persistence = persistence
        # Phase 19: optional real-time fan-out. `None` keeps this class's
        # existing behavior exactly as it was pre-Phase-19 -- callers that
        # want `/ws/projects/{project_id}` to actually emit anything pass
        # in an `app.realtime.manager.ConnectionManager` (normally the
        # same instance registered on `app.state.realtime_manager`, see
        # `app.api.app.create_app`).
        self.realtime_manager = realtime_manager

    def _persist_in_background(self, state: AHSEAState) -> None:
        if self.persistence is None:
            return

        async def _save() -> None:
            try:
                await self.persistence.save_state(state)
            except Exception:  # pragma: no cover - persistence must never break a request
                logger.exception(
                    "Failed to persist project '%s'.", state.project.project_id
                )

        asyncio.ensure_future(_save())

    # ------------------------------------------------------------------
    # Project lifecycle: create / list / get
    # ------------------------------------------------------------------

    async def create_project(
        self, name: str, description: str, idea_prompt: str, repo_url: str | None = None
    ) -> AHSEAState:
        project = ProjectMetadata(
            name=name, description=description, idea_prompt=idea_prompt, repo_url=repo_url
        )
        state = AHSEAState(project=project)
        self.store.add(ProjectRecord(state=state))
        self._persist_in_background(state)
        return state

    def list_projects(self) -> list[AHSEAState]:
        return [record.state for record in self.store.list()]

    def get_project(self, project_id: str) -> AHSEAState:
        return self.store.get(project_id).state

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    async def run_project(self, project_id: str) -> ProjectRunControl:
        try:
            record = self.store.get(project_id)
        except ProjectNotFoundError:
            if self.persistence is None:
                raise
            restored = await self.persistence.load_state(project_id)
            if restored is None:
                raise ProjectNotFoundError(project_id)
            record = ProjectRecord(state=restored)
            self.store.add(record)
        control = record.control

        if control.status in (ProjectRunStatus.PLANNING, ProjectRunStatus.RUNNING):
            raise InvalidProjectStateError(f"Project '{project_id}' is already running.")
        if control.status == ProjectRunStatus.PAUSED:
            if not record.state.deployment.approved_by:
                raise InvalidProjectStateError(
                    f"Project '{project_id}' is paused; it is awaiting human approval."
                )

        # A fresh control per run so a re-run after COMPLETED/FAILED/CANCELLED
        # starts from a clean PENDING state rather than inheriting the old
        # terminal status/error.
        control = ProjectRunControl()
        record.control = control

        orchestrator = self.orchestrator_factory(record.state)
        control.asyncio_task = asyncio.ensure_future(orchestrator.run(record.state, control))
        # Snapshot the final state once the run reaches a terminal point
        # (completed/failed/cancelled), regardless of which one.
        control.asyncio_task.add_done_callback(
            lambda _task: self._persist_in_background(record.state)
        )
        return control

    def pause_project(self, project_id: str) -> ProjectRunControl:
        control = self.store.get(project_id).control
        if control.status != ProjectRunStatus.RUNNING:
            raise InvalidProjectStateError(
                f"Project '{project_id}' is not running (status: {control.status.value})."
            )
        control.pause()
        return control

    def resume_project(self, project_id: str) -> ProjectRunControl:
        control = self.store.get(project_id).control
        if control.status != ProjectRunStatus.PAUSED:
            raise InvalidProjectStateError(
                f"Project '{project_id}' is not paused (status: {control.status.value})."
            )
        control.resume()
        return control

    def cancel_project(self, project_id: str) -> ProjectRunControl:
        control = self.store.get(project_id).control
        if control.is_terminal or control.status == ProjectRunStatus.PENDING:
            raise InvalidProjectStateError(
                f"Project '{project_id}' has no active run to cancel "
                f"(status: {control.status.value})."
            )
        control.request_cancel()
        return control

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    def get_status(self, project_id: str) -> dict:
        record = self.store.get(project_id)
        counts: dict[str, int] = {status.value: 0 for status in TaskStatus}
        for task in record.state.tasks.values():
            counts[task.status.value] += 1
        return {
            "project_id": project_id,
            "status": record.control.status,
            "error": record.control.error,
            "task_counts": counts,
            "updated_at": record.state.updated_at,
        }

    def get_agents(self, project_id: str) -> list:
        return list(self.store.get(project_id).state.agents.values())

    def get_tasks(self, project_id: str) -> list:
        return list(self.store.get(project_id).state.tasks.values())

    def get_artifacts(self, project_id: str) -> list:
        return list(self.store.get(project_id).state.artifacts.values())

    def get_events(self, project_id: str) -> list[dict]:
        state = self.store.get(project_id).state
        events = [
            {
                "event_id": e.event_id,
                "scope": "agent",
                "agent_id": e.agent_id,
                "level": e.level,
                "message": e.message,
                "task_id": e.task_id,
                "data": e.data,
                "created_at": e.created_at,
            }
            for e in state.agent_events
        ] + [
            {
                "event_id": e.event_id,
                "scope": "project",
                "agent_id": None,
                "level": e.level,
                "message": e.message,
                "task_id": None,
                "data": e.data,
                "created_at": e.created_at,
            }
            for e in state.project_events
        ]
        events.sort(key=lambda e: e["created_at"])
        return events

    def get_qa_reports(self, project_id: str) -> list:
        return list(self.store.get(project_id).state.qa_reports)

    def get_deployment(self, project_id: str):
        return self.store.get(project_id).state.deployment

    # ------------------------------------------------------------------
    # Deployment approval
    # ------------------------------------------------------------------

    def approve_deployment(self, project_id: str, approved_by: str, notes: str = ""):
        """Record explicit human approval. Never itself triggers a deploy --
        `DeploymentManager` (Phase 15) is what checks for a recorded
        approval before it will ever call its `deploy()` step, keeping
        "never deploy without explicit approval" enforced at the point of
        action, not just at the point of recording consent."""

        state = self.store.get(project_id).state
        record_deployment_approval(state, approved_by=approved_by, approved=True)
        if notes:
            state.deployment.deployment_log.append(f"Approval note ({approved_by}): {notes}")
        self._persist_in_background(state)
        # The paused LangGraph run ended at its human gate. Start a new
        # invocation from its durable checkpoint once approval is recorded.
        record = self.store.get(project_id)
        if record.control.status == ProjectRunStatus.PAUSED:
            asyncio.ensure_future(self.run_project(project_id))
        return state.deployment
