"""Factory helpers for assembling the full tool system (Phase 9).

`build_default_registry()` wires up every concrete tool (filesystem, shell,
git). `make_executor()` binds that registry to a fresh `ToolContext` for a
specific agent/workspace/permission-set -- this is the one function agent
constructors (Phase 7/8) should call rather than importing individual tool
classes.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.audit import AuditLog
from app.tools.base import BaseTool, ToolContext, ToolExecutor, ToolRegistry
from app.tools.docker import build_docker_tools
from app.tools.filesystem import build_filesystem_tools
from app.tools.git import build_git_tools
from app.tools.permissions import Permission
from app.tools.sandbox import WorkspaceSandbox
from app.tools.shell import build_shell_tools


def all_tools() -> list[BaseTool]:
    return [
        *build_filesystem_tools(),
        *build_shell_tools(),
        *build_git_tools(),
        *build_docker_tools(),
    ]


def build_default_registry() -> ToolRegistry:
    return ToolRegistry(all_tools())


def tool_names_for_permissions(permissions: frozenset[Permission]) -> list[str]:
    """Every tool name a given permission set actually grants against the
    default registry -- e.g. `tool_names_for_permissions(WORKER_DEFAULT)`.

    This is the single source of truth `AgentDefinition.allowed_tools`
    (the metadata surfaced by `GET /api/projects/{id}/agents`) should be
    built from. Before this helper existed, nothing populated that field
    at all for dynamically-generated agents (see
    `app.agents.hierarchy.DynamicHierarchyGenerator.instantiate_hierarchy`),
    so every agent showed `allowed_tools: []` in the API regardless of
    what tools it actually had at runtime via its real `ToolExecutor` --
    a real observability bug (the two are entirely independent code
    paths), even though it was never the reason a task actually failed or
    hung.
    """

    from app.tools.base import permission_satisfied

    registry = build_default_registry()
    return [
        name
        for name in registry.names()
        if permission_satisfied(registry.get(name).required_permission, permissions)
    ]


def make_executor(
    *,
    agent_id: str,
    workspace_root: str | Path,
    permissions: frozenset[Permission],
    registry: ToolRegistry | None = None,
    audit_log: AuditLog | None = None,
) -> ToolExecutor:
    """Build a `ToolExecutor` scoped to one agent's workspace and permissions.

    `audit_log` may be shared across many executors (e.g. one per project
    run) so a single audit trail covers every agent's tool use.
    """

    sandbox = WorkspaceSandbox(workspace_root)
    context = ToolContext(
        agent_id=agent_id,
        permissions=permissions,
        sandbox=sandbox,
        audit_log=audit_log if audit_log is not None else AuditLog(),
    )
    return ToolExecutor(registry or build_default_registry(), context)
