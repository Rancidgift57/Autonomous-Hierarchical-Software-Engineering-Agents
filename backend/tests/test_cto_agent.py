"""Unit tests for app.agents.cto (Phase 5 -- CTO / Main Manager agent)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agents.cto import CTOAgent
from app.agents.cto_schemas import (
    CTOArchitectureOutput,
    CTODecompositionOutput,
    CTOPlanningError,
    CTORequirementsOutput,
)
from app.llm.models import TaskType


def _requirements_payload() -> dict:
    return {
        "functional_requirements": [
            {
                "title": "User signup",
                "description": "Users can create an account with email/password.",
                "category": "functional",
                "priority": "must_have",
                "acceptance_criteria": ["Signup form validates email"],
            }
        ],
        "non_functional_requirements": [
            {
                "title": "Response time",
                "description": "API responses under 200ms p95.",
                "category": "non_functional",
                "priority": "should_have",
                "acceptance_criteria": [],
            }
        ],
        "testing_requirements": ["Unit tests for all services"],
        "deployment_requirements": ["Deploy via Docker to staging then prod"],
    }


def _architecture_payload() -> dict:
    return {
        "technology_stack": [
            {
                "name": "FastAPI",
                "category": "backend",
                "version_constraint": None,
                "rationale": None,
            }
        ],
        "modules": [
            {
                "name": "AuthModule",
                "description": "Handles signup/login.",
                "owning_team": "Backend",
                "depends_on_module_names": [],
                "technologies": [
                    {
                        "name": "FastAPI",
                        "category": "backend",
                        "version_constraint": None,
                        "rationale": None,
                    }
                ],
                "requirement_titles": ["User signup"],
            },
            {
                "name": "APIModule",
                "description": "Public HTTP API.",
                "owning_team": "Backend",
                "depends_on_module_names": ["AuthModule"],
                "technologies": [],
                "requirement_titles": ["Response time"],
            },
        ],
        "dependencies": [
            {
                "from_module": "APIModule",
                "to_module": "AuthModule",
                "integration_type": "api",
                "description": "API module calls into auth module.",
            }
        ],
        "decisions": [
            {
                "title": "Use FastAPI",
                "context": "Need an async Python web framework.",
                "decision": "Use FastAPI for the backend.",
                "consequences": "Team must know Python typing.",
                "alternatives_considered": ["Flask", "Django"],
            }
        ],
    }


def _decomposition_payload() -> dict:
    return {
        "teams": [
            {
                "name": "Backend",
                "description": "Owns the API and auth.",
                "module_names": ["AuthModule", "APIModule"],
            }
        ],
        "high_level_tasks": [
            {
                "title": "Implement auth module",
                "description": "Build signup/login endpoints.",
                "owner_team": "Backend",
                "worker_type": "auth_worker",
                "depends_on_task_titles": [],
                "expected_outputs": ["Working /signup and /login endpoints"],
                "priority": 5,
                "complexity": "medium",
            },
            {
                "title": "Implement public API",
                "description": "Build the public HTTP API on top of auth.",
                "owner_team": "Backend",
                "worker_type": "api_worker",
                "depends_on_task_titles": ["Implement auth module"],
                "expected_outputs": ["Working public API"],
                "priority": 3,
                "complexity": "high",
            },
        ],
    }


@pytest.fixture
def mock_gateway():
    gateway = AsyncMock()
    return gateway


@pytest.fixture
def cto(mock_gateway):
    return CTOAgent(gateway=mock_gateway)


def _install_happy_path(mock_gateway):
    mock_gateway.generate_json = AsyncMock(
        side_effect=[
            CTORequirementsOutput.model_validate(_requirements_payload()),
            CTOArchitectureOutput.model_validate(_architecture_payload()),
            CTODecompositionOutput.model_validate(_decomposition_payload()),
        ]
    )


@pytest.mark.asyncio
async def test_cto_plan_routes_all_three_task_types(cto, mock_gateway):
    _install_happy_path(mock_gateway)

    await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    assert mock_gateway.generate_json.await_count == 3
    called_task_types = [
        call.kwargs["task_type"] for call in mock_gateway.generate_json.await_args_list
    ]
    assert called_task_types == [
        TaskType.REQUIREMENTS,
        TaskType.ARCHITECTURE,
        TaskType.DECOMPOSITION,
    ]


@pytest.mark.asyncio
async def test_cto_plan_never_requests_coding(cto, mock_gateway):
    _install_happy_path(mock_gateway)

    await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    called_task_types = {
        call.kwargs["task_type"] for call in mock_gateway.generate_json.await_args_list
    }
    assert TaskType.CODING not in called_task_types


@pytest.mark.asyncio
async def test_cto_plan_resolves_requirements(cto, mock_gateway):
    _install_happy_path(mock_gateway)

    plan = await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    assert len(plan.requirements) == 2
    titles = {r.title for r in plan.requirements}
    assert titles == {"User signup", "Response time"}
    assert plan.testing_requirements == ["Unit tests for all services"]
    assert plan.deployment_requirements == ["Deploy via Docker to staging then prod"]


@pytest.mark.asyncio
async def test_cto_plan_resolves_module_dependencies(cto, mock_gateway):
    _install_happy_path(mock_gateway)

    plan = await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    modules_by_name = {m.name: m for m in plan.architecture.modules}
    auth = modules_by_name["AuthModule"]
    api = modules_by_name["APIModule"]
    assert api.depends_on_module_ids == [auth.module_id]
    assert auth.requirement_ids  # resolved from "User signup" title


@pytest.mark.asyncio
async def test_cto_plan_resolves_task_dependencies(cto, mock_gateway):
    _install_happy_path(mock_gateway)

    plan = await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    tasks_by_title = {t.title: t for t in plan.tasks}
    auth_task = tasks_by_title["Implement auth module"]
    api_task = tasks_by_title["Implement public API"]
    assert api_task.depends_on_task_ids == [auth_task.task_id]
    assert api_task.owner_manager == "Backend"
    assert api_task.worker_type == "api_worker"


@pytest.mark.asyncio
async def test_cto_plan_raises_on_unresolved_task_dependency(cto, mock_gateway):
    decomposition = _decomposition_payload()
    decomposition["high_level_tasks"][1]["depends_on_task_titles"] = ["Nonexistent task"]

    mock_gateway.generate_json = AsyncMock(
        side_effect=[
            CTORequirementsOutput.model_validate(_requirements_payload()),
            CTOArchitectureOutput.model_validate(_architecture_payload()),
            CTODecompositionOutput.model_validate(decomposition),
        ]
    )

    with pytest.raises(CTOPlanningError):
        await cto.plan(idea_prompt="A todo app", project_name="TodoApp")


@pytest.mark.asyncio
async def test_cto_plan_raises_on_unresolved_module_dependency(cto, mock_gateway):
    architecture = _architecture_payload()
    architecture["modules"][1]["depends_on_module_names"] = ["GhostModule"]

    mock_gateway.generate_json = AsyncMock(
        side_effect=[
            CTORequirementsOutput.model_validate(_requirements_payload()),
            CTOArchitectureOutput.model_validate(architecture),
            CTODecompositionOutput.model_validate(_decomposition_payload()),
        ]
    )

    with pytest.raises(CTOPlanningError):
        await cto.plan(idea_prompt="A todo app", project_name="TodoApp")


@pytest.mark.asyncio
async def test_cto_plan_does_not_call_ollama_directly(cto, mock_gateway):
    """The CTO agent must only ever touch the gateway -- never a provider."""

    _install_happy_path(mock_gateway)
    await cto.plan(idea_prompt="A todo app", project_name="TodoApp")

    # The mock gateway stands in for LLMGateway; CTOAgent holds no other
    # network-capable attribute.
    assert not hasattr(cto, "provider")
    assert not hasattr(cto, "ollama")
