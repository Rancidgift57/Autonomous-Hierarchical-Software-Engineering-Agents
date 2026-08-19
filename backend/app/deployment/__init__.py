"""Deployment System (Phase 15): plans, generates, validates, builds,
starts, verifies, and -- only after explicit human approval -- deploys a
service, with rollback preparation throughout.

Pipeline::

    QA PASS -> build -> Docker build -> start -> health check -> smoke test
      -> deployment ready -> human approval -> deploy
"""

from app.deployment.agents import (
    DeploymentConfigAgent,
    DeploymentPlanningAgent,
    DeploymentScriptAgent,
    DockerfileGeneratorAgent,
)
from app.deployment.events import DeploymentEventBus
from app.deployment.exceptions import (
    ApprovalRequiredError,
    DeploymentError,
    DeploymentPipelineFailedError,
    QAGateNotPassedError,
    ValidationFailedError,
)
from app.deployment.manager import DeploymentManager
from app.deployment.schemas import (
    DeploymentApproval,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentPlan,
    DeploymentReport,
    EnvVarSpec,
    GeneratedDeploymentConfig,
    GeneratedDeploymentScript,
    GeneratedDockerfile,
    HealthCheckResult,
    RollbackPlan,
    SmokeTestCase,
    SmokeTestResult,
    ValidationResult,
)

__all__ = [
    "ApprovalRequiredError",
    "DeploymentApproval",
    "DeploymentConfigAgent",
    "DeploymentError",
    "DeploymentEvent",
    "DeploymentEventBus",
    "DeploymentEventType",
    "DeploymentManager",
    "DeploymentPipelineFailedError",
    "DeploymentPlan",
    "DeploymentPlanningAgent",
    "DeploymentReport",
    "DeploymentScriptAgent",
    "DockerfileGeneratorAgent",
    "EnvVarSpec",
    "GeneratedDeploymentConfig",
    "GeneratedDeploymentScript",
    "GeneratedDockerfile",
    "HealthCheckResult",
    "QAGateNotPassedError",
    "RollbackPlan",
    "SmokeTestCase",
    "SmokeTestResult",
    "ValidationFailedError",
    "ValidationResult",
]
