"""Secure Agent Tool System (Phase 9).

Agents never touch the filesystem, a shell, or git directly -- they hold a
`ToolExecutor` (built via `make_executor`) and call `await
executor.run("read_file", path=...)`. Every call is permission-checked
against `Permission`, path-validated against a `WorkspaceSandbox`, and
recorded in an `AuditLog`.
"""

from app.tools.audit import AuditLog, AuditLogEntry
from app.tools.base import BaseTool, ToolContext, ToolExecutor, ToolRegistry, ToolResult
from app.tools.exceptions import (
    CommandNotAllowedError,
    GitOperationError,
    PathValidationError,
    PermissionDeniedError,
    ResourceLimitError,
    ToolError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from app.tools.permissions import (
    FULL_ACCESS,
    NO_PERMISSIONS,
    READ_ONLY,
    WORKER_DEFAULT,
    Permission,
    permission_satisfied,
)
from app.tools.registry import all_tools, build_default_registry, make_executor
from app.tools.sandbox import WorkspaceSandbox
from app.tools.shell import COMMAND_ALLOWLIST, validate_command

__all__ = [
    "COMMAND_ALLOWLIST",
    "FULL_ACCESS",
    "NO_PERMISSIONS",
    "READ_ONLY",
    "WORKER_DEFAULT",
    "AuditLog",
    "AuditLogEntry",
    "BaseTool",
    "CommandNotAllowedError",
    "GitOperationError",
    "PathValidationError",
    "Permission",
    "PermissionDeniedError",
    "ResourceLimitError",
    "ToolContext",
    "ToolError",
    "ToolExecutor",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolResult",
    "ToolTimeoutError",
    "WorkspaceSandbox",
    "all_tools",
    "build_default_registry",
    "make_executor",
    "permission_satisfied",
    "validate_command",
]
