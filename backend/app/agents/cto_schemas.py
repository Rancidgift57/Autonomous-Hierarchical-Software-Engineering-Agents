"""Structured output schemas for the CTO agent (Phase 5).

Two layers of models live here:

1. `CTO*Output` models -- exactly what we ask the LLM to produce via
   `LLMGateway.generate_json`. These are deliberately simple/flat (LLMs are
   much more reliable at flat JSON than deeply nested IDs) and reference
   each other by human-readable *name/title*, not by ID, since the LLM
   can't know IDs it hasn't generated yet.

2. `CTOPlan` -- the fully-resolved result after the CTO agent has run all
   three LLM calls and stitched the name-based references together into
   the real `app.state.models` types (`Requirement`, `ArchitectureState`,
   `Task`, ...) that get merged into `AHSEAState`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.state.models import ArchitectureState, Requirement, Task

# ---------------------------------------------------------------------------
# 1. Raw LLM-facing output schemas
# ---------------------------------------------------------------------------


class CTORequirementOutput(BaseModel):
    title: str
    description: str
    category: Literal["functional", "non_functional"]
    priority: Literal["must_have", "should_have", "nice_to_have"] = "must_have"
    acceptance_criteria: list[str] = Field(default_factory=list)


class CTORequirementsOutput(BaseModel):
    """Result of the `TaskType.REQUIREMENTS` call."""

    functional_requirements: list[CTORequirementOutput] = Field(default_factory=list)
    non_functional_requirements: list[CTORequirementOutput] = Field(default_factory=list)
    testing_requirements: list[str] = Field(
        default_factory=list,
        description="Testing expectations, e.g. 'unit tests for all services', 'e2e login flow'.",
    )
    deployment_requirements: list[str] = Field(
        default_factory=list,
        description="Deployment expectations, e.g. 'containerized', 'staging then prod'.",
    )


class CTOTechnologyOutput(BaseModel):
    name: str
    category: str = Field(description="e.g. backend, frontend, database, llm, testing, infra")
    version_constraint: str | None = None
    rationale: str | None = None


class CTOModuleOutput(BaseModel):
    name: str
    description: str
    owning_team: str | None = None
    depends_on_module_names: list[str] = Field(default_factory=list)
    technologies: list[CTOTechnologyOutput] = Field(default_factory=list)
    requirement_titles: list[str] = Field(
        default_factory=list, description="Titles of requirements this module satisfies."
    )


class CTODependencyOutput(BaseModel):
    """An integration point / dependency edge between two modules."""

    from_module: str
    to_module: str
    integration_type: Literal["api", "database", "event", "shared_library", "other"] = "other"
    description: str = ""


class CTODecisionOutput(BaseModel):
    title: str
    context: str
    decision: str
    consequences: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)


class CTOArchitectureOutput(BaseModel):
    """Result of the `TaskType.ARCHITECTURE` call."""

    technology_stack: list[CTOTechnologyOutput] = Field(default_factory=list)
    modules: list[CTOModuleOutput] = Field(default_factory=list)
    dependencies: list[CTODependencyOutput] = Field(default_factory=list)
    decisions: list[CTODecisionOutput] = Field(default_factory=list)


class CTOTeamOutput(BaseModel):
    name: str
    description: str = ""
    module_names: list[str] = Field(default_factory=list)


class CTOHighLevelTaskOutput(BaseModel):
    title: str
    description: str
    owner_team: str = Field(description="Team name expected to own this task.")
    worker_type: str = Field(description="Kind of worker expected to execute this task.")
    depends_on_task_titles: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    priority: int = 0
    complexity: Literal["low", "medium", "high"] = "medium"


class CTODecompositionOutput(BaseModel):
    """Result of the `TaskType.DECOMPOSITION` call."""

    teams: list[CTOTeamOutput] = Field(default_factory=list)
    high_level_tasks: list[CTOHighLevelTaskOutput] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. Resolved plan
# ---------------------------------------------------------------------------


class CTOPlanningError(Exception):
    """Raised when the CTO agent's three outputs can't be reconciled.

    E.g. a task or module references a name that doesn't exist in any of
    the other two LLM calls' output.
    """


class CTOPlan(BaseModel):
    """Fully-resolved CTO output, ready to merge into `AHSEAState`."""

    model_config = {"arbitrary_types_allowed": True}

    requirements: list[Requirement] = Field(default_factory=list)
    architecture: ArchitectureState = Field(default_factory=ArchitectureState)
    teams: list[CTOTeamOutput] = Field(default_factory=list)
    dependencies: list[CTODependencyOutput] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    testing_requirements: list[str] = Field(default_factory=list)
    deployment_requirements: list[str] = Field(default_factory=list)
