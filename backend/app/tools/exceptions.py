"""Exceptions for the Agent Tool System (Phase 9)."""

from __future__ import annotations


class ToolError(Exception):
    """Base exception for all tool-system errors."""


class PermissionDeniedError(ToolError):
    """Raised when the calling agent lacks the permission a tool requires."""


class PathValidationError(ToolError):
    """Raised when a path escapes the workspace sandbox or is otherwise invalid."""


class CommandNotAllowedError(ToolError):
    """Raised when a shell command fails allowlist validation."""


class ToolTimeoutError(ToolError):
    """Raised when a tool invocation exceeds its timeout budget."""


class ResourceLimitError(ToolError):
    """Raised when a tool invocation exceeds a configured resource limit."""


class ToolNotFoundError(ToolError):
    """Raised when an unknown tool name is requested from the registry."""


class GitOperationError(ToolError):
    """Raised when a git subcommand fails or is used unsafely."""
