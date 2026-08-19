"""CTO / Main Manager agent (Phase 5).

The CTO agent turns a natural-language project idea into structured
planning output -- requirements, architecture, a team layout, and a set of
high-level tasks -- entirely through three `LLMGateway.generate_json`
calls (`REQUIREMENTS`, `ARCHITECTURE`, `DECOMPOSITION`). All three task
types are routed by the gateway to the reasoning model (Qwen3); the CTO
never requests `TaskType.CODING` and therefore never generates application
code.

The CTO does not talk to Ollama, `OllamaProvider`, or `httpx` directly --
only `LLMGateway`, exactly like every other consumer.
"""

from __future__ import annotations

from typing import Any

from app.agents.cto_schemas import (
    CTOArchitectureOutput,
    CTODecompositionOutput,
    CTOPlan,
    CTOPlanningError,
    CTORequirementOutput,
    CTORequirementsOutput,
)
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.memory.service import MemoryService, MemoryType
from app.state.enums import RequirementPriority, RequirementStatus, TaskComplexity
from app.state.models import (
    ArchitectureDecision,
    ArchitectureState,
    Module,
    Requirement,
    Task,
    Technology,
)

_CTO_CHARTER = """\
You are the CTO / Main Manager for an autonomous software engineering \
system called AHSEA. You are responsible for understanding the project, \
defining requirements (functional and non-functional), designing the \
architecture and technology stack, defining modules, teams, dependencies \
and integration points, and defining testing and deployment requirements.

You MUST NOT write, generate, or output any application source code. You \
only produce planning artifacts.
"""


def _priority_from_str(raw: str) -> RequirementPriority:
    return RequirementPriority(raw)


def _complexity_from_str(raw: str) -> TaskComplexity:
    return TaskComplexity(raw)


class CTOAgent:
    """Understands a project idea and produces a structured `CTOPlan`."""

    def __init__(self, gateway: LLMGateway, memory_service: MemoryService | None = None):
        self.gateway = gateway
        #: Phase 22 wiring: when given, `plan()` retrieves relevant prior
        #: project memory (past architecture decisions, requirements,
        #: known failures) and folds it into the architecture/decomposition
        #: prompts, and stores this run's own decisions back for future
        #: runs (e.g. re-planning after a failed run, or scope changes) to
        #: build on. Optional and `None` by default so callers that don't
        #: care about persistent memory (most unit tests) are unaffected.
        self.memory_service = memory_service

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _requirements_prompt(idea_prompt: str, project_name: str) -> str:
        return (
            f"{_CTO_CHARTER}\n\n"
            f"Project name: {project_name}\n"
            f"Project idea (verbatim from the user):\n{idea_prompt}\n\n"
            "Analyze this project idea and produce:\n"
            "- functional_requirements: what the system must DO\n"
            "- non_functional_requirements: performance, security, "
            "reliability, scalability, usability, etc.\n"
            "- testing_requirements: what kinds of testing this project needs\n"
            "- deployment_requirements: how/where this project should be deployed\n"
            "Be concrete and specific to this project idea, not generic boilerplate."
        )

    @staticmethod
    def _architecture_prompt(
        idea_prompt: str,
        project_name: str,
        requirements: CTORequirementsOutput,
        memory_context: str = "",
    ) -> str:
        all_reqs = requirements.functional_requirements + requirements.non_functional_requirements
        req_summary = "\n".join(
            f"- [{r.category}] {r.title}: {r.description}" for r in all_reqs
        )
        memory_section = f"\n\n{memory_context}" if memory_context else ""
        return (
            f"{_CTO_CHARTER}\n\n"
            f"Project name: {project_name}\n"
            f"Project idea: {idea_prompt}\n\n"
            f"Approved requirements:\n{req_summary}{memory_section}\n\n"
            "Design the architecture for this project:\n"
            "- technology_stack: concrete technologies/frameworks/libraries to use\n"
            "- modules: logical subsystems/components (name, description, owning_team, "
            "depends_on_module_names referencing OTHER module names in this same list, "
            "technologies used, and requirement_titles this module satisfies -- titles "
            "must match the requirement titles above exactly)\n"
            "- dependencies: integration points between modules (from_module, to_module, "
            "integration_type, description) -- from_module/to_module must be module names "
            "from the modules list above\n"
            "- decisions: key architecture decision records (ADRs)\n"
            "Do not write any code -- structure and technology choices only."
        )

    @staticmethod
    def _decomposition_prompt(
        idea_prompt: str,
        project_name: str,
        architecture: CTOArchitectureOutput,
        memory_context: str = "",
    ) -> str:
        module_summary = "\n".join(
            f"- {m.name} (owning_team: {m.owning_team}): {m.description}"
            for m in architecture.modules
        )
        memory_section = f"\n\n{memory_context}" if memory_context else ""
        return (
            f"{_CTO_CHARTER}\n\n"
            f"Project name: {project_name}\n"
            f"Project idea: {idea_prompt}\n\n"
            f"Approved modules:\n{module_summary}{memory_section}\n\n"
            "Decompose this project into:\n"
            "- teams: the teams needed to build this project (name, description, "
            "module_names owned -- module_names must match the module list above)\n"
            "- high_level_tasks: high-level implementation tasks (title, description, "
            "owner_team referencing a team name above, worker_type describing the kind "
            "of engineer/worker needed, depends_on_task_titles referencing OTHER task "
            "titles in this same list, expected_outputs, priority, complexity). "
            "Do NOT include actual code, only task descriptions."
        )

    # ------------------------------------------------------------------
    # Resolution helpers (name-based references -> real IDs)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_requirements(output: CTORequirementsOutput) -> list[Requirement]:
        requirements: list[Requirement] = []

        def _convert(items: list[CTORequirementOutput], category_label: str) -> None:
            for item in items:
                requirements.append(
                    Requirement(
                        title=item.title,
                        description=f"({category_label}) {item.description}",
                        priority=_priority_from_str(item.priority),
                        status=RequirementStatus.ACCEPTED,
                        acceptance_criteria=item.acceptance_criteria,
                        source="cto",
                    )
                )

        _convert(output.functional_requirements, "Functional")
        _convert(output.non_functional_requirements, "Non-Functional")
        return requirements

    @staticmethod
    def _resolve_architecture(
        output: CTOArchitectureOutput, requirements_by_title: dict[str, Requirement]
    ) -> ArchitectureState:
        modules_by_name: dict[str, Module] = {}
        for mod in output.modules:
            requirement_ids = []
            for title in mod.requirement_titles:
                req = requirements_by_title.get(title.strip().lower())
                if req is not None:
                    requirement_ids.append(req.requirement_id)
            modules_by_name[mod.name] = Module(
                name=mod.name,
                description=mod.description,
                owning_team=mod.owning_team,
                technologies=[
                    Technology(
                        name=t.name,
                        category=t.category,
                        version_constraint=t.version_constraint,
                        rationale=t.rationale,
                    )
                    for t in mod.technologies
                ],
                requirement_ids=requirement_ids,
            )

        # Second pass: resolve inter-module depends_on_module_names -> IDs.
        unresolved: list[str] = []
        for mod_output in output.modules:
            module = modules_by_name[mod_output.name]
            dep_ids = []
            for dep_name in mod_output.depends_on_module_names:
                dep_module = modules_by_name.get(dep_name)
                if dep_module is None:
                    unresolved.append(
                        f"module '{mod_output.name}' depends on unknown module '{dep_name}'"
                    )
                else:
                    dep_ids.append(dep_module.module_id)
            module.depends_on_module_ids = dep_ids

        for dep in output.dependencies:
            if dep.from_module not in modules_by_name:
                unresolved.append(f"dependency references unknown module '{dep.from_module}'")
            if dep.to_module not in modules_by_name:
                unresolved.append(f"dependency references unknown module '{dep.to_module}'")

        if unresolved:
            raise CTOPlanningError(
                "CTO architecture output has unresolved module references: "
                + "; ".join(unresolved)
            )

        return ArchitectureState(
            modules=list(modules_by_name.values()),
            technologies=[
                Technology(
                    name=t.name,
                    category=t.category,
                    version_constraint=t.version_constraint,
                    rationale=t.rationale,
                )
                for t in output.technology_stack
            ],
            decisions=[
                ArchitectureDecision(
                    title=d.title,
                    context=d.context,
                    decision=d.decision,
                    consequences=d.consequences,
                    alternatives_considered=d.alternatives_considered,
                )
                for d in output.decisions
            ],
        )

    @staticmethod
    def _resolve_tasks(
        output: CTODecompositionOutput, team_names: set[str]
    ) -> list[Task]:
        tasks_by_title: dict[str, Task] = {}
        unresolved: list[str] = []

        for task_output in output.high_level_tasks:
            if task_output.owner_team not in team_names:
                unresolved.append(
                    f"task '{task_output.title}' has unknown owner_team "
                    f"'{task_output.owner_team}'"
                )
            key = task_output.title.strip().lower()
            if key in tasks_by_title:
                unresolved.append(f"duplicate task title '{task_output.title}'")
                continue
            tasks_by_title[key] = Task(
                title=task_output.title,
                description=task_output.description,
                owner_manager=task_output.owner_team,
                worker_type=task_output.worker_type,
                expected_outputs=task_output.expected_outputs,
                priority=task_output.priority,
                complexity=_complexity_from_str(task_output.complexity),
            )

        for task_output in output.high_level_tasks:
            key = task_output.title.strip().lower()
            task = tasks_by_title.get(key)
            if task is None:
                continue
            dep_ids = []
            for dep_title in task_output.depends_on_task_titles:
                dep_task = tasks_by_title.get(dep_title.strip().lower())
                if dep_task is None:
                    unresolved.append(
                        f"task '{task_output.title}' depends on unknown task "
                        f"'{dep_title}'"
                    )
                elif dep_task.task_id == task.task_id:
                    unresolved.append(f"task '{task_output.title}' depends on itself")
                else:
                    dep_ids.append(dep_task.task_id)
            task.depends_on_task_ids = dep_ids

        if unresolved:
            raise CTOPlanningError(
                "CTO decomposition output has unresolved references: " + "; ".join(unresolved)
            )

        return list(tasks_by_title.values())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(
        self,
        idea_prompt: str,
        project_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> CTOPlan:
        """Understand `idea_prompt` and produce a fully-resolved `CTOPlan`.

        Makes exactly three LLM Gateway calls -- `REQUIREMENTS`,
        `ARCHITECTURE`, `DECOMPOSITION` -- each automatically routed to the
        reasoning model. Never requests `TaskType.CODING`.

        Phase 22: when this agent was built with a `memory_service`, and
        `metadata` carries a `project_id`, planning both *reads* prior
        project memory (folded into the architecture/decomposition
        prompts, e.g. earlier ADRs or lessons from a previous failed run)
        and *writes* this run's own architecture decisions back as memory
        once planning succeeds.
        """

        project_id = (metadata or {}).get("project_id")
        memory_context = ""
        if self.memory_service is not None and project_id:
            memory_context = await self.memory_service.context_for_prompt(
                project_id,
                idea_prompt,
                limit=5,
                memory_types=[MemoryType.DECISION, MemoryType.PROJECT, MemoryType.FAILURE],
            )

        requirements_output = await self.gateway.generate_json(
            task_type=TaskType.REQUIREMENTS,
            prompt=self._requirements_prompt(idea_prompt, project_name),
            response_model=CTORequirementsOutput,
            metadata=metadata,
        )

        architecture_output = await self.gateway.generate_json(
            task_type=TaskType.ARCHITECTURE,
            prompt=self._architecture_prompt(
                idea_prompt, project_name, requirements_output, memory_context
            ),
            response_model=CTOArchitectureOutput,
            metadata=metadata,
        )

        decomposition_output = await self.gateway.generate_json(
            task_type=TaskType.DECOMPOSITION,
            prompt=self._decomposition_prompt(
                idea_prompt, project_name, architecture_output, memory_context
            ),
            response_model=CTODecompositionOutput,
            metadata=metadata,
        )

        requirements = self._resolve_requirements(requirements_output)
        requirements_by_title = {r.title.strip().lower(): r for r in requirements}

        architecture = self._resolve_architecture(architecture_output, requirements_by_title)

        team_names = {t.name for t in decomposition_output.teams}
        unresolved_teams = [
            f"team '{team.name}' references unknown module '{mod_name}'"
            for team in decomposition_output.teams
            for mod_name in team.module_names
            if mod_name not in {m.name for m in architecture_output.modules}
        ]
        if unresolved_teams:
            raise CTOPlanningError(
                "CTO decomposition output has unresolved module references: "
                + "; ".join(unresolved_teams)
            )

        tasks = self._resolve_tasks(decomposition_output, team_names)

        if self.memory_service is not None and project_id:
            for decision in architecture.decisions:
                await self.memory_service.store(
                    project_id,
                    MemoryType.DECISION,
                    title=decision.title,
                    content=f"{decision.decision} (context: {decision.context})",
                    tags=["cto", "architecture"],
                    importance=0.7,
                )
            if requirements_output.testing_requirements or requirements_output.deployment_requirements:
                await self.memory_service.store(
                    project_id,
                    MemoryType.PROJECT,
                    title="Testing & deployment requirements",
                    content=(
                        "Testing: " + "; ".join(requirements_output.testing_requirements)
                        + " | Deployment: " + "; ".join(requirements_output.deployment_requirements)
                    ),
                    tags=["cto", "requirements"],
                    importance=0.5,
                )

        return CTOPlan(
            requirements=requirements,
            architecture=architecture,
            teams=decomposition_output.teams,
            dependencies=architecture_output.dependencies,
            tasks=tasks,
            testing_requirements=requirements_output.testing_requirements,
            deployment_requirements=requirements_output.deployment_requirements,
        )
