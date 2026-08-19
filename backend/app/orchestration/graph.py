"""LangGraph orchestration graph (Phase 5).

Wires the CTO agent in as the entry node of the AHSEA orchestration graph.
The graph's state schema is `AHSEAState` itself (LangGraph supports Pydantic
models as state schemas natively); node functions return partial updates
that LangGraph merges into the running state.

Later phases add more nodes (task execution, QA, integration, deployment,
self-healing, ...) after the CTO node -- this module intentionally only
wires up planning for now.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.cto import CTOAgent
from app.agents.cto_schemas import CTOPlanningError
from app.llm.gateway import LLMGateway
from app.state.models import AHSEAState, ProjectMetadata
from app.state.operations import add_task
from app.tasks.dag import TaskGraphValidationError, create_graph, validate_graph


def _make_cto_node(gateway: LLMGateway):
    cto_agent = CTOAgent(gateway=gateway)

    async def cto_node(state: AHSEAState) -> dict[str, Any]:
        metadata = {"project_id": state.project.project_id}

        plan = await cto_agent.plan(
            idea_prompt=state.project.idea_prompt,
            project_name=state.project.name,
            metadata=metadata,
        )

        # Sanity-check the resolved task set actually forms a valid DAG
        # (Phase 6) before merging it into shared state -- catches
        # multi-hop dependency cycles the CTO's own per-task resolution
        # can't see (e.g. A depends on B, B depends on A via three
        # different LLM-authored titles).
        try:
            validate_graph(create_graph(plan.tasks))
        except TaskGraphValidationError as exc:
            raise CTOPlanningError(f"CTO-produced tasks do not form a valid DAG: {exc}") from exc

        working = state.model_copy(deep=True)
        working.requirements.extend(plan.requirements)
        working.architecture = plan.architecture
        for task in plan.tasks:
            add_task(working, task)
        working.shared_context["cto_teams"] = [t.model_dump(mode="json") for t in plan.teams]
        working.shared_context["cto_dependencies"] = [
            d.model_dump(mode="json") for d in plan.dependencies
        ]
        working.shared_context["testing_requirements"] = plan.testing_requirements
        working.shared_context["deployment_requirements"] = plan.deployment_requirements

        return {
            "requirements": working.requirements,
            "architecture": working.architecture,
            "tasks": working.tasks,
            "task_dependencies": working.task_dependencies,
            "shared_context": working.shared_context,
            "updated_at": working.updated_at,
        }

    return cto_node


def build_graph(gateway: LLMGateway) -> CompiledStateGraph:
    """Build and compile the AHSEA orchestration graph.

    Currently a single-node graph: `cto -> END`. Every LLM call the graph
    makes goes through `gateway` (an `LLMGateway`), never directly through
    Ollama.
    """

    builder = StateGraph(AHSEAState)
    builder.add_node("cto", _make_cto_node(gateway))
    builder.set_entry_point("cto")
    builder.add_edge("cto", END)
    return builder.compile()


async def run_cto_planning(gateway: LLMGateway, project: ProjectMetadata) -> AHSEAState:
    """Convenience entry point: run the graph for a fresh project and
    return the resulting `AHSEAState` (reconstructed from LangGraph's
    dict-shaped output).
    """

    graph = build_graph(gateway)
    initial_state = AHSEAState(project=project)
    result = await graph.ainvoke(initial_state)
    return AHSEAState.model_validate(result)
