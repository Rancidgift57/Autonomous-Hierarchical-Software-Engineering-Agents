"""`BaseWorkerAgent` (Phase 8).

Workflow (exactly as specified): READ TASK -> READ CONTEXT -> PLAN ->
IMPLEMENT -> TEST -> SELF REVIEW -> SUBMIT.

Routing:
    * PLAN uses `TaskType.PLANNING` -> routed by the gateway to Qwen3.
    * IMPLEMENT uses `TaskType.CODING` -> routed to Qwen2.5-Coder.
    * SELF REVIEW uses `TaskType.CODE_REVIEW` -> also routed to
      Qwen2.5-Coder (it's judging code, same model family as wrote it).

A worker never talks to `LLMProvider`/Ollama directly and never passes a
model name anywhere -- only `TaskType` values go to `self.gateway`. All
filesystem/shell/git work goes through `self.tools` (a `ToolExecutor`,
Phase 9): a worker holds no `WorkspaceSandbox` or raw path of its own.

Qwen2.5-Coder's proposed file changes and any command text embedded in its
output are treated as *untrusted suggestions*: they are validated (against
`WorkerScope` here, and again against the sandbox/command-allowlist inside
the Phase 9 tools) before anything is written or executed. A worker cannot
bypass that validation by, say, writing raw file bytes itself -- `_run`
only ever reaches the filesystem through `self.tools.run(...)`.
"""

from __future__ import annotations

import abc
from typing import Any

from app.agents.workers.schemas import (
    WorkerContext,
    WorkerFileChange,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerResult,
    WorkerScope,
    WorkerSelfReview,
    WorkerStatus,
)
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.state.models import Task
from app.tools.base import ToolExecutor
from app.tools.exceptions import ToolError


class WorkerScopeError(Exception):
    """Raised when a proposed file change violates the worker's `WorkerScope`."""


_CHARTER_TEMPLATE = """\
You are a {worker_type} on an autonomous software engineering system \
called AHSEA. Your job is to implement exactly the assigned task -- \
nothing broader. {role_description}

You MUST stay within the files relevant to this task. Do not modify \
unrelated modules, do not invent new requirements, and do not perform \
destructive operations unless the task explicitly calls for them.
"""


class BaseWorkerAgent(abc.ABC):  # noqa: B024 -- intentionally non-abstract; see BaseManagerAgent
    """Executes a single `Task` end-to-end via the READ->PLAN->...->SUBMIT workflow."""

    #: Identifies the kind of worker (matches `worker_type` on `Task` /
    #: `config/agents.yaml` entries), e.g. "api_worker".
    worker_type: str = "worker"
    capabilities: list[str] = []
    role_description: str = ""

    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolExecutor,
        agent_id: str,
        scope: WorkerScope | None = None,
    ):
        self.gateway = gateway
        self.tools = tools
        self.agent_id = agent_id
        self.scope = scope or WorkerScope()

    # ------------------------------------------------------------------
    # Charter / prompt scaffolding (subclasses may override `role_description`)
    # ------------------------------------------------------------------

    def _charter(self) -> str:
        return _CHARTER_TEMPLATE.format(
            worker_type=self.worker_type, role_description=self.role_description
        )

    @staticmethod
    def _format_context(context: WorkerContext) -> str:
        parts = [
            f"Module summary: {context.module_summary}" if context.module_summary else "",
            (
                "Related requirements:\n"
                + "\n".join(f"- {r}" for r in context.related_requirements)
                if context.related_requirements
                else ""
            ),
            (
                "Relevant existing files:\n"
                + "\n".join(f"- {f}" for f in context.relevant_files)
                if context.relevant_files
                else ""
            ),
            f"Team notes: {context.team_notes}" if context.team_notes else "",
            (
                f"Manager instructions: {context.manager_instructions}"
                if context.manager_instructions
                else ""
            ),
        ]
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Stage 1: READ TASK
    # ------------------------------------------------------------------

    def _read_task(self, task: Task) -> str:
        """Validate and summarize the task. Purely local -- no LLM/tool call."""

        if not task.title or not task.description:
            raise WorkerScopeError(f"Task '{task.task_id}' is missing a title/description.")
        return (
            f"Task: {task.title}\n"
            f"Description: {task.description}\n"
            f"Expected outputs: {', '.join(task.expected_outputs) or 'unspecified'}"
        )

    # ------------------------------------------------------------------
    # Stage 2: READ CONTEXT
    # ------------------------------------------------------------------

    def _read_context(self, context: WorkerContext) -> str:
        """Format the (already-scoped) context handed to this worker."""

        return self._format_context(context)

    # ------------------------------------------------------------------
    # Stage 3: PLAN (Qwen3, via TaskType.PLANNING)
    # ------------------------------------------------------------------

    async def _plan(
        self, task: Task, task_summary: str, context_text: str, metadata: dict[str, Any] | None
    ) -> WorkerPlan:
        prompt = (
            f"{self._charter()}\n\n{task_summary}\n\nContext:\n{context_text}\n\n"
            "Produce a short implementation plan: an overall approach, a list of "
            "concrete steps, and the repository-relative file paths you expect to "
            "touch. Do not write code yet."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.PLANNING,
            prompt=prompt,
            response_model=WorkerPlan,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Stage 4: IMPLEMENT (Qwen2.5-Coder, via TaskType.CODING)
    # ------------------------------------------------------------------

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
            f"Approved plan:\nApproach: {plan.approach}\n"
            f"Steps: {'; '.join(plan.steps)}\n"
            f"Target files: {', '.join(plan.target_files)}\n\n"
            "Implement the plan. Return the full content for any new file "
            "(action='create') or a unique old_str/new_str pair for a targeted "
            "edit to an existing file (action='edit'). Only touch files relevant "
            "to this task."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.CODING,
            prompt=prompt,
            response_model=WorkerImplementationOutput,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Scope enforcement + applying changes via the Phase 9 tool system
    # ------------------------------------------------------------------

    def _validate_scope(self, files: list[WorkerFileChange]) -> None:
        if len(files) > self.scope.max_files_changed:
            raise WorkerScopeError(
                f"Implementation touches {len(files)} files, exceeding the "
                f"{self.scope.max_files_changed}-file scope limit for this worker."
            )
        for change in files:
            if self.scope.allowed_path_prefixes and not any(
                change.path.startswith(prefix) for prefix in self.scope.allowed_path_prefixes
            ):
                raise WorkerScopeError(
                    f"Path '{change.path}' is outside this worker's allowed scope "
                    f"({self.scope.allowed_path_prefixes})."
                )
            if any(change.path.startswith(bad) for bad in self.scope.forbidden_paths):
                raise WorkerScopeError(f"Path '{change.path}' is explicitly forbidden.")

    async def _apply_changes(self, files: list[WorkerFileChange]) -> list[str]:
        """Apply each file change through `self.tools`. Never touches disk directly."""

        self._validate_scope(files)

        changed: list[str] = []
        for change in files:
            if change.action == "create":
                result = await self.tools.run(
                    "write_file", path=change.path, content=change.content or ""
                )
            elif change.action == "edit":
                result = await self.tools.run(
                    "edit_file",
                    path=change.path,
                    old_str=change.old_str or "",
                    new_str=change.new_str or "",
                )
            elif change.action == "delete":
                result = await self.tools.run("delete_file", path=change.path)
            else:  # pragma: no cover - blocked by pydantic Literal validation
                raise WorkerScopeError(f"Unknown file action: {change.action!r}")

            if result.success:
                changed.append(change.path)
        return changed

    # ------------------------------------------------------------------
    # Stage 5: TEST
    # ------------------------------------------------------------------

    async def _test(self) -> dict[str, Any]:
        try:
            result = await self.tools.run("run_pytest", path=self.scope.test_path)
        except ToolError as exc:
            return {"success": False, "output": str(exc), "ran": False}
        stdout = ""
        if isinstance(result.output, dict):
            stdout = result.output.get("stdout", "")
        return {"success": result.success, "output": stdout, "ran": True}

    # ------------------------------------------------------------------
    # Stage 6: SELF REVIEW (coder model, via TaskType.CODE_REVIEW)
    # ------------------------------------------------------------------

    async def _self_review(
        self,
        task: Task,
        implementation: WorkerImplementationOutput,
        test_output: dict[str, Any],
        metadata: dict[str, Any] | None,
    ) -> WorkerSelfReview:
        prompt = (
            f"{self._charter()}\n\nTask: {task.title}\n\n"
            f"Implementation summary: {implementation.summary}\n"
            f"Files touched: {', '.join(f.path for f in implementation.files)}\n"
            f"Test run success: {test_output.get('success')}\n"
            f"Test output (truncated): {str(test_output.get('output', ''))[:1000]}\n\n"
            "Review your own implementation against the task. Decide 'pass' if it "
            "satisfies the task and tests are healthy, otherwise 'needs_fix' with "
            "concrete issues."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.CODE_REVIEW,
            prompt=prompt,
            response_model=WorkerSelfReview,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Stage 7: SUBMIT (public entry point)
    # ------------------------------------------------------------------

    async def run(
        self,
        task: Task,
        context: WorkerContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkerResult:
        """Run the full READ->PLAN->IMPLEMENT->TEST->SELF REVIEW->SUBMIT workflow."""

        context = context or WorkerContext()
        errors: list[str] = []
        files_changed: list[str] = []
        tests: dict[str, Any] = {}
        summary = ""

        try:
            task_summary = self._read_task(task)
            context_text = self._read_context(context)

            plan = await self._plan(task, task_summary, context_text, metadata)
            implementation = await self._implement(
                task, task_summary, context_text, plan, metadata
            )
            files_changed = await self._apply_changes(implementation.files)
            tests = await self._test()
            review = await self._self_review(task, implementation, tests, metadata)

            summary = implementation.summary
            if review.decision == "pass" and (not tests.get("ran") or tests.get("success")):
                status = WorkerStatus.SUCCESS
            else:
                status = WorkerStatus.PARTIAL
                errors.extend(review.issues)

        except (WorkerScopeError, LLMError, ToolError) as exc:
            errors.append(str(exc))
            status = WorkerStatus.FAILED
            summary = summary or f"Task failed during execution: {exc}"

        return WorkerResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            status=status,
            summary=summary,
            files_changed=files_changed,
            artifacts=list(files_changed),
            tests=tests,
            errors=errors,
        )
