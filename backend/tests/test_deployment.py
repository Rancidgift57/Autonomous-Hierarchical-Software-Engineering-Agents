"""Unit tests for app.deployment (Phase 15 -- Deployment System)."""

from __future__ import annotations

import pytest

from app.deployment.exceptions import (
    ApprovalRequiredError,
    DeploymentPipelineFailedError,
    QAGateNotPassedError,
    ValidationFailedError,
)
from app.deployment.manager import DeploymentManager
from app.deployment.schemas import (
    DeploymentEventType,
    DeploymentPlan,
    EnvVarSpec,
    GeneratedDeploymentConfig,
    GeneratedDeploymentScript,
    GeneratedDockerfile,
    SmokeTestCase,
)
from app.deployment.validator import (
    redact_secrets,
    validate_docker_compose,
    validate_dockerfile,
    validate_env_vars,
)
from app.llm.models import TaskType
from app.state.enums import DeploymentStage
from app.state.models import AHSEAState, ProjectMetadata
from app.tools.audit import AuditLog
from app.tools.base import ToolContext, ToolExecutor, ToolResult
from app.tools.exceptions import PermissionDeniedError
from app.tools.permissions import (
    DEPLOYMENT_MANAGER_DEFAULT,
    WORKER_DEFAULT,
    Permission,
)
from app.tools.registry import ToolRegistry, all_tools, make_executor

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

VALID_DOCKERFILE = (
    "FROM python:3.12-slim\n"
    "USER appuser\n"
    "HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1\n"
    "COPY . /app\n"
    'CMD ["python", "-m", "app"]\n'
)

VALID_COMPOSE = (
    "services:\n"
    "  demo-service:\n"
    "    image: demo-service:latest\n"
    "    ports:\n"
    '      - "8000:8000"\n'
    "    environment:\n"
    "      - APP_ENV=production\n"
    "    healthcheck:\n"
    '      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]\n'
)


def make_state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )


class FakeQAReport:
    def __init__(self, gate_passed: bool = True):
        self.gate_passed = gate_passed


class FakeTool:
    """Stand-in BaseTool: returns a scripted ToolResult, records calls."""

    def __init__(
        self,
        name: str,
        success: bool = True,
        error: str | None = None,
        output: dict | None = None,
    ):
        self.name = name
        self.required_permission = Permission.EXECUTE
        self._success = success
        self._error = error
        self._output = output if output is not None else {"stdout": "ok", "stderr": ""}
        self.calls: list[dict] = []

    async def __call__(self, ctx, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=self.name, success=self._success, output=self._output, error=self._error
        )


class SequencedFakeTool:
    """Like FakeTool, but returns a different scripted ToolResult on each call
    (repeating the last one once exhausted) -- used to test health-check retries."""

    def __init__(self, name: str, results: list[ToolResult]):
        self.name = name
        self.required_permission = Permission.EXECUTE
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, ctx, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def make_fake_executor(tools: dict[str, object]) -> ToolExecutor:
    registry = ToolRegistry(list(tools.values()))
    ctx = ToolContext(
        agent_id="deployment-manager",
        permissions=frozenset(
            {Permission.READ, Permission.WRITE, Permission.EXECUTE, Permission.DEPLOY}
        ),
        sandbox=None,  # FakeTool never touches it
        audit_log=AuditLog(),
    )
    return ToolExecutor(registry=registry, context=ctx)


def default_tools(
    *,
    write_ok: bool = True,
    build_ok: bool = True,
    up_ok: bool = True,
    down_ok: bool = True,
    health_output: dict | None = None,
) -> dict[str, object]:
    return {
        "write_file": FakeTool("write_file", success=write_ok),
        "docker_build": FakeTool("docker_build", success=build_ok),
        "docker_compose_up": FakeTool("docker_compose_up", success=up_ok),
        "docker_compose_down": FakeTool("docker_compose_down", success=down_ok),
        "docker_health_check": FakeTool(
            "docker_health_check",
            success=True,
            output=health_output or {"status": "running", "health": "healthy"},
        ),
        "run_command": FakeTool("run_command", success=True, output={"returncode": 0}),
    }


class FakeGateway:
    """Routes by response_model, mirroring the QA/self-healing test fakes."""

    def __init__(
        self,
        dockerfile_content: str = VALID_DOCKERFILE,
        compose_content: str = VALID_COMPOSE,
    ):
        self.calls: list[TaskType] = []
        self.dockerfile_content = dockerfile_content
        self.compose_content = compose_content

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        if response_model is DeploymentPlan:
            assert task_type == TaskType.PLANNING
            return DeploymentPlan(
                summary="Build, start, verify, then deploy on approval.",
                steps=["build", "start", "verify", "deploy"],
                target_environment="staging",
                base_image_recommendation="python:3.12-slim",
                rollback_strategy="docker compose down, redeploy previous image",
            )
        if response_model is GeneratedDockerfile:
            assert task_type == TaskType.CODING
            return GeneratedDockerfile(content=self.dockerfile_content)
        if response_model is GeneratedDeploymentConfig:
            assert task_type == TaskType.CONFIGURATION
            return GeneratedDeploymentConfig(
                docker_compose_content=self.compose_content,
                env_template_content="APP_ENV=production\n",
            )
        if response_model is GeneratedDeploymentScript:
            assert task_type == TaskType.DOCUMENTATION
            return GeneratedDeploymentScript(
                filename="deploy.sh", content="#!/bin/sh\ndocker compose up -d\n"
            )
        raise AssertionError(f"Unexpected response_model {response_model}")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_validate_dockerfile_valid_passes():
    result = validate_dockerfile(VALID_DOCKERFILE)
    assert result.passed
    assert not result.blocking_issues


def test_validate_dockerfile_missing_from_is_blocking():
    result = validate_dockerfile("RUN echo hi\n")
    assert not result.passed
    assert any("FROM" in i.message for i in result.blocking_issues)


def test_validate_dockerfile_hardcoded_secret_is_blocking():
    content = "FROM python:3.12-slim\nENV DB_PASSWORD=hunter2\n"
    result = validate_dockerfile(content)
    assert not result.passed
    assert any("secret" in i.message.lower() for i in result.blocking_issues)


def test_validate_dockerfile_untagged_base_image_is_warning_not_blocking():
    content = "FROM python\nUSER app\nHEALTHCHECK CMD true\n"
    result = validate_dockerfile(content)
    assert result.passed
    assert any("tag" in i.message.lower() for i in result.issues)


def test_validate_docker_compose_valid_passes():
    result = validate_docker_compose(VALID_COMPOSE)
    assert result.passed


def test_validate_docker_compose_invalid_yaml_is_blocking():
    result = validate_docker_compose("services: [this is not: valid: yaml:")
    assert not result.passed


def test_validate_docker_compose_missing_services_is_blocking():
    result = validate_docker_compose("version: '3'\n")
    assert not result.passed


def test_validate_docker_compose_hardcoded_secret_is_blocking():
    content = (
        "services:\n"
        "  demo:\n"
        "    image: demo:1.0\n"
        "    environment:\n"
        "      - API_TOKEN=sk-realtoken1234567890\n"
    )
    result = validate_docker_compose(content)
    assert not result.passed
    assert any("secret" in i.message.lower() for i in result.blocking_issues)


def test_validate_env_vars_missing_required_is_blocking():
    specs = [EnvVarSpec(name="DATABASE_URL", required=True)]
    result = validate_env_vars(specs, provided={})
    assert not result.passed


def test_validate_env_vars_satisfied_passes():
    specs = [EnvVarSpec(name="DATABASE_URL", required=True)]
    result = validate_env_vars(specs, provided={"DATABASE_URL": "postgres://x"})
    assert result.passed


def test_redact_secrets_scrubs_env_style_assignment():
    text = "ENV DB_PASSWORD=supersecret\nAPP_ENV=production"
    redacted = redact_secrets(text)
    assert "supersecret" not in redacted
    assert "production" in redacted


def test_redact_secrets_scrubs_known_token_shapes():
    text = "token is sk-abcdefghijklmnopqrstuvwx"
    redacted = redact_secrets(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in redacted


# ---------------------------------------------------------------------------
# DeploymentManager pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_blocks_when_qa_gate_not_passed():
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=make_fake_executor(default_tools()))

    with pytest.raises(QAGateNotPassedError):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=False), service_name="demo-service"
        )

    assert state.deployment.stage == DeploymentStage.FAILED


@pytest.mark.asyncio
async def test_pipeline_happy_path_reaches_awaiting_approval():
    tools = default_tools()
    executor = make_fake_executor(tools)
    gateway = FakeGateway()
    state = make_state()
    manager = DeploymentManager(gateway=gateway, tools=executor)

    report = await manager.run_pipeline(
        state,
        qa_report=FakeQAReport(gate_passed=True),
        service_name="Demo Service",
        service_summary="a demo web service",
        env_vars=[EnvVarSpec(name="APP_ENV", required=True, default="production")],
    )

    assert report.stage == "awaiting_approval"
    assert report.ready_for_approval
    assert report.validation_passed
    assert report.health_check is not None and report.health_check.passed
    assert report.smoke_tests and all(s.passed for s in report.smoke_tests)
    assert state.deployment.stage == DeploymentStage.AWAITING_APPROVAL
    assert report.events[-1].event_type == DeploymentEventType.APPROVAL_REQUESTED

    # Artifacts were written before docker build ran.
    assert tools["write_file"].calls
    written_paths = {c["path"] for c in tools["write_file"].calls}
    assert {"Dockerfile", "docker-compose.yml", ".env.example", "deploy.sh"} <= written_paths
    assert tools["docker_build"].calls
    assert tools["docker_compose_up"].calls

    # Qwen3 planning + Qwen2.5-Coder generation all used the right task types.
    assert TaskType.PLANNING in gateway.calls
    assert TaskType.CODING in gateway.calls
    assert TaskType.CONFIGURATION in gateway.calls
    assert TaskType.DOCUMENTATION in gateway.calls


@pytest.mark.asyncio
async def test_pipeline_validation_failure_blocks_before_any_docker_call():
    # Missing FROM -> blocking Dockerfile validation issue.
    gateway = FakeGateway(dockerfile_content="RUN echo hi\n")
    tools = default_tools()
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=gateway, tools=executor)

    with pytest.raises(ValidationFailedError):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
        )

    assert state.deployment.stage == DeploymentStage.FAILED
    assert not tools["docker_build"].calls
    assert not tools["write_file"].calls


@pytest.mark.asyncio
async def test_pipeline_artifact_write_failure_raises_and_marks_failed():
    """Regression test: `write_file` failing (e.g. sandbox path validation,
    a size-limit error, a locked file) must not be silently ignored. Before
    the fix, `run_pipeline` never checked the `write_file` `ToolResult` at
    all, so a failed artifact write was invisible -- the pipeline sailed on
    into `docker_build` against a Dockerfile that was never actually
    written, and any resulting failure looked like a Docker problem instead
    of pointing at the real cause."""

    tools = default_tools(write_ok=False)
    tools["write_file"]._error = "Could not write 'Dockerfile': disk quota exceeded"
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    with pytest.raises(DeploymentPipelineFailedError, match="disk quota exceeded"):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
        )

    assert state.deployment.stage == DeploymentStage.FAILED
    # The failure must be caught at the very first artifact write --
    # docker_build must never be reached with an artifact that wasn't
    # actually written.
    assert not tools["docker_build"].calls


@pytest.mark.asyncio
async def test_pipeline_docker_build_failure_raises_and_marks_failed():
    tools = default_tools(build_ok=False)
    tools["docker_build"]._error = "no space left on device"
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    with pytest.raises(DeploymentPipelineFailedError):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
        )


@pytest.mark.asyncio
async def test_pipeline_skips_cleanly_when_docker_not_installed():
    """`docker_build` reporting the `DOCKER_NOT_AVAILABLE` sentinel (real
    `_run_subprocess` sets this for a returncode-127 "executable not
    found") must skip the rest of the deployment pipeline, not raise --
    this was previously untested even though the code path already
    existed."""
    from app.tools.docker import DOCKER_NOT_AVAILABLE

    tools = default_tools(build_ok=False)
    tools["docker_build"]._error = DOCKER_NOT_AVAILABLE
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    assert report.stage == "skipped"
    assert state.deployment.stage == DeploymentStage.SKIPPED
    assert not tools["docker_compose_up"].calls


@pytest.mark.asyncio
async def test_pipeline_skips_cleanly_when_docker_daemon_unreachable():
    """Regression test for the real bug this diagnosis found: Docker
    installed (so the CLI runs -> a real, non-127 exit code) but the
    daemon/Docker Desktop isn't running -- the single most common way to
    hit this on a Windows dev machine, since Docker Desktop must be
    launched manually. Before the fix, this fell through to the generic
    "build genuinely failed" branch and raised
    `DeploymentPipelineFailedError`, failing the entire project run over
    "Docker Desktop isn't open" rather than skipping deployment the same
    way "docker isn't installed at all" already does."""
    from app.tools.docker import DOCKER_NOT_AVAILABLE

    tools = default_tools(build_ok=False)
    # This is what `_tool_result` in app/tools/docker.py now maps to
    # DOCKER_NOT_AVAILABLE via `_looks_like_daemon_unreachable`, even
    # though the underlying `_run_subprocess` call succeeded in *starting*
    # the process (a real returncode like 1, not 127).
    tools["docker_build"]._error = DOCKER_NOT_AVAILABLE
    tools["docker_build"]._output = {
        "stdout": "",
        "stderr": "error during connect: this error may indicate that the docker daemon is not running.",
        "returncode": 1,
    }
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    assert report.stage == "skipped"
    assert state.deployment.stage == DeploymentStage.SKIPPED
    assert not tools["docker_compose_up"].calls


@pytest.mark.asyncio
async def test_pipeline_start_failure_raises_and_marks_failed():
    tools = default_tools(up_ok=False)
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    with pytest.raises(DeploymentPipelineFailedError):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
        )

    assert state.deployment.stage == DeploymentStage.FAILED


@pytest.mark.asyncio
async def test_pipeline_health_check_retries_then_succeeds():
    tools = default_tools()
    unhealthy = ToolResult(
        tool_name="docker_health_check",
        success=True,
        output={"status": "starting", "health": "starting"},
    )
    healthy = ToolResult(
        tool_name="docker_health_check",
        success=True,
        output={"status": "running", "health": "healthy"},
    )
    tools["docker_health_check"] = SequencedFakeTool(
        "docker_health_check", [unhealthy, unhealthy, healthy]
    )
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(
        gateway=FakeGateway(), tools=executor, health_check_interval_seconds=0
    )

    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    assert report.health_check.passed
    assert report.health_check.attempts == 3


@pytest.mark.asyncio
async def test_pipeline_health_check_exhausts_attempts_and_fails():
    tools = default_tools(health_output={"status": "starting", "health": "starting"})
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(
        gateway=FakeGateway(),
        tools=executor,
        health_check_max_attempts=2,
        health_check_interval_seconds=0,
    )

    with pytest.raises(DeploymentPipelineFailedError):
        await manager.run_pipeline(
            state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
        )

    assert state.deployment.verification_passed is False
    assert state.deployment.stage == DeploymentStage.FAILED


@pytest.mark.asyncio
async def test_pipeline_smoke_test_failure_blocks_readiness():
    tools = default_tools()
    tools["run_command"] = FakeTool("run_command", success=False, output={"returncode": 1})
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)

    with pytest.raises(DeploymentPipelineFailedError):
        await manager.run_pipeline(
            state,
            qa_report=FakeQAReport(gate_passed=True),
            service_name="demo-service",
            smoke_tests=[SmokeTestCase(name="homepage", command=["curl", "-f", "http://x/health"])],
        )

    assert state.deployment.stage == DeploymentStage.FAILED


# ---------------------------------------------------------------------------
# Approval gate + deploy + rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_without_approval_raises():
    executor = make_fake_executor(default_tools())
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    with pytest.raises(ApprovalRequiredError):
        await manager.deploy(state, report)

    assert not report.deployed
    assert state.deployment.stage == DeploymentStage.FAILED


@pytest.mark.asyncio
async def test_deploy_after_rejection_still_raises():
    executor = make_fake_executor(default_tools())
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    await manager.reject(state, report, rejected_by="alice", reason="not ready")
    assert state.deployment.approved_by is None

    with pytest.raises(ApprovalRequiredError):
        await manager.deploy(state, report)


@pytest.mark.asyncio
async def test_approve_then_deploy_succeeds_and_prepares_rollback():
    tools = default_tools()
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    await manager.approve(state, report, approved_by="alice", reason="looks good")
    assert state.deployment.approved_by == "alice"

    deployed_report = await manager.deploy(state, report, previous_image_tag="demo-service:old")

    assert deployed_report.deployed
    assert deployed_report.stage == "deployed"
    assert deployed_report.rollback_plan is not None
    assert deployed_report.rollback_plan.previous_image_tag == "demo-service:old"
    assert state.deployment.stage == DeploymentStage.DEPLOYED
    assert state.deployment.last_deployed_at is not None
    # docker_compose_up called twice: once for "start" (verification), once for "deploy".
    assert len(tools["docker_compose_up"].calls) == 2


@pytest.mark.asyncio
async def test_rollback_without_prior_deploy_raises():
    executor = make_fake_executor(default_tools())
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    with pytest.raises(DeploymentPipelineFailedError):
        await manager.rollback(state, report, reason="never deployed")


@pytest.mark.asyncio
async def test_rollback_after_deploy_tears_down_and_records_state():
    tools = default_tools()
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )
    await manager.approve(state, report, approved_by="alice")
    await manager.deploy(state, report)

    rolled_back = await manager.rollback(state, report, reason="smoke test regression in prod")

    assert rolled_back.stage == "rolled_back"
    assert not rolled_back.deployed
    assert state.deployment.stage == DeploymentStage.ROLLED_BACK
    assert state.deployment.rollback_reason == "smoke test regression in prod"
    assert tools["docker_compose_down"].calls


@pytest.mark.asyncio
async def test_events_never_contain_raw_secret_values():
    tools = default_tools()
    executor = make_fake_executor(tools)
    state = make_state()
    manager = DeploymentManager(gateway=FakeGateway(), tools=executor)
    report = await manager.run_pipeline(
        state, qa_report=FakeQAReport(gate_passed=True), service_name="demo-service"
    )

    await manager._emit(
        report,
        DeploymentEventType.DEPLOY_STARTED,
        "deploy",
        "ENV DB_PASSWORD=hunter2",
    )
    assert "hunter2" not in report.events[-1].message
    assert "hunter2" not in "\n".join(report.logs)


# ---------------------------------------------------------------------------
# Tool-system permission enforcement (real tools, real sandbox)
# ---------------------------------------------------------------------------


def test_worker_default_permissions_exclude_deploy(tmp_path):
    worker_executor = make_executor(
        agent_id="worker",
        workspace_root=tmp_path,
        permissions=WORKER_DEFAULT,
        registry=ToolRegistry(all_tools()),
    )
    assert "docker_compose_up" not in worker_executor.available_tools()
    assert "docker_build" in worker_executor.available_tools()  # EXECUTE-level, allowed


def test_deployment_manager_permissions_include_deploy(tmp_path):
    executor = make_executor(
        agent_id="deployment-manager",
        workspace_root=tmp_path,
        permissions=DEPLOYMENT_MANAGER_DEFAULT,
        registry=ToolRegistry(all_tools()),
    )
    assert "docker_compose_up" in executor.available_tools()
    assert "docker_compose_down" in executor.available_tools()
    assert "docker_build" in executor.available_tools()


@pytest.mark.asyncio
async def test_docker_compose_up_denied_without_deploy_permission(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(VALID_COMPOSE)
    executor = make_executor(
        agent_id="worker",
        workspace_root=tmp_path,
        permissions=WORKER_DEFAULT,
        registry=ToolRegistry(all_tools()),
    )
    with pytest.raises(PermissionDeniedError):
        await executor.run("docker_compose_up", compose_path="docker-compose.yml")
