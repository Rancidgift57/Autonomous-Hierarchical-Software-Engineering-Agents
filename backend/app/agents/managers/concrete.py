"""Concrete manager agents (Phase 7).

Five of the six (`BackendManager`, `FrontendManager`, `DatabaseManager`,
`AIManager`, `QAManager`) are thin `BaseManagerAgent` subclasses that only
set `team_name`/`managed_worker_types`/`role_description` -- all workflow
behavior comes from the base class.

`DeploymentManager` is the one manager with no Phase 8 workers to
delegate to (there is no "deployment_worker" in the Phase 8 worker list),
so it overrides `_direct_execution` to use its own (read-only, see
`app.agents.managers.registry.make_manager_tools`) tool access to inspect
repository state instead. This exercises the generic abstraction's
"managed_worker_types is empty" branch.
"""

from __future__ import annotations

from typing import Any

from app.agents.managers.base import BaseManagerAgent
from app.agents.managers.schemas import (
    ManagerAnalysis,
    ManagerContext,
    ManagerReport,
    ManagerReportStatus,
)
from app.state.models import Task
from app.tools.exceptions import ToolError


class BackendManager(BaseManagerAgent):
    team_name = "Backend"
    managed_worker_types = ["api_worker", "auth_worker", "service_worker"]
    role_description = "Coordinates backend implementation work and owns the API/service layer."


class FrontendManager(BaseManagerAgent):
    team_name = "Frontend"
    managed_worker_types = ["ui_worker", "component_worker"]
    role_description = "Coordinates frontend implementation work and owns the UI layer."


class DatabaseManager(BaseManagerAgent):
    team_name = "Database"
    managed_worker_types = ["schema_worker", "migration_worker"]
    role_description = "Coordinates schema design and migration work."


class AIManager(BaseManagerAgent):
    team_name = "AI"
    managed_worker_types = ["model_worker", "evaluation_worker"]
    role_description = "Coordinates LLM integration and evaluation work."


class QAManager(BaseManagerAgent):
    team_name = "QA"
    # QA work can touch any layer of the codebase (writing missing tests,
    # fixing a failure a worker's own self-review missed, ...), so unlike
    # the other managers it isn't scoped to one team's worker types.
    managed_worker_types = [
        "api_worker", "auth_worker", "service_worker", "ui_worker",
        "component_worker", "schema_worker", "migration_worker",
        "model_worker", "evaluation_worker",
    ]
    role_description = "Runs automated QA and delegates fixes to the appropriate worker."


class DeploymentManager(BaseManagerAgent):
    team_name = "Deployment"
    managed_worker_types = []  # no Phase 8 worker type fits deployment prep
    role_description = "Prepares deployments, subject to explicit approval."

    async def _direct_execution(
        self,
        task: Task,
        context: ManagerContext,
        analysis: ManagerAnalysis,
        metadata: dict[str, Any] | None,
    ) -> ManagerReport:
        """Inspect repository state via read-only tools instead of delegating.

        Deployment execution itself (actually shipping something) is
        explicitly out of scope for a manager -- it has no EXECUTE/WRITE
        tool permission (see `make_manager_tools`) and no worker to hand
        the task to. This step only gathers information to report upward;
        an EXECUTE-capable Deployment system agent (outside Phase 7/8's
        scope) would act on that report.
        """

        if self.tools is None:
            return ManagerReport(
                task_id=task.task_id,
                manager_id=self.manager_id,
                team_name=self.team_name,
                status=ManagerReportStatus.NO_ELIGIBLE_WORKER,
                summary="No workspace tools configured; cannot inspect deployment readiness.",
            )

        try:
            status_result = await self.tools.run("git_status")
        except ToolError as exc:
            return ManagerReport(
                task_id=task.task_id,
                manager_id=self.manager_id,
                team_name=self.team_name,
                status=ManagerReportStatus.FAILED,
                errors=[str(exc)],
            )

        return ManagerReport(
            task_id=task.task_id,
            manager_id=self.manager_id,
            team_name=self.team_name,
            status=ManagerReportStatus.ACCEPTED,
            summary=f"Deployment readiness check: {analysis.summary}",
            artifacts=[],
            errors=[] if status_result.success else [status_result.error or "git_status failed"],
        )


#: team_name -> concrete class, mirroring `app.agents.workers.concrete.WORKER_CLASSES`.
MANAGER_CLASSES: dict[str, type[BaseManagerAgent]] = {
    cls.team_name: cls
    for cls in (
        BackendManager,
        FrontendManager,
        DatabaseManager,
        AIManager,
        QAManager,
        DeploymentManager,
    )
}
