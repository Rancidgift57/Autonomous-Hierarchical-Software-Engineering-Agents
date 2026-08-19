"""Task-execution events for parallel scheduling (Phase 10).

`TaskScheduler` and `TaskExecutor` never call back into arbitrary user code
directly -- they only ever publish a `TaskEvent` through an `EventBus`.
This keeps observability (progress bars, dashboards, tests asserting on
ordering) decoupled from the scheduling/execution logic itself.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("ahsea.orchestration.events")


class TaskEventType(str, Enum):
    """Every lifecycle transition the scheduler/executor can emit."""

    TASK_READY = "task_ready"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_RETRYING = "task_retrying"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCELLED = "task_cancelled"


class TaskEvent(BaseModel):
    """A single, structured scheduling/execution event."""

    event_type: TaskEventType
    task_id: str
    attempt: int = 0
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


EventHandler = Callable[[TaskEvent], Any]


class EventBus:
    """Fan-out publisher for `TaskEvent`s.

    Handlers may be sync or async callables; a handler raising is logged
    and swallowed so one broken subscriber (e.g. a flaky dashboard) can
    never take down task scheduling.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._history: list[TaskEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    @property
    def history(self) -> list[TaskEvent]:
        return list(self._history)

    async def publish(self, event: TaskEvent) -> None:
        self._history.append(event)
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - a subscriber must never break scheduling
                logger.exception(
                    "Event handler raised while processing %s for task %s",
                    event.event_type,
                    event.task_id,
                )

    def emit_soon(self, event: TaskEvent) -> None:
        """Fire-and-forget publish for call sites that aren't `async def`."""

        asyncio.ensure_future(self.publish(event))


TaskRunner = Callable[..., Awaitable[Any]]
