"""Shell execution tools (Phase 9): run_command, run_pytest, run_lint,
run_typecheck.

Security model
---------------
Commands are never run through a shell (`shell=True` is never used) and
are never built from a single interpolated string -- callers (including an
LLM-authored suggestion routed through a worker agent) must supply an
argv-style list. That list is validated against `COMMAND_ALLOWLIST` before
`asyncio.create_subprocess_exec` ever sees it:

    * the executable (argv[0]) must be an allowlisted program
    * every argument is checked against a denylist of shell metacharacters
      so no argument can smuggle in `;`, `|`, `&&`, backticks, `$()`, I/O
      redirection, or a newline
    * `run_pytest`/`run_lint`/`run_typecheck` don't take a free-form
      command at all -- they build a fixed, safe argv themselves and only
      accept a path/extra-args allowlist on top

Every invocation runs with `cwd` pinned to the workspace sandbox root, a
wall-clock timeout, and (on POSIX) a CPU-time/memory rlimit applied to the
child process before exec.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass

from app.tools.base import BaseTool, ToolContext, ToolResult
from app.tools.exceptions import CommandNotAllowedError, ToolTimeoutError
from app.tools.permissions import Permission

if sys.platform != "win32":  # ``resource`` is POSIX-only.
    import resource

#: program name -> whether bare invocation ("python") is allowed and, if
#: given, the set of allowed first-subcommand tokens (e.g. "python -m
#: pytest" but not "python -c ...").
COMMAND_ALLOWLIST: dict[str, set[str] | None] = {
    "pytest": None,
    "ruff": None,
    "mypy": None,
    "python": {"-m"},
    "python3": {"-m"},
    # Dependency installation is intentionally absent. A model must never
    # alter the dependency graph or execute lifecycle hooks autonomously.
    "npm": {"test"},
    # Smoke tests (Phase 15): simple HTTP reachability/health-endpoint checks
    # against a just-started container.
    "curl": None,
    # NOTE: "docker" is deliberately absent from this allowlist.
    # `validate_command` also backs the generic `run_command` tool, which
    # can be reached with an LLM-suggested/free-form argv. Docker exposes
    # far too much (bind mounts, `--privileged`, arbitrary image pulls) to
    # accept generically. The fixed, hardcoded-argv docker tools in
    # app.tools.docker use their own `validate_docker_command` instead --
    # see that module.
}

_MAX_OUTPUT_BYTES = 200_000
_DANGEROUS_CHARS = re.compile(r"[;&|`$<>\n\r]")


def validate_argument_safety(argv: list[str]) -> None:
    """Reject empty argv, non-string args, oversized args, or shell
    metacharacters. Shared by `validate_command` and
    `app.tools.docker.validate_docker_command` so both entry points apply
    the exact same character-level defenses even though they enforce
    different program/subcommand allowlists.
    """

    if not argv:
        raise CommandNotAllowedError("Empty command.")
    for arg in argv:
        if not isinstance(arg, str):
            raise CommandNotAllowedError("Every command argument must be a string.")
        if len(arg) > 4096:
            raise CommandNotAllowedError("Command argument exceeds the 4096-character limit.")
        if _DANGEROUS_CHARS.search(arg):
            raise CommandNotAllowedError(
                f"Argument contains a disallowed shell metacharacter: {arg!r}."
            )


@dataclass
class ResourceLimits:
    """Best-effort resource caps applied to child processes on POSIX."""

    cpu_seconds: int = 30
    address_space_bytes: int = 1_024 * 1024 * 1024  # 1 GiB


def _preexec_resource_limits(limits: ResourceLimits):
    """Return a `preexec_fn` (POSIX only) enforcing `limits` on the child."""

    def _apply() -> None:  # pragma: no cover - exercised via subprocess only
        try:
            resource.setrlimit(
                resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds)
            )
            resource.setrlimit(
                resource.RLIMIT_AS,
                (limits.address_space_bytes, limits.address_space_bytes),
            )
        except (ValueError, OSError):
            # Some sandboxes/containers disallow rlimit changes; fail open
            # on the resource limit rather than refusing to run the tool.
            pass

    return _apply


def validate_command(argv: list[str]) -> None:
    """Validate an argv-style command against the allowlist.

    Raises:
        CommandNotAllowedError: on an empty argv, a non-allowlisted
            executable, a disallowed subcommand, or an argument containing
            shell metacharacters.
    """

    validate_argument_safety(argv)

    program = argv[0]
    if program not in COMMAND_ALLOWLIST:
        raise CommandNotAllowedError(
            f"Program '{program}' is not on the command allowlist. "
            f"Allowed: {sorted(COMMAND_ALLOWLIST)}."
        )

    allowed_subcommands = COMMAND_ALLOWLIST[program]
    if allowed_subcommands is not None:
        if len(argv) < 2 or argv[1] not in allowed_subcommands:
            raise CommandNotAllowedError(
                f"'{program}' requires one of {sorted(allowed_subcommands)} as its "
                f"first argument."
            )


async def _run_subprocess(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    limits: ResourceLimits | None = None,
) -> tuple[int, str, str]:
    """Run `argv` with no shell, a timeout, and best-effort resource limits."""

    limits = limits or ResourceLimits()
    kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _preexec_resource_limits(limits)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError as exc:
        # The executable is not installed / not on PATH. This must surface
        # as a normal failed ToolResult (so QA/self-healing can react to
        # it) rather than as an unhandled exception that aborts the whole
        # orchestration run -- fault tolerance principle (#8) applies to
        # missing tooling just as much as to failing commands.
        return 127, "", f"Executable not found: {argv[0]!r} ({exc})."
    except NotImplementedError as exc:
        # Windows: `asyncio.create_subprocess_exec` requires the Proactor
        # event loop. If the running loop is a Selector loop instead --
        # which happens under `uvicorn --reload` on Windows (the reloader
        # forces `WindowsSelectorEventLoopPolicy` for multiprocessing
        # compatibility) and can also happen under some pytest-asyncio
        # loop-scope configurations -- subprocess support is missing
        # entirely and every child-process call raises this. Without this
        # handler that exception is not a `ToolError`, so it isn't caught
        # by `BaseTool.__call__`/`_test()`/`WorkerAgent.run()` and instead
        # propagates all the way up and fails the *entire* project run
        # over what should have been "this one test/lint/typecheck step
        # didn't run." Same fault-tolerance principle as the
        # `FileNotFoundError` case above: degrade to a failed ToolResult.
        return (
            127,
            "",
            "Subprocess execution is not supported on the current event "
            "loop (Windows Selector event loop does not implement "
            f"subprocess support): {exc}. If running the API server "
            "yourself, start it without `--reload` on Windows, or set "
            "`asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())` "
            "before the app starts.",
        )
    except OSError as exc:
        # Catch-all for other OS-level failures launching the child
        # process (e.g. permission denied, WinError variants not covered
        # above). Same rationale: never let a shelled-out tool call crash
        # the orchestration run itself.
        return 127, "", f"Failed to start {argv[0]!r}: {exc}."

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ToolTimeoutError(
            f"Command {' '.join(argv)!r} exceeded {timeout}s and was killed."
        ) from exc

    stdout = stdout_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return process.returncode or 0, stdout, stderr


class RunCommandTool(BaseTool):
    """Generic allowlisted command runner.

    Any command text an LLM (e.g. Qwen2.5-Coder) produces is an *untrusted
    suggestion*: the caller must split it into argv and pass it here, where
    `validate_command` checks it before anything is executed. There is no
    path from raw LLM text straight into a shell.
    """

    name = "run_command"
    required_permission = Permission.EXECUTE
    default_timeout = 60.0

    async def _run(
        self, ctx: ToolContext, argv: list[str], timeout: float | None = None, **_: object
    ) -> ToolResult:
        validate_command(argv)
        effective_timeout = min(timeout or self.default_timeout, self.default_timeout)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=effective_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr, "returncode": returncode},
            error=None if returncode == 0 else f"Command exited with status {returncode}.",
            metadata={"argv": argv},
        )


class RunPytestTool(BaseTool):
    name = "run_pytest"
    required_permission = Permission.EXECUTE
    default_timeout = 120.0

    async def _run(
        self,
        ctx: ToolContext,
        path: str = "tests",
        extra_args: list[str] | None = None,
        **_: object,
    ) -> ToolResult:
        ctx.sandbox.resolve(path, must_exist=True)
        argv = ["python", "-m", "pytest", path, "-q"]
        for arg in extra_args or []:
            if _DANGEROUS_CHARS.search(arg):
                raise CommandNotAllowedError(f"Disallowed pytest argument: {arg!r}.")
            argv.append(arg)
        validate_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr, "returncode": returncode},
            error=None if returncode == 0 else "pytest reported failures.",
        )


class RunLintTool(BaseTool):
    name = "run_lint"
    required_permission = Permission.EXECUTE
    default_timeout = 60.0

    async def _run(self, ctx: ToolContext, path: str = ".", **_: object) -> ToolResult:
        ctx.sandbox.resolve(path, must_exist=True)
        argv = ["ruff", "check", path]
        validate_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr, "returncode": returncode},
            error=None if returncode == 0 else "Lint check reported issues.",
        )


class RunTypecheckTool(BaseTool):
    name = "run_typecheck"
    required_permission = Permission.EXECUTE
    default_timeout = 90.0

    async def _run(self, ctx: ToolContext, path: str = ".", **_: object) -> ToolResult:
        ctx.sandbox.resolve(path, must_exist=True)
        argv = ["mypy", path]
        validate_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr, "returncode": returncode},
            error=None if returncode == 0 else "Type check reported issues.",
        )


def build_shell_tools() -> list[BaseTool]:
    return [RunCommandTool(), RunPytestTool(), RunLintTool(), RunTypecheckTool()]
