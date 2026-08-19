"""Structured types for the Git-based agent development workflow (Phase 14)."""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field

from app.qa.schemas import QAPipelineReport
from app.state.models import _new_id

#: `agent/<slug>` -- enforced by `make_branch_name`, checked again by
#: `GitWorkflowEngine` before ever calling the `git_checkout` tool.
BRANCH_PREFIX = "agent/"
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "task"


def make_branch_name(team_name: str, task_id: str) -> str:
    """`agent/<team>-<task-suffix>`, e.g. `agent/backend-task-9f2c1a`."""

    team_slug = _slugify(team_name)
    task_slug = _slugify(task_id)
    return f"{BRANCH_PREFIX}{team_slug}-{task_slug}"


def make_commit_message(scope: str, description: str) -> str:
    """`agent(<scope>): <description>`, e.g. `agent(backend): fix login null check`."""

    scope_slug = _slugify(scope) or "agent"
    description = description.strip().splitlines()[0] if description.strip() else "update"
    return f"agent({scope_slug}): {description}"


_COMMIT_MESSAGE_RE = re.compile(r"^agent\([a-z0-9._-]+\): .+")


def is_valid_commit_message(message: str) -> bool:
    return bool(_COMMIT_MESSAGE_RE.match(message))


def is_valid_branch_name(branch_name: str) -> bool:
    return branch_name.startswith(BRANCH_PREFIX) and len(branch_name) > len(BRANCH_PREFIX)


class GitWorkflowStage(str, Enum):
    """Every stage the spec's pipeline can reach, in order."""

    BRANCH_CREATED = "branch_created"
    IMPLEMENTED = "implemented"
    COMMITTED = "committed"
    TESTED = "tested"
    REVIEWED = "reviewed"
    INTEGRATION_VALIDATED = "integration_validated"
    QA_VALIDATED = "qa_validated"
    MERGED = "merged"
    ABORTED = "aborted"


class ArchitectureReviewDecision(BaseModel):
    """Result of the architecture review call (`TaskType.REASONING`, Qwen3).

    Deliberately narrow: Qwen3 judges structural/architectural fit, never
    proposes replacement code -- `CodeReviewAgent` (Qwen2.5-Coder, Phase 12)
    already owns line-level code review.
    """

    decision: str = Field(description="'pass' or 'needs_fix'.")
    concerns: list[str] = Field(default_factory=list)
    notes: str = ""


class MergeGateCheck(BaseModel):
    """One named pre-merge requirement from the spec: tests pass, integration
    passes, no unauthorized files, diff reviewed."""

    name: str
    passed: bool
    detail: str = ""


class GitWorkflowReport(BaseModel):
    """What `GitWorkflowEngine.run()` returns for one task."""

    report_id: str = Field(default_factory=lambda: _new_id("gitflow"))
    task_id: str
    branch_name: str
    commit_message: str | None = None
    commit_sha: str | None = None
    stage_reached: GitWorkflowStage
    merged: bool = False
    merge_target: str | None = None
    checks: list[MergeGateCheck] = Field(default_factory=list)
    unauthorized_files: list[str] = Field(default_factory=list)
    qa_report: QAPipelineReport | None = None
    architecture_review: ArchitectureReviewDecision | None = None
    errors: list[str] = Field(default_factory=list)
    rework_task_id: str | None = None

    @property
    def all_gates_passed(self) -> bool:
        return all(c.passed for c in self.checks)
