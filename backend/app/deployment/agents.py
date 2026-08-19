"""LLM-facing agents for the Deployment System (Phase 15).

Each agent wraps exactly one `LLMGateway.generate_json` call for one
`TaskType`, matching the model-responsibility split laid out in
`app.deployment.schemas`: `DeploymentPlanningAgent` uses
`TaskType.PLANNING` (Qwen3, prose only); the three generator agents use
`TaskType.CODING` / `TaskType.CONFIGURATION` / `TaskType.DOCUMENTATION`
(Qwen2.5-Coder, actual file contents).
"""

from __future__ import annotations

from typing import Any

from app.deployment.schemas import (
    DeploymentPlan,
    EnvVarSpec,
    GeneratedDeploymentConfig,
    GeneratedDeploymentScript,
    GeneratedDockerfile,
)
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType


def _env_var_summary(env_vars: list[EnvVarSpec]) -> str:
    if not env_vars:
        return "(none declared)"
    lines = []
    for spec in env_vars:
        flags = []
        if spec.required:
            flags.append("required")
        if spec.secret:
            flags.append("secret")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {spec.name}{flag_text}: {spec.description or 'no description'}")
    return "\n".join(lines)


class DeploymentPlanningAgent:
    """`TaskType.PLANNING` (Qwen3): produces a `DeploymentPlan`.

    Prose only -- `DeploymentPlan` has no field that could hold file
    contents, so this agent structurally cannot emit a Dockerfile or
    config; that's exclusively the generator agents' job below.
    """

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def plan(
        self,
        service_name: str,
        service_summary: str,
        target_environment: str,
        env_vars: list[EnvVarSpec],
        metadata: dict[str, Any] | None = None,
    ) -> DeploymentPlan:
        prompt = (
            "You are planning the deployment of a service. Do not write any file "
            "contents, code, or configuration -- describe the plan in prose only.\n\n"
            f"Service: {service_name}\n"
            f"Summary: {service_summary}\n"
            f"Target environment: {target_environment}\n"
            f"Declared environment variables:\n{_env_var_summary(env_vars)}\n\n"
            "Produce an ordered list of deployment steps (build image, start "
            "container, health check, smoke test, etc.), a recommended base "
            "image, any risks worth flagging, and a rollback strategy."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.PLANNING,
            prompt=prompt,
            response_model=DeploymentPlan,
            metadata=metadata,
        )


class DockerfileGeneratorAgent:
    """`TaskType.CODING` (Qwen2.5-Coder): produces `GeneratedDockerfile`."""

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def generate(
        self,
        service_name: str,
        service_summary: str,
        plan: DeploymentPlan,
        entrypoint_hint: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> GeneratedDockerfile:
        prompt = (
            f"Write a production-quality Dockerfile for the service '{service_name}'.\n"
            f"Summary: {service_summary}\n"
            f"Recommended base image: {plan.base_image_recommendation or '(choose one)'}\n"
            f"Entrypoint hint: {entrypoint_hint or '(infer from summary)'}\n\n"
            "Requirements: pin the base image to a specific tag (never 'latest'), "
            "run as a non-root USER, include a HEALTHCHECK instruction, and never "
            "hardcode any secret, password, token, or API key as a literal ENV/ARG "
            "value -- reference them via runtime environment variables only."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.CODING,
            prompt=prompt,
            response_model=GeneratedDockerfile,
            metadata=metadata,
        )


class DeploymentConfigAgent:
    """`TaskType.CONFIGURATION` (Qwen2.5-Coder): produces
    `GeneratedDeploymentConfig` (docker-compose + env template)."""

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def generate(
        self,
        service_name: str,
        plan: DeploymentPlan,
        env_vars: list[EnvVarSpec],
        image_tag: str,
        metadata: dict[str, Any] | None = None,
    ) -> GeneratedDeploymentConfig:
        prompt = (
            f"Write a docker-compose.yml for the service '{service_name}' using image "
            f"tag '{image_tag}', plus a matching .env.example template.\n"
            f"Target environment: {plan.target_environment}\n"
            f"Declared environment variables:\n{_env_var_summary(env_vars)}\n\n"
            "Requirements: include a healthcheck for the service, reference every "
            "environment variable by name via 'environment:'/'env_file:' -- never "
            "write a literal secret value into the compose file itself. The "
            ".env.example template should list every variable name with a placeholder "
            "(never a real value) for anything marked secret."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.CONFIGURATION,
            prompt=prompt,
            response_model=GeneratedDeploymentConfig,
            metadata=metadata,
        )


class DeploymentScriptAgent:
    """`TaskType.DOCUMENTATION` (Qwen2.5-Coder): produces an auxiliary
    deploy/rollback shell script alongside the generated config."""

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def generate(
        self,
        service_name: str,
        plan: DeploymentPlan,
        image_tag: str,
        metadata: dict[str, Any] | None = None,
    ) -> GeneratedDeploymentScript:
        prompt = (
            f"Write a short, well-commented deploy.sh script for the service "
            f"'{service_name}' (image tag '{image_tag}') that runs 'docker compose up "
            f"-d', matching this plan:\n{plan.summary}\n\n"
            "It must not contain any hardcoded secret, password, token, or API key -- "
            "read those only from the environment or an env_file the operator supplies."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.DOCUMENTATION,
            prompt=prompt,
            response_model=GeneratedDeploymentScript,
            metadata=metadata,
        )
