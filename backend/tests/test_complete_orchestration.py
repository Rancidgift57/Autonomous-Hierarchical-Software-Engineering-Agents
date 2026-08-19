"""End-to-end topology tests for the Phase 24 LangGraph workflow."""
from types import SimpleNamespace

import pytest

from app.orchestration.complete import CompleteOrchestration
from app.orchestration.project_runner import ProjectRunStatus
from app.state.models import AHSEAState, ProjectMetadata


class _WorkflowWithoutExternalSystems(CompleteOrchestration):
    """Exercises graph routing/checkpointing without Ollama, Docker, or tools."""

    async def _plan(self, graph_state):
        await self._checkpoint("cto_requirements_architecture")
        return {"phase": "cto_requirements_architecture"}

    async def _hierarchy(self, graph_state):
        await self._checkpoint("dynamic_hierarchy")
        return {"phase": "dynamic_hierarchy"}

    async def _execute(self, graph_state):
        await self._checkpoint("task_execution")
        return {"phase": "task_execution", "failed": False}

    async def _integration(self, graph_state):
        await self._checkpoint("integration")
        return {"phase": "integration", "failed": False}

    async def _qa(self, graph_state):
        await self._checkpoint("qa")
        return {"phase": "qa", "failed": False}

    async def _git(self, graph_state):
        await self._checkpoint("git")
        return {"phase": "git"}

    async def _deployment_prepare(self, graph_state):
        await self._checkpoint("awaiting_human_approval")
        return {"phase": "awaiting_human_approval", "waiting_for_approval": not bool(self.state.deployment.approved_by)}

    async def _deployment_finalize(self, graph_state):
        await self._checkpoint("deployment_finalized")
        return {"phase": "deployment_finalized"}


@pytest.mark.asyncio
async def test_complete_graph_pauses_then_resumes_at_human_approval():
    state = AHSEAState(
        project=ProjectMetadata(name="Demo", description="Demo project", idea_prompt="demo")
    )
    control = SimpleNamespace(status=ProjectRunStatus.RUNNING)
    workflow = _WorkflowWithoutExternalSystems(
        gateway=object(), workspace_root=".", control=control,
        plan=lambda _state: None, runner_factory=lambda _state: None,
    )

    await workflow.run(state)
    assert control.status == ProjectRunStatus.PAUSED
    assert state.shared_context[workflow.CHECKPOINT_KEY] == "awaiting_human_approval"

    state.deployment.approved_by = "reviewer"
    await workflow.run(state)
    assert state.shared_context[workflow.CHECKPOINT_KEY] == "final_report"


def test_complete_graph_includes_each_required_phase():
    workflow = _WorkflowWithoutExternalSystems(
        gateway=object(), workspace_root=".", control=SimpleNamespace(status=ProjectRunStatus.RUNNING),
        plan=lambda _state: None, runner_factory=lambda _state: None,
    )
    nodes = set(workflow.build_graph().get_graph().nodes)
    assert {"project_intake", "cto_requirements_architecture", "dynamic_hierarchy", "task_execution", "failure_recovery", "integration", "qa", "git", "deployment_prepare", "human_approval", "deployment_finalize", "final_report"} <= nodes
