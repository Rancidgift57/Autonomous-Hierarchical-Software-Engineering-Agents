"""System agents: Integration Agent (Phase 11), QA System (Phase 12), and
self-healing (Phase 13) all live under `app.agents.system` / dedicated
top-level packages -- see `app.qa` and `app.self_healing`."""

from app.agents.system.integration import IntegrationAgent, IntegrationAnalysis, IntegrationReport
from app.agents.system.integration_schemas import (
    APIContractValidator,
    APIEndpointUsage,
    ContractMismatch,
    ContractRegistry,
    DatabaseContractValidator,
    DatabaseUsage,
    EnvironmentContractValidator,
    EnvironmentUsage,
    EventSchemaUsage,
    MismatchKind,
    ServiceDependency,
    validate_event_schemas,
    validate_service_dependencies,
)

__all__ = [
    "APIContractValidator",
    "APIEndpointUsage",
    "ContractMismatch",
    "ContractRegistry",
    "DatabaseContractValidator",
    "DatabaseUsage",
    "EnvironmentContractValidator",
    "EnvironmentUsage",
    "EventSchemaUsage",
    "IntegrationAgent",
    "IntegrationAnalysis",
    "IntegrationReport",
    "MismatchKind",
    "ServiceDependency",
    "validate_event_schemas",
    "validate_service_dependencies",
]
