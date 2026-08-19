"""Contract types, `ContractRegistry`, and validators for the Integration
Agent (Phase 11).

Reuses `app.state.models.Contract`/`APIContract`/`DatabaseContract`/
`EnvironmentContract` as the *declared* source of truth (what the
architecture says should exist), and adds lightweight "usage" records --
what frontend/backend/deployment code actually reference -- so validators
can diff declared-vs-used and report a concrete mismatch, e.g. the
`POST /api/users/login` (frontend) vs `POST /auth/login` (backend)
example from the spec.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.state.enums import ContractType, ErrorSeverity
from app.state.models import Contract, DatabaseContract

# ---------------------------------------------------------------------------
# Usage records: what components actually reference, gathered from
# artifacts/worker output rather than the architecture's declared contracts.
# ---------------------------------------------------------------------------


class APIEndpointUsage(BaseModel):
    """One HTTP call a component makes or exposes."""

    component: str = Field(description="e.g. 'frontend' or 'backend'.")
    method: str
    path: str
    description: str | None = None


class DatabaseUsage(BaseModel):
    """One table/column reference a component makes."""

    component: str
    table_name: str
    columns: list[str] = Field(default_factory=list)


class EnvironmentUsage(BaseModel):
    """One environment variable a component reads."""

    component: str
    key: str


class EventSchemaUsage(BaseModel):
    """One event a component publishes or subscribes to."""

    component: str
    event_name: str
    role: str = Field(description="'publisher' or 'subscriber'.")
    payload_schema: dict[str, str] = Field(default_factory=dict)


class ServiceDependency(BaseModel):
    """A declared runtime dependency of one service on another."""

    service: str
    depends_on_service: str
    required: bool = True


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


class MismatchKind(str, Enum):
    MISSING_ENDPOINT = "missing_endpoint"
    METHOD_MISMATCH = "method_mismatch"
    MISSING_TABLE = "missing_table"
    MISSING_COLUMN = "missing_column"
    MISSING_ENV_VAR = "missing_env_var"
    UNDECLARED_ENV_VAR = "undeclared_env_var"
    EVENT_SCHEMA_MISMATCH = "event_schema_mismatch"
    MISSING_SUBSCRIBER = "missing_subscriber"
    UNRESOLVED_SERVICE_DEPENDENCY = "unresolved_service_dependency"


class ContractMismatch(BaseModel):
    """A single detected incompatibility, in exactly the shape Phase 11 asks
    the Integration Agent to return: failure + affected_components +
    responsible_team + recommended_fix."""

    kind: MismatchKind
    contract_type: ContractType
    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    failure: str
    affected_components: list[str] = Field(default_factory=list)
    responsible_team: str = Field(
        description="Team whose contribution should be reworked, e.g. 'Frontend', 'Backend'."
    )
    recommended_fix: str


# ---------------------------------------------------------------------------
# ContractRegistry
# ---------------------------------------------------------------------------


class ContractRegistry:
    """In-memory store of *declared* contracts (`app.state.models.Contract`).

    This is the single source of truth validators diff usage records
    against. `IntegrationAgent` builds one from whatever the architecture
    phase (Phase 5/7) has produced and registered into `AHSEAState.
    contracts`.
    """

    def __init__(self, contracts: list[Contract] | None = None):
        self._contracts: dict[str, Contract] = {c.contract_id: c for c in (contracts or [])}

    def register(self, contract: Contract) -> Contract:
        self._contracts[contract.contract_id] = contract
        return contract

    def get(self, contract_id: str) -> Contract | None:
        return self._contracts.get(contract_id)

    def all(self) -> list[Contract]:
        return list(self._contracts.values())

    def by_type(self, contract_type: ContractType) -> list[Contract]:
        return [c for c in self._contracts.values() if c.contract_type == contract_type]

    def api_contracts(self) -> list[Contract]:
        return [c for c in self.by_type(ContractType.API) if c.api is not None]

    def database_contracts(self) -> list[Contract]:
        return [c for c in self.by_type(ContractType.DATABASE) if c.database is not None]

    def environment_contracts(self) -> list[Contract]:
        return [c for c in self.by_type(ContractType.ENVIRONMENT) if c.environment is not None]


def _normalize_path(path: str) -> str:
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# APIContractValidator
# ---------------------------------------------------------------------------


class APIContractValidator:
    """Detects frontend/backend API mismatches against declared `APIContract`s."""

    def validate(
        self,
        registry: ContractRegistry,
        usages: list[APIEndpointUsage],
    ) -> list[ContractMismatch]:
        declared = {
            (_normalize_path(c.api.endpoint), c.api.method.upper()): c
            for c in registry.api_contracts()
        }
        declared_paths = {p for p, _m in declared}

        mismatches: list[ContractMismatch] = []
        for usage in usages:
            key = (_normalize_path(usage.path), usage.method.upper())
            if key in declared:
                continue

            same_path_diff_method = [m for (p, m) in declared if p == key[0] and m != key[1]]
            if same_path_diff_method:
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.METHOD_MISMATCH,
                        contract_type=ContractType.API,
                        severity=ErrorSeverity.HIGH,
                        failure=(
                            f"{usage.component} calls {usage.method.upper()} {usage.path}, "
                            f"but the contract declares {same_path_diff_method[0]} for that path."
                        ),
                        affected_components=[usage.component, "backend"],
                        responsible_team="Backend" if usage.component != "backend" else "Frontend",
                        recommended_fix=(
                            f"Align on a single HTTP method for {usage.path} between "
                            f"{usage.component} and the API contract."
                        ),
                    )
                )
                continue

            # No path matches at all -- classic "frontend calls a route the
            # backend never defined" case (the login example from the spec).
            closest = _closest_path(key[0], declared_paths)
            failure = (
                f"{usage.component} calls {usage.method.upper()} {usage.path}, which has no "
                "matching backend contract."
            )
            if closest:
                failure += f" Closest declared route: {closest}."
            mismatches.append(
                ContractMismatch(
                    kind=MismatchKind.MISSING_ENDPOINT,
                    contract_type=ContractType.API,
                    severity=ErrorSeverity.HIGH,
                    failure=failure,
                    affected_components=[usage.component, "backend"],
                    responsible_team="Backend",
                    recommended_fix=(
                        f"Backend should expose {usage.method.upper()} {usage.path}, or the "
                        f"contract/frontend should be updated to call the correct existing route"
                        + (f" ({closest})." if closest else ".")
                    ),
                )
            )
        return mismatches


def _closest_path(target: str, candidates: set[str]) -> str | None:
    """Cheap best-effort suggestion: same trailing segment counts as close."""

    target_parts = [p for p in target.split("/") if p]
    best: str | None = None
    best_score = 0
    for candidate in candidates:
        cand_parts = [p for p in candidate.split("/") if p]
        score = len(set(target_parts) & set(cand_parts))
        if score > best_score:
            best_score = score
            best = candidate
    return best


# ---------------------------------------------------------------------------
# DatabaseContractValidator
# ---------------------------------------------------------------------------


class DatabaseContractValidator:
    """Detects backend/database mismatches against declared `DatabaseContract`s."""

    def validate(
        self,
        registry: ContractRegistry,
        usages: list[DatabaseUsage],
    ) -> list[ContractMismatch]:
        declared_by_table: dict[str, DatabaseContract] = {
            c.database.table_name: c.database for c in registry.database_contracts()
        }

        mismatches: list[ContractMismatch] = []
        for usage in usages:
            declared_table = declared_by_table.get(usage.table_name)
            if declared_table is None:
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.MISSING_TABLE,
                        contract_type=ContractType.DATABASE,
                        severity=ErrorSeverity.HIGH,
                        failure=(
                            f"{usage.component} references table '{usage.table_name}', which "
                            "has no declared database contract."
                        ),
                        affected_components=[usage.component, "database"],
                        responsible_team="Database",
                        recommended_fix=(
                            f"Database team should add a contract for '{usage.table_name}', or "
                            f"{usage.component} should be updated to use an existing table."
                        ),
                    )
                )
                continue

            missing_columns = [c for c in usage.columns if c not in declared_table.columns]
            for column in missing_columns:
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.MISSING_COLUMN,
                        contract_type=ContractType.DATABASE,
                        severity=ErrorSeverity.MEDIUM,
                        failure=(
                            f"{usage.component} references column "
                            f"'{usage.table_name}.{column}', which is not declared in the "
                            "database contract."
                        ),
                        affected_components=[usage.component, "database"],
                        responsible_team="Database",
                        recommended_fix=(
                            f"Add column '{column}' to the '{usage.table_name}' contract, or "
                            f"correct {usage.component}'s reference."
                        ),
                    )
                )
        return mismatches


# ---------------------------------------------------------------------------
# EnvironmentContractValidator
# ---------------------------------------------------------------------------


class EnvironmentContractValidator:
    """Detects environment-variable mismatches against declared `EnvironmentContract`s."""

    def validate(
        self,
        registry: ContractRegistry,
        usages: list[EnvironmentUsage],
        provided_keys: set[str] | None = None,
    ) -> list[ContractMismatch]:
        declared = {c.environment.key: c.environment for c in registry.environment_contracts()}
        provided_keys = provided_keys or set()

        mismatches: list[ContractMismatch] = []

        # Required-but-unprovided: a declared, required var with no default
        # and no value supplied anywhere.
        for key, env_contract in declared.items():
            if (
                env_contract.required
                and env_contract.default_value is None
                and key not in provided_keys
            ):
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.MISSING_ENV_VAR,
                        contract_type=ContractType.ENVIRONMENT,
                        severity=ErrorSeverity.CRITICAL,
                        failure=f"Required environment variable '{key}' is not provided anywhere.",
                        affected_components=["deployment"],
                        responsible_team="Deployment",
                        recommended_fix=(
                            f"Set '{key}' in the deployment environment/.env, or mark it "
                            "optional with a default value if it truly is."
                        ),
                    )
                )

        # Used-but-undeclared: some component reads a var no contract admits exists.
        for usage in usages:
            if usage.key not in declared:
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.UNDECLARED_ENV_VAR,
                        contract_type=ContractType.ENVIRONMENT,
                        severity=ErrorSeverity.MEDIUM,
                        failure=(
                            f"{usage.component} reads environment variable '{usage.key}', which "
                            "has no environment contract."
                        ),
                        affected_components=[usage.component],
                        responsible_team=usage.component.title(),
                        recommended_fix=(
                            f"Add an EnvironmentContract for '{usage.key}', or remove the "
                            f"undeclared reference in {usage.component}."
                        ),
                    )
                )
        return mismatches


# ---------------------------------------------------------------------------
# Event-schema and service-dependency checks
#
# The spec lists these as things the Integration Agent must validate, but
# (unlike API/DB/env) doesn't ask for dedicated validator classes -- they're
# implemented as plain functions the agent calls directly.
# ---------------------------------------------------------------------------


def validate_event_schemas(usages: list[EventSchemaUsage]) -> list[ContractMismatch]:
    """Every subscribed event must have at least one publisher, and every
    publisher/subscriber pair for the same event must agree on payload keys."""

    mismatches: list[ContractMismatch] = []
    by_event: dict[str, list[EventSchemaUsage]] = {}
    for usage in usages:
        by_event.setdefault(usage.event_name, []).append(usage)

    for event_name, group in by_event.items():
        publishers = [u for u in group if u.role == "publisher"]
        subscribers = [u for u in group if u.role == "subscriber"]

        if subscribers and not publishers:
            mismatches.append(
                ContractMismatch(
                    kind=MismatchKind.MISSING_SUBSCRIBER,
                    contract_type=ContractType.API,
                    severity=ErrorSeverity.HIGH,
                    failure=f"Event '{event_name}' has subscriber(s) but no publisher.",
                    affected_components=[u.component for u in subscribers],
                    responsible_team="Backend",
                    recommended_fix=f"Implement a publisher for event '{event_name}'.",
                )
            )
            continue

        if not publishers:
            continue

        reference_schema = publishers[0].payload_schema
        for other in publishers[1:] + subscribers:
            if other.payload_schema and other.payload_schema != reference_schema:
                mismatches.append(
                    ContractMismatch(
                        kind=MismatchKind.EVENT_SCHEMA_MISMATCH,
                        contract_type=ContractType.API,
                        severity=ErrorSeverity.MEDIUM,
                        failure=(
                            f"Event '{event_name}' payload schema mismatch between "
                            f"'{publishers[0].component}' and '{other.component}'."
                        ),
                        affected_components=[publishers[0].component, other.component],
                        responsible_team=other.component.title(),
                        recommended_fix=(
                            f"Align the '{event_name}' payload schema across all publishers "
                            "and subscribers."
                        ),
                    )
                )
    return mismatches


def validate_service_dependencies(
    dependencies: list[ServiceDependency], known_services: set[str]
) -> list[ContractMismatch]:
    """Every required dependency must point at a service that actually exists."""

    mismatches: list[ContractMismatch] = []
    for dep in dependencies:
        if dep.depends_on_service not in known_services:
            mismatches.append(
                ContractMismatch(
                    kind=MismatchKind.UNRESOLVED_SERVICE_DEPENDENCY,
                    contract_type=ContractType.ENVIRONMENT,
                    severity=ErrorSeverity.CRITICAL if dep.required else ErrorSeverity.LOW,
                    failure=(
                        f"Service '{dep.service}' depends on '{dep.depends_on_service}', which "
                        "is not a known/deployed service."
                    ),
                    affected_components=[dep.service],
                    responsible_team="Deployment",
                    recommended_fix=(
                        f"Deploy/register '{dep.depends_on_service}', or remove the dependency "
                        f"declaration on '{dep.service}'."
                    ),
                )
            )
    return mismatches
