"""`GitWorkflowEngine` (Phase 14): task -> branch -> worker -> tests -> review
-> integration -> QA -> merge.

Every git operation goes through the Phase 9 `git_*` tools (never raw
`subprocess`/`os` calls here), so the same permission/audit/sandbox
guarantees Phase 9 already provides apply unchanged: this engine only
ever touches git through a caller-supplied `ToolExecutor`, and never
constructs a shell command itself.

Reuse, not reinvention: "tests -> review -> integration -> QA" is exactly
what `app.qa.manager.QAManager.run_pipeline` (Phase 12) already does --
including its own `TaskType.CODE_REVIEW` (Qwen2.5-Coder) step and its
Phase 11 `IntegrationAgent` contract-validation step. This module adds
only what Phase 12 doesn't already cover: the git branch/commit/merge
mechanics, and a second, *architectural* review pass
(`TaskType.REASONING`, Qwen3) distinct from Phase 12's line-level code
review.

Hard constraints, enforced structurally:
    * "Never merge failing work": `_evaluate_gates` is computed strictly
      before `_merge` is ever called, and `_merge` is unreachable unless
      every gate passed.
    * "Never overwrite user changes": before merging, the target branch's
      working tree is checked for uncommitted changes (`git status
      --porcelain`); if it's dirty, the merge is refused outright. Merges
      also never pass `-X ours`/`-X theirs`/force flags (see
      `app.tools.git.GitMergeTool`), so a genuine conflict simply fails
      the merge rather than silently picking a side.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.agents.system.integration_schemas import ContractRegistry
from app.agents.workers.schemas import WorkerScope
from app.git_workflow.schemas import (
    ArchitectureReviewDecision,
    GitWorkflowReport,
    GitWorkflowStage,
    MergeGateCheck,
    is_valid_branch_name,
    is_valid_commit_message,
    make_branch_name,
    make_commit_message,
)
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.qa.manager import QAManager
from app.qa.schemas import QACheckCategory
from app.state.enums import TaskComplexity
from app.state.models import AHSEAState, Task
from app.state.operations import add_task
from app.tools.base import ToolExecutor

_DEFAULT_TARGET_BRANCH = "main"

_ARCHITECTURE_CHARTER = """\
You are performing an architecture-level review for an automated Git \
workflow. You judge structural fit -- does this change belong where it \
was made, does it introduce hidden coupling, does it fit the project's \
existing design -- not line-level style or correctness (a separate \
code-review pass already covers that). You never propose replacement \
code, only a decision and concerns in plain language.
"""


class DelegatableWorker(Protocol):
    agent_id: str

    async def run(
        self, task: Task, context: Any, metadata: dict[str, Any] | None = None
    ) -> Any: ...


def _has_uncommitted_changes(status_porcelain_output: str) -> bool:
    """`git status --porcelain=v1 -b` output: the first line is always the
    `## branch...` header; any further line means the working tree is dirty."""

    lines = [line for line in (status_porcelain_output or "").splitlines() if line.strip()]
    content_lines = [line for line in lines if not line.startswith("##")]
    return bool(content_lines)


class GitWorkflowEngine:
    """Drives one task through the full Git-based development workflow."""

    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolExecutor,
        qa_manager: QAManager,
        target_branch: str = _DEFAULT_TARGET_BRANCH,
    ):
        self.gateway = gateway
        #: Must hold `Permission.GIT` (branch/checkout/add/commit/merge)
        #: and whatever permission the worker itself needs (typically
        #: `WORKER_DEFAULT`) -- this engine never elevates permissions on
        #: the caller's behalf.
        self.tools = tools
        self.qa_manager = qa_manager
        self.target_branch = target_branch

    # ------------------------------------------------------------------
    # Architecture review (Qwen3, via TaskType.REASONING)
    # ------------------------------------------------------------------

    async def _architecture_review(
        self, diff_text: str, summary: str, metadata: dict[str, Any] | None
    ) -> ArchitectureReviewDecision:
        prompt = (
            f"{_ARCHITECTURE_CHARTER}\n\nChange summary: {summary}\n\n"
            f"Diff:\n{diff_text[:8000]}\n\n"
            "Decide 'pass' if this fits the architecture, otherwise 'needs_fix' "
            "with concrete structural concerns."
        )
        try:
            return await self.gateway.generate_json(
                task_type=TaskType.REASONING,
                prompt=prompt,
                response_model=ArchitectureReviewDecision,
                metadata=metadata,
            )
        except LLMError as exc:
            # Fail closed: an unreviewable diff is not a reviewed diff.
            return ArchitectureReviewDecision(
                decision="needs_fix",
                concerns=[f"Architecture review unavailable: {exc}"],
            )

    # ------------------------------------------------------------------
    # Unauthorized-file check
    # ------------------------------------------------------------------

    @staticmethod
    def _unauthorized_files(files: list[str], scope: WorkerScope | None) -> list[str]:
        if scope is None:
            return []
        unauthorized: list[str] = []
        for path in files:
            if any(
                path == forbidden or path.startswith(forbidden.rstrip("/") + "/")
                for forbidden in scope.forbidden_paths
            ):
                unauthorized.append(path)
                continue
            if scope.allowed_path_prefixes and not any(
                path.startswith(prefix) for prefix in scope.allowed_path_prefixes
            ):
                unauthorized.append(path)
        return unauthorized

    # ------------------------------------------------------------------
    # Gate evaluation -- exactly the four checks the spec names
    # ------------------------------------------------------------------

    def _evaluate_gates(
        self,
        qa_report: Any,
        unauthorized_files: list[str],
        architecture_review: ArchitectureReviewDecision,
    ) -> list[MergeGateCheck]:
        checks = qa_report.all_checks

        failed_unit = [
            c for c in checks if c.category == QACheckCategory.UNIT_TEST and not c.passed
        ]
        tests_pass = MergeGateCheck(
            name="tests_pass",
            passed=not failed_unit,
            detail="Unit tests passed."
            if not failed_unit
            else "; ".join(c.message for c in failed_unit),
        )

        failed_integration = [
            c
            for c in checks
            if c.category in (QACheckCategory.INTEGRATION_TEST, QACheckCategory.CONTRACT_VALIDATION)
            and not c.passed
        ]
        integration_passes = MergeGateCheck(
            name="integration_passes",
            passed=not failed_integration,
            detail=(
                "Integration/contract checks passed."
                if not failed_integration
                else "; ".join(c.message for c in failed_integration)
            ),
        )

        no_unauthorized_files = MergeGateCheck(
            name="no_unauthorized_files",
            passed=not unauthorized_files,
            detail=(
                "All changed files are within the worker's authorized scope."
                if not unauthorized_files
                else f"Unauthorized file(s): {', '.join(unauthorized_files)}"
            ),
        )

        failed_code_review = [
            c for c in checks if c.category == QACheckCategory.CODE_REVIEW and not c.passed
        ]
        diff_reviewed = MergeGateCheck(
            name="diff_reviewed",
            passed=not failed_code_review and architecture_review.decision == "pass",
            detail=(
                "Code review and architecture review both passed."
                if not failed_code_review and architecture_review.decision == "pass"
                else "; ".join(
                    [c.message for c in failed_code_review] + architecture_review.concerns
                )
            ),
        )

        return [tests_pass, integration_passes, no_unauthorized_files, diff_reviewed]

    # ------------------------------------------------------------------
    # Rework task on gate failure -- never fixes the code itself
    # ------------------------------------------------------------------

    def _create_rework_task(
        self, state: AHSEAState, task: Task, checks: list[MergeGateCheck]
    ) -> str:
        failed = [c for c in checks if not c.passed]
        rework = Task(
            title=f"Rework: {task.title}",
            description=(
                "Pre-merge gate check(s) failed for the agent branch of this task:\n"
                + "\n".join(f"- [{c.name}] {c.detail}" for c in failed)
            ),
            owner_manager=task.owner_manager,
            complexity=TaskComplexity.MEDIUM,
            expected_outputs=task.expected_outputs,
        )
        add_task(state, rework)
        return rework.task_id

    # ------------------------------------------------------------------
    # Full workflow
    # ------------------------------------------------------------------

    async def run(
        self,
        state: AHSEAState,
        task: Task,
        worker: DelegatableWorker,
        worker_context: Any = None,
        scope: WorkerScope | None = None,
        contract_registry: ContractRegistry | None = None,
        target_branch: str | None = None,
        create_rework_on_failure: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> GitWorkflowReport:
        """task -> branch -> worker -> tests -> review -> integration -> QA -> merge."""

        team = task.owner_manager or "agent"
        target_branch = target_branch or self.target_branch
        branch_name = make_branch_name(team, task.task_id)
        assert is_valid_branch_name(branch_name)  # defence in depth: our own naming must comply

        report = GitWorkflowReport(
            task_id=task.task_id, branch_name=branch_name, stage_reached=GitWorkflowStage.ABORTED
        )

        # 1. branch
        checkout = await self.tools.run("git_checkout", branch_name=branch_name, create=True)
        if not checkout.success:
            report.errors.append(f"Failed to create branch '{branch_name}': {checkout.error}")
            return report
        report.stage_reached = GitWorkflowStage.BRANCH_CREATED

        # 2. worker
        worker_result = await worker.run(task, worker_context, metadata=metadata)
        files_changed = list(getattr(worker_result, "files_changed", None) or [])
        worker_status = str(getattr(worker_result, "status", "")).lower()
        if not files_changed or "failed" in worker_status:
            report.errors.append(
                "Worker produced no file changes or reported failure: "
                + "; ".join(getattr(worker_result, "errors", []) or ["(no changes)"])
            )
            return report
        report.stage_reached = GitWorkflowStage.IMPLEMENTED

        # 3. commit (branch-local; does not touch the target branch)
        add_result = await self.tools.run("git_add", paths=files_changed)
        if not add_result.success:
            report.errors.append(f"git add failed: {add_result.error}")
            return report

        commit_message = make_commit_message(
            team, getattr(worker_result, "summary", None) or task.title
        )
        assert is_valid_commit_message(commit_message)
        commit_result = await self.tools.run("git_commit", message=commit_message)
        if not commit_result.success:
            report.errors.append(f"git commit failed: {commit_result.error}")
            return report
        report.commit_message = commit_message
        report.stage_reached = GitWorkflowStage.COMMITTED

        # Diff of the branch against the target, for review + file-scope checks.
        diff_ref = f"{target_branch}..{branch_name}"
        diff_result = await self.tools.run("git_diff", ref=diff_ref)
        diff_text = diff_result.output if diff_result.success else ""
        name_only_result = await self.tools.run("git_diff", ref=diff_ref, name_only=True)
        diffed_files = (
            [f for f in (name_only_result.output or "").splitlines() if f.strip()]
            if name_only_result.success
            else files_changed
        )

        # 4/5/6. tests -> [code] review -> integration -> QA (Phase 12 pipeline)
        qa_report = await self.qa_manager.run_pipeline(
            state,
            contract_registry=contract_registry,
            code_summary=getattr(worker_result, "summary", "") or "",
            files_changed=diffed_files,
            diff_or_content=diff_text,
            metadata=metadata,
        )
        report.qa_report = qa_report
        report.stage_reached = GitWorkflowStage.TESTED

        # Architecture review: a second, distinct review pass (Qwen3, REASONING).
        architecture_review = await self._architecture_review(
            diff_text, getattr(worker_result, "summary", "") or "", metadata
        )
        report.architecture_review = architecture_review
        report.stage_reached = GitWorkflowStage.REVIEWED

        # No-unauthorized-files check.
        unauthorized_files = self._unauthorized_files(diffed_files, scope)
        report.unauthorized_files = unauthorized_files

        # Integration-validated / QA-validated bookkeeping (informational --
        # the actual gate is `_evaluate_gates`, computed next).
        if not any(
            c.category in (QACheckCategory.INTEGRATION_TEST, QACheckCategory.CONTRACT_VALIDATION)
            and not c.passed
            for c in qa_report.all_checks
        ):
            report.stage_reached = GitWorkflowStage.INTEGRATION_VALIDATED
        if qa_report.gate_passed:
            report.stage_reached = GitWorkflowStage.QA_VALIDATED

        # ------------------------------------------------------------------
        # Pre-merge gate: tests pass, integration passes, no unauthorized
        # files, diff reviewed. Never merge failing work.
        # ------------------------------------------------------------------

        checks = self._evaluate_gates(qa_report, unauthorized_files, architecture_review)
        report.checks = checks

        if not report.all_gates_passed:
            report.stage_reached = GitWorkflowStage.ABORTED
            report.errors.append(
                "One or more merge gates failed; the branch was committed but not merged."
            )
            if create_rework_on_failure:
                report.rework_task_id = self._create_rework_task(state, task, checks)
            return report

        # ------------------------------------------------------------------
        # 7. Merge -- never overwrite user changes.
        # ------------------------------------------------------------------

        checkout_target = await self.tools.run("git_checkout", branch_name=target_branch)
        if not checkout_target.success:
            report.errors.append(
                f"Failed to checkout target branch '{target_branch}': {checkout_target.error}"
            )
            return report

        status_result = await self.tools.run("git_status")
        if status_result.success and _has_uncommitted_changes(status_result.output):
            report.errors.append(
                f"Target branch '{target_branch}' has uncommitted changes; refusing to merge "
                "to avoid overwriting user work."
            )
            return report

        merge_message = f"Merge {branch_name} into {target_branch}: {commit_message}"
        merge_result = await self.tools.run(
            "git_merge", branch_name=branch_name, message=merge_message, no_ff=True
        )
        if not merge_result.success:
            report.errors.append(f"Merge failed: {merge_result.error}")
            return report

        report.merged = True
        report.merge_target = target_branch
        report.stage_reached = GitWorkflowStage.MERGED
        return report
