"""Structured types for `BaseWorkerAgent` (Phase 8).

Two layers, mirroring `app.agents.cto_schemas`:

1. LLM-facing output schemas (`WorkerPlan`, `WorkerImplementationOutput`,
   `WorkerSelfReview`) -- what we ask Qwen3 (planning) / Qwen2.5-Coder
   (implementation, review) to produce via `LLMGateway.generate_json`.
2. `WorkerContext` / `WorkerScope` / `WorkerResult` -- the plain data a
   worker is given and the exact result shape it must return (per the
   Phase 8 spec: task_id, agent_id, status, summary, files_changed,
   artifacts, tests, errors).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. LLM-facing schemas
# ---------------------------------------------------------------------------


class WorkerPlan(BaseModel):
    """Result of the `TaskType.PLANNING` call (routed to Qwen3)."""

    approach: str = Field(description="One or two sentence implementation approach.")
    steps: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(
        default_factory=list,
        description="Repository-relative paths this plan expects to touch.",
    )


class WorkerFileChange(BaseModel):
    """A single file-level change requested by the implementation step.

    `action='create'` uses `content` (the full new file body). `action='edit'`
    uses `old_str`/`new_str`, applied via the sandboxed `edit_file` tool's
    unique find/replace semantics. `action='delete'` needs no content.
    """

    path: str
    action: Literal["create", "edit", "delete"] = "create"
    content: str | None = None
    old_str: str | None = None
    new_str: str | None = None
    description: str = ""


class WorkerImplementationOutput(BaseModel):
    """Result of the `TaskType.CODING` call (routed to Qwen2.5-Coder)."""

    summary: str
    files: list[WorkerFileChange] = Field(default_factory=list)


class WorkerSelfReview(BaseModel):
    """Result of the self-review call (`TaskType.CODE_REVIEW`, coder model)."""

    decision: Literal["pass", "needs_fix"]
    issues: list[str] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# 2. Plain data: context in, result out
# ---------------------------------------------------------------------------


class WorkerContext(BaseModel):
    """The *scoped* slice of project state a worker is allowed to see.

    Deliberately flat and small -- a worker never receives the raw
    `AHSEAState`. Callers (typically a manager, via
    `app.agents.managers.base`) are responsible for building this from
    whatever full state they have access to.
    """

    module_summary: str = ""
    related_requirements: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    team_notes: str = ""
    manager_instructions: str = ""


class WorkerScope(BaseModel):
    """Least-privilege guardrails applied to a worker's file changes.

    Enforced by `BaseWorkerAgent`, independently of whatever permissions
    the worker's `ToolExecutor` happens to carry -- two layers of defence.
    """

    allowed_path_prefixes: list[str] = Field(
        default_factory=list,
        description="If non-empty, every changed path must start with one of these.",
    )
    forbidden_paths: list[str] = Field(default_factory=list)
    max_files_changed: int = 15
    test_path: str = "tests"


class WorkerStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkerResult(BaseModel):
    """Exactly the fields the Phase 8 spec requires a worker to return."""

    task_id: str
    agent_id: str
    status: WorkerStatus
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    tests: dict[str, object] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
