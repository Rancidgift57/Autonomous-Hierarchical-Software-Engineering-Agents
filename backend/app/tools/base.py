"""Core tool abstraction for the Agent Tool System (Phase 9).

`BaseTool` is the single choke point every concrete tool goes through:
permission check -> timeout enforcement -> audit logging -> result. Agents
(managers, workers) never call filesystem/shell/git primitives directly --
they only ever go through a `ToolExecutor`, which is the only object that
holds a `WorkspaceSandbox` and an `AuditLog`.
"""

from __future__ import annotations

import abc
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.tools.audit import AuditLog, AuditLogEntry
from app.tools.exceptions import (
    PermissionDeniedError,
    ToolError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from app.tools.permissions import Permission, permission_satisfied
from app.tools.sandbox import WorkspaceSandbox

#: Default wall-clock budget for any single tool call. Individual tools may
#: request a tighter budget (e.g. `run_pytest` gets more time than
#: `read_file`) via their own `default_timeout`.
DEFAULT_TOOL_TIMEOUT = 30.0
_SENSITIVE_ARGUMENT = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|private[_-]?key|credential|authorization)",
    re.IGNORECASE,
)


class ToolResult(BaseModel):
    """Uniform return value for every tool call."""

    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolContext:
    """Everything a tool needs to run, scoped to a single calling agent.

    Holds non-serializable collaborators (sandbox, audit log) so it's a
    plain dataclass rather than a Pydantic model.
    """

    agent_id: str
    permissions: frozenset[Permission]
    sandbox: WorkspaceSandbox
    audit_log: AuditLog
    extra: dict[str, Any] = field(default_factory=dict)


def _summarize_arguments(kwargs: dict[str, Any]) -> dict[str, str]:
    """Redact/truncate tool arguments for the audit log.

    File *contents* must never land in the audit trail -- only enough to
    know what was invoked and against what path/command.
    """

    summary: dict[str, str] = {}
    for key, value in kwargs.items():
        if key in ("content", "old_str", "new_str"):
            summary[key] = f"<{len(str(value))} chars>"
        elif _SENSITIVE_ARGUMENT.search(key):
            summary[key] = "***REDACTED***"
        else:
            text = str(value)
            summary[key] = text if len(text) <= 200 else text[:200] + "...<truncated>"
    return summary


class BaseTool(abc.ABC):
    """Abstract base for every concrete tool.

    Subclasses set `name` and `required_permission` as class attributes and
    implement `_run`. `__call__` handles permission enforcement, timeout,
    and audit logging uniformly so no individual tool can accidentally skip
    one of those checks.
    """

    name: str
    required_permission: Permission
    default_timeout: float = DEFAULT_TOOL_TIMEOUT

    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        granted = permission_satisfied(self.required_permission, ctx.permissions)
        arg_summary = _summarize_arguments(kwargs)

        if not granted:
            ctx.audit_log.record(
                AuditLogEntry(
                    agent_id=ctx.agent_id,
                    tool_name=self.name,
                    required_permission=self.required_permission,
                    permission_granted=False,
                    arguments_summary=arg_summary,
                    success=False,
                    error_type="PermissionDeniedError",
                    error_message=(
                        f"Agent '{ctx.agent_id}' lacks '{self.required_permission.value}' "
                        f"permission required by tool '{self.name}'."
                    ),
                    duration_seconds=0.0,
                )
            )
            raise PermissionDeniedError(
                f"Agent '{ctx.agent_id}' lacks '{self.required_permission.value}' "
                f"permission required by tool '{self.name}'."
            )

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._run(ctx, **kwargs), timeout=self.default_timeout
            )
        except TimeoutError as exc:
            duration = time.monotonic() - start
            ctx.audit_log.record(
                AuditLogEntry(
                    agent_id=ctx.agent_id,
                    tool_name=self.name,
                    required_permission=self.required_permission,
                    permission_granted=True,
                    arguments_summary=arg_summary,
                    success=False,
                    error_type="ToolTimeoutError",
                    error_message=f"Tool '{self.name}' exceeded {self.default_timeout}s.",
                    duration_seconds=duration,
                )
            )
            raise ToolTimeoutError(
                f"Tool '{self.name}' exceeded its {self.default_timeout}s timeout."
            ) from exc
        except ToolError as exc:
            duration = time.monotonic() - start
            ctx.audit_log.record(
                AuditLogEntry(
                    agent_id=ctx.agent_id,
                    tool_name=self.name,
                    required_permission=self.required_permission,
                    permission_granted=True,
                    arguments_summary=arg_summary,
                    success=False,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    duration_seconds=duration,
                )
            )
            raise

        duration = time.monotonic() - start
        ctx.audit_log.record(
            AuditLogEntry(
                agent_id=ctx.agent_id,
                tool_name=self.name,
                required_permission=self.required_permission,
                permission_granted=True,
                arguments_summary=arg_summary,
                success=result.success,
                error_type=None if result.success else "ToolExecutionError",
                error_message=result.error,
                duration_seconds=duration,
            )
        )
        return result

    @abc.abstractmethod
    async def _run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    """Maps tool names to `BaseTool` instances."""

    def __init__(self, tools: list[BaseTool] | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"No tool registered with name '{name}'.")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)


class ToolExecutor:
    """The only object agents hold a reference to for doing tool work.

    Binds a `ToolRegistry` to a single agent's `ToolContext` so callers
    never touch permissions, sandbox, or audit log directly -- they just
    call `await executor.run("read_file", path="app/main.py")`.
    """

    def __init__(self, registry: ToolRegistry, context: ToolContext) -> None:
        self.registry = registry
        self.context = context

    async def run(self, tool_name: str, **kwargs: Any) -> ToolResult:
        tool = self.registry.get(tool_name)
        return await tool(self.context, **kwargs)

    @property
    def agent_id(self) -> str:
        return self.context.agent_id

    @property
    def permissions(self) -> frozenset[Permission]:
        return self.context.permissions

    def available_tools(self) -> list[str]:
        """Tool names this agent actually has permission to call."""

        return [
            name
            for name in self.registry.names()
            if permission_satisfied(
                self.registry.get(name).required_permission, self.context.permissions
            )
        ]
