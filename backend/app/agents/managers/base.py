"""`BaseManagerAgent` (Phase 7).

Workflow (exactly as specified): receive task -> inspect context ->
analyze -> select worker -> delegate -> review -> accept/rework -> update
state -> report upward.

Hard constraints, and how this module enforces each one structurally
rather than just by convention:

    * "directly call models" / "directly select Qwen3" / "directly select
      Qwen2.5-Coder" / "bypass the gateway": `BaseManagerAgent` holds only
      `self.gateway: LLMGateway` and only ever calls
      `self.gateway.generate_json(task_type=TaskType.MANAGEMENT, ...)`.
      There is no code path here that references a model name, an
      `LLMProvider`, or Ollama's HTTP API -- the class doesn't import them.
    * "execute unrestricted shell commands": a manager's own tool access
      (`self.tools`, optional) is a `ToolExecutor` built by the caller with
      whatever permission set it chooses to grant -- `make_manager_executor`
      (see `app.agents.managers.registry`) grants `READ` only, so a manager
      has no path to `run_command`/`run_pytest`/etc. even if it wanted one.
      Managers never build or run shell commands themselves.
    * "access irrelevant context": a manager is only ever given a
      `ManagerContext` (its own team's slice), never the raw `AHSEAState`.
      There is no attribute on this class that could hold full state.
    * Delegation to a worker is a plain Python call
      (`await worker.run(...)`, Phase 8) -- not an LLM call -- so it does
      not go through `self.gateway` and does not need a `task_type`.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from typing import Any, Protocol

from app.agents.managers.schemas import (
    ManagerAnalysis,
    ManagerContext,
    ManagerReport,
    ManagerReportStatus,
    ManagerReviewDecision,
    WorkerSelection,
)
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.state.models import Task
from app.tools.base import ToolExecutor
from app.tools.exceptions import ToolError


class DelegatableWorker(Protocol):
    """Structural type for whatever `worker_factory` hands back.

    Matches `app.agents.workers.base.BaseWorkerAgent.run` without importing
    it, so managers depend only on the shape they need.
    """

    agent_id: str

    async def run(self, task: Task, context: Any, metadata: dict[str, Any] | None = None) -> Any:
        ...


#: A callable that builds a ready-to-use worker instance for a given
#: worker_type string. Concrete wiring lives in
#: `app.agents.managers.registry` so `BaseManagerAgent` doesn't need to
#: import every Phase 8 worker class itself.
WorkerFactory = Callable[[str], DelegatableWorker]


_CHARTER_TEMPLATE = """\
You are the {team_name} Manager on an autonomous software engineering \
system called AHSEA. {role_description}

You coordinate a fixed set of workers: {managed_worker_types}. You do not \
write code yourself -- you analyze the task, select the right worker, \
delegate with clear instructions, and review what comes back.
"""


class BaseManagerAgent(abc.ABC):  # noqa: B024 -- intentionally non-abstract, see below
    """Owns the receive->...->report-upward workflow for one team's tasks."""

    #: Team this manager owns, e.g. "Backend". Matches `Task.owner_manager`
    #: / `AgentDefinition.team_name`.
    team_name: str = "unassigned"
    #: worker_type values this manager may delegate to. Empty means this
    #: manager does not delegate to Phase 8 workers at all -- subclasses in
    #: that situation must override `_direct_execution`.
    managed_worker_types: list[str] = []
    role_description: str = ""
    max_rework_cycles: int = 2

    # No abstract methods: every workflow stage has a sensible default
    # implementation driven entirely by class attributes
    # (team_name/managed_worker_types/role_description). Subclasses are
    # "abstract" only in the sense that instantiating the base class
    # directly is meaningless (team_name="unassigned"), not because any
    # method requires overriding.

    def __init__(
        self,
        gateway: LLMGateway,
        manager_id: str,
        worker_factory: WorkerFactory | None = None,
        tools: ToolExecutor | None = None,
    ):
        self.gateway = gateway
        self.manager_id = manager_id
        self.worker_factory = worker_factory
        #: Optional, deliberately least-privilege tool access (see class
        #: docstring) -- most managers never need this at all.
        self.tools = tools

    # ------------------------------------------------------------------
    # Charter / prompt scaffolding
    # ------------------------------------------------------------------

    def _charter(self) -> str:
        return _CHARTER_TEMPLATE.format(
            team_name=self.team_name,
            role_description=self.role_description,
            managed_worker_types=(
                ", ".join(self.managed_worker_types) or "(none -- direct execution)"
            ),
        )

    # ------------------------------------------------------------------
    # Stage 1: RECEIVE TASK / INSPECT CONTEXT
    # ------------------------------------------------------------------

    def _receive_task(self, task: Task) -> str:
        if not task.title or not task.description:
            raise ValueError(f"Task '{task.task_id}' is missing a title/description.")
        return f"Task: {task.title}\nDescription: {task.description}"

    def _inspect_context(self, context: ManagerContext) -> str:
        """Format the (already team-scoped) `ManagerContext` for prompts.

        This is the only place manager code reads project state -- it never
        reaches past what `context` was constructed with.
        """

        if context.team_name != self.team_name:
            raise ValueError(
                f"Manager for team '{self.team_name}' was given context for "
                f"team '{context.team_name}'."
            )

        parts = [
            f"Global summary: {context.global_summary}" if context.global_summary else "",
            f"Team context: {context.team_context}" if context.team_context else "",
            (
                "Relevant artifacts:\n" + "\n".join(f"- {a}" for a in context.relevant_artifacts)
                if context.relevant_artifacts
                else ""
            ),
        ]
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Stage 2: ANALYZE (Qwen3, via TaskType.MANAGEMENT)
    # ------------------------------------------------------------------

    async def _analyze(
        self, task_summary: str, context_text: str, metadata: dict[str, Any] | None
    ) -> ManagerAnalysis:
        prompt = (
            f"{self._charter()}\n\n{task_summary}\n\nContext:\n{context_text}\n\n"
            "Analyze this task: summarize what needs to happen, list key "
            "considerations, and flag any risks. Do not write code."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.MANAGEMENT,
            prompt=prompt,
            response_model=ManagerAnalysis,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Stage 3: SELECT WORKER (Qwen3, via TaskType.MANAGEMENT)
    # ------------------------------------------------------------------

    async def _select_worker(
        self,
        task_summary: str,
        context_text: str,
        analysis: ManagerAnalysis,
        metadata: dict[str, Any] | None,
    ) -> WorkerSelection:
        prompt = (
            f"{self._charter()}\n\n{task_summary}\n\nContext:\n{context_text}\n\n"
            f"Your analysis: {analysis.summary}\n\n"
            f"Choose exactly one worker type from: {self.managed_worker_types}. "
            "Give your rationale and the instructions you want handed down "
            "to that worker."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.MANAGEMENT,
            prompt=prompt,
            response_model=WorkerSelection,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Stage 4: DELEGATE (plain Python call to a Phase 8 worker -- no LLM call here)
    # ------------------------------------------------------------------

    async def _delegate(
        self,
        worker: DelegatableWorker,
        task: Task,
        worker_context: Any,
        metadata: dict[str, Any] | None,
    ) -> Any:
        return await worker.run(task, worker_context, metadata=metadata)

    # ------------------------------------------------------------------
    # Stage 5: REVIEW (Qwen3, via TaskType.MANAGEMENT)
    # ------------------------------------------------------------------

    async def _review(
        self,
        task_summary: str,
        worker_result: Any,
        metadata: dict[str, Any] | None,
    ) -> ManagerReviewDecision:
        prompt = (
            f"{self._charter()}\n\n{task_summary}\n\n"
            f"Worker status: {getattr(worker_result, 'status', 'unknown')}\n"
            f"Worker summary: {getattr(worker_result, 'summary', '')}\n"
            f"Files changed: {getattr(worker_result, 'files_changed', [])}\n"
            f"Test results: {getattr(worker_result, 'tests', {})}\n"
            f"Worker-reported errors: {getattr(worker_result, 'errors', [])}\n\n"
            "Review this result against the task. Decide 'accept' if it "
            "satisfies the task, otherwise 'rework' with concrete feedback "
            "and issues the worker must address."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.MANAGEMENT,
            prompt=prompt,
            response_model=ManagerReviewDecision,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Hook for managers with no delegatable workers (e.g. DeploymentManager)
    # ------------------------------------------------------------------

    async def _direct_execution(
        self, task: Task, context: ManagerContext, analysis: ManagerAnalysis,
        metadata: dict[str, Any] | None,
    ) -> ManagerReport:
        """Default: no workers configured and no override -> nothing to do."""

        return ManagerReport(
            task_id=task.task_id,
            manager_id=self.manager_id,
            team_name=self.team_name,
            status=ManagerReportStatus.NO_ELIGIBLE_WORKER,
            summary=(
                f"Manager '{self.manager_id}' has no managed_worker_types and no "
                "_direct_execution override; nothing was delegated or executed."
            ),
        )

    # ------------------------------------------------------------------
    # Rework loop input: how manager feedback reaches the next attempt.
    # Subclasses may override to shape this differently for their worker's
    # WorkerContext type; default assumes a Phase 8 `WorkerContext`-shaped
    # object with a `manager_instructions` field.
    # ------------------------------------------------------------------

    def _apply_feedback(self, worker_context: Any, review: ManagerReviewDecision) -> Any:
        feedback_text = review.feedback
        if review.issues:
            feedback_text += "\nIssues to fix: " + "; ".join(review.issues)
        if hasattr(worker_context, "model_copy"):
            return worker_context.model_copy(update={"manager_instructions": feedback_text})
        return worker_context

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_task(
        self,
        task: Task,
        context: ManagerContext,
        metadata: dict[str, Any] | None = None,
    ) -> ManagerReport:
        """Run receive->inspect->analyze->select->delegate->review->report."""

        try:
            task_summary = self._receive_task(task)
            context_text = self._inspect_context(context)
            analysis = await self._analyze(task_summary, context_text, metadata)

            if not self.managed_worker_types:
                return await self._direct_execution(task, context, analysis, metadata)

            selection = await self._select_worker(task_summary, context_text, analysis, metadata)

            if selection.selected_worker_type not in self.managed_worker_types:
                # Defence in depth: even though the prompt told the model
                # which worker types are valid, its output is untrusted --
                # never instantiate a worker type we didn't explicitly
                # authorize this manager to use.
                return ManagerReport(
                    task_id=task.task_id,
                    manager_id=self.manager_id,
                    team_name=self.team_name,
                    status=ManagerReportStatus.NO_ELIGIBLE_WORKER,
                    summary=(
                        f"Selected worker type '{selection.selected_worker_type}' is not "
                        f"managed by this manager ({self.managed_worker_types})."
                    ),
                )

            if self.worker_factory is None:
                raise ValueError(
                    f"Manager '{self.manager_id}' has managed_worker_types but no "
                    "worker_factory was provided."
                )

            worker = self.worker_factory(selection.selected_worker_type)
            worker_context = self._build_worker_context(context, selection)

            rework_cycles = 0
            worker_result: Any = None
            review: ManagerReviewDecision | None = None

            while True:
                worker_result = await self._delegate(worker, task, worker_context, metadata)
                review = await self._review(task_summary, worker_result, metadata)
                if review.decision == "accept" or rework_cycles >= self.max_rework_cycles:
                    break
                rework_cycles += 1
                worker_context = self._apply_feedback(worker_context, review)

            status = (
                ManagerReportStatus.ACCEPTED
                if review is not None and review.decision == "accept"
                else ManagerReportStatus.REWORK_EXHAUSTED
            )

            return ManagerReport(
                task_id=task.task_id,
                manager_id=self.manager_id,
                team_name=self.team_name,
                status=status,
                selected_worker_type=selection.selected_worker_type,
                worker_agent_id=getattr(worker, "agent_id", None),
                rework_cycles=rework_cycles,
                summary=getattr(worker_result, "summary", ""),
                files_changed=list(getattr(worker_result, "files_changed", [])),
                artifacts=list(getattr(worker_result, "artifacts", [])),
                errors=(
                    list(getattr(worker_result, "errors", []))
                    + (review.issues if review and review.decision == "rework" else [])
                ),
            )

        except (LLMError, ToolError, ValueError) as exc:
            return ManagerReport(
                task_id=task.task_id,
                manager_id=self.manager_id,
                team_name=self.team_name,
                status=ManagerReportStatus.FAILED,
                errors=[str(exc)],
            )

    # ------------------------------------------------------------------
    # Building a Phase 8 WorkerContext from manager-level ManagerContext
    # ------------------------------------------------------------------

    def _build_worker_context(self, context: ManagerContext, selection: WorkerSelection) -> Any:
        """Translate manager-scoped context into whatever the worker expects.

        Default assumes `app.agents.workers.schemas.WorkerContext`; import
        is deferred to avoid a hard Phase 7 -> Phase 8 module-level
        dependency for managers that override this (e.g. direct-execution
        managers with no workers at all).
        """

        from app.agents.workers.schemas import WorkerContext

        return WorkerContext(
            module_summary=context.global_summary,
            team_notes=str(context.team_context),
            manager_instructions=selection.instructions,
        )
