"""Event schema for the Phase 19 real-time WebSocket feed.

`RealtimeEvent` is the single wire format every `/ws/projects/{project_id}`
subscriber receives, regardless of which subsystem (orchestrator, QA,
self-healing, deployment...) produced it. Keeping one flat schema -- rather
than a union of per-subsystem event shapes -- is what lets a single
`ConnectionManager` fan out to a browser tab without the frontend needing
to know anything about the backend's internal module boundaries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RealtimeEventType(str, Enum):
    """Exactly the event set specified for Phase 19 -- nothing more, nothing less."""

    PROJECT_STARTED = "project_started"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_TOOL_CALL = "agent_tool_call"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    INTEGRATION_FAILED = "integration_failed"
    QA_STARTED = "qa_started"
    QA_FAILED = "qa_failed"
    REPAIR_STARTED = "repair_started"
    REPAIR_COMPLETED = "repair_completed"
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_COMPLETED = "deployment_completed"


class RealtimeEvent(BaseModel):
    """A single, structured real-time event broadcast over the WebSocket.

    `payload` is always run through `app.realtime.redaction.sanitize_payload`
    before this object is constructed (see `RealtimeEmitter.emit`) -- nothing
    that reaches a client here should ever contain secrets, raw environment
    values, hidden system prompts, or other sensitive LLM data.
    """

    event_id: str = Field(default_factory=lambda: f"rtevt_{uuid.uuid4().hex[:16]}")
    project_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: RealtimeEventType
    agent_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
