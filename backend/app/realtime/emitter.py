"""`RealtimeEmitter`: the one object every subsystem that wants to appear
on `/ws/projects/{project_id}` is handed (Phase 19).

Every producer in this codebase already publishes *something* through its
own typed event bus (`app.orchestration.events.EventBus`,
`app.deployment.events.DeploymentEventBus`) or directly mutates
`AHSEAState` (self-healing, integration, QA). `RealtimeEmitter` is the
translation layer: it turns any of those into the flat `RealtimeEvent`
wire format and hands it to a `ConnectionManager`, redacting the payload
on the way out. Nothing upstream of this module needs to know a
WebSocket exists at all -- every integration point below takes an
`emitter: RealtimeEmitter | None = None` and simply no-ops when it's
`None`, so this is purely additive to every subsystem it touches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.realtime.manager import ConnectionManager
from app.realtime.redaction import sanitize_payload
from app.realtime.schemas import RealtimeEvent, RealtimeEventType

logger = logging.getLogger("ahsea.realtime.emitter")


class RealtimeEmitter:
    """Bound to one project; every subsystem working on that project's run
    gets (a reference to) the same instance."""

    def __init__(self, manager: ConnectionManager, project_id: str):
        self.manager = manager
        self.project_id = project_id

    async def emit(
        self,
        event_type: RealtimeEventType,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = RealtimeEvent(
            project_id=self.project_id,
            event_type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            payload=sanitize_payload(payload or {}),
        )
        await self.manager.broadcast(self.project_id, event)

    def emit_soon(
        self,
        event_type: RealtimeEventType,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget variant for call sites that aren't `async def`
        (e.g. `AuditLog.record`), mirroring `EventBus.emit_soon`."""

        try:
            asyncio.ensure_future(
                self.emit(event_type, agent_id=agent_id, task_id=task_id, payload=payload)
            )
        except RuntimeError:  # pragma: no cover - no running loop (e.g. a sync test)
            logger.debug("Dropped realtime event %s: no running event loop.", event_type)


# ---------------------------------------------------------------------------
# Translators: bridge an existing typed event bus onto a `RealtimeEmitter`.
# ---------------------------------------------------------------------------


def attach_task_events(emitter: RealtimeEmitter, events: Any, state: Any) -> None:
    """Subscribe `emitter` to an `app.orchestration.events.EventBus`,
    translating `TASK_STARTED`/`TASK_COMPLETED`/`TASK_FAILED` into the
    matching `RealtimeEventType`s. Other `TaskEventType`s (ready, blocked,
    cancelled, retrying) are outside the Phase 19 event list and are
    intentionally not forwarded.
    """

    from app.orchestration.events import TaskEvent, TaskEventType

    _MAPPING = {
        TaskEventType.TASK_STARTED: RealtimeEventType.TASK_STARTED,
        TaskEventType.TASK_COMPLETED: RealtimeEventType.TASK_COMPLETED,
        TaskEventType.TASK_FAILED: RealtimeEventType.TASK_FAILED,
    }

    async def _handler(task_event: TaskEvent) -> None:
        realtime_type = _MAPPING.get(task_event.event_type)
        if realtime_type is None:
            return
        task = state.tasks.get(task_event.task_id)
        await emitter.emit(
            realtime_type,
            agent_id=task.owner_manager if task is not None else None,
            task_id=task_event.task_id,
            payload={
                "attempt": task_event.attempt,
                "message": task_event.message,
                **task_event.data,
            },
        )

    events.subscribe(_handler)


def attach_deployment_events(emitter: RealtimeEmitter, event_bus: Any) -> None:
    """Subscribe `emitter` to an `app.deployment.events.DeploymentEventBus`,
    translating `DEPLOY_STARTED`/`DEPLOY_COMPLETED` into the matching
    `RealtimeEventType`s. `DeploymentEvent.data` is already redacted by
    `app.deployment.validator.redact_secrets` upstream (see
    `DeploymentManager._emit`); `RealtimeEmitter.emit` redacts it again
    regardless, so this is defense in depth, not a trust boundary.
    """

    from app.deployment.schemas import DeploymentEvent, DeploymentEventType

    _MAPPING = {
        DeploymentEventType.DEPLOY_STARTED: RealtimeEventType.DEPLOYMENT_STARTED,
        DeploymentEventType.DEPLOY_COMPLETED: RealtimeEventType.DEPLOYMENT_COMPLETED,
    }

    async def _handler(deployment_event: DeploymentEvent) -> None:
        realtime_type = _MAPPING.get(deployment_event.event_type)
        if realtime_type is None:
            return
        await emitter.emit(
            realtime_type,
            payload={
                "stage": deployment_event.stage,
                "message": deployment_event.message,
                **deployment_event.data,
            },
        )

    event_bus.subscribe(_handler)
