"""Git tools (Phase 9, extended in Phase 14): git_status, git_diff,
git_current_branch, git_branch, git_checkout, git_add, git_commit, git_merge.

`git_status`/`git_diff` are read-only (`Permission.READ`); the mutating
operations require `Permission.GIT`, a separate level from `WRITE` so an
agent can be given filesystem write access without also being able to
change branches or create commits (and vice versa).

Every git invocation is a fixed argv built here -- callers never supply an
arbitrary git command string -- and always runs with `cwd` pinned to the
sandbox root via `git -C <root>`, so a caller cannot point git at a
repository outside the workspace.
"""

from __future__ import annotations

import re

from app.tools.base import BaseTool, ToolContext, ToolResult
from app.tools.exceptions import GitOperationError, PathValidationError
from app.tools.permissions import Permission
from app.tools.shell import _run_subprocess

_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_MAX_COMMIT_MESSAGE_LEN = 2000


def _validate_branch_name(name: str) -> None:
    if not _BRANCH_NAME_RE.match(name) or name.startswith("-") or ".." in name:
        raise GitOperationError(f"Invalid or unsafe branch name: {name!r}.")


def _validate_ref(ref: str) -> None:
    """A ref may be a branch name, or `branch1..branch2` / `branch1...branch2`.
    Rejects anything that could smuggle extra argv (e.g. a leading `-`)."""

    parts = re.split(r"\.{2,3}", ref, maxsplit=1)
    for part in parts:
        if part and not _BRANCH_NAME_RE.match(part):
            raise GitOperationError(f"Invalid or unsafe git ref: {ref!r}.")
    if ref.startswith("-"):
        raise GitOperationError(f"Invalid or unsafe git ref: {ref!r}.")


async def _git(ctx: ToolContext, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    argv = ["git", "-C", str(ctx.sandbox.root), *args]
    return await _run_subprocess(argv, cwd=str(ctx.sandbox.root), timeout=timeout)


class GitStatusTool(BaseTool):
    name = "git_status"
    required_permission = Permission.READ
    default_timeout = 15.0

    async def _run(self, ctx: ToolContext, **_: object) -> ToolResult:
        returncode, stdout, stderr = await _git(ctx, "status", "--porcelain=v1", "-b")
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitDiffTool(BaseTool):
    name = "git_diff"
    required_permission = Permission.READ
    default_timeout = 15.0

    async def _run(
        self,
        ctx: ToolContext,
        path: str | None = None,
        ref: str | None = None,
        name_only: bool = False,
        **_: object,
    ) -> ToolResult:
        args = ["diff"]
        if name_only:
            args.append("--name-only")
        if ref:
            _validate_ref(ref)
            args.append(ref)
        if path:
            ctx.sandbox.resolve(path)  # validate, doesn't need to exist (may be deleted)
            args.append("--")
            args.append(path)
        returncode, stdout, stderr = await _git(ctx, *args)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitCurrentBranchTool(BaseTool):
    """Read-only: the branch currently checked out (`git rev-parse --abbrev-ref HEAD`)."""

    name = "git_current_branch"
    required_permission = Permission.READ
    default_timeout = 15.0

    async def _run(self, ctx: ToolContext, **_: object) -> ToolResult:
        returncode, stdout, stderr = await _git(ctx, "rev-parse", "--abbrev-ref", "HEAD")
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout.strip(),
            error=None if returncode == 0 else stderr,
        )


class GitBranchTool(BaseTool):
    """Create (or list) local branches. Never force-deletes or pushes."""

    name = "git_branch"
    required_permission = Permission.GIT
    default_timeout = 15.0

    async def _run(
        self, ctx: ToolContext, branch_name: str | None = None, **_: object
    ) -> ToolResult:
        if branch_name is None:
            returncode, stdout, stderr = await _git(ctx, "branch", "--list")
        else:
            _validate_branch_name(branch_name)
            returncode, stdout, stderr = await _git(ctx, "branch", branch_name)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitCheckoutTool(BaseTool):
    """Switch to an existing local branch, or create+switch with `create=True`."""

    name = "git_checkout"
    required_permission = Permission.GIT
    default_timeout = 15.0

    async def _run(
        self, ctx: ToolContext, branch_name: str, create: bool = False, **_: object
    ) -> ToolResult:
        _validate_branch_name(branch_name)
        args = ["checkout", "-b", branch_name] if create else ["checkout", branch_name]
        returncode, stdout, stderr = await _git(ctx, *args)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitAddTool(BaseTool):
    name = "git_add"
    required_permission = Permission.GIT
    default_timeout = 15.0

    async def _run(self, ctx: ToolContext, paths: list[str], **_: object) -> ToolResult:
        if not paths:
            raise GitOperationError("git_add requires at least one path.")
        for path in paths:
            try:
                ctx.sandbox.resolve(path)
            except PathValidationError as exc:
                raise GitOperationError(str(exc)) from exc
        returncode, stdout, stderr = await _git(ctx, "add", "--", *paths)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitCommitTool(BaseTool):
    name = "git_commit"
    required_permission = Permission.GIT
    default_timeout = 15.0

    async def _run(self, ctx: ToolContext, message: str, **_: object) -> ToolResult:
        if not message or not message.strip():
            raise GitOperationError("Commit message must not be empty.")
        if len(message) > _MAX_COMMIT_MESSAGE_LEN:
            raise GitOperationError(f"Commit message exceeds {_MAX_COMMIT_MESSAGE_LEN} characters.")
        returncode, stdout, stderr = await _git(ctx, "commit", "-m", message)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


class GitMergeTool(BaseTool):
    """Merge `branch_name` into the currently checked-out branch.

    Always `--no-ff` by default (a real merge commit, never a silent
    fast-forward that could quietly rewrite the target branch's tip) and
    never passes `-X ours`/`-X theirs` or any force flag -- a real
    conflict simply fails the tool call (`success=False`), exactly like
    running `git merge` by hand. Callers (see `app.git_workflow`) are
    responsible for verifying the target branch is clean *before*
    invoking this, since git itself will refuse to merge onto an unclean
    working tree.
    """

    name = "git_merge"
    required_permission = Permission.GIT
    default_timeout = 30.0

    async def _run(
        self,
        ctx: ToolContext,
        branch_name: str,
        message: str | None = None,
        no_ff: bool = True,
        **_: object,
    ) -> ToolResult:
        _validate_branch_name(branch_name)
        args = ["merge"]
        if no_ff:
            args.append("--no-ff")
        if message:
            if len(message) > _MAX_COMMIT_MESSAGE_LEN:
                raise GitOperationError(
                    f"Merge message exceeds {_MAX_COMMIT_MESSAGE_LEN} characters."
                )
            args += ["-m", message]
        else:
            args.append("--no-edit")
        args.append(branch_name)
        returncode, stdout, stderr = await _git(ctx, *args)
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output=stdout,
            error=None if returncode == 0 else stderr,
        )


def build_git_tools() -> list[BaseTool]:
    return [
        GitStatusTool(),
        GitDiffTool(),
        GitCurrentBranchTool(),
        GitBranchTool(),
        GitCheckoutTool(),
        GitAddTool(),
        GitCommitTool(),
        GitMergeTool(),
    ]
