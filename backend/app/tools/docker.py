"""Docker tools (Phase 15): build, compose up/down, health, logs, tag, rm.

Security model matches `app.tools.shell`: every tool here builds a fixed,
argv-style command itself (never a caller-supplied free-form string), runs
it through this module's own `validate_docker_command` allowlist check
(see below) and the same no-shell `_run_subprocess` runner, pinned to the
workspace sandbox root.

Permission split, distinct from plain `EXECUTE`:
    * Building an image, checking health/status, and reading logs are
      inspection/preparation actions -> `Permission.EXECUTE`.
    * Starting, stopping, or removing a running deployment is a materially
      different action (it changes what's actually running) -> the
      dedicated `Permission.DEPLOY`, granted separately so a plain worker
      agent can never accidentally start/stop containers just because it
      holds `EXECUTE`.
"""

from __future__ import annotations

import re

from app.tools.base import BaseTool, ToolContext, ToolResult
from app.tools.exceptions import CommandNotAllowedError
from app.tools.permissions import Permission
from app.tools.shell import _run_subprocess, validate_argument_safety

#: docker subcommand -> allowed second token (None means no further check
#: beyond argv[0]/argv[1]). Deliberately narrow: only the handful of
#: read-only/lifecycle operations this module's fixed-argv tools actually
#: build. Nothing here ever accepts caller-supplied free-form argv, so this
#: is a defense-in-depth check on top of the hardcoded templates above, not
#: the primary safety boundary.
_DOCKER_ALLOWED_SUBCOMMANDS: dict[str, str | None] = {
    "build": None,
    "compose": None,
    "inspect": None,
    "logs": None,
    "rmi": None,
}


def validate_docker_command(argv: list[str]) -> None:
    """Validate a `docker ...` argv built by this module's own tools.

    Separate from `app.tools.shell.validate_command`, which backs the
    generic `run_command` tool reachable with LLM-suggested/free-form
    argv and deliberately rejects `docker` outright -- docker exposes far
    too much (bind mounts, `--privileged`, arbitrary image pulls) to allow
    generically. This validator only ever sees argv already assembled from
    the fixed templates in this file, and simply confirms that assembly
    didn't drift and that no dynamic value (tag/name/path) smuggled in a
    shell metacharacter.
    """

    validate_argument_safety(argv)
    if argv[0] != "docker" or len(argv) < 2 or argv[1] not in _DOCKER_ALLOWED_SUBCOMMANDS:
        raise CommandNotAllowedError(f"Docker subcommand not allowed: {argv!r}.")

#: Docker image tags/names: lowercase, digits, separators, optional
#: `:tag` -- deliberately stricter than what Docker itself accepts, since
#: this string is about to be placed straight into an argv list.
_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_./-]*(:[a-zA-Z0-9_.-]+)?$")
#: Container/project/network names Docker accepts.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

#: Sentinel written into `ToolResult.error` (in addition to raw stderr in
#: `output`) whenever `_run_subprocess` reports returncode 127 for a
#: `docker` invocation. `DeploymentManager` looks for this exact string to
#: distinguish "docker isn't usable on this host" (skip, not a pipeline
#: failure) from "docker ran and the build/compose step genuinely failed"
#: (a real failure needing a repair task).
DOCKER_NOT_AVAILABLE = "docker_not_available"

#: Stderr substrings that mean "the `docker` CLI itself ran fine, but
#: there's no reachable daemon behind it" -- Docker Desktop installed but
#: not started is the single most common way to hit this, especially on
#: Windows (Docker Desktop must be manually launched; it isn't a system
#: service that starts automatically the way the Linux daemon is). This
#: case returns a real, non-127 exit code (commonly 1), so without this
#: check it fell through to the generic "the build genuinely failed"
#: branch -- raising `DeploymentPipelineFailedError` and failing the
#: *entire* project run over "Docker Desktop isn't open right now", the
#: same class of environment-vs-defect problem `DOCKER_NOT_AVAILABLE`
#: already exists to handle for "docker isn't installed at all". Checked
#: case-insensitively against combined stdout+stderr; kept broad on
#: purpose since Docker's own daemon-unreachable wording differs across
#: versions/platforms (Linux socket vs. Windows named pipe).
_DAEMON_UNREACHABLE_MARKERS = (
    "cannot connect to the docker daemon",
    "daemon is not running",
    "docker daemon is not running",
    "error during connect",
    "dockerdesktoplinuxengine",  # Windows named-pipe path Docker Desktop uses
    "the system cannot find the file specified",  # Windows: pipe doesn't exist -> Desktop not running
    "is the docker daemon running",
)


def _tool_result(
    name: str, returncode: int, stdout: str, stderr: str, generic_error: str, **extra_output: object
) -> ToolResult:
    if returncode == 127 or _looks_like_daemon_unreachable(stdout, stderr):
        error = DOCKER_NOT_AVAILABLE
    elif returncode != 0:
        error = generic_error
    else:
        error = None
    return ToolResult(
        tool_name=name,
        success=returncode == 0,
        output={"stdout": stdout, "stderr": stderr, "returncode": returncode, **extra_output},
        error=error,
    )


def _looks_like_daemon_unreachable(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(marker in combined for marker in _DAEMON_UNREACHABLE_MARKERS)


def _validate_tag(tag: str) -> None:
    if not tag or not _TAG_PATTERN.match(tag):
        raise CommandNotAllowedError(f"Invalid image tag: {tag!r}.")


def _validate_name(name: str) -> None:
    if not name or not _NAME_PATTERN.match(name):
        raise CommandNotAllowedError(f"Invalid container/project name: {name!r}.")


class DockerBuildTool(BaseTool):
    """`docker build -f <dockerfile> -t <tag> <context>`."""

    name = "docker_build"
    required_permission = Permission.EXECUTE
    default_timeout = 600.0

    async def _run(
        self,
        ctx: ToolContext,
        dockerfile_path: str = "Dockerfile",
        image_tag: str = "ahsea-app:latest",
        context_path: str = ".",
        **_: object,
    ) -> ToolResult:
        ctx.sandbox.resolve(dockerfile_path, must_exist=True)
        if context_path != ".":
            ctx.sandbox.resolve(context_path, must_exist=True)
        _validate_tag(image_tag)

        argv = [
            "docker",
            "build",
            "-f",
            dockerfile_path,
            "-t",
            image_tag,
            context_path,
        ]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        result = _tool_result(self.name, returncode, stdout, stderr, "docker build failed.")
        result.metadata["image_tag"] = image_tag
        return result


class DockerComposeUpTool(BaseTool):
    """`docker compose -f <file> -p <project> up -d`. Starts containers."""

    name = "docker_compose_up"
    required_permission = Permission.DEPLOY
    default_timeout = 300.0

    async def _run(
        self,
        ctx: ToolContext,
        compose_path: str = "docker-compose.yml",
        project_name: str = "ahsea-deploy",
        **_: object,
    ) -> ToolResult:
        ctx.sandbox.resolve(compose_path, must_exist=True)
        _validate_name(project_name)

        argv = [
            "docker",
            "compose",
            "-f",
            compose_path,
            "-p",
            project_name,
            "up",
            "-d",
        ]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        result = _tool_result(self.name, returncode, stdout, stderr, "docker compose up failed.")
        result.metadata["project_name"] = project_name
        return result


class DockerComposeDownTool(BaseTool):
    """`docker compose -f <file> -p <project> down`. Used for rollback/teardown."""

    name = "docker_compose_down"
    required_permission = Permission.DEPLOY
    default_timeout = 120.0

    async def _run(
        self,
        ctx: ToolContext,
        compose_path: str = "docker-compose.yml",
        project_name: str = "ahsea-deploy",
        **_: object,
    ) -> ToolResult:
        ctx.sandbox.resolve(compose_path, must_exist=True)
        _validate_name(project_name)

        argv = [
            "docker",
            "compose",
            "-f",
            compose_path,
            "-p",
            project_name,
            "down",
        ]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr, "returncode": returncode},
            error=None if returncode == 0 else "docker compose down failed.",
            metadata={"project_name": project_name},
        )


class DockerHealthCheckTool(BaseTool):
    """`docker inspect --format {{...}} <container>`. Read-only status probe."""

    name = "docker_health_check"
    required_permission = Permission.EXECUTE
    default_timeout = 30.0

    async def _run(self, ctx: ToolContext, container_name: str, **_: object) -> ToolResult:
        _validate_name(container_name)
        argv = [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.Health.Status}}",
            container_name,
        ]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        status, _, health = stdout.strip().partition("|")
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={
                "status": status or None,
                "health": health.replace("<no value>", "").strip() or None,
                "stderr": stderr,
            },
            error=None if returncode == 0 else "docker inspect failed.",
        )


class DockerLogsTool(BaseTool):
    """`docker logs --tail 200 <container>`. Read-only, used for smoke-test diagnostics."""

    name = "docker_logs"
    required_permission = Permission.EXECUTE
    default_timeout = 30.0

    async def _run(
        self, ctx: ToolContext, container_name: str, tail: int = 200, **_: object
    ) -> ToolResult:
        _validate_name(container_name)
        safe_tail = max(1, min(int(tail), 2000))
        argv = ["docker", "logs", "--tail", str(safe_tail), container_name]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr},
            error=None if returncode == 0 else "docker logs failed.",
        )


class DockerRemoveImageTool(BaseTool):
    """`docker rmi <tag>`. Used during rollback cleanup."""

    name = "docker_remove_image"
    required_permission = Permission.DEPLOY
    default_timeout = 60.0

    async def _run(self, ctx: ToolContext, image_tag: str, **_: object) -> ToolResult:
        _validate_tag(image_tag)
        argv = ["docker", "rmi", "-f", image_tag]
        validate_docker_command(argv)
        returncode, stdout, stderr = await _run_subprocess(
            argv, cwd=str(ctx.sandbox.root), timeout=self.default_timeout
        )
        return ToolResult(
            tool_name=self.name,
            success=returncode == 0,
            output={"stdout": stdout, "stderr": stderr},
            error=None if returncode == 0 else "docker rmi failed.",
        )


def build_docker_tools() -> list[BaseTool]:
    return [
        DockerBuildTool(),
        DockerComposeUpTool(),
        DockerComposeDownTool(),
        DockerHealthCheckTool(),
        DockerLogsTool(),
        DockerRemoveImageTool(),
    ]
