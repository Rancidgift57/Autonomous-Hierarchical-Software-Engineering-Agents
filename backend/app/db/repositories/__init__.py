"""Repository layer (Phase 17).

Repositories are the only code allowed to build SQLAlchemy `select` /
`insert` / `delete` statements. Every table listed in the Phase 17 spec has
exactly one repository class here; `app.db.persistence_service` is the only
caller.
"""

from __future__ import annotations

from app.db.repositories.agent_repository import AgentRepository
from app.db.repositories.architecture_repository import ArchitectureDecisionRepository
from app.db.repositories.artifact_repository import ArtifactRepository
from app.db.repositories.contract_repository import ContractRepository
from app.db.repositories.deployment_repository import DeploymentRunRepository
from app.db.repositories.error_repository import ErrorRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.llm_request_repository import LLMRequestRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.repair_attempt_repository import RepairAttemptRepository
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.test_result_repository import TestResultRepository

__all__ = [
    "AgentRepository",
    "ArchitectureDecisionRepository",
    "ArtifactRepository",
    "ContractRepository",
    "DeploymentRunRepository",
    "ErrorRepository",
    "EventRepository",
    "LLMRequestRepository",
    "ProjectRepository",
    "RepairAttemptRepository",
    "TaskRepository",
    "TestResultRepository",
]
