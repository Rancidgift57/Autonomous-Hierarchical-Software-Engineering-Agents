"""`/ws/projects/{project_id}` (Phase 19).

Streams `app.realtime.schemas.RealtimeEvent`s for one project live. Kept
as its own router (rather than folded into `app.api.routers.projects`)
because a WebSocket route has a different lifecycle (accept/receive-loop/
disconnect) than the request/response handlers there, and doesn't share
their `Depends(get_current_principal)` header-based auth -- browsers can't
set arbitrary headers on a WebSocket handshake, so auth here (when
enabled) comes from a `?api_key=` query parameter instead.

Reconnect handling: a client that reconnects (flaky network, laptop
sleep, backgrounded tab) should pass `?after=<last_event_id_it_saw>`;
`ConnectionManager.replay` sends everything it missed before the live
stream resumes, so the client never has to guess whether it has a gap.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api import security as security_module
from app.api.services.project_service import ProjectNotFoundError, ProjectService
from app.realtime.manager import ConnectionManager

logger = logging.getLogger("ahsea.api.websocket")

router = APIRouter(tags=["realtime"])

#: How often, absent any client message, a heartbeat ping is sent -- lets
#: the client (and any intermediary proxy/load balancer) tell a stalled
#: connection apart from one that's simply quiet.
HEARTBEAT_INTERVAL_SECONDS = 30.0

#: WebSocket close codes (private-use range, 4000-4999).
_CLOSE_NOT_FOUND = 4404
_CLOSE_UNAUTHORIZED = 4401


def _authorized(websocket: WebSocket) -> bool:
    settings = security_module.get_security_settings()
    if not settings.require_api_key:
        return True
    api_key = websocket.query_params.get("api_key")
    return api_key is not None and api_key in settings.valid_keys


@router.websocket("/ws/projects/{project_id}")
async def project_events_ws(websocket: WebSocket, project_id: str) -> None:
    if not _authorized(websocket):
        await websocket.close(code=_CLOSE_UNAUTHORIZED, reason="Missing or invalid API key.")
        return

    service: ProjectService = websocket.app.state.project_service
    manager: ConnectionManager = websocket.app.state.realtime_manager

    try:
        service.get_project(project_id)
    except ProjectNotFoundError:
        await websocket.close(code=_CLOSE_NOT_FOUND, reason=f"Project '{project_id}' not found.")
        return

    await manager.connect(project_id, websocket)
    try:
        after = websocket.query_params.get("after")
        for event in manager.replay(project_id, after_event_id=after):
            await websocket.send_json(event.model_dump(mode="json"))

        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=HEARTBEAT_INTERVAL_SECONDS
                )
            except TimeoutError:
                # No client traffic within the interval -- send a heartbeat
                # so the client (and any proxy in between) can tell a live
                # connection apart from a dead one, rather than relying on
                # events alone (a quiet project would otherwise look dead).
                await websocket.send_json({"type": "ping"})
                continue

            # The client has no commands it needs to send today -- this is
            # purely a keepalive/ack channel. "ping" gets an explicit
            # "pong" so a client can also proactively probe liveness.
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never let one bad socket take the server down
        logger.exception("WebSocket error for project '%s'.", project_id)
    finally:
        await manager.disconnect(project_id, websocket)
