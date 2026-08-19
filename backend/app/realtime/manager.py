"""`ConnectionManager`: fan-out + reconnect/replay support for
`/ws/projects/{project_id}` (Phase 19).

Mirrors the shape of `app.orchestration.events.EventBus` and
`app.deployment.events.DeploymentEventBus` (a small pub/sub with a bounded
history), but fans out to live `WebSocket` connections grouped by
`project_id` instead of to in-process handlers, and keeps a short replay
buffer per project so a client that reconnects (flaky wifi, laptop sleep,
tab backgrounded) can catch up on whatever it missed instead of silently
losing events.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque

from fastapi import WebSocket

from app.realtime.schemas import RealtimeEvent

logger = logging.getLogger("ahsea.realtime.manager")

#: How many recent events per project are kept for reconnect replay.
#: Bounded so a long-running project can't grow this without limit --
#: clients that fall further behind than this should re-fetch state via
#: the REST `GET /api/projects/{id}/events` endpoint instead.
DEFAULT_HISTORY_SIZE = 500


class ConnectionManager:
    """Owns every live WebSocket connection, grouped by `project_id`.

    Not shared across app instances -- one `ConnectionManager` lives on
    `app.state.realtime_manager` for the lifetime of one FastAPI process.
    A multi-process deployment would swap this for a Redis/NATS-backed
    fan-out without changing anything upstream (`RealtimeEmitter` only
    ever depends on `.broadcast`/`.replay`).
    """

    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self._history_size = history_size
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._history: dict[str, deque[RealtimeEvent]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, project_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[project_id].add(websocket)
        logger.info("WebSocket connected for project '%s'.", project_id)

    async def disconnect(self, project_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[project_id].discard(websocket)
            if not self._connections[project_id]:
                del self._connections[project_id]
        logger.info("WebSocket disconnected for project '%s'.", project_id)

    def connection_count(self, project_id: str) -> int:
        return len(self._connections.get(project_id, ()))

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(self, project_id: str, event: RealtimeEvent) -> None:
        """Record `event` in the replay buffer and push it to every live
        connection for `project_id`.

        A send failing (client gone but the disconnect hasn't been
        observed yet) never breaks the broadcast for other subscribers --
        the dead socket is just dropped, matching `EventBus.publish`'s
        "one broken subscriber can't take down the rest" behavior.
        """

        self._history[project_id].append(event)

        connections = list(self._connections.get(project_id, ()))
        if not connections:
            return

        payload = event.model_dump(mode="json")
        dead: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 - a dead socket must never break the fan-out
                dead.append(websocket)

        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections[project_id].discard(websocket)

    # ------------------------------------------------------------------
    # Reconnect replay
    # ------------------------------------------------------------------

    def replay(self, project_id: str, after_event_id: str | None = None) -> list[RealtimeEvent]:
        """Events for `project_id` since `after_event_id` (exclusive), for a
        client that just (re)connected via `?after=<event_id>`.

        If `after_event_id` isn't found in the buffer (client was gone
        longer than `DEFAULT_HISTORY_SIZE` events, or this is a fresh
        server process), every buffered event is replayed -- a client
        should treat the replay as authoritative and reconcile against
        its own last-known state rather than assume gap-free delivery.
        """

        history = list(self._history.get(project_id, ()))
        if after_event_id is None:
            return history

        for index, event in enumerate(history):
            if event.event_id == after_event_id:
                return history[index + 1 :]
        return history
