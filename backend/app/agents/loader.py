"""Load agent hierarchy definitions from YAML into an `AgentRegistry`.

The YAML file (`config/agents.yaml`) is an *example* bootstrap hierarchy
used for local development, defaults, and tests. Real projects are
expected to generate their hierarchy dynamically (e.g. via an LLM planning
step) and register agents programmatically -- this loader is one
convenient way to populate a registry, not the only way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.agents.registry import AgentRegistry, RegistryError
from app.state.enums import AgentType, SystemAgentKind
from app.state.models import AgentDefinition

DEFAULT_AGENTS_YAML_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "agents.yaml"
)


class AgentConfigError(Exception):
    """Raised when `config/agents.yaml` is malformed."""


def _agent_type_from_str(raw: str, *, context: str) -> AgentType:
    try:
        return AgentType(raw.lower())
    except ValueError as exc:
        valid = ", ".join(t.value for t in AgentType)
        raise AgentConfigError(
            f"{context}: invalid agent_type '{raw}'. Must be one of: {valid}."
        ) from exc


def _system_kind_from_str(raw: str, *, context: str) -> SystemAgentKind:
    try:
        return SystemAgentKind(raw.lower())
    except ValueError as exc:
        valid = ", ".join(k.value for k in SystemAgentKind)
        raise AgentConfigError(
            f"{context}: invalid system_kind '{raw}'. Must be one of: {valid}."
        ) from exc


def load_agents_yaml(path: str | Path | None = None) -> dict[str, Any]:
    """Read and parse the raw YAML document."""

    resolved = Path(path) if path is not None else DEFAULT_AGENTS_YAML_PATH
    if not resolved.exists():
        raise AgentConfigError(f"Agent config file not found: {resolved}")

    with resolved.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if not isinstance(data, dict):
        raise AgentConfigError(f"Top-level YAML in {resolved} must be a mapping.")

    return data


def _build_agent_definition(
    node: dict[str, Any],
    *,
    parent_agent_id: str | None,
    team_name: str | None,
    context: str,
) -> AgentDefinition:
    if "id" not in node or "name" not in node or "type" not in node:
        raise AgentConfigError(
            f"{context}: each agent entry requires 'id', 'name', and 'type'."
        )

    agent_type = _agent_type_from_str(node["type"], context=context)
    system_kind = None
    if agent_type == AgentType.SYSTEM_AGENT:
        if "system_kind" not in node:
            raise AgentConfigError(
                f"{context}: SYSTEM_AGENT entries require 'system_kind'."
            )
        system_kind = _system_kind_from_str(node["system_kind"], context=context)

    return AgentDefinition(
        agent_id=node["id"],
        name=node["name"],
        agent_type=agent_type,
        system_kind=system_kind,
        parent_agent_id=parent_agent_id,
        team_name=team_name,
        role_description=node.get("role_description", ""),
        capabilities=node.get("capabilities", []) or [],
        allowed_tools=node.get("allowed_tools", []) or [],
    )


def build_registry_from_config(path: str | Path | None = None) -> AgentRegistry:
    """Build and validate an `AgentRegistry` from `config/agents.yaml`.

    Expected YAML shape::

        cto:
          id: cto
          name: CTO Agent
          type: cto

        teams:
          Backend:
            manager:
              id: backend_manager
              name: Backend Manager
              type: manager
            workers:
              - id: api_worker
                name: API Worker
                type: worker

        system_agents:
          - id: integration_agent
            name: Integration Agent
            type: system_agent
            system_kind: integration
    """

    data = load_agents_yaml(path)
    registry = AgentRegistry()

    cto_node = data.get("cto")
    if cto_node is None:
        raise AgentConfigError("agents.yaml must define a top-level 'cto' entry.")

    cto_def = _build_agent_definition(
        cto_node, parent_agent_id=None, team_name=None, context="cto"
    )
    try:
        registry.register_agent(cto_def)
    except RegistryError as exc:
        raise AgentConfigError(str(exc)) from exc

    teams = data.get("teams", {}) or {}
    for team_name, team_node in teams.items():
        if "manager" not in team_node:
            raise AgentConfigError(f"Team '{team_name}' must define a 'manager'.")

        manager_def = _build_agent_definition(
            team_node["manager"],
            parent_agent_id=cto_def.agent_id,
            team_name=team_name,
            context=f"teams.{team_name}.manager",
        )
        try:
            registry.register_agent(manager_def)
        except RegistryError as exc:
            raise AgentConfigError(str(exc)) from exc

        for i, worker_node in enumerate(team_node.get("workers", []) or []):
            worker_def = _build_agent_definition(
                worker_node,
                parent_agent_id=manager_def.agent_id,
                team_name=team_name,
                context=f"teams.{team_name}.workers[{i}]",
            )
            try:
                registry.register_agent(worker_def)
            except RegistryError as exc:
                raise AgentConfigError(str(exc)) from exc

    for i, sys_node in enumerate(data.get("system_agents", []) or []):
        sys_def = _build_agent_definition(
            sys_node,
            parent_agent_id=None,
            team_name=None,
            context=f"system_agents[{i}]",
        )
        try:
            registry.register_agent(sys_def)
        except RegistryError as exc:
            raise AgentConfigError(str(exc)) from exc

    try:
        registry.validate_hierarchy()
    except Exception as exc:  # HierarchyValidationError
        raise AgentConfigError(f"Loaded hierarchy failed validation: {exc}") from exc

    return registry
