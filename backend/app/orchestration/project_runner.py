"""`ProjectOrchestrator` (Phase 16 backing): the "orchestration" layer the
FastAPI control plane's service layer drives, so that `POST /run` etc. never
contain agent logic themselves -- they only ever call into this module.

Two pieces:
    * `ProjectRunControl` -- a small, service-owned handle for pausing,
      resuming, and cancelling a project run in progress.
    * `DefaultProjectOrchestrator` -- runs CTO planning (Phase 5), then
      dispatches every resulting task through `TaskScheduler` (Phase 10)
      to the right team's manager (Phase 7) / worker (Phase 8), honoring
      `ProjectRunControl` throughout.

`ProjectOrchestrator` is a `Protocol`, not a concrete dependency, so the
API layer's tests can substitute a fake orchestrator and stay fast/offline
(see `tests/test_api.py`) while `DefaultProjectOrchestrator` gets its own,
separate tests against a fake LLM provider (see
`tests/test_project_orchestrator.py`).
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from app.agents.cto import CTOAgent
from app.agents.cto_schemas import CTOPlanningError
from app.agents.managers.base import BaseManagerAgent
from app.agents.managers.concrete import MANAGER_CLASSES
from app.agents.managers.schemas import ManagerContext, ManagerReportStatus
from app.agents.workers.concrete import WORKER_CLASSES
from app.llm.gateway import LLMGateway
from app.memory.service import MemoryService, MemoryType
from app.realtime.emitter import RealtimeEmitter
from app.realtime.schemas import RealtimeEventType
from app.state.enums import EventLevel
from app.state.models import AHSEAState, ErrorRecord, ProjectEvent, Task
from app.state.operations import add_error, add_event, add_task, update_task
from app.tasks.dag import TaskGraphValidationError, create_graph, validate_graph
from app.tools.audit import AuditLog
from app.tools.base import ToolExecutor
from app.tools.permissions import READ_ONLY, WORKER_DEFAULT
from app.tools.registry import build_default_registry, make_executor

logger = logging.getLogger("ahsea.orchestration.project_runner")


class ProjectRunStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProjectRunControl:
    """Owned by the service layer (one instance per in-progress run).

    `pause_event` gates `ManagerDispatchRunner` between task dispatches --
    it is *set* when running and *cleared* when paused, so a paused run
    simply lets any already-dispatched task finish and then blocks before
    starting the next one, rather than yanking work out from under an
    in-flight agent.
    """

    def __init__(self) -> None:
        self.status: ProjectRunStatus = ProjectRunStatus.PENDING
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.cancel_requested = False
        self.error: str | None = None
        #: Set by the service layer once it schedules the background
        #: coroutine, so `request_cancel` can cancel it directly.
        self.asyncio_task: asyncio.Task | None = None

    def pause(self) -> None:
        self.pause_event.clear()
        if self.status == ProjectRunStatus.RUNNING:
            self.status = ProjectRunStatus.PAUSED

    def resume(self) -> None:
        self.pause_event.set()
        if self.status == ProjectRunStatus.PAUSED:
            self.status = ProjectRunStatus.RUNNING

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.pause_event.set()  # unblock a paused run so it can observe cancellation
        if self.asyncio_task is not None and not self.asyncio_task.done():
            self.asyncio_task.cancel()

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ProjectRunStatus.COMPLETED,
            ProjectRunStatus.FAILED,
            ProjectRunStatus.CANCELLED,
        )


class ProjectOrchestrator(Protocol):
    async def run(self, state: AHSEAState, control: ProjectRunControl) -> None: ...


class ManagerDispatchRunner:
    """The default `TaskExecutor`/`TaskScheduler` `TaskRunner`: routes each
    `Task` to its `owner_manager`'s manager, building managers/workers
    lazily and caching them for the lifetime of one project run.

    Honors `ProjectRunControl`: blocks on `control.pause_event` before
    starting a task's actual work, and raises `asyncio.CancelledError`
    immediately if a cancellation was requested while waiting.
    """

    def __init__(
        self,
        state: AHSEAState,
        gateway: LLMGateway,
        workspace_root: str | Path,
        control: ProjectRunControl,
        audit_log: AuditLog | None = None,
        realtime: RealtimeEmitter | None = None,
        memory_service: MemoryService | None = None,
    ):
        self.state = state
        self.gateway = gateway
        self.workspace_root = workspace_root
        self.control = control
        self.realtime = realtime
        #: Phase 22 wiring: when given, each dispatched task's
        #: `ManagerContext.global_summary` is enriched with relevant prior
        #: project memory before delegation, and the manager's outcome is
        #: written back as memory afterward -- so later tasks in the same
        #: run (and re-runs) can build on what earlier tasks/repairs
        #: learned. Best-effort: a memory read/write failure is logged and
        #: never fails the task itself.
        self.memory_service = memory_service
        # NOTE: `AuditLog` defines `__len__`, so a freshly-built (0-entry)
        # instance is falsy in Python -- `audit_log or AuditLog()` would
        # silently discard a real, hook-carrying audit log passed in here
        # (it's always empty at this point, moments after being built by
        # `DefaultProjectOrchestrator`) and replace it with a fresh one,
        # dropping the Phase 19 `on_record` -> `agent_tool_call` wiring.
        # Compare against `None` explicitly instead.
        self.audit_log = audit_log if audit_log is not None else AuditLog()
        self._registry = build_default_registry()
        self._worker_tools: ToolExecutor = make_executor(
            agent_id="worker-pool",
            workspace_root=workspace_root,
            permissions=WORKER_DEFAULT,
            registry=self._registry,
            audit_log=self.audit_log,
        )
        self._managers: dict[str, BaseManagerAgent] = {}

    def _get_manager(self, team_name: str) -> BaseManagerAgent:
        if team_name in self._managers:
            return self._managers[team_name]

        manager_cls = MANAGER_CLASSES.get(team_name)
        if manager_cls is None:
            # Phase 23/24 teams are generated at runtime.  Give an unknown
            # team a constrained generic manager rather than requiring a
            # hand-edited registry/YAML entry for every project.
            manager_cls = type(
                f"{team_name.replace(' ', '')}Manager",
                (BaseManagerAgent,),
                {
                    "team_name": team_name,
                    "managed_worker_types": list(WORKER_CLASSES),
                    "role_description": f"Coordinates {team_name} implementation work.",
                },
            )

        def worker_factory(worker_type: str, _team: str = team_name):
            worker_cls = WORKER_CLASSES.get(worker_type)
            if worker_cls is None:
                raise ValueError(f"No worker registered for type '{worker_type}'.")
            return worker_cls(
                gateway=self.gateway,
                tools=self._worker_tools,
                agent_id=f"{_team}-{worker_type}",
            )

        manager_tools = make_executor(
            agent_id=f"{team_name}-manager",
            workspace_root=self.workspace_root,
            permissions=READ_ONLY,
            registry=self._registry,
            audit_log=self.audit_log,
        )
        manager = manager_cls(
            gateway=self.gateway,
            manager_id=f"{team_name.lower()}-manager",
            worker_factory=worker_factory,
            tools=manager_tools,
        )
        self._managers[team_name] = manager
        return manager

    async def __call__(self, task: Task) -> Any:
        await self.control.pause_event.wait()
        if self.control.cancel_requested:
            raise asyncio.CancelledError()

        team_name = task.owner_manager or "Backend"
        agent_id = f"{team_name.lower()}-manager"
        manager = self._get_manager(team_name)
        # Real bug fix: `Task.assigned_agent_id` was declared on the model
        # and persisted to the DB, but nothing anywhere in the
        # orchestration code ever wrote to it -- every task showed
        # `assigned_agent_id: null` via the API/dashboard regardless of
        # whether (and by whom) it was actually being worked. Set it as
        # soon as dispatch to a manager begins, so it's non-null for the
        # whole time the task is genuinely running, not just after it
        # finishes.
        update_task(self.state, task.task_id, assigned_agent_id=agent_id)
        global_summary = self.state.project.description
        if self.memory_service is not None:
            try:
                memory_context = await self.memory_service.context_for_prompt(
                    self.state.project.project_id,
                    f"{task.title} {task.description}",
                    limit=5,
                )
            except Exception:  # noqa: BLE001 - memory is an enrichment, never fatal
                logger.warning("Memory retrieval failed for task '%s'.", task.task_id, exc_info=True)
                memory_context = ""
            if memory_context:
                global_summary = f"{global_summary}\n\n{memory_context}"
        context = ManagerContext(
            team_name=team_name,
            team_context=self.state.team_context.get(team_name, {}),
            global_summary=global_summary,
        )

        if self.realtime is not None:
            await self.realtime.emit(
                RealtimeEventType.AGENT_STARTED,
                agent_id=agent_id,
                task_id=task.task_id,
                payload={"team": team_name, "task_title": task.title},
            )

        try:
            report = await manager.handle_task(
                task, context, metadata={"project_id": self.state.project.project_id}
            )
        except Exception as exc:  # noqa: BLE001 - re-raised after telemetry, never swallowed
            if self.realtime is not None:
                await self.realtime.emit(
                    RealtimeEventType.AGENT_COMPLETED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    payload={"team": team_name, "success": False, "error": str(exc)},
                )
            raise

        if self.realtime is not None:
            status = getattr(report, "status", None)
            await self.realtime.emit(
                RealtimeEventType.AGENT_COMPLETED,
                agent_id=agent_id,
                task_id=task.task_id,
                payload={
                    "team": team_name,
                    "status": getattr(status, "value", status),
                    "summary": getattr(report, "summary", None),
                },
            )

        # Refine `assigned_agent_id` from "the manager" to "the specific
        # worker that actually did the work", now that we know who that
        # was (`ManagerReport.worker_agent_id`, already populated by
        # `BaseManagerAgent.handle_task` -- it just was never read here).
        worker_agent_id = getattr(report, "worker_agent_id", None)
        if worker_agent_id:
            update_task(self.state, task.task_id, assigned_agent_id=worker_agent_id)

        if self.memory_service is not None:
            try:
                if report.status == ManagerReportStatus.ACCEPTED:
                    await self.memory_service.store(
                        self.state.project.project_id,
                        MemoryType.DECISION,
                        title=f"Completed: {task.title}",
                        content=report.summary or "(no summary provided)",
                        tags=[team_name, task.worker_type or ""],
                        importance=0.5,
                    )
                else:
                    await self.memory_service.store(
                        self.state.project.project_id,
                        MemoryType.FAILURE,
                        title=f"Did not complete: {task.title}",
                        content=(
                            f"Status: {report.status.value}. "
                            + ("; ".join(report.errors) if report.errors else report.summary)
                        ),
                        tags=[team_name, task.worker_type or ""],
                        importance=0.6,
                    )
            except Exception:  # noqa: BLE001 - memory is an enrichment, never fatal
                logger.warning(
                    "Memory write failed for task '%s'.", task.task_id, exc_info=True
                )
        return report


class DefaultProjectOrchestrator:
    """CTO planning -> `TaskScheduler` execution, honoring `ProjectRunControl`."""

    def __init__(
        self,
        gateway: LLMGateway,
        workspace_root: str | Path,
        max_task_concurrency: int = 4,
        audit_log: AuditLog | None = None,
        realtime: RealtimeEmitter | None = None,
        persistence: Any = None,
        memory_service: MemoryService | None = None,
    ):
        self.gateway = gateway
        self.workspace_root = workspace_root
        self.max_task_concurrency = max_task_concurrency
        self.realtime = realtime
        self.persistence = persistence
        #: Phase 22 wiring: passed straight through to the CTO agent
        #: (read/write project memory during planning) and to each
        #: `ManagerDispatchRunner` built for this run (read/write around
        #: task delegation). `None` by default -- callers that don't pass
        #: one get the pre-fix behavior exactly.
        self.memory_service = memory_service
        self.audit_log = audit_log or AuditLog(
            on_record=self._on_tool_call if realtime is not None else None
        )

    def _on_tool_call(self, entry: Any) -> None:
        """`AuditLog.on_record` hook: fire-and-forget `AGENT_TOOL_CALL` for
        every tool invocation. `entry.arguments_summary` is already
        redacted/truncated by `app.tools.base._summarize_arguments` (file
        contents never land there); `RealtimeEmitter.emit` redacts the
        payload again regardless.
        """

        if self.realtime is None:  # pragma: no cover - defensive, hook only wired when set
            return
        self.realtime.emit_soon(
            RealtimeEventType.AGENT_TOOL_CALL,
            agent_id=entry.agent_id,
            payload={
                "tool_name": entry.tool_name,
                "success": entry.success,
                "permission_granted": entry.permission_granted,
                "arguments": entry.arguments_summary,
                "error_message": entry.error_message,
                "duration_seconds": entry.duration_seconds,
            },
        )

    async def _plan(self, state: AHSEAState) -> None:
        cto_agent = CTOAgent(gateway=self.gateway, memory_service=self.memory_service)
        metadata = {"project_id": state.project.project_id}
        plan = await cto_agent.plan(
            idea_prompt=state.project.idea_prompt,
            project_name=state.project.name,
            metadata=metadata,
        )
        try:
            validate_graph(create_graph(plan.tasks))
        except TaskGraphValidationError as exc:
            raise CTOPlanningError(f"CTO-produced tasks do not form a valid DAG: {exc}") from exc

        state.requirements.extend(plan.requirements)
        state.architecture = plan.architecture
        for task in plan.tasks:
            add_task(state, task)
        state.shared_context["cto_teams"] = [t.model_dump(mode="json") for t in plan.teams]
        state.shared_context["cto_dependencies"] = [
            d.model_dump(mode="json") for d in plan.dependencies
        ]
        state.shared_context["testing_requirements"] = plan.testing_requirements
        state.shared_context["deployment_requirements"] = plan.deployment_requirements

    async def run(self, state: AHSEAState, control: ProjectRunControl) -> None:
        """Run the Phase 24 LangGraph workflow, including recovery and approval."""
        from app.orchestration.complete import CompleteOrchestration

        control.status = ProjectRunStatus.PLANNING
        add_event(
            state,
            ProjectEvent(level=EventLevel.INFO, message="Project run started: complete orchestration."),
        )
        if self.realtime is not None:
            await self.realtime.emit(
                RealtimeEventType.PROJECT_STARTED,
                payload={"project_name": state.project.name},
            )

        def runner_factory(current: AHSEAState) -> ManagerDispatchRunner:
            return ManagerDispatchRunner(
                state=current,
                gateway=self.gateway,
                workspace_root=self.workspace_root,
                control=control,
                audit_log=self.audit_log,
                realtime=self.realtime,
                memory_service=self.memory_service,
            )

        try:
            workflow = CompleteOrchestration(
                gateway=self.gateway,
                workspace_root=str(self.workspace_root),
                control=control,
                plan=self._plan,
                runner_factory=runner_factory,
                persistence=self.persistence,
                realtime=self.realtime,
                max_task_concurrency=self.max_task_concurrency,
                memory_service=self.memory_service,
            )
            await workflow.run(state)
        except asyncio.CancelledError:
            control.status = ProjectRunStatus.CANCELLED
            add_event(
                state,
                ProjectEvent(level=EventLevel.WARNING, message="Run cancelled during orchestration."),
            )
            return
        except Exception as exc:  # noqa: BLE001
            control.status = ProjectRunStatus.FAILED
            control.error = str(exc)
            add_error(state, ErrorRecord(source="DefaultProjectOrchestrator", message=str(exc)))
            return
        if control.cancel_requested:
            control.status = ProjectRunStatus.CANCELLED
        elif control.status != ProjectRunStatus.PAUSED:
            control.status = ProjectRunStatus.FAILED if state.errors else ProjectRunStatus.COMPLETED
