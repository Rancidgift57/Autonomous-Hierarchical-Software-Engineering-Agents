"""Safe helper operations for mutating and querying `AHSEAState`.

All mutation of shared state should go through these functions rather than
manipulating `AHSEAState` fields directly. This keeps invariants (e.g.
timestamps, dependency bookkeeping, status transitions) enforced in one
place and gives the orchestration layer a stable, testable API.

Every function returns the (mutated) `AHSEAState` so calls can be chained,
but note that mutation happens in-place on the dict/list fields of the
Pydantic model -- these are not pure/immutable operations.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.state.enums import AgentStatus, DeploymentStage, TaskStatus
from app.state.models import (
    AgentDefinition,
    AgentEvent,
    AgentRuntimeStatus,
    AHSEAState,
    Artifact,
    Contract,
    DeploymentState,
    ErrorRecord,
    ProjectEvent,
    Task,
    TaskDependency,
    TaskResult,
)


class StateError(Exception):
    """Raised when a requested state operation is invalid."""


def _touch(state: AHSEAState) -> None:
    state.updated_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def add_task(state: AHSEAState, task: Task) -> Task:
    """Register a new task and its declared dependencies.

    If the task has no unmet dependencies its status is promoted from
    PENDING to READY automatically.
    """

    if task.task_id in state.tasks:
        raise StateError(f"Task '{task.task_id}' already exists.")

    for dep_id in task.depends_on_task_ids:
        if dep_id not in state.tasks and dep_id != task.task_id:
            # Dependency may be declared before it exists yet (e.g. batch
            # construction of a DAG); we don't hard-fail, but we do refuse
            # self-dependencies below.
            pass
        state.task_dependencies.append(
            TaskDependency(task_id=task.task_id, depends_on_task_id=dep_id)
        )

    if task.task_id in task.depends_on_task_ids:
        raise StateError(f"Task '{task.task_id}' cannot depend on itself.")

    state.tasks[task.task_id] = task
    _recompute_task_readiness(state, task.task_id)
    _touch(state)
    return task


def update_task(state: AHSEAState, task_id: str, **fields: object) -> Task:
    """Apply a partial update to an existing task."""

    task = _get_task_or_raise(state, task_id)
    updated = task.model_copy(update=fields)
    updated.updated_at = datetime.now(UTC)
    state.tasks[task_id] = updated
    _touch(state)
    return updated


def mark_task_completed(
    state: AHSEAState,
    task_id: str,
    result: TaskResult | None = None,
) -> Task:
    """Mark a task COMPLETED, store its result, and unblock dependents."""

    task = _get_task_or_raise(state, task_id)
    now = datetime.now(UTC)
    task.status = TaskStatus.COMPLETED
    task.result = result
    task.completed_at = now
    task.updated_at = now
    state.tasks[task_id] = task

    for dependent_id in _dependents_of(state, task_id):
        _recompute_task_readiness(state, dependent_id)

    _touch(state)
    return task


def mark_task_failed(
    state: AHSEAState,
    task_id: str,
    error_message: str,
    retry: bool = True,
) -> Task:
    """Mark a task FAILED, optionally flipping it to RETRYING if budget remains."""

    task = _get_task_or_raise(state, task_id)
    now = datetime.now(UTC)

    if retry and task.retries < task.max_retries:
        task.retries += 1
        task.status = TaskStatus.RETRYING
    else:
        task.status = TaskStatus.FAILED

    task.result = TaskResult(
        task_id=task_id,
        success=False,
        error_message=error_message,
        completed_at=now,
    )
    task.updated_at = now
    state.tasks[task_id] = task

    for dependent_id in _dependents_of(state, task_id):
        dependent = state.tasks.get(dependent_id)
        if dependent and dependent.status in (TaskStatus.PENDING, TaskStatus.READY):
            dependent.status = TaskStatus.BLOCKED
            dependent.updated_at = now

    _touch(state)
    return task


def get_ready_tasks(state: AHSEAState) -> list[Task]:
    """Return all tasks currently in the READY state, highest priority first."""

    ready = [t for t in state.tasks.values() if t.status == TaskStatus.READY]
    return sorted(ready, key=lambda t: (-t.priority, t.created_at))


def get_blocked_tasks(state: AHSEAState) -> list[Task]:
    """Return all tasks currently BLOCKED on an incomplete dependency."""

    return [t for t in state.tasks.values() if t.status == TaskStatus.BLOCKED]


def _dependents_of(state: AHSEAState, task_id: str) -> list[str]:
    return [dep.task_id for dep in state.task_dependencies if dep.depends_on_task_id == task_id]


def _dependencies_of(state: AHSEAState, task_id: str) -> list[str]:
    return [dep.depends_on_task_id for dep in state.task_dependencies if dep.task_id == task_id]


def _recompute_task_readiness(state: AHSEAState, task_id: str) -> None:
    """Promote PENDING/BLOCKED tasks to READY once all deps are satisfied."""

    task = state.tasks.get(task_id)
    if task is None or task.status not in (
        TaskStatus.PENDING,
        TaskStatus.BLOCKED,
    ):
        return

    dep_ids = _dependencies_of(state, task_id)
    if not dep_ids:
        task.status = TaskStatus.READY
        return

    all_completed = all(
        state.tasks[dep_id].status == TaskStatus.COMPLETED
        for dep_id in dep_ids
        if dep_id in state.tasks
    )
    unknown_deps = any(dep_id not in state.tasks for dep_id in dep_ids)

    if all_completed and not unknown_deps:
        task.status = TaskStatus.READY
    else:
        task.status = TaskStatus.BLOCKED if task.status == TaskStatus.PENDING else task.status


def _get_task_or_raise(state: AHSEAState, task_id: str) -> Task:
    task = state.tasks.get(task_id)
    if task is None:
        raise StateError(f"Task '{task_id}' does not exist.")
    return task


# ---------------------------------------------------------------------------
# Artifacts & contracts
# ---------------------------------------------------------------------------


def add_artifact(state: AHSEAState, artifact: Artifact) -> Artifact:
    if artifact.artifact_id in state.artifacts:
        raise StateError(f"Artifact '{artifact.artifact_id}' already exists.")
    state.artifacts[artifact.artifact_id] = artifact
    _touch(state)
    return artifact


def add_contract(state: AHSEAState, contract: Contract) -> Contract:
    if contract.contract_id in state.contracts:
        raise StateError(f"Contract '{contract.contract_id}' already exists.")
    state.contracts[contract.contract_id] = contract
    _touch(state)
    return contract


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def add_agent(state: AHSEAState, agent: AgentDefinition) -> AgentDefinition:
    if agent.agent_id in state.agents:
        raise StateError(f"Agent '{agent.agent_id}' already exists.")
    if agent.parent_agent_id and agent.parent_agent_id not in state.agents:
        raise StateError(
            f"Parent agent '{agent.parent_agent_id}' does not exist for '{agent.agent_id}'."
        )
    state.agents[agent.agent_id] = agent
    state.agent_statuses[agent.agent_id] = AgentRuntimeStatus(agent_id=agent.agent_id)
    _touch(state)
    return agent


def update_agent_status(
    state: AHSEAState,
    agent_id: str,
    status: AgentStatus,
    current_task_id: str | None = None,
    message: str | None = None,
) -> AgentRuntimeStatus:
    if agent_id not in state.agents:
        raise StateError(f"Agent '{agent_id}' does not exist.")

    runtime = state.agent_statuses.get(agent_id) or AgentRuntimeStatus(agent_id=agent_id)
    runtime.status = status
    runtime.current_task_id = current_task_id
    runtime.message = message
    runtime.last_heartbeat = datetime.now(UTC)
    state.agent_statuses[agent_id] = runtime
    _touch(state)
    return runtime


# ---------------------------------------------------------------------------
# Errors & events
# ---------------------------------------------------------------------------


def add_error(state: AHSEAState, error: ErrorRecord) -> ErrorRecord:
    state.errors.append(error)
    _touch(state)
    return error


def add_event(state: AHSEAState, event: AgentEvent | ProjectEvent) -> AgentEvent | ProjectEvent:
    if isinstance(event, AgentEvent):
        state.agent_events.append(event)
    else:
        state.project_events.append(event)
    _touch(state)
    return event


# ---------------------------------------------------------------------------
# Deployment (Phase 15)
# ---------------------------------------------------------------------------


def set_deployment_stage(
    state: AHSEAState, stage: DeploymentStage, log_message: str | None = None
) -> DeploymentState:
    """Transition `state.deployment.stage` and optionally append a log line.

    `log_message` should already be redacted (see
    `app.deployment.validator.redact_secrets`) -- this function does not
    scrub its input, since scrubbing is a static-analysis concern that
    belongs to the caller, not to this generic state-mutation helper.
    """

    state.deployment.stage = stage
    if log_message:
        state.deployment.deployment_log.append(log_message)
    _touch(state)
    return state.deployment


def record_deployment_approval(
    state: AHSEAState, approved_by: str, approved: bool
) -> DeploymentState:
    now = datetime.now(UTC)
    if approved:
        state.deployment.approved_by = approved_by
        state.deployment.approved_at = now
    else:
        state.deployment.approved_by = None
        state.deployment.approved_at = None
    _touch(state)
    return state.deployment


def record_deployment_result(
    state: AHSEAState, *, deployed: bool, verification_passed: bool | None = None
) -> DeploymentState:
    now = datetime.now(UTC)
    if deployed:
        state.deployment.last_deployed_at = now
    if verification_passed is not None:
        state.deployment.verification_passed = verification_passed
    _touch(state)
    return state.deployment


def record_rollback(state: AHSEAState, reason: str) -> DeploymentState:
    state.deployment.stage = DeploymentStage.ROLLED_BACK
    state.deployment.rollback_reason = reason
    _touch(state)
    return state.deployment
