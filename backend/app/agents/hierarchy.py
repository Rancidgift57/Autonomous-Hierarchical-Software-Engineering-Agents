"""LLM-driven hierarchy planning and dynamic registry instantiation (Phase 23)."""
from __future__ import annotations

import re
from pydantic import BaseModel, Field

from app.agents.registry import AgentRegistry
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.state.enums import AgentType
from app.state.models import AgentDefinition
from app.tools.permissions import READ_ONLY, WORKER_DEFAULT
from app.tools.registry import tool_names_for_permissions


class HierarchyWorker(BaseModel):
    name: str
    responsibility: str
    capabilities: list[str] = Field(default_factory=list)


class HierarchyTeam(BaseModel):
    name: str
    responsibility: str
    dependencies: list[str] = Field(default_factory=list)
    complexity: str = "medium"
    workers: list[HierarchyWorker] = Field(default_factory=list)


class HierarchyPlan(BaseModel):
    cto_responsibility: str = "Architecture, planning, and cross-team coordination"
    complexity: str = "medium"
    teams: list[HierarchyTeam] = Field(default_factory=list)


class DynamicHierarchyGenerator:
    """Uses Qwen3 via ARCHITECTURE routing; YAML is not part of this path."""
    def __init__(self, gateway: LLMGateway) -> None:
        self.gateway = gateway

    async def generate_hierarchy(self, project_description: str, *, metadata: dict | None = None) -> HierarchyPlan:
        prompt = ("You are the CTO. Design only the agent organization for this project. "
                  "Return teams with a manager, required workers, responsibilities, inter-team dependencies, and complexity. "
                  "Use the smallest practical organization; include no code.\n\nProject:\n" + project_description)
        plan = await self.gateway.generate_json(TaskType.ARCHITECTURE, prompt, HierarchyPlan, metadata=metadata)
        self.validate_hierarchy(plan)
        return plan

    def validate_hierarchy(self, plan: HierarchyPlan) -> None:
        names = [team.name.strip().lower() for team in plan.teams]
        if not plan.teams or len(names) != len(set(names)):
            raise ValueError("Hierarchy requires at least one uniquely named team.")
        unknown = [f"{team.name}->{dep}" for team in plan.teams for dep in team.dependencies if dep.strip().lower() not in names]
        if unknown:
            raise ValueError("Hierarchy has unknown dependencies: " + ", ".join(unknown))
        if any(team.name.strip().lower() in {dep.strip().lower() for dep in team.dependencies} for team in plan.teams):
            raise ValueError("A team cannot depend on itself.")

    def instantiate_hierarchy(self, plan: HierarchyPlan, registry: AgentRegistry | None = None) -> AgentRegistry:
        self.validate_hierarchy(plan)
        registry = registry or AgentRegistry()
        # These mirror the real runtime permission sets each agent kind
        # actually gets from `ManagerDispatchRunner` (see
        # app/orchestration/project_runner.py): managers get `READ_ONLY`,
        # workers get `WORKER_DEFAULT`. The CTO never receives a
        # `ToolExecutor` at all (`CTOAgent` only takes a gateway), so an
        # empty list is correct for it, not a bug. Populating this here is
        # what actually fixes `allowed_tools: []` showing for every
        # worker in `GET /api/projects/{id}/agents` -- previously nothing
        # set this field for dynamically-generated agents at all.
        manager_tool_names = tool_names_for_permissions(READ_ONLY)
        worker_tool_names = tool_names_for_permissions(WORKER_DEFAULT)
        cto = AgentDefinition(agent_id="cto", name="CTO", agent_type=AgentType.CTO, role_description=plan.cto_responsibility, capabilities=["architecture", "planning", "delegation"])
        registry.register_agent(cto)
        for team in plan.teams:
            key = re.sub(r"[^a-z0-9]+", "_", team.name.lower()).strip("_")
            manager = AgentDefinition(agent_id=f"{key}_manager", name=f"{team.name} Manager", agent_type=AgentType.MANAGER, parent_agent_id=cto.agent_id, team_name=team.name, role_description=team.responsibility, capabilities=["management", team.complexity], allowed_tools=manager_tool_names)
            registry.register_agent(manager)
            for index, worker in enumerate(team.workers, 1):
                registry.register_agent(AgentDefinition(agent_id=f"{key}_worker_{index}", name=worker.name, agent_type=AgentType.WORKER, parent_agent_id=manager.agent_id, team_name=team.name, role_description=worker.responsibility, capabilities=worker.capabilities, allowed_tools=worker_tool_names))
        registry.validate_hierarchy()
        return registry
