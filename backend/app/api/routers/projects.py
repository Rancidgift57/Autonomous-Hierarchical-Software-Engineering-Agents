"""`/api/projects` router (Phase 16).

Every handler here does exactly one thing: parse the request, call one
`ProjectService` method, shape the response. No agent/orchestration logic
lives in this module -- see `app.api.services.project_service` (service)
and `app.orchestration.project_runner` (orchestration) for that.

Error translation (`ProjectNotFoundError` -> 404, `InvalidProjectStateError`
-> 409) is registered once as FastAPI exception handlers in
`app.api.app.create_app`, not repeated in every handler below.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_project_service
from app.api.schemas import (
    DeploymentApprovalRequest,
    EventOut,
    ProjectCreateRequest,
    ProjectDetail,
    ProjectStatusResponse,
    ProjectSummary,
    RunControlResponse,
)
from app.api.security import Principal, get_current_principal
from app.api.services.project_service import ProjectService
from app.state.models import AgentDefinition, Artifact, DeploymentState, QAReport, Task

router = APIRouter(
    prefix="/api/projects",
    tags=["projects"],
    dependencies=[Depends(get_current_principal)],
)


# ---------------------------------------------------------------------------
# Create / list / get
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectDetail, status_code=201)
async def create_project(
    body: ProjectCreateRequest, service: ProjectService = Depends(get_project_service)
) -> ProjectDetail:
    state = await service.create_project(
        name=body.name,
        description=body.description,
        idea_prompt=body.idea_prompt,
        repo_url=body.repo_url,
    )
    status_info = service.get_status(state.project.project_id)
    return ProjectDetail(
        project_id=state.project.project_id,
        name=state.project.name,
        description=state.project.description,
        idea_prompt=state.project.idea_prompt,
        repo_url=state.project.repo_url,
        status=status_info["status"],
        created_at=state.project.created_at,
        updated_at=state.project.updated_at,
        task_count=len(state.tasks),
        error=status_info["error"],
    )


@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    service: ProjectService = Depends(get_project_service),
) -> list[ProjectSummary]:
    summaries = []
    for state in service.list_projects():
        status_info = service.get_status(state.project.project_id)
        summaries.append(
            ProjectSummary(
                project_id=state.project.project_id,
                name=state.project.name,
                description=state.project.description,
                status=status_info["status"],
                created_at=state.project.created_at,
                updated_at=state.project.updated_at,
            )
        )
    return summaries


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> ProjectDetail:
    state = service.get_project(project_id)
    status_info = service.get_status(project_id)
    return ProjectDetail(
        project_id=state.project.project_id,
        name=state.project.name,
        description=state.project.description,
        idea_prompt=state.project.idea_prompt,
        repo_url=state.project.repo_url,
        status=status_info["status"],
        created_at=state.project.created_at,
        updated_at=state.project.updated_at,
        task_count=len(state.tasks),
        error=status_info["error"],
    )


# ---------------------------------------------------------------------------
# Run control
# ---------------------------------------------------------------------------


@router.post("/{project_id}/run", response_model=RunControlResponse)
async def run_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> RunControlResponse:
    control = await service.run_project(project_id)
    return RunControlResponse(project_id=project_id, status=control.status, message="Run started.")


@router.post("/{project_id}/pause", response_model=RunControlResponse)
async def pause_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> RunControlResponse:
    control = service.pause_project(project_id)
    return RunControlResponse(
        project_id=project_id, status=control.status, message="Pause requested."
    )


@router.post("/{project_id}/resume", response_model=RunControlResponse)
async def resume_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> RunControlResponse:
    control = service.resume_project(project_id)
    return RunControlResponse(project_id=project_id, status=control.status, message="Resumed.")


@router.post("/{project_id}/cancel", response_model=RunControlResponse)
async def cancel_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> RunControlResponse:
    control = service.cancel_project(project_id)
    return RunControlResponse(
        project_id=project_id, status=control.status, message="Cancel requested."
    )


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------


@router.get("/{project_id}/status", response_model=ProjectStatusResponse)
async def get_status(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> ProjectStatusResponse:
    return ProjectStatusResponse(**service.get_status(project_id))


@router.get("/{project_id}/agents", response_model=list[AgentDefinition])
async def get_agents(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> list[AgentDefinition]:
    return service.get_agents(project_id)


@router.get("/{project_id}/tasks", response_model=list[Task])
async def get_tasks(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> list[Task]:
    return service.get_tasks(project_id)


@router.get("/{project_id}/artifacts", response_model=list[Artifact])
async def get_artifacts(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> list[Artifact]:
    return service.get_artifacts(project_id)


@router.get("/{project_id}/events", response_model=list[EventOut])
async def get_events(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> list[EventOut]:
    return [EventOut(**e) for e in service.get_events(project_id)]


@router.get("/{project_id}/qa", response_model=list[QAReport])
async def get_qa_reports(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> list[QAReport]:
    return service.get_qa_reports(project_id)


@router.get("/{project_id}/deployment", response_model=DeploymentState)
async def get_deployment(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> DeploymentState:
    return service.get_deployment(project_id)


# ---------------------------------------------------------------------------
# Deployment approval
# ---------------------------------------------------------------------------


@router.post("/{project_id}/approve-deployment", response_model=DeploymentState)
async def approve_deployment(
    project_id: str,
    body: DeploymentApprovalRequest,
    service: ProjectService = Depends(get_project_service),
    principal: Principal = Depends(get_current_principal),
) -> DeploymentState:
    approved_by = body.approved_by or (principal.subject if principal.authenticated else "unknown")
    return service.approve_deployment(project_id, approved_by=approved_by, notes=body.notes)
