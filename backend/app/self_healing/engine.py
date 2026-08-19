"""`SelfHealingEngine` (Phase 13).

Workflow, exactly as specified::

    FAILURE
      -> Qwen3 ERROR_ANALYSIS   (classify, identify responsible team, propose a solution)
      -> create rework task
      -> manager                (BaseManagerAgent.handle_task -- Phase 7's own
                                  analyze/select/delegate/REVIEW loop)
      -> Qwen2.5-Coder DEBUGGING (DebuggingWorker -- modifies code, runs tests)
      -> test
      -> repeat, up to MAX_REPAIR_ATTEMPTS, then ESCALATE_TO_HUMAN

Model responsibility boundary (enforced structurally, not just by
convention):
    * Qwen3 (`ErrorDiagnosis`, via `TaskType.ERROR_ANALYSIS`) never
      produces file contents -- `ErrorDiagnosis` has no field that could
      hold one. It can only describe the problem in prose.
    * Qwen2.5-Coder (`DebuggingWorker`, via `TaskType.DEBUGGING`) is the
      only thing here that calls `TaskType.CODING`'s sibling,
      `TaskType.DEBUGGING`, and it never runs standalone -- it is always
      instantiated as a worker *inside* a `BaseManagerAgent`, whose
      analyze -> select -> delegate -> REVIEW loop is exactly the
      "manager review" gate the spec requires before a fix ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from app.agents.managers.base import BaseManagerAgent
from app.agents.managers.schemas import ManagerContext, ManagerReportStatus
from app.agents.workers.base import BaseWorkerAgent
from app.agents.workers.schemas import WorkerImplementationOutput, WorkerPlan
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.memory.service import MemoryService, MemoryType
from app.realtime.emitter import RealtimeEmitter
from app.realtime.schemas import RealtimeEventType
from app.self_healing.schemas import ErrorDiagnosis, RepairAttempt, RepairOutcome, SelfHealingResult
from app.state.enums import ErrorSeverity, EventLevel, TaskComplexity
from app.state.models import AHSEAState, ErrorRecord, ProjectEvent, Task, TaskResult
from app.state.operations import (
    add_error,
    add_event,
    add_task,
    mark_task_completed,
    mark_task_failed,
)

#: Safety cap on repair cycles for a single failure. After this many
#: attempts, `SelfHealingEngine.heal` stops trying and escalates -- this
#: is the only thing standing between a stubborn bug and an infinite
#: diagnose/fix/fail loop, so it is enforced unconditionally, never
#: overridable per-call.
MAX_REPAIR_ATTEMPTS = 3


class ManagerFactory(Protocol):
    def __call__(self, team_name: str) -> BaseManagerAgent | None: ...


class DebuggingWorker(BaseWorkerAgent):
    """The one worker type self-healing uses: implements via `TaskType.DEBUGGING`
    (Qwen2.5-Coder) instead of the default `TaskType.CODING`.

    Everything else -- PLAN, TEST, SELF REVIEW, scope enforcement -- is
    inherited unchanged from `BaseWorkerAgent`, so a debugging fix is
    scoped, tested, and self-reviewed exactly like an ordinary worker's
    implementation.
    """

    worker_type = "debug_worker"
    capabilities = ["debugging"]
    role_description = (
        "Diagnoses and fixes a specific reported failure. Does not perform "
        "unrelated refactors or architecture changes."
    )

    async def _implement(
        self,
        task: Task,
        task_summary: str,
        context_text: str,
        plan: WorkerPlan,
        metadata: dict[str, Any] | None,
    ) -> WorkerImplementationOutput:
        prompt = (
            f"{self._charter()}\n\n{task_summary}\n\nContext:\n{context_text}\n\n"
            f"Debugging plan:\nApproach: {plan.approach}\n"
            f"Steps: {'; '.join(plan.steps)}\n"
            f"Target files: {', '.join(plan.target_files)}\n\n"
            "Produce the minimal fix that resolves the described failure. Return the "
            "full content for any new file (action='create') or a unique old_str/new_str "
            "pair for a targeted edit to an existing file (action='edit')."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.DEBUGGING,
            prompt=prompt,
            response_model=WorkerImplementationOutput,
            metadata=metadata,
        )


_DIAGNOSIS_CHARTER = """\
You are the error-analysis stage of AHSEA's self-healing system. You \
diagnose failures, classify them, identify which team is responsible, \
and propose a solution in plain language. You never write code or file \
contents -- only a description of what should change and why.
"""


class SelfHealingEngine:
    """Diagnoses a failure and drives repair attempts up to `MAX_REPAIR_ATTEMPTS`."""

    def __init__(
        self,
        gateway: LLMGateway,
        manager_factory: ManagerFactory,
        max_attempts: int = MAX_REPAIR_ATTEMPTS,
        realtime: RealtimeEmitter | None = None,
        memory_service: MemoryService | None = None,
    ):
        self.gateway = gateway
        self.manager_factory = manager_factory
        self.max_attempts = max_attempts
        self.realtime = realtime
        #: Phase 22 wiring: when given, `diagnose()` is informed by memory
        #: of past failures/repairs on this project (e.g. "we already
        #: tried X here and it didn't work"), and `heal()` writes each
        #: terminal outcome (a successful repair or an escalation) back as
        #: memory so later failures -- in this run or a future one -- can
        #: benefit. `None` by default; failures to read/write memory never
        #: interrupt the repair loop itself.
        self.memory_service = memory_service
        #: task_id -> attempts made so far. Persisted for the engine's
        #: lifetime so repeated calls for the same failure can never
        #: exceed `max_attempts`, even across separate `heal()` calls.
        self._history: dict[str, list[RepairAttempt]] = {}

    def attempts_for(self, task_id: str) -> list[RepairAttempt]:
        return list(self._history.get(task_id, []))

    # ------------------------------------------------------------------
    # Qwen3: understand failure, diagnose root cause, identify team, propose solution
    # ------------------------------------------------------------------

    async def diagnose(
        self,
        error_message: str,
        metadata: dict[str, Any] | None = None,
        *,
        project_id: str | None = None,
    ) -> ErrorDiagnosis:
        memory_section = ""
        if self.memory_service is not None and project_id:
            try:
                memory_context = await self.memory_service.context_for_prompt(
                    project_id,
                    error_message,
                    limit=5,
                    memory_types=[MemoryType.FAILURE, MemoryType.REPAIR],
                )
            except Exception:  # noqa: BLE001 - memory is an enrichment, never fatal
                memory_context = ""
            if memory_context:
                memory_section = f"\n\n{memory_context}"
        prompt = (
            f"{_DIAGNOSIS_CHARTER}\n\nFailure description:\n{error_message}{memory_section}\n\n"
            "Classify this failure, identify the root cause, name the responsible team "
            "(e.g. 'Backend', 'Frontend', 'Database', 'AI', 'QA', 'Deployment'), and "
            "propose a solution in plain language."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.ERROR_ANALYSIS,
            prompt=prompt,
            response_model=ErrorDiagnosis,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def _escalate(
        self, state: AHSEAState, task: Task, attempts: list[RepairAttempt], reason: str
    ) -> SelfHealingResult:
        add_error(
            state,
            ErrorRecord(
                severity=ErrorSeverity.CRITICAL,
                source="SelfHealingEngine",
                task_id=task.task_id,
                message=f"ESCALATE_TO_HUMAN: {reason}",
            ),
        )
        add_event(
            state,
            ProjectEvent(
                level=EventLevel.CRITICAL,
                message=f"Self-healing escalated task '{task.task_id}' to a human.",
                data={"task_id": task.task_id, "attempts": len(attempts), "reason": reason},
            ),
        )
        return SelfHealingResult(
            task_id=task.task_id,
            outcome=RepairOutcome.ESCALATED,
            attempts=attempts,
            escalation_reason=reason,
        )

    # ------------------------------------------------------------------
    # Core repair loop
    # ------------------------------------------------------------------

    async def heal(
        self,
        state: AHSEAState,
        task: Task,
        error_message: str,
        context_builder: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> SelfHealingResult:
        """Diagnose `error_message` and drive repair attempts to a terminal outcome.

        `context_builder`, if given, is `Callable[[ErrorDiagnosis], ManagerContext]`
        for callers that need to scope context beyond the default (team
        name + root cause summary). Never exceeds `MAX_REPAIR_ATTEMPTS`
        attempts across the lifetime of this engine for a given task_id --
        this is what prevents an infinite diagnose/fix/fail loop.
        """

        attempts = self._history.setdefault(task.task_id, [])

        if len(attempts) >= self.max_attempts:
            return self._escalate(
                state,
                task,
                attempts,
                reason=f"Already exhausted MAX_REPAIR_ATTEMPTS ({self.max_attempts}).",
            )

        current_error = error_message

        while len(attempts) < self.max_attempts:
            attempt_number = len(attempts) + 1
            attempt = RepairAttempt(task_id=task.task_id, attempt_number=attempt_number)

            if self.realtime is not None:
                await self.realtime.emit(
                    RealtimeEventType.REPAIR_STARTED,
                    task_id=task.task_id,
                    payload={"attempt_number": attempt_number, "error_message": current_error},
                )

            try:
                diagnosis = await self.diagnose(
                    current_error, metadata, project_id=state.project.project_id
                )
            except LLMError as exc:
                attempt.outcome = RepairOutcome.FAILED
                attempt.detail = f"Diagnosis failed: {exc}"
                attempt.completed_at = datetime.now(UTC)
                attempts.append(attempt)
                current_error = attempt.detail
                continue

            attempt.diagnosis = diagnosis

            manager = self.manager_factory(diagnosis.responsible_team)
            if manager is None:
                attempt.outcome = RepairOutcome.FAILED
                attempt.detail = f"No manager registered for team '{diagnosis.responsible_team}'."
                attempt.completed_at = datetime.now(UTC)
                attempts.append(attempt)
                current_error = attempt.detail
                continue

            rework_task = Task(
                title=f"Self-healing repair (attempt {attempt_number}): {task.title}",
                description=(
                    f"Root cause: {diagnosis.root_cause}\n"
                    f"Classification: {diagnosis.classification}\n"
                    f"Proposed solution: {diagnosis.proposed_solution}"
                ),
                owner_manager=diagnosis.responsible_team,
                complexity=TaskComplexity.MEDIUM,
                expected_outputs=[diagnosis.proposed_solution],
            )
            add_task(state, rework_task)
            attempt.rework_task_id = rework_task.task_id

            context = (
                context_builder(diagnosis)
                if context_builder is not None
                else ManagerContext(
                    team_name=diagnosis.responsible_team,
                    global_summary=(
                        f"Self-healing repair. Root cause: {diagnosis.root_cause}. "
                        f"Proposed solution: {diagnosis.proposed_solution}"
                    ),
                )
            )

            report = await manager.handle_task(rework_task, context, metadata=metadata)

            if report.status == ManagerReportStatus.ACCEPTED:
                mark_task_completed(
                    state,
                    rework_task.task_id,
                    result=TaskResult(
                        task_id=rework_task.task_id, success=True, summary=report.summary
                    ),
                )
                attempt.outcome = RepairOutcome.SUCCESS
                attempt.detail = report.summary
                attempt.completed_at = datetime.now(UTC)
                attempts.append(attempt)
                add_event(
                    state,
                    ProjectEvent(
                        level=EventLevel.INFO,
                        message=(
                            f"Self-healing repaired task '{task.task_id}' on "
                            f"attempt {attempt_number}."
                        ),
                        data={"task_id": task.task_id, "rework_task_id": rework_task.task_id},
                    ),
                )
                if self.realtime is not None:
                    await self.realtime.emit(
                        RealtimeEventType.REPAIR_COMPLETED,
                        agent_id=diagnosis.responsible_team,
                        task_id=task.task_id,
                        payload={
                            "attempt_number": attempt_number,
                            "rework_task_id": rework_task.task_id,
                            "summary": report.summary,
                        },
                    )
                await self._remember(
                    state,
                    MemoryType.REPAIR,
                    title=f"Repaired: {task.title}",
                    content=(
                        f"Root cause: {diagnosis.root_cause}. Fix: {report.summary}"
                    ),
                    tags=[diagnosis.responsible_team, "self_healing"],
                    importance=0.6,
                )
                return SelfHealingResult(
                    task_id=task.task_id, outcome=RepairOutcome.SUCCESS, attempts=attempts
                )

            failure_detail = "; ".join(report.errors) or f"Manager status: {report.status.value}"
            mark_task_failed(state, rework_task.task_id, error_message=failure_detail, retry=False)
            attempt.outcome = RepairOutcome.FAILED
            attempt.detail = failure_detail
            attempt.completed_at = datetime.now(UTC)
            attempts.append(attempt)
            current_error = failure_detail
            # loop: repeat diagnose -> rework -> test with the new failure detail

        await self._remember(
            state,
            MemoryType.FAILURE,
            title=f"Escalated to human: {task.title}",
            content=(
                f"Exceeded {self.max_attempts} repair attempts. Last error: {current_error}"
            ),
            tags=["self_healing", "escalated"],
            importance=0.8,
        )
        return self._escalate(
            state,
            task,
            attempts,
            reason=f"Exceeded MAX_REPAIR_ATTEMPTS ({self.max_attempts}) without a successful fix.",
        )

    async def _remember(
        self,
        state: AHSEAState,
        memory_type: MemoryType,
        *,
        title: str,
        content: str,
        tags: list[str],
        importance: float,
    ) -> None:
        """Best-effort memory write -- never lets a memory failure break healing."""
        if self.memory_service is None:
            return
        try:
            await self.memory_service.store(
                state.project.project_id,
                memory_type,
                title=title,
                content=content,
                tags=tags,
                importance=importance,
            )
        except Exception:  # noqa: BLE001 - memory is an enrichment, never fatal
            pass
