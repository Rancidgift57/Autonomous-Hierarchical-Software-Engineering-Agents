"""Structured types for `BaseManagerAgent` (Phase 7).

All three LLM-facing schemas here are produced by calls with
`task_type=TaskType.MANAGEMENT` -- the *only* task type a manager is
allowed to request (routed by the gateway to Qwen3, never Qwen2.5-Coder).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ManagerAnalysis(BaseModel):
    """Result of the ANALYZE step: the manager's read on the task."""

    summary: str
    key_considerations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class WorkerSelection(BaseModel):
    """Result of the SELECT WORKER step."""

    selected_worker_type: str = Field(
        description="Must be one of the worker types this manager is allowed to delegate to."
    )
    rationale: str = ""
    instructions: str = Field(
        default="", description="Instructions to hand down to the selected worker."
    )


class ManagerReviewDecision(BaseModel):
    """Result of the REVIEW step, judging a worker's `WorkerResult`."""

    decision: Literal["accept", "rework"]
    feedback: str = ""
    issues: list[str] = Field(default_factory=list)


class ManagerContext(BaseModel):
    """The *scoped* slice of project state a manager is allowed to see.

    A manager must not receive the raw `AHSEAState` -- only its own team's
    context plus a short, redacted global summary. Building this from full
    state (see `app.agents.managers.context.build_manager_context`) is the
    only place team-scoping decisions get made; `BaseManagerAgent` itself
    never reaches past what's on this object.
    """

    team_name: str
    team_context: dict[str, object] = Field(default_factory=dict)
    global_summary: str = ""
    relevant_artifacts: list[str] = Field(default_factory=list)


class ManagerReportStatus(str, Enum):
    ACCEPTED = "accepted"
    REWORK_EXHAUSTED = "rework_exhausted"
    NO_ELIGIBLE_WORKER = "no_eligible_worker"
    FAILED = "failed"


class ManagerReport(BaseModel):
    """What a manager reports upward to its parent (CTO) after handling a task."""

    task_id: str
    manager_id: str
    team_name: str
    status: ManagerReportStatus
    selected_worker_type: str | None = None
    worker_agent_id: str | None = None
    rework_cycles: int = 0
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
