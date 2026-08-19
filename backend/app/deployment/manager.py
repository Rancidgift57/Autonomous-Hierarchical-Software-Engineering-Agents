"""`DeploymentManager` (Phase 15): orchestrates the full deployment pipeline.

Pipeline, exactly as specified::

    QA PASS -> build -> Docker build -> start -> health check -> smoke test
      -> deployment ready -> human approval -> deploy

Stage mapping onto `app.state.enums.DeploymentStage`::

    QA PASS check            (no transition; gates entry into the pipeline)
    build                    -> PREPARING   (plan, generate + validate artifacts)
    Docker build             -> BUILDING
    start                    -> DEPLOYING   (launch the built image for verification)
    health check / smoke test -> VERIFYING
    deployment ready          -> AWAITING_APPROVAL
    human approval             (external call: `approve()` / `reject()`)
    deploy                   -> DEPLOYED    (final, post-approval promotion)

Hard constraints, enforced structurally rather than just by convention:
    * `deploy()` cannot reach a `docker compose up` call while
      `report.approval` is missing or `approved=False` --
      `ApprovalRequiredError` is raised before any tool call.
    * Every value written into `state.deployment.deployment_log` or a
      `DeploymentEvent` has gone through `app.deployment.validator.redact_secrets`
      first.
    * Validation of generated artifacts (Dockerfile / compose / env vars) is
      blocking: `run_pipeline` stops and raises before anything is written
      to disk or a single Docker command runs if validation fails.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.deployment.agents import (
    DeploymentConfigAgent,
    DeploymentPlanningAgent,
    DeploymentScriptAgent,
    DockerfileGeneratorAgent,
)
from app.deployment.events import DeploymentEventBus
from app.deployment.exceptions import (
    ApprovalRequiredError,
    DeploymentPipelineFailedError,
    QAGateNotPassedError,
    ValidationFailedError,
)
from app.deployment.schemas import (
    DeploymentApproval,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentReport,
    EnvVarSpec,
    HealthCheckResult,
    RollbackPlan,
    SmokeTestCase,
    SmokeTestResult,
)
from app.deployment.validator import (
    redact_secrets,
    validate_docker_compose,
    validate_dockerfile,
    validate_env_vars,
)
from app.llm.gateway import LLMGateway
from app.qa.schemas import QAPipelineReport
from app.state.enums import DeploymentStage
from app.state.models import AHSEAState
from app.state.operations import (
    record_deployment_approval,
    record_deployment_result,
    record_rollback,
    set_deployment_stage,
)
from app.tools.base import ToolExecutor
from app.tools.docker import DOCKER_NOT_AVAILABLE

#: Safety cap on health-check polling so a container that never becomes
#: healthy can't stall the pipeline indefinitely.
DEFAULT_HEALTH_CHECK_MAX_ATTEMPTS = 10
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 2.0

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    slug = _SLUG_PATTERN.sub("-", name.lower()).strip("-")
    return slug or "service"


class DeploymentManager:
    """Orchestrates QA-gated build -> deploy -> verify -> approval -> deploy."""

    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolExecutor,
        event_bus: DeploymentEventBus | None = None,
        health_check_max_attempts: int = DEFAULT_HEALTH_CHECK_MAX_ATTEMPTS,
        health_check_interval_seconds: float = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    ):
        self.gateway = gateway
        self.tools = tools
        self.event_bus = event_bus or DeploymentEventBus()
        self.health_check_max_attempts = health_check_max_attempts
        self.health_check_interval_seconds = health_check_interval_seconds

        self.planning_agent = DeploymentPlanningAgent(gateway=gateway)
        self.dockerfile_agent = DockerfileGeneratorAgent(gateway=gateway)
        self.config_agent = DeploymentConfigAgent(gateway=gateway)
        self.script_agent = DeploymentScriptAgent(gateway=gateway)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _emit(
        self,
        report: DeploymentReport,
        event_type: DeploymentEventType,
        stage: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        safe_message = redact_secrets(message)
        safe_data = {k: redact_secrets(str(v)) for k, v in (data or {}).items()}
        event = DeploymentEvent(
            event_type=event_type, stage=stage, message=safe_message, data=safe_data
        )
        report.events.append(event)
        report.logs.append(f"[{stage}] {safe_message}")
        await self.event_bus.publish(event)

    # ------------------------------------------------------------------
    # Automated pipeline: QA PASS -> ... -> deployment ready
    # ------------------------------------------------------------------

    async def run_pipeline(
        self,
        state: AHSEAState,
        qa_report: QAPipelineReport,
        service_name: str,
        service_summary: str = "",
        target_environment: str = "staging",
        env_vars: list[EnvVarSpec] | None = None,
        env_values: dict[str, str] | None = None,
        entrypoint_hint: str = "",
        smoke_tests: list[SmokeTestCase] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeploymentReport:
        """Run every automated stage, stopping at AWAITING_APPROVAL.

        Raises:
            QAGateNotPassedError: `qa_report.gate_passed` is falsy.
            ValidationFailedError: a generated artifact fails blocking validation.
            DeploymentPipelineFailedError: build/start/health-check/smoke-test fails.
        """

        env_vars = env_vars or []
        project_name = _slugify(service_name)
        image_tag = f"{project_name}:latest"

        report = DeploymentReport(
            service_name=service_name,
            target_environment=target_environment,
            image_tag=image_tag,
            container_name=project_name,
            project_name=project_name,
        )

        # -- QA PASS -----------------------------------------------------
        gate_passed = bool(getattr(qa_report, "gate_passed", False))
        await self._emit(
            report,
            DeploymentEventType.QA_GATE_CHECKED,
            "qa_pass",
            f"QA gate {'passed' if gate_passed else 'did not pass'}.",
        )
        if not gate_passed:
            report.stage = "failed"
            report.error = "QA quality gate did not pass; deployment pipeline will not start."
            set_deployment_stage(state, DeploymentStage.FAILED, report.error)
            raise QAGateNotPassedError(report.error)

        # -- build: plan + generate artifacts -----------------------------
        set_deployment_stage(state, DeploymentStage.PREPARING, "Preparing deployment artifacts.")
        report.stage = "preparing"

        plan = await self.planning_agent.plan(
            service_name=service_name,
            service_summary=service_summary,
            target_environment=target_environment,
            env_vars=env_vars,
            metadata=metadata,
        )
        report.plan = plan
        await self._emit(report, DeploymentEventType.PLAN_CREATED, "build", plan.summary)

        dockerfile = await self.dockerfile_agent.generate(
            service_name=service_name,
            service_summary=service_summary,
            plan=plan,
            entrypoint_hint=entrypoint_hint,
            metadata=metadata,
        )
        config = await self.config_agent.generate(
            service_name=service_name,
            plan=plan,
            env_vars=env_vars,
            image_tag=image_tag,
            metadata=metadata,
        )
        script = await self.script_agent.generate(
            service_name=service_name, plan=plan, image_tag=image_tag, metadata=metadata
        )
        await self._emit(
            report,
            DeploymentEventType.ARTIFACTS_GENERATED,
            "build",
            "Generated Dockerfile, docker-compose config, and deploy script.",
        )

        # -- validate (blocking) ------------------------------------------
        dockerfile_result = validate_dockerfile(dockerfile.content)
        compose_result = validate_docker_compose(config.docker_compose_content)
        env_result = validate_env_vars(env_vars, provided=env_values or {})
        report.validation_results = [dockerfile_result, compose_result, env_result]

        await self._emit(
            report,
            DeploymentEventType.VALIDATION_COMPLETED,
            "build",
            f"Artifact validation {'passed' if report.validation_passed else 'FAILED'}.",
            data={"passed": report.validation_passed},
        )
        if not report.validation_passed:
            report.stage = "failed"
            issues = [
                f"{i.field}: {i.message}"
                for r in report.validation_results
                for i in r.blocking_issues
            ]
            report.error = "Blocking validation issues: " + "; ".join(issues)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise ValidationFailedError(report.error)

        # Write validated artifacts into the sandbox.
        await self.tools.run("write_file", path=report.dockerfile_path, content=dockerfile.content)
        await self.tools.run(
            "write_file", path=report.compose_path, content=config.docker_compose_content
        )
        if config.env_template_content:
            await self.tools.run(
                "write_file", path=".env.example", content=config.env_template_content
            )
        if script.filename:
            await self.tools.run("write_file", path=script.filename, content=script.content)

        # -- Docker build --------------------------------------------------
        set_deployment_stage(state, DeploymentStage.BUILDING, f"Building image '{image_tag}'.")
        report.stage = "building"
        await self._emit(
            report,
            DeploymentEventType.BUILD_STARTED,
            "docker_build",
            f"docker build -t {image_tag}",
        )
        build_result = await self.tools.run(
            "docker_build",
            dockerfile_path=report.dockerfile_path,
            image_tag=image_tag,
            context_path=".",
        )
        if not build_result.success and build_result.error == DOCKER_NOT_AVAILABLE:
            # Docker isn't usable on this host -- either the `docker` CLI
            # isn't installed at all, or it's installed but there's no
            # daemon behind it to talk to (Docker Desktop not started is
            # the single most common way to hit this, especially on
            # Windows, where Desktop must be launched manually rather than
            # running as an always-on system service). Both are expected
            # on plain dev/CI machines (see the project's HARDWARE
            # CONSTRAINTS notes) and are not a defect in the generated
            # project -- skip the rest of the pipeline instead of treating
            # it as a failure that would trigger a repair task / fail the
            # entire run.
            report.stage = "skipped"
            report.error = "Deployment skipped: Docker is not usable on this host (not installed, or the daemon/Docker Desktop is not running)."
            await self._emit(report, DeploymentEventType.BUILD_FAILED, "docker_build", report.error)
            set_deployment_stage(state, DeploymentStage.SKIPPED, report.error)
            return report
        if not build_result.success:
            report.stage = "failed"
            report.error = build_result.error or "Docker build failed."
            await self._emit(report, DeploymentEventType.BUILD_FAILED, "docker_build", report.error)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise DeploymentPipelineFailedError(report.error)
        await self._emit(
            report, DeploymentEventType.BUILD_COMPLETED, "docker_build", "Image built successfully."
        )

        # -- start (launch for verification) --------------------------------
        set_deployment_stage(
            state, DeploymentStage.DEPLOYING, "Starting containers for verification."
        )
        report.stage = "starting"
        await self._emit(
            report,
            DeploymentEventType.START_STARTED,
            "start",
            f"docker compose up -d ({project_name})",
        )
        start_result = await self.tools.run(
            "docker_compose_up", compose_path=report.compose_path, project_name=project_name
        )
        if not start_result.success and start_result.error == DOCKER_NOT_AVAILABLE:
            report.stage = "skipped"
            report.error = "Deployment skipped: Docker is not usable on this host (not installed, or the daemon/Docker Desktop is not running)."
            await self._emit(report, DeploymentEventType.START_FAILED, "start", report.error)
            set_deployment_stage(state, DeploymentStage.SKIPPED, report.error)
            return report
        if not start_result.success:
            report.stage = "failed"
            report.error = start_result.error or "docker compose up failed."
            await self._emit(report, DeploymentEventType.START_FAILED, "start", report.error)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise DeploymentPipelineFailedError(report.error)
        await self._emit(
            report, DeploymentEventType.START_COMPLETED, "start", "Containers started."
        )

        # -- health check + smoke test --------------------------------------
        set_deployment_stage(
            state, DeploymentStage.VERIFYING, "Running health check and smoke tests."
        )
        report.stage = "verifying"

        await self._emit(
            report,
            DeploymentEventType.HEALTH_CHECK_STARTED,
            "health_check",
            "Polling container health.",
        )
        health_result = await self._run_health_check(project_name)
        report.health_check = health_result
        if not health_result.passed:
            report.stage = "failed"
            report.error = health_result.message
            await self._emit(
                report,
                DeploymentEventType.HEALTH_CHECK_FAILED,
                "health_check",
                health_result.message,
            )
            record_deployment_result(state, deployed=False, verification_passed=False)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise DeploymentPipelineFailedError(report.error)
        await self._emit(
            report, DeploymentEventType.HEALTH_CHECK_PASSED, "health_check", health_result.message
        )

        await self._emit(
            report, DeploymentEventType.SMOKE_TEST_STARTED, "smoke_test", "Running smoke tests."
        )
        smoke_results = await self._run_smoke_tests(smoke_tests)
        report.smoke_tests = smoke_results
        smoke_passed = all(s.passed for s in smoke_results)
        if not smoke_passed:
            report.stage = "failed"
            failed_names = ", ".join(s.name for s in smoke_results if not s.passed)
            report.error = f"Smoke test(s) failed: {failed_names}"
            await self._emit(
                report, DeploymentEventType.SMOKE_TEST_FAILED, "smoke_test", report.error
            )
            record_deployment_result(state, deployed=False, verification_passed=False)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise DeploymentPipelineFailedError(report.error)
        await self._emit(
            report, DeploymentEventType.SMOKE_TEST_PASSED, "smoke_test", "All smoke tests passed."
        )
        record_deployment_result(state, deployed=False, verification_passed=True)

        # -- deployment ready -------------------------------------------------
        set_deployment_stage(
            state,
            DeploymentStage.AWAITING_APPROVAL,
            "Deployment verified; awaiting human approval.",
        )
        report.stage = "awaiting_approval"
        report.ready_for_approval = True
        await self._emit(
            report,
            DeploymentEventType.DEPLOYMENT_READY,
            "deployment_ready",
            "Build verified and ready to deploy.",
        )
        await self._emit(
            report,
            DeploymentEventType.APPROVAL_REQUESTED,
            "human_approval",
            f"Awaiting explicit human approval to deploy to '{target_environment}'.",
        )
        return report

    # ------------------------------------------------------------------
    # Health check / smoke tests
    # ------------------------------------------------------------------

    async def _run_health_check(self, container_name: str) -> HealthCheckResult:
        last_status: str | None = None
        last_health: str | None = None
        for attempt in range(1, self.health_check_max_attempts + 1):
            result = await self.tools.run("docker_health_check", container_name=container_name)
            if result.success:
                last_status = result.output.get("status")
                last_health = result.output.get("health")
                healthy = last_status == "running" and last_health in (None, "", "healthy")
                if healthy:
                    return HealthCheckResult(
                        passed=True,
                        container_name=container_name,
                        attempts=attempt,
                        last_status=last_status,
                        last_health=last_health,
                        message=f"Container healthy after {attempt} attempt(s).",
                    )
            if attempt < self.health_check_max_attempts:
                await asyncio.sleep(self.health_check_interval_seconds)

        return HealthCheckResult(
            passed=False,
            container_name=container_name,
            attempts=self.health_check_max_attempts,
            last_status=last_status,
            last_health=last_health,
            message=(
                f"Container did not become healthy within "
                f"{self.health_check_max_attempts} attempt(s) "
                f"(last status={last_status!r}, health={last_health!r})."
            ),
        )

    async def _run_smoke_tests(self, cases: list[SmokeTestCase] | None) -> list[SmokeTestResult]:
        if not cases:
            return [
                SmokeTestResult(
                    name="default", passed=True, message="No smoke tests configured; skipped."
                )
            ]

        results: list[SmokeTestResult] = []
        for case in cases:
            try:
                tool_result = await self.tools.run("run_command", argv=case.command)
            except Exception as exc:  # noqa: BLE001 - a bad smoke test must not crash the pipeline
                results.append(SmokeTestResult(name=case.name, passed=False, message=str(exc)))
                continue

            returncode = (tool_result.output or {}).get("returncode")
            passed = tool_result.success and returncode == case.expected_returncode
            results.append(
                SmokeTestResult(
                    name=case.name,
                    passed=passed,
                    message="ok" if passed else (tool_result.error or "smoke test failed"),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Human approval gate
    # ------------------------------------------------------------------

    async def approve(
        self, state: AHSEAState, report: DeploymentReport, approved_by: str, reason: str = ""
    ) -> DeploymentReport:
        """Record explicit human approval. Does not deploy by itself."""

        report.approval = DeploymentApproval(approved=True, approved_by=approved_by, reason=reason)
        record_deployment_approval(state, approved_by, True)
        await self._emit(
            report,
            DeploymentEventType.APPROVAL_GRANTED,
            "human_approval",
            f"Approved by '{approved_by}'.",
        )
        return report

    async def reject(
        self, state: AHSEAState, report: DeploymentReport, rejected_by: str, reason: str = ""
    ) -> DeploymentReport:
        """Record an explicit rejection. Leaves the pipeline unable to deploy."""

        report.approval = DeploymentApproval(approved=False, approved_by=rejected_by, reason=reason)
        report.stage = "rejected"
        record_deployment_approval(state, rejected_by, False)
        await self._emit(
            report,
            DeploymentEventType.APPROVAL_REJECTED,
            "human_approval",
            f"Rejected by '{rejected_by}': {reason}" if reason else f"Rejected by '{rejected_by}'.",
        )
        return report

    # ------------------------------------------------------------------
    # Final deploy (post-approval) + rollback preparation
    # ------------------------------------------------------------------

    async def deploy(
        self,
        state: AHSEAState,
        report: DeploymentReport,
        previous_image_tag: str | None = None,
    ) -> DeploymentReport:
        """Perform the final, post-approval deploy step.

        Raises:
            ApprovalRequiredError: `report.approval` is missing or not approved.
                There is no code path past this check that reaches a tool call.
        """

        if report.approval is None or not report.approval.approved:
            message = (
                f"Deployment to '{report.target_environment}' requires explicit human "
                "approval before 'deploy' can run."
            )
            set_deployment_stage(state, DeploymentStage.FAILED, message)
            raise ApprovalRequiredError(message)

        # -- rollback preparation, before anything changes -----------------
        rollback_plan = RollbackPlan(
            previous_image_tag=previous_image_tag,
            previous_project_name=report.project_name,
            compose_path=report.compose_path,
            instructions=(
                f"Run `docker compose -f {report.compose_path} -p {report.project_name} down`, "
                f"then redeploy image '{previous_image_tag}' if one was previously running."
                if previous_image_tag
                else "No prior image on record; rollback should stop the current deployment "
                "via `docker compose down` and restore service manually."
            ),
        )
        report.rollback_plan = rollback_plan
        await self._emit(
            report,
            DeploymentEventType.ROLLBACK_PREPARED,
            "deploy",
            "Rollback plan captured before promoting the new deployment.",
        )

        # -- deploy -----------------------------------------------------------
        set_deployment_stage(
            state, DeploymentStage.DEPLOYING, f"Deploying to '{report.target_environment}'."
        )
        await self._emit(
            report,
            DeploymentEventType.DEPLOY_STARTED,
            "deploy",
            f"Promoting verified build to '{report.target_environment}'.",
        )

        deploy_result = await self.tools.run(
            "docker_compose_up", compose_path=report.compose_path, project_name=report.project_name
        )
        if not deploy_result.success:
            report.stage = "failed"
            report.error = deploy_result.error or "Deployment failed to start."
            await self._emit(report, DeploymentEventType.DEPLOY_FAILED, "deploy", report.error)
            set_deployment_stage(state, DeploymentStage.FAILED, redact_secrets(report.error))
            raise DeploymentPipelineFailedError(report.error)

        report.stage = "deployed"
        report.deployed = True
        record_deployment_result(state, deployed=True, verification_passed=True)
        set_deployment_stage(
            state, DeploymentStage.DEPLOYED, f"Deployed to '{report.target_environment}'."
        )
        await self._emit(
            report,
            DeploymentEventType.DEPLOY_COMPLETED,
            "deploy",
            f"Deployment to '{report.target_environment}' complete.",
        )
        return report

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def rollback(
        self, state: AHSEAState, report: DeploymentReport, reason: str
    ) -> DeploymentReport:
        """Tear down the current deployment using the plan captured in `deploy()`."""

        if report.rollback_plan is None:
            raise DeploymentPipelineFailedError(
                "No rollback plan was prepared for this deployment; cannot roll back."
            )

        await self._emit(
            report, DeploymentEventType.ROLLBACK_STARTED, "rollback", redact_secrets(reason)
        )
        down_result = await self.tools.run(
            "docker_compose_down",
            compose_path=report.rollback_plan.compose_path or report.compose_path,
            project_name=report.rollback_plan.previous_project_name or report.project_name,
        )
        report.deployed = False
        report.stage = "rolled_back"
        record_rollback(state, redact_secrets(reason))
        await self._emit(
            report,
            DeploymentEventType.ROLLBACK_COMPLETED,
            "rollback",
            "Rollback complete."
            if down_result.success
            else f"Rollback teardown reported an error: {down_result.error}",
        )
        return report
