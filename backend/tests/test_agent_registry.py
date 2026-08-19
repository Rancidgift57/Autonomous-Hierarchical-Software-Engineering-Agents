"""Unit tests for app.agents (Phase 3 — dynamic agent registry)."""

from __future__ import annotations

import pytest

from app.agents import (
    AgentConfigError,
    AgentRegistry,
    HierarchyValidationError,
    RegistryError,
    build_registry_from_config,
)
from app.agents.loader import DEFAULT_AGENTS_YAML_PATH
from app.state.enums import AgentType, SystemAgentKind
from app.state.models import AgentDefinition


def make_agent(agent_id: str, agent_type: AgentType, parent_agent_id: str | None = None,
                system_kind: SystemAgentKind | None = None) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id.replace("_", " ").title(),
        agent_type=agent_type,
        parent_agent_id=parent_agent_id,
        system_kind=system_kind,
    )


# ---------------------------------------------------------------------------
# Basic registration
# ---------------------------------------------------------------------------


def test_register_root_cto():
    registry = AgentRegistry()
    cto = make_agent("cto", AgentType.CTO)
    registry.register_agent(cto)
    assert "cto" in registry
    assert registry.get_agent("cto").name == "Cto"


def test_register_manager_and_worker():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("backend_mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry.register_agent(
        make_agent("api_worker", AgentType.WORKER, parent_agent_id="backend_mgr")
    )

    assert len(registry) == 3
    children = registry.get_children("backend_mgr")
    assert [c.agent_id for c in children] == ["api_worker"]


def test_duplicate_id_raises():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    with pytest.raises(RegistryError):
        registry.register_agent(make_agent("cto", AgentType.CTO))


def test_missing_parent_raises():
    registry = AgentRegistry()
    with pytest.raises(RegistryError):
        registry.register_agent(
            make_agent("mgr", AgentType.MANAGER, parent_agent_id="does_not_exist")
        )


def test_self_parent_raises():
    registry = AgentRegistry()
    agent = make_agent("cto", AgentType.CTO)
    agent.parent_agent_id = "cto"
    with pytest.raises(RegistryError):
        registry.register_agent(agent)


def test_orphan_worker_raises_on_register():
    registry = AgentRegistry()
    with pytest.raises(RegistryError):
        registry.register_agent(make_agent("lone_worker", AgentType.WORKER))


def test_worker_parent_must_be_manager_or_cto():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("worker_a", AgentType.WORKER, parent_agent_id="cto"))
    with pytest.raises(RegistryError):
        registry.register_agent(
            make_agent("worker_b", AgentType.WORKER, parent_agent_id="worker_a")
        )


def test_system_agent_requires_system_kind():
    registry = AgentRegistry()
    bad = make_agent("integration", AgentType.SYSTEM_AGENT)
    with pytest.raises(RegistryError):
        registry.register_agent(bad)


def test_non_system_agent_rejects_system_kind():
    registry = AgentRegistry()
    bad = make_agent("cto", AgentType.CTO, system_kind=SystemAgentKind.QA)
    with pytest.raises(RegistryError):
        registry.register_agent(bad)


def test_system_agent_registers_as_root():
    registry = AgentRegistry()
    registry.register_agent(
        make_agent("qa_agent", AgentType.SYSTEM_AGENT, system_kind=SystemAgentKind.QA)
    )
    assert registry.get_agent("qa_agent").system_kind == SystemAgentKind.QA
    assert "qa_agent" in [a.agent_id for a in registry.get_roots()]


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_remove_agent_cascades_by_default():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry.register_agent(make_agent("wkr", AgentType.WORKER, parent_agent_id="mgr"))

    registry.remove_agent("mgr")

    assert "mgr" not in registry
    assert "wkr" not in registry
    assert "cto" in registry


def test_remove_agent_without_cascade_raises_if_has_children():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))

    with pytest.raises(RegistryError):
        registry.remove_agent("cto", cascade=False)


def test_remove_unknown_agent_raises():
    registry = AgentRegistry()
    with pytest.raises(RegistryError):
        registry.remove_agent("nope")


# ---------------------------------------------------------------------------
# Ancestors / descendants
# ---------------------------------------------------------------------------


def test_get_descendants_and_ancestors():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry.register_agent(make_agent("wkr", AgentType.WORKER, parent_agent_id="mgr"))

    descendant_ids = {a.agent_id for a in registry.get_descendants("cto")}
    assert descendant_ids == {"mgr", "wkr"}

    ancestor_ids = [a.agent_id for a in registry.get_ancestors("wkr")]
    assert ancestor_ids == ["mgr", "cto"]

    assert registry.get_parent("wkr").agent_id == "mgr"
    assert registry.get_parent("cto") is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_hierarchy_passes_for_valid_tree():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry.register_agent(make_agent("wkr", AgentType.WORKER, parent_agent_id="mgr"))
    registry.validate_hierarchy()  # should not raise


def test_validate_hierarchy_detects_cycle():
    registry = AgentRegistry()
    a = make_agent("a", AgentType.MANAGER)
    b = make_agent("b", AgentType.MANAGER, parent_agent_id="a")
    registry.register_agent(a)
    registry.register_agent(b)

    # Manually force a cycle (bypassing register_agent's own checks) to
    # exercise validate_hierarchy's cycle detector directly.
    registry._agents["a"].parent_agent_id = "b"  # noqa: SLF001

    with pytest.raises(HierarchyValidationError) as excinfo:
        registry.validate_hierarchy()
    assert any("Circular" in e for e in excinfo.value.errors)


def test_validate_hierarchy_detects_missing_parent_reference():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry._agents["mgr"].parent_agent_id = "ghost"  # noqa: SLF001

    with pytest.raises(HierarchyValidationError) as excinfo:
        registry.validate_hierarchy()
    assert any("missing parent" in e for e in excinfo.value.errors)


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------


def test_build_tree_structure():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("backend_mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry.register_agent(
        make_agent("api_worker", AgentType.WORKER, parent_agent_id="backend_mgr")
    )
    registry.register_agent(
        make_agent("qa_agent", AgentType.SYSTEM_AGENT, system_kind=SystemAgentKind.QA)
    )

    forest = registry.build_tree()
    root_ids = {node.agent_id for node in forest}
    assert root_ids == {"cto", "qa_agent"}

    cto_node = next(n for n in forest if n.agent_id == "cto")
    assert [c.agent_id for c in cto_node.children] == ["backend_mgr"]
    assert [c.agent_id for c in cto_node.children[0].children] == ["api_worker"]

    as_dict = cto_node.to_dict()
    assert as_dict["agent_id"] == "cto"
    assert as_dict["children"][0]["agent_id"] == "backend_mgr"


def test_build_tree_raises_on_invalid_hierarchy():
    registry = AgentRegistry()
    registry.register_agent(make_agent("cto", AgentType.CTO))
    registry.register_agent(make_agent("mgr", AgentType.MANAGER, parent_agent_id="cto"))
    registry._agents["mgr"].parent_agent_id = "ghost"  # noqa: SLF001

    with pytest.raises(HierarchyValidationError):
        registry.build_tree()


# ---------------------------------------------------------------------------
# YAML loader / example config
# ---------------------------------------------------------------------------


def test_default_agents_yaml_exists():
    assert DEFAULT_AGENTS_YAML_PATH.exists()


def test_build_registry_from_default_config():
    registry = build_registry_from_config()
    registry.validate_hierarchy()

    cto = registry.get_agent("cto")
    assert cto.agent_type == AgentType.CTO

    backend_mgr = registry.get_agent("backend_manager")
    assert backend_mgr.agent_type == AgentType.MANAGER
    assert backend_mgr.parent_agent_id == "cto"
    assert backend_mgr.team_name == "Backend"

    api_worker = registry.get_agent("api_worker")
    assert api_worker.agent_type == AgentType.WORKER
    assert api_worker.parent_agent_id == "backend_manager"

    qa_agent = registry.get_agent("qa_agent")
    assert qa_agent.agent_type == AgentType.SYSTEM_AGENT
    assert qa_agent.system_kind == SystemAgentKind.QA

    forest = registry.build_tree()
    root_ids = {node.agent_id for node in forest}
    assert "cto" in root_ids
    assert "qa_agent" in root_ids


def test_build_registry_from_missing_file_raises(tmp_path):
    with pytest.raises(AgentConfigError):
        build_registry_from_config(tmp_path / "does_not_exist.yaml")


def test_build_registry_rejects_missing_cto(tmp_path):
    bad_yaml = tmp_path / "agents.yaml"
    bad_yaml.write_text("teams: {}\n")
    with pytest.raises(AgentConfigError):
        build_registry_from_config(bad_yaml)


def test_build_registry_rejects_bad_agent_type(tmp_path):
    bad_yaml = tmp_path / "agents.yaml"
    bad_yaml.write_text(
        "cto:\n  id: cto\n  name: CTO\n  type: not_a_real_type\n"
    )
    with pytest.raises(AgentConfigError):
        build_registry_from_config(bad_yaml)
