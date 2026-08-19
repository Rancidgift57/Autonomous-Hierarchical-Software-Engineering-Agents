"""Phase 19: real-time project events over `/ws/projects/{project_id}`."""

from app.realtime.emitter import RealtimeEmitter, attach_deployment_events, attach_task_events
from app.realtime.manager import ConnectionManager
from app.realtime.schemas import RealtimeEvent, RealtimeEventType

__all__ = [
    "ConnectionManager",
    "RealtimeEmitter",
    "RealtimeEvent",
    "RealtimeEventType",
    "attach_deployment_events",
    "attach_task_events",
]
