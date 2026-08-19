"""Wiring helpers for Phase 7 managers.

Keeps `BaseManagerAgent` free of any import on concrete Phase 8 worker
classes or Phase 9 tool permission presets -- this module is where those
two phases actually get connected.
"""

from __future__ import annotations

from pathlib import Path

from app.agents.workers.base import BaseWorkerAgent
from app.agents.workers.concrete import WORKER_CLASSES
from app.agents.workers.schemas import WorkerScope
from app.llm.gateway import LLMGateway
from app.tools.audit import AuditLog
from app.tools.permissions import READ_ONLY, WORKER_DEFAULT
from app.tools.registry import make_executor


def make_worker_factory(
    gateway: LLMGateway,
    workspace_root: str | Path,
    audit_log: AuditLog | None = None,
    scope_by_worker_type: dict[str, WorkerScope] | None = None,
):
    """Build a `worker_factory` callable for `BaseManagerAgent`.

    Each call creates a *fresh* worker instance with its own
    `ToolExecutor` (least-privilege `WORKER_DEFAULT` permissions: read,
    write, execute, git -- never admin) scoped to `workspace_root`, sharing
    one `AuditLog` so a project run has a single audit trail.
    """

    shared_audit_log = audit_log if audit_log is not None else AuditLog()
    scope_by_worker_type = scope_by_worker_type or {}

    def factory(worker_type: str) -> BaseWorkerAgent:
        worker_cls = WORKER_CLASSES.get(worker_type)
        if worker_cls is None:
            raise ValueError(f"Unknown worker_type '{worker_type}'.")

        executor = make_executor(
            agent_id=f"{worker_type}_{shared_audit_log.__len__():04d}",
            workspace_root=workspace_root,
            permissions=WORKER_DEFAULT,
            audit_log=shared_audit_log,
        )
        return worker_cls(
            gateway=gateway,
            tools=executor,
            agent_id=executor.agent_id,
            scope=scope_by_worker_type.get(worker_type),
        )

    return factory


def make_manager_tools(
    manager_id: str,
    workspace_root: str | Path,
    audit_log: AuditLog | None = None,
):
    """Read-only `ToolExecutor` for a manager to inspect the workspace.

    Deliberately `READ_ONLY`: managers must not execute shell commands,
    write files, or make git changes -- only workers (Phase 8) do that.
    """

    return make_executor(
        agent_id=manager_id,
        workspace_root=workspace_root,
        permissions=READ_ONLY,
        audit_log=audit_log if audit_log is not None else AuditLog(),
    )
