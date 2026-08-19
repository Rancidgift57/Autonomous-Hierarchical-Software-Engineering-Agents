"""Structured types for the Self-Healing System (Phase 13)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.state.models import _new_id


class ErrorDiagnosis(BaseModel):
    """Result of the `TaskType.ERROR_ANALYSIS` call (routed to Qwen3).

    Qwen3's job stops here: it understands the failure, proposes a
    solution *in words*, and names who should apply it -- it never emits
    file contents or a diff. Turning `proposed_solution` into an actual
    code change is exclusively `DebuggingWorker`'s (Qwen2.5-Coder's) job,
    one layer down, and even that goes through a manager's review loop.
    """

    root_cause: str
    classification: str = Field(
        description="e.g. 'logic_error', 'contract_mismatch', 'test_failure', 'config_error'."
    )
    responsible_team: str
    proposed_solution: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RepairOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"


class RepairAttempt(BaseModel):
    """One full diagnose -> rework-task -> manager-reviewed-fix -> retest cycle."""

    attempt_id: str = Field(default_factory=lambda: _new_id("repair"))
    task_id: str
    error_id: str | None = None
    attempt_number: int
    diagnosis: ErrorDiagnosis | None = None
    rework_task_id: str | None = None
    outcome: RepairOutcome | None = None
    detail: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class SelfHealingResult(BaseModel):
    """What `SelfHealingEngine.heal()` returns for one failure."""

    task_id: str
    outcome: RepairOutcome
    attempts: list[RepairAttempt] = Field(default_factory=list)
    escalation_reason: str | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)
