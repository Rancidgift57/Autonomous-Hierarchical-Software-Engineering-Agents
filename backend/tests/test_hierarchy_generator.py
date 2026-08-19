"""Tests for app.agents.hierarchy.DynamicHierarchyGenerator.

`instantiate_hierarchy` had no test coverage at all before this file.
These focus on the bug a live-run diagnosis surfaced: every dynamically
generated `AgentDefinition` -- CTO, managers, and workers alike -- showed
`allowed_tools: []` via `GET /api/projects/{id}/agents`, because nothing
ever populated that field, even though the *actual* runtime workers get a
real `ToolExecutor` with real permissions (see
`ManagerDispatchRunner` in app/orchestration/project_runner.py) that is
entirely independent of this metadata field. The bug was real (the API
was lying about what tools an agent had), but it was never the reason a
task actually failed to use tools at runtime.
"""

from __future__ import annotations

from app.agents.hierarchy import DynamicHierarchyGenerator, HierarchyPlan, HierarchyTeam, HierarchyWorker
from app.state.enums import AgentType
from app.tools.registry import tool_names_for_permissions
from app.tools.permissions import READ_ONLY, WORKER_DEFAULT


def _sample_plan() -> HierarchyPlan:
    return HierarchyPlan(
        cto_responsibility="Own architecture and delegate.",
        teams=[
            HierarchyTeam(
                name="Backend",
                responsibility="Implement the API.",
                workers=[
                    HierarchyWorker(name="Backend Engineer", responsibility="Write endpoints.", capabilities=["coding"]),
                    HierarchyWorker(name="Test Engineer", responsibility="Write tests.", capabilities=["testing"]),
                ],
            )
        ],
    )


def _generator() -> DynamicHierarchyGenerator:
    # `instantiate_hierarchy` never touches the gateway, so `None` is fine.
    return DynamicHierarchyGenerator(gateway=None)  # type: ignore[arg-type]


def test_workers_get_real_worker_default_tool_names():
    registry = _generator().instantiate_hierarchy(_sample_plan())
    workers = [a for a in registry.all_agents() if a.agent_type == AgentType.WORKER]

    assert workers, "expected at least one worker to be registered"
    expected = tool_names_for_permissions(WORKER_DEFAULT)
    for worker in workers:
        assert worker.allowed_tools == expected
        assert worker.allowed_tools != [], "worker must not report an empty tool list"
        assert "write_file" in worker.allowed_tools
        assert "run_command" in worker.allowed_tools


def test_managers_get_read_only_tool_names():
    registry = _generator().instantiate_hierarchy(_sample_plan())
    managers = [a for a in registry.all_agents() if a.agent_type == AgentType.MANAGER]

    assert managers, "expected at least one manager to be registered"
    expected = tool_names_for_permissions(READ_ONLY)
    for manager in managers:
        assert manager.allowed_tools == expected
        assert "write_file" not in manager.allowed_tools


def test_cto_has_no_tools_by_design():
    """The CTO genuinely never receives a `ToolExecutor` at runtime
    (`CTOAgent` only takes a gateway) -- an empty list here is correct,
    not a repeat of the bug above."""
    registry = _generator().instantiate_hierarchy(_sample_plan())
    cto = registry.get_agent("cto")
    assert cto.allowed_tools == []


def test_worker_and_manager_tool_lists_are_disjoint_in_write_access():
    """Sanity check on the fix: managers must never end up with the same
    (write-capable) tool set as workers -- that would defeat the least-
    privilege design `READ_ONLY`/`WORKER_DEFAULT` encode."""
    registry = _generator().instantiate_hierarchy(_sample_plan())
    worker = next(a for a in registry.all_agents() if a.agent_type == AgentType.WORKER)
    manager = next(a for a in registry.all_agents() if a.agent_type == AgentType.MANAGER)

    assert set(manager.allowed_tools) < set(worker.allowed_tools)
