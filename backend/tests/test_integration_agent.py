"""Unit tests for app.agents.system (Phase 11 -- Integration Agent)."""

from __future__ import annotations

import pytest

from app.agents.system import (
    APIContractValidator,
    APIEndpointUsage,
    ContractRegistry,
    DatabaseContractValidator,
    DatabaseUsage,
    EnvironmentContractValidator,
    EnvironmentUsage,
    EventSchemaUsage,
    IntegrationAgent,
    IntegrationAnalysis,
    MismatchKind,
    ServiceDependency,
    validate_event_schemas,
    validate_service_dependencies,
)
from app.llm.models import TaskType
from app.state.enums import ContractType
from app.state.models import (
    AHSEAState,
    APIContract,
    Contract,
    DatabaseContract,
    EnvironmentContract,
    ProjectMetadata,
)


def make_state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )


# ---------------------------------------------------------------------------
# APIContractValidator -- the login example from the spec
# ---------------------------------------------------------------------------


def test_api_validator_detects_missing_endpoint():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    usage = [APIEndpointUsage(component="frontend", method="POST", path="/api/users/login")]

    mismatches = APIContractValidator().validate(registry, usage)

    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.MISSING_ENDPOINT
    assert mismatches[0].responsible_team == "Backend"
    assert "frontend" in mismatches[0].affected_components


def test_api_validator_passes_when_endpoint_matches():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    usage = [APIEndpointUsage(component="frontend", method="POST", path="/auth/login")]

    assert APIContractValidator().validate(registry, usage) == []


def test_api_validator_detects_method_mismatch():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="users",
                api=APIContract(endpoint="/users", method="GET"),
            )
        ]
    )
    usage = [APIEndpointUsage(component="frontend", method="DELETE", path="/users")]

    mismatches = APIContractValidator().validate(registry, usage)

    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.METHOD_MISMATCH


# ---------------------------------------------------------------------------
# DatabaseContractValidator
# ---------------------------------------------------------------------------


def test_database_validator_detects_missing_table_and_column():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.DATABASE,
                name="users_table",
                database=DatabaseContract(
                    table_name="users", columns={"id": "uuid", "email": "text"}
                ),
            )
        ]
    )
    usages = [
        DatabaseUsage(component="backend", table_name="users", columns=["id", "phone"]),
        DatabaseUsage(component="backend", table_name="sessions", columns=["id"]),
    ]

    mismatches = DatabaseContractValidator().validate(registry, usages)

    kinds = {m.kind for m in mismatches}
    assert MismatchKind.MISSING_COLUMN in kinds
    assert MismatchKind.MISSING_TABLE in kinds
    assert all(m.responsible_team == "Database" for m in mismatches)


# ---------------------------------------------------------------------------
# EnvironmentContractValidator
# ---------------------------------------------------------------------------


def test_environment_validator_detects_missing_required_var():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.ENVIRONMENT,
                name="db_url",
                environment=EnvironmentContract(key="DATABASE_URL", required=True),
            )
        ]
    )

    mismatches = EnvironmentContractValidator().validate(registry, [], provided_keys=set())

    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.MISSING_ENV_VAR


def test_environment_validator_detects_undeclared_var():
    registry = ContractRegistry([])
    usages = [EnvironmentUsage(component="backend", key="SECRET_KEY")]

    mismatches = EnvironmentContractValidator().validate(registry, usages)

    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.UNDECLARED_ENV_VAR


def test_environment_validator_passes_when_provided():
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.ENVIRONMENT,
                name="db_url",
                environment=EnvironmentContract(key="DATABASE_URL", required=True),
            )
        ]
    )
    mismatches = EnvironmentContractValidator().validate(
        registry, [], provided_keys={"DATABASE_URL"}
    )
    assert mismatches == []


# ---------------------------------------------------------------------------
# Event schema / service dependency checks
# ---------------------------------------------------------------------------


def test_event_schema_detects_missing_publisher():
    usages = [EventSchemaUsage(component="frontend", event_name="user.created", role="subscriber")]
    mismatches = validate_event_schemas(usages)
    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.MISSING_SUBSCRIBER


def test_event_schema_detects_payload_mismatch():
    usages = [
        EventSchemaUsage(
            component="backend",
            event_name="user.created",
            role="publisher",
            payload_schema={"id": "uuid"},
        ),
        EventSchemaUsage(
            component="notifications",
            event_name="user.created",
            role="subscriber",
            payload_schema={"user_id": "uuid"},
        ),
    ]
    mismatches = validate_event_schemas(usages)
    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.EVENT_SCHEMA_MISMATCH


def test_service_dependency_detects_unresolved_dependency():
    deps = [ServiceDependency(service="backend", depends_on_service="redis")]
    mismatches = validate_service_dependencies(deps, known_services={"backend", "postgres"})
    assert len(mismatches) == 1
    assert mismatches[0].kind == MismatchKind.UNRESOLVED_SERVICE_DEPENDENCY


# ---------------------------------------------------------------------------
# IntegrationAgent end-to-end
# ---------------------------------------------------------------------------


class FakeGateway:
    """Only accepts task_type=INTEGRATION_REASONING calls."""

    def __init__(self):
        self.calls: list[TaskType] = []

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        if task_type != TaskType.INTEGRATION_REASONING:
            raise AssertionError(
                f"IntegrationAgent requested non-INTEGRATION_REASONING task_type: {task_type}"
            )
        assert response_model is IntegrationAnalysis
        return IntegrationAnalysis(summary="1 mismatch found.", additional_risks=[])


@pytest.mark.asyncio
async def test_integration_agent_reports_mismatch_and_only_uses_integration_reasoning():
    state = make_state()
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    gateway = FakeGateway()
    agent = IntegrationAgent(gateway=gateway)

    report = await agent.run(
        state=state,
        registry=registry,
        api_usages=[APIEndpointUsage(component="frontend", method="POST", path="/api/users/login")],
    )

    assert not report.passed
    assert len(report.mismatches) == 1
    assert report.mismatches[0].responsible_team == "Backend"
    assert gateway.calls == [TaskType.INTEGRATION_REASONING]


@pytest.mark.asyncio
async def test_integration_agent_never_modifies_code_creates_rework_task_instead():
    state = make_state()
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    gateway = FakeGateway()
    agent = IntegrationAgent(gateway=gateway)

    # IntegrationAgent has no tools attribute at all -- it structurally
    # cannot call write_file/edit_file/run_command.
    assert not hasattr(agent, "tools")

    report = await agent.run(
        state=state,
        registry=registry,
        api_usages=[APIEndpointUsage(component="frontend", method="POST", path="/api/users/login")],
    )

    assert len(report.created_rework_task_ids) == 1
    task_id = report.created_rework_task_ids[0]
    assert task_id in state.tasks
    assert state.tasks[task_id].owner_manager == "Backend"


@pytest.mark.asyncio
async def test_integration_agent_passes_with_no_mismatches():
    state = make_state()
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    gateway = FakeGateway()
    agent = IntegrationAgent(gateway=gateway)

    report = await agent.run(
        state=state,
        registry=registry,
        api_usages=[APIEndpointUsage(component="frontend", method="POST", path="/auth/login")],
    )

    assert report.passed
    assert report.created_rework_task_ids == []


# ---------------------------------------------------------------------------
# Phase 19: realtime event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_agent_emits_integration_failed_on_mismatch():
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager
    from app.realtime.schemas import RealtimeEventType

    state = make_state()
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    agent = IntegrationAgent(gateway=FakeGateway(), realtime=emitter)

    report = await agent.run(
        state=state,
        registry=registry,
        api_usages=[APIEndpointUsage(component="frontend", method="POST", path="/api/users/login")],
    )

    assert not report.passed
    events = conn_manager.replay(state.project.project_id)
    assert len(events) == 1
    assert events[0].event_type == RealtimeEventType.INTEGRATION_FAILED
    assert events[0].payload["mismatch_count"] == 1
    assert events[0].payload["mismatches_by_team"] == {"Backend": 1}


@pytest.mark.asyncio
async def test_integration_agent_emits_nothing_when_no_mismatches():
    from app.realtime.emitter import RealtimeEmitter
    from app.realtime.manager import ConnectionManager

    state = make_state()
    registry = ContractRegistry(
        [
            Contract(
                contract_type=ContractType.API,
                name="login",
                api=APIContract(endpoint="/auth/login", method="POST"),
            )
        ]
    )
    conn_manager = ConnectionManager()
    emitter = RealtimeEmitter(conn_manager, project_id=state.project.project_id)
    agent = IntegrationAgent(gateway=FakeGateway(), realtime=emitter)

    report = await agent.run(
        state=state,
        registry=registry,
        api_usages=[APIEndpointUsage(component="frontend", method="POST", path="/auth/login")],
    )

    assert report.passed
    assert conn_manager.replay(state.project.project_id) == []
