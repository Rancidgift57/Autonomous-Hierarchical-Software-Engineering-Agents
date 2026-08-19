"""Phase 22 observability endpoints."""
from fastapi import APIRouter, Depends

from app.api.security import get_current_principal
from app.observability.service import ObservabilityService

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(get_current_principal)])


def get_observability_service() -> ObservabilityService:
    return ObservabilityService()


@router.get("")
async def get_metrics(project_id: str | None = None, service: ObservabilityService = Depends(get_observability_service)) -> dict:
    """Aggregated, prompt-free metrics filtered by optional project ID."""
    return await service.metrics(project_id)


@router.get("/agents")
async def get_agent_scorecards(
    project_id: str | None = None,
    recent_days: int = 7,
    service: ObservabilityService = Depends(get_observability_service),
) -> list[dict]:
    """Phase 23: per-agent performance -- success rate, average duration,
    models/task types used, and a trend signal (recent `recent_days` days
    vs. everything before that), one entry per `agent_id`.
    """
    return await service.agent_scorecards(project_id, recent_days=recent_days)


@router.get("/task-types")
async def get_task_type_model_scorecards(
    project_id: str | None = None,
    service: ObservabilityService = Depends(get_observability_service),
) -> list[dict]:
    """Phase 23: success rate and average duration broken down by
    (task_type, model) -- the data a future model-routing feedback loop
    would consume.
    """
    return await service.task_type_model_scorecards(project_id)
