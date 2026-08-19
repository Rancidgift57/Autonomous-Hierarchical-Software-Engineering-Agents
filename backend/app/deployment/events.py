"""Event bus for the Deployment System (Phase 15).

Mirrors `app.orchestration.events.EventBus` (Phase 10): a small fan-out
publisher so observability (dashboards, tests asserting on ordering) stays
decoupled from `DeploymentManager` itself. Kept as its own tiny class,
rather than reusing the orchestration one directly, since it's typed for
`DeploymentEvent` rather than `TaskEvent`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from app.deployment.schemas import DeploymentEvent

logger = logging.getLogger("ahsea.deployment.events")

DeploymentEventHandler = Callable[[DeploymentEvent], Awaitable[None] | None]


class DeploymentEventBus:
    """Fan-out publisher for `DeploymentEvent`s.

    A handler raising is logged and swallowed so one broken subscriber can
    never interrupt a deployment pipeline in progress.
    """

    def __init__(self) -> None:
        self._handlers: list[DeploymentEventHandler] = []
        self._history: list[DeploymentEvent] = []

    def subscribe(self, handler: DeploymentEventHandler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: DeploymentEventHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    @property
    def history(self) -> list[DeploymentEvent]:
        return list(self._history)

    async def publish(self, event: DeploymentEvent) -> None:
        self._history.append(event)
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - a subscriber must never break deployment
                logger.exception(
                    "Deployment event handler raised while processing %s", event.event_type
                )

    def emit_soon(self, event: DeploymentEvent) -> None:
        """Fire-and-forget publish for call sites that aren't `async def`."""

        asyncio.ensure_future(self.publish(event))
