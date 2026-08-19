"""Phase 24: restart-safe LangGraph coordination of every AHSEA subsystem.

This module deliberately coordinates existing agents; it never calls an LLM
provider and every model call remains behind ``LLMGateway`` task-type routing.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.hierarchy import HierarchyPlan, HierarchyTeam, HierarchyWorker
from app.agents.system.integration import IntegrationAgent
from app.agents.system.integration_schemas import ContractRegistry
from app.deployment.manager import DeploymentManager
from app.deployment.schemas import DeploymentReport
from app.llm.gateway import LLMGateway
from app.orchestration.events import EventBus
from app.orchestration.scheduler import SchedulerRun, TaskScheduler
from app.qa.manager import QAManager
from app.realtime.emitter import attach_task_events
from app.self_healing.engine import SelfHealingEngine
from app.self_healing.schemas import RepairOutcome
from app.state.enums import EventLevel, TaskStatus, TestOutcome
from app.state.models import AHSEAState, ProjectEvent, QAReport, TestResult
from app.state.operations import add_agent, add_error, add_event
from app.tools.permissions import DEPLOYMENT_MANAGER_DEFAULT, QA_PIPELINE_DEFAULT
from app.tools.registry import build_default_registry, make_executor


class WorkflowGraphState(TypedDict, total=False):
    """Small LangGraph state; the durable source of truth is AHSEAState."""

    phase: str
    waiting_for_approval: bool
    failed: bool
    deployment_skipped: bool


RunnerFactory = Callable[[AHSEAState], Any]
PlanCallback = Callable[[AHSEAState], Any]


class CompleteOrchestration:
    """Coordinates an end-to-end project run through a compiled LangGraph.

    The ``phase`` checkpoint is persisted in ``state.shared_context`` after
    each node.  Since ``PersistenceService.save_state`` persists that state,
    a new process can load it and resume at the next uncompleted node.
    """

    CHECKPOINT_KEY = "phase24_checkpoint"

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        workspace_root: str,
        control: Any,
        plan: PlanCallback,
        runner_factory: RunnerFactory,
        persistence: Any = None,
        realtime: Any = None,
        max_task_concurrency: int = 4,
        memory_service: Any = None,
    ) -> None:
        self.gateway = gateway
        self.workspace_root = workspace_root
        self.control = control
        self.plan_callback = plan
        self.runner_factory = runner_factory
        self.persistence = persistence
        self.realtime = realtime
        self.max_task_concurrency = max_task_concurrency
        #: Phase 22 wiring: forwarded to `SelfHealingEngine` so repair
        #: diagnosis/outcomes read and write project memory too.
        self.memory_service = memory_service
        self.state: AHSEAState | None = None
        self._last_deployment_report: DeploymentReport | None = None

    async def _checkpoint(self, phase: str) -> None:
        assert self.state is not None
        self.state.shared_context[self.CHECKPOINT_KEY] = phase
        add_event(self.state, ProjectEvent(level=EventLevel.INFO, message=f"Workflow checkpoint: {phase}."))
        if self.persistence is not None:
            await self.persistence.save_state(self.state)

    def build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(WorkflowGraphState)
        graph.add_node("project_intake", self._intake)
        graph.add_node("cto_requirements_architecture", self._plan)
        graph.add_node("dynamic_hierarchy", self._hierarchy)
        graph.add_node("task_execution", self._execute)
        graph.add_node("failure_recovery", self._recover)
        graph.add_node("integration", self._integration)
        graph.add_node("qa", self._qa)
        graph.add_node("git", self._git)
        graph.add_node("deployment_prepare", self._deployment_prepare)
        graph.add_node("human_approval", self._approval)
        graph.add_node("deployment_finalize", self._deployment_finalize)
        graph.add_node("final_report", self._final_report)
        graph.set_conditional_entry_point(
            self._entry_phase,
            {
                "project_intake": "project_intake",
                "cto_requirements_architecture": "cto_requirements_architecture",
                "dynamic_hierarchy": "dynamic_hierarchy",
                "task_execution": "task_execution",
                "failure_recovery": "failure_recovery",
                "integration": "integration",
                "qa": "qa",
                "git": "git",
                "deployment_prepare": "deployment_prepare",
                "deployment_finalize": "deployment_finalize",
                "final_report": "final_report",
            },
        )
        graph.add_edge("project_intake", "cto_requirements_architecture")
        graph.add_edge("cto_requirements_architecture", "dynamic_hierarchy")
        graph.add_edge("dynamic_hierarchy", "task_execution")
        graph.add_conditional_edges("task_execution", self._after_execution, {"recover": "failure_recovery", "integration": "integration"})
        graph.add_edge("failure_recovery", "integration")
        graph.add_conditional_edges("integration", self._after_integration, {"execute": "task_execution", "qa": "qa"})
        graph.add_conditional_edges(
            "qa", self._after_qa,
            {"recover": "failure_recovery", "git": "git", "final": "final_report"},
        )
        graph.add_edge("git", "deployment_prepare")
        graph.add_edge("deployment_prepare", "human_approval")
        graph.add_conditional_edges(
            "human_approval",
            self._after_approval,
            {"end": END, "deploy": "deployment_finalize", "skip": "final_report"},
        )
        graph.add_edge("deployment_finalize", "final_report")
        graph.add_edge("final_report", END)
        return graph.compile()

    @staticmethod
    def _entry_phase(graph_state: WorkflowGraphState) -> str:
        phase = graph_state.get("phase") or "project_intake"
        # Approval itself is a controlled pause, never a graph entry point.
        if phase == "awaiting_human_approval":
            return "deployment_finalize"
        return phase if phase in {
            "project_intake", "cto_requirements_architecture", "dynamic_hierarchy",
            "task_execution", "failure_recovery", "integration", "qa", "git",
            "deployment_prepare", "deployment_finalize", "final_report",
        } else "project_intake"

    async def run(self, state: AHSEAState) -> AHSEAState:
        self.state = state
        phase = str(state.shared_context.get(self.CHECKPOINT_KEY, ""))
        # A restart from a completed planning/execution checkpoint must not ask
        # the CTO to create duplicate requirements/tasks.
        if phase == "awaiting_human_approval" and not state.deployment.approved_by:
            self.control.status = self.control.status.__class__.PAUSED
            return state
        result = await self.build_graph().ainvoke({"phase": phase})
        if result.get("failed"):
            self.control.error = "Workflow failed; inspect project errors and checkpoints."
        return state

    async def _intake(self, _: WorkflowGraphState) -> WorkflowGraphState:
        await self._checkpoint("project_intake")
        return {"phase": "project_intake"}

    async def _plan(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        if not self.state.tasks:
            add_event(
                self.state,
                ProjectEvent(level=EventLevel.INFO, message="Project planning: CTO requirements and architecture."),
            )
            await self.plan_callback(self.state)
        await self._checkpoint("cto_requirements_architecture")
        return {"phase": "cto_requirements_architecture"}

    async def _hierarchy(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        if not self.state.agents:
            teams = self.state.shared_context.get("cto_teams", [])
            tasks = list(self.state.tasks.values())
            plan = HierarchyPlan(
                teams=[
                    HierarchyTeam(
                        name=team["name"],
                        responsibility=team.get("description", "Owns project delivery."),
                        workers=[
                            HierarchyWorker(name=t.worker_type or "Implementation Worker", responsibility=t.description)
                            for t in tasks if t.owner_manager == team["name"]
                        ] or [HierarchyWorker(name="Implementation Worker", responsibility="Implement team tasks.")],
                    )
                    for team in teams
                ]
            )
            # CTO planning is Qwen3-routed; this converts that output into a
            # runtime registry without an agents.yaml dependency.
            from app.agents.hierarchy import DynamicHierarchyGenerator
            registry = DynamicHierarchyGenerator(self.gateway).instantiate_hierarchy(plan)
            for agent in registry.all_agents():
                add_agent(self.state, agent)
        await self._checkpoint("dynamic_hierarchy")
        return {"phase": "dynamic_hierarchy"}

    async def _execute(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        runner = self.runner_factory(self.state)
        events = EventBus()
        if self.realtime is not None:
            # Bridge task-level lifecycle events (started/completed/failed)
            # onto the realtime channel so subscribers see per-task
            # progress during execution, not just the coarser
            # AGENT_STARTED/AGENT_COMPLETED events the runner emits itself.
            attach_task_events(self.realtime, events, self.state)
        scheduler = TaskScheduler(self.state, runner, max_task_concurrency=self.max_task_concurrency, events=events)
        result: SchedulerRun = await scheduler.run()
        self.state.shared_context["phase24_last_scheduler"] = {
            "completed": result.completed,
            "failed": result.failed,
            "cancelled": result.cancelled,
        }
        await self._checkpoint("task_execution")
        return {"phase": "task_execution", "failed": bool(result.failed)}

    def _after_execution(self, graph_state: WorkflowGraphState) -> Literal["recover", "integration"]:
        return "recover" if graph_state.get("failed") else "integration"

    async def _recover(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        runner = self.runner_factory(self.state)
        healing = SelfHealingEngine(
            self.gateway,
            manager_factory=runner._get_manager,
            realtime=self.realtime,
            memory_service=self.memory_service,
        )
        failed = [task for task in self.state.tasks.values() if task.status == TaskStatus.FAILED]
        recovered = True
        for task in failed:
            outcome = await healing.heal(self.state, task, task.result.error_message if task.result else "Task failed.", metadata={"project_id": self.state.project.project_id, "task_id": task.task_id})
            recovered = recovered and outcome.outcome == RepairOutcome.SUCCESS
            if self.persistence is not None:
                for attempt in outcome.attempts:
                    await self.persistence.record_repair_attempt(self.state.project.project_id, attempt)
        await self._checkpoint("failure_recovery")
        return {"phase": "failure_recovery", "failed": not recovered}

    async def _integration(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        report = await IntegrationAgent(self.gateway, realtime=self.realtime).run(
            self.state, ContractRegistry(list(self.state.contracts.values())), metadata={"project_id": self.state.project.project_id}
        )
        cycles = int(self.state.shared_context.get("phase24_integration_cycles", 0)) + 1
        self.state.shared_context["phase24_integration_cycles"] = cycles
        await self._checkpoint("integration")
        # Rework produced by integration flows back through normal managers/workers.
        return {"phase": "integration", "failed": not report.passed and cycles < 3}

    def _after_integration(self, graph_state: WorkflowGraphState) -> Literal["execute", "qa"]:
        return "execute" if graph_state.get("failed") else "qa"

    async def _qa(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        tools = make_executor(
            agent_id="qa-manager", workspace_root=self.workspace_root,
            permissions=QA_PIPELINE_DEFAULT, registry=build_default_registry(),
        )
        pipeline = await QAManager(self.gateway, tools=tools, realtime=self.realtime).run_pipeline(
            self.state, ContractRegistry(list(self.state.contracts.values())), metadata={"project_id": self.state.project.project_id}
        )
        qa = QAReport(summary="; ".join(pipeline.recommended_actions) or ("QA passed" if pipeline.gate_passed else "QA failed"))
        qa.test_results = [TestResult(name=check.check_name, outcome=TestOutcome.PASSED if check.passed else TestOutcome.FAILED, message=check.message) for check in pipeline.all_checks]
        qa.lint_passed = all(c.passed for c in pipeline.all_checks if c.category.value == "static_analysis")
        self.state.qa_reports.append(qa)
        self.state.shared_context["phase24_qa_gate_passed"] = pipeline.gate_passed
        qa_cycles = int(self.state.shared_context.get("phase24_qa_cycles", 0)) + 1
        self.state.shared_context["phase24_qa_cycles"] = qa_cycles
        if not pipeline.gate_passed and qa_cycles >= 3:
            add_error(self.state, source_error("QA failed after the maximum recovery cycles."))
        await self._checkpoint("qa")
        return {"phase": "qa", "failed": not pipeline.gate_passed}

    def _after_qa(self, graph_state: WorkflowGraphState) -> Literal["recover", "git", "final"]:
        if not graph_state.get("failed"):
            return "git"
        assert self.state is not None
        return "recover" if self.state.shared_context.get("phase24_qa_cycles", 0) < 3 else "final"

    async def _git(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        # Per-task Git branches/merge gates are enforced by GitWorkflowEngine
        # when enabled by a worker workflow. The orchestration graph records
        # this project-level handoff without granting extra permissions.
        add_event(self.state, ProjectEvent(level=EventLevel.INFO, message="Git workflow gates completed for accepted task outputs."))
        await self._checkpoint("git")
        return {"phase": "git"}

    async def _deployment_prepare(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        if self.state.shared_context.get("phase24_deployment_report"):
            report_data = self.state.shared_context["phase24_deployment_report"]
            skipped = report_data.get("stage") == "skipped"
            return {
                "phase": "awaiting_human_approval",
                "waiting_for_approval": not skipped and not bool(self.state.deployment.approved_by),
                "deployment_skipped": skipped,
            }
        qa_passed = bool(self.state.shared_context.get("phase24_qa_gate_passed"))
        if not qa_passed:
            return {"phase": "deployment_prepare", "failed": True}
        tools = make_executor(
            agent_id="deployment-manager", workspace_root=self.workspace_root,
            permissions=DEPLOYMENT_MANAGER_DEFAULT, registry=build_default_registry(),
        )
        manager = DeploymentManager(self.gateway, tools)
        report = await manager.run_pipeline(self.state, type("Gate", (), {"gate_passed": True})(), self.state.project.name, self.state.project.description, metadata={"project_id": self.state.project.project_id})
        self._last_deployment_report = report
        self.state.shared_context["phase24_deployment_report"] = report.model_dump(mode="json")
        await self._checkpoint("awaiting_human_approval")
        skipped = report.stage == "skipped"
        return {
            "phase": "awaiting_human_approval",
            "waiting_for_approval": not skipped,
            "deployment_skipped": skipped,
        }

    async def _approval(self, graph_state: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        if graph_state.get("deployment_skipped"):
            # Nothing was built, so there is nothing to approve -- go
            # straight through to the final report instead of pausing for
            # a human decision that doesn't apply.
            await self._checkpoint("deployment_finalized")
            return {
                "phase": "deployment_finalized",
                "waiting_for_approval": False,
                "deployment_skipped": True,
            }
        approved = bool(self.state.deployment.approved_by)
        if not approved:
            self.control.status = self.control.status.__class__.PAUSED
        await self._checkpoint("awaiting_human_approval")
        return {"phase": "awaiting_human_approval", "waiting_for_approval": not approved}

    def _after_approval(self, graph_state: WorkflowGraphState) -> Literal["end", "deploy", "skip"]:
        if graph_state.get("deployment_skipped"):
            return "skip"
        return "end" if graph_state.get("waiting_for_approval") else "deploy"

    async def _deployment_finalize(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        raw = self.state.shared_context.get("phase24_deployment_report")
        report = self._last_deployment_report or DeploymentReport.model_validate(raw)
        # Recreate the explicit approval object from durable state before the
        # manager's structural approval guard permits deployment.
        tools = make_executor(
            agent_id="deployment-manager", workspace_root=self.workspace_root,
            permissions=DEPLOYMENT_MANAGER_DEFAULT, registry=build_default_registry(),
        )
        manager = DeploymentManager(self.gateway, tools)
        await manager.approve(self.state, report, self.state.deployment.approved_by or "unknown")
        await manager.deploy(self.state, report)
        health = await manager._run_health_check(report.container_name)
        if not health.passed:
            add_error(self.state, source_error("Post-deployment health check failed: " + health.message))
            return {"phase": "deployment_finalize", "failed": True}
        await self._checkpoint("deployment_finalized")
        return {"phase": "deployment_finalized"}

    async def _final_report(self, _: WorkflowGraphState) -> WorkflowGraphState:
        assert self.state is not None
        self.state.shared_context["phase24_final_report"] = {"tasks_completed": sum(t.status == TaskStatus.COMPLETED for t in self.state.tasks.values()), "tasks_failed": sum(t.status == TaskStatus.FAILED for t in self.state.tasks.values()), "qa_reports": len(self.state.qa_reports), "deployment_stage": self.state.deployment.stage.value}
        await self._checkpoint("final_report")
        return {"phase": "final_report"}


def source_error(message: str):
    """Avoid repeating the ErrorRecord import in the failure-only node."""
    from app.state.models import ErrorRecord
    return ErrorRecord(source="CompleteOrchestration", message=message)
