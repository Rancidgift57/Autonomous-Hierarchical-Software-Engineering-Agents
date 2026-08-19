"""`IntegrationAgent` (Phase 11).

Validates that what different teams actually built agrees with the
declared contracts between them (API, database, environment, event
schemas, service dependencies). Reasoning about *why* a mismatch matters
and how to phrase the fix uses `task_type=TaskType.INTEGRATION_REASONING`
exclusively -- routed by `LLMGateway.route_model` to Qwen3 (see
`app.llm.gateway.REASONING_TASK_TYPES`), never Qwen2.5-Coder.

Hard constraint from the spec: "Do not allow the Integration Agent to
directly modify application code." This class holds no `ToolExecutor`
with WRITE/EXECUTE permission and never imports anything from
`app.tools` -- structurally, there is no code path here that could touch
a file. Instead, every mismatch becomes a rework `Task` (via
`app.state.operations.add_task`) with `owner_manager` set to the mismatch's
`responsible_team`, so the ordinary manager/worker pipeline (Phase 7/8) is
what actually changes code.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    ServiceDependency,
    validate_event_schemas,
    validate_service_dependencies,
)
from app.llm.exceptions import LLMError
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.realtime.emitter import RealtimeEmitter
from app.realtime.schemas import RealtimeEventType
from app.state.enums import TaskComplexity
from app.state.models import AHSEAState, Task
from app.state.operations import add_task


class IntegrationReport(BaseModel):
    """What the Integration Agent hands back after a validation pass."""

    passed: bool
    mismatches: list[ContractMismatch] = Field(default_factory=list)
    summary: str = ""
    created_rework_task_ids: list[str] = Field(default_factory=list)


class IntegrationAnalysis(BaseModel):
    """LLM-facing schema for `TaskType.INTEGRATION_REASONING` (Qwen3):
    a plain-language summary of the detected mismatches and any
    additional cross-cutting risk the mechanical validators wouldn't
    catch (e.g. two individually-valid contracts that are still a bad
    combination)."""

    summary: str
    additional_risks: list[str] = Field(default_factory=list)


_CHARTER = """\
You are the Integration Agent on an autonomous software engineering \
system called AHSEA. You validate that contracts between subsystems \
(API, database, environment, event schemas, service dependencies) are \
actually honored by what each team built. You never write or edit code \
yourself -- you only report findings so the responsible team's manager \
can create the fix.
"""


class IntegrationAgent:
    """Runs contract validation across a project and reports mismatches."""

    def __init__(self, gateway: LLMGateway, realtime: RealtimeEmitter | None = None):
        self.gateway = gateway
        self.realtime = realtime
        self.api_validator = APIContractValidator()
        self.database_validator = DatabaseContractValidator()
        self.environment_validator = EnvironmentContractValidator()

    async def _summarize(
        self, mismatches: list[ContractMismatch], metadata: dict[str, Any] | None
    ) -> IntegrationAnalysis:
        if not mismatches:
            return IntegrationAnalysis(summary="All validated contracts are satisfied.")

        findings_text = "\n".join(
            f"- [{m.contract_type.value}/{m.kind.value}] {m.failure} "
            f"(responsible: {m.responsible_team})"
            for m in mismatches
        )
        prompt = (
            f"{_CHARTER}\n\nDetected contract mismatches:\n{findings_text}\n\n"
            "Summarize the overall integration risk in a couple of sentences, and "
            "flag any additional cross-cutting risk you notice from the combination "
            "of these findings (not just each one individually). Do not propose code."
        )
        return await self.gateway.generate_json(
            task_type=TaskType.INTEGRATION_REASONING,
            prompt=prompt,
            response_model=IntegrationAnalysis,
            metadata=metadata,
        )

    def _create_rework_tasks(
        self, state: AHSEAState, mismatches: list[ContractMismatch]
    ) -> list[str]:
        """One rework `Task` per responsible team, batching that team's
        mismatches into a single task rather than one task per finding."""

        by_team: dict[str, list[ContractMismatch]] = {}
        for mismatch in mismatches:
            by_team.setdefault(mismatch.responsible_team, []).append(mismatch)

        created_ids: list[str] = []
        for team, team_mismatches in by_team.items():
            description_lines = [
                f"- [{m.contract_type.value}] {m.failure}\n  Fix: {m.recommended_fix}"
                for m in team_mismatches
            ]
            task = Task(
                title=f"Integration rework: {len(team_mismatches)} contract mismatch(es)",
                description=(
                    "The Integration Agent detected the following contract mismatch(es) "
                    f"attributed to the {team} team:\n" + "\n".join(description_lines)
                ),
                owner_manager=team,
                complexity=(
                    TaskComplexity.HIGH
                    if any(m.severity.value in ("high", "critical") for m in team_mismatches)
                    else TaskComplexity.MEDIUM
                ),
                expected_outputs=[m.recommended_fix for m in team_mismatches],
            )
            add_task(state, task)
            created_ids.append(task.task_id)
        return created_ids

    async def run(
        self,
        state: AHSEAState,
        registry: ContractRegistry,
        api_usages: list[APIEndpointUsage] | None = None,
        database_usages: list[DatabaseUsage] | None = None,
        environment_usages: list[EnvironmentUsage] | None = None,
        provided_env_keys: set[str] | None = None,
        event_usages: list[EventSchemaUsage] | None = None,
        service_dependencies: list[ServiceDependency] | None = None,
        known_services: set[str] | None = None,
        create_rework_tasks: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> IntegrationReport:
        """Validate every contract dimension and report mismatches.

        Any mismatch found gets a rework `Task` added to `state` (unless
        `create_rework_tasks=False`) -- this method never modifies
        application source itself.
        """

        mismatches: list[ContractMismatch] = []
        mismatches += self.api_validator.validate(registry, api_usages or [])
        mismatches += self.database_validator.validate(registry, database_usages or [])
        mismatches += self.environment_validator.validate(
            registry, environment_usages or [], provided_env_keys
        )
        mismatches += validate_event_schemas(event_usages or [])
        mismatches += validate_service_dependencies(
            service_dependencies or [], known_services or set()
        )

        try:
            analysis = await self._summarize(mismatches, metadata)
            summary = analysis.summary
        except LLMError as exc:
            # Mechanical validation results are still authoritative even if
            # the reasoning summary call fails -- never swallow findings.
            summary = f"(LLM summary unavailable: {exc}) {len(mismatches)} mismatch(es) found."

        created_ids: list[str] = []
        if mismatches and create_rework_tasks:
            created_ids = self._create_rework_tasks(state, mismatches)

        if mismatches and self.realtime is not None:
            by_team: dict[str, int] = {}
            for m in mismatches:
                by_team[m.responsible_team] = by_team.get(m.responsible_team, 0) + 1
            await self.realtime.emit(
                RealtimeEventType.INTEGRATION_FAILED,
                payload={
                    "mismatch_count": len(mismatches),
                    "mismatches_by_team": by_team,
                    "summary": summary,
                    "rework_task_ids": created_ids,
                },
            )

        return IntegrationReport(
            passed=not mismatches,
            mismatches=mismatches,
            summary=summary,
            created_rework_task_ids=created_ids,
        )
