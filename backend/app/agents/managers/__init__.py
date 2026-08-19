"""Manager agents (Phase 7)."""

from app.agents.managers.base import BaseManagerAgent, DelegatableWorker, WorkerFactory
from app.agents.managers.concrete import (
    MANAGER_CLASSES,
    AIManager,
    BackendManager,
    DatabaseManager,
    DeploymentManager,
    FrontendManager,
    QAManager,
)
from app.agents.managers.registry import make_manager_tools, make_worker_factory
from app.agents.managers.schemas import (
    ManagerAnalysis,
    ManagerContext,
    ManagerReport,
    ManagerReportStatus,
    ManagerReviewDecision,
    WorkerSelection,
)

__all__ = [
    "MANAGER_CLASSES",
    "AIManager",
    "BackendManager",
    "BaseManagerAgent",
    "DatabaseManager",
    "DelegatableWorker",
    "DeploymentManager",
    "FrontendManager",
    "ManagerAnalysis",
    "ManagerContext",
    "ManagerReport",
    "ManagerReportStatus",
    "ManagerReviewDecision",
    "QAManager",
    "WorkerFactory",
    "WorkerSelection",
    "make_manager_tools",
    "make_worker_factory",
]
