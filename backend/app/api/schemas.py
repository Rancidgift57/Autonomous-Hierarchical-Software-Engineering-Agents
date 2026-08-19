"""Request/response schemas for the FastAPI control plane (Phase 16).

Where an existing `app.state.models` type is already the right shape for
a response (e.g. `Task`, `Artifact`, `DeploymentState`), routers return it
directly rather than re-declaring a parallel schema -- these are only the
API-specific request bodies and small response envelopes that don't
already exist elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.orchestration.project_runner import ProjectRunStatus
from app.state.enums import EventLevel, TaskStatus


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    idea_prompt: str = Field(min_length=1, max_length=20000)
    repo_url: str | None = None


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    description: str
    status: ProjectRunStatus
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    idea_prompt: str
    repo_url: str | None
    task_count: int
    error: str | None = None


class RunControlResponse(BaseModel):
    """Response for /run, /pause, /resume, /cancel -- an acknowledgement,
    not the final outcome (the run continues in the background; poll
    `GET /status` for progress)."""

    project_id: str
    status: ProjectRunStatus
    message: str = ""


class ProjectStatusResponse(BaseModel):
    project_id: str
    status: ProjectRunStatus
    error: str | None = None
    task_counts: dict[str, int]
    updated_at: datetime


class EventOut(BaseModel):
    """Unifies `AgentEvent` and `ProjectEvent` into one response shape for
    `GET /events`."""

    event_id: str
    scope: str = Field(description="'agent' or 'project'.")
    agent_id: str | None = None
    level: EventLevel
    message: str
    task_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DeploymentApprovalRequest(BaseModel):
    approved_by: str = Field(min_length=1, max_length=200)
    notes: str = ""


class ErrorResponse(BaseModel):
    detail: str


__all__ = [
    "DeploymentApprovalRequest",
    "ErrorResponse",
    "EventOut",
    "ProjectCreateRequest",
    "ProjectDetail",
    "ProjectStatusResponse",
    "ProjectSummary",
    "RunControlResponse",
    "TaskStatus",
]
