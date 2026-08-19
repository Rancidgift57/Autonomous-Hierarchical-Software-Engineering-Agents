"""`TaskExecutor` (Phase 10).

Executes exactly one `Task` through a caller-supplied `runner` (typically
a bound call into a manager's `handle_task`, see `app.agents.managers.
base.BaseManagerAgent.handle_task`), and is the single place that
implements:

    * timeout enforcement (`asyncio.wait_for`)
    * retry with backoff, bounded by `Task.max_retries`
    * cooperative cancellation (`asyncio.CancelledError` propagation +
      an explicit `cancel(task_id)` API for `TaskScheduler`)
    * failure propagation into `AHSEAState` (`mark_task_failed`, which
      itself cascades BLOCKED status onto dependents)
    * `task_started` / `task_completed` / `task_failed` / `task_retrying`
      events (`task_ready` / `task_blocked` are emitted by
      `TaskScheduler`, which is where readiness is actually computed)

`TaskExecutor` never decides *what* a task needs to do -- that's the
`runner`'s job. It only owns the reliability envelope around calling it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from app.orchestration.events import EventBus, TaskEvent, TaskEventType
from app.state.enums import TaskStatus
from app.state.models import AHSEAState, Task, TaskResult
from app.state.operations import mark_task_completed, mark_task_failed, update_task

logger = logging.getLogger("ahsea.orchestration.executor")


class TaskRunner(Protocol):
    """Whatever actually *does* the task -- e.g. a manager's `handle_task`.

    Must return something with `.summary`, `.artifacts` (or `.files_changed`
    used as a fallback), and `.errors` -- matching `ManagerReport`/
    `WorkerResult`'s shape, without this module needing to import either.
    """

    async def __call__(self, task: Task) -> object: ...


@dataclass
class ExecutionOutcome:
    """What `TaskExecutor.execute` returns for one task attempt sequence."""

    task_id: str
    success: bool
    attempts: int
    result: TaskResult | None = None
    cancelled: bool = False


class TaskExecutionError(Exception):
    """Raised internally to unify timeout/runner-exception handling per attempt."""


DEFAULT_TASK_TIMEOUT_SECONDS = 600.0
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0


@dataclass
class _RunningHandle:
    asyncio_task: asyncio.Task
    cancel_requested: bool = False


class TaskExecutor:
    """Runs one `Task` to completion (success, exhausted retries, or cancellation)."""

    def __init__(
        self,
        state: AHSEAState,
        runner: TaskRunner,
        events: EventBus | None = None,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ):
        self.state = state
        self.runner = runner
        self.events = events or EventBus()
        self.timeout_seconds = timeout_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self._handles: dict[str, _RunningHandle] = {}

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of a currently-executing task.

        Returns True if a running attempt was found and cancelled, False
        if the task isn't currently being executed by this executor.
        """

        handle = self._handles.get(task_id)
        if handle is None:
            return False
        handle.cancel_requested = True
        handle.asyncio_task.cancel()
        return True

    # ------------------------------------------------------------------
    # Single-attempt execution
    # ------------------------------------------------------------------

    async def _run_once(self, task: Task) -> object:
        try:
            return await asyncio.wait_for(self.runner(task), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise TaskExecutionError(
                f"Task '{task.task_id}' exceeded its {self.timeout_seconds}s timeout."
            ) from exc

    @staticmethod
    def _report_errors(report: object) -> list[str]:
        errors = getattr(report, "errors", None)
        return list(errors) if errors else []

    @staticmethod
    def _report_success(report: object) -> bool:
        status = getattr(report, "status", None)
        status_value = getattr(status, "value", status)
        if status_value is not None:
            return str(status_value).lower() in ("accepted", "success")
        return not TaskExecutor._report_errors(report)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def execute(self, task: Task) -> ExecutionOutcome:
        """Run `task` to a terminal outcome, retrying on failure up to
        `task.max_retries`, and publishing events at each transition."""

        attempts = 0
        update_task(self.state, task.task_id, status=TaskStatus.RUNNING, started_at=None)

        while True:
            attempts += 1
            current = self.state.tasks[task.task_id]
            update_task(self.state, task.task_id, status=TaskStatus.RUNNING)
            await self.events.publish(
                TaskEvent(
                    event_type=TaskEventType.TASK_STARTED,
                    task_id=task.task_id,
                    attempt=attempts,
                )
            )

            start = time.monotonic()
            inner_task = asyncio.ensure_future(self._run_once(current))
            handle = _RunningHandle(asyncio_task=inner_task)
            self._handles[task.task_id] = handle

            try:
                report = await inner_task
            except asyncio.CancelledError:
                del self._handles[task.task_id]
                mark_task_failed(
                    self.state, task.task_id, error_message="Task cancelled.", retry=False
                )
                update_task(self.state, task.task_id, status=TaskStatus.CANCELLED)
                await self.events.publish(
                    TaskEvent(
                        event_type=TaskEventType.TASK_CANCELLED,
                        task_id=task.task_id,
                        attempt=attempts,
                    )
                )
                return ExecutionOutcome(
                    task_id=task.task_id, success=False, attempts=attempts, cancelled=True
                )
            except (TaskExecutionError, Exception) as exc:  # noqa: BLE001
                del self._handles[task.task_id]
                duration = time.monotonic() - start
                outcome = await self._handle_failure(task, attempts, str(exc), duration)
                if outcome is not None:
                    return outcome
                continue
            else:
                del self._handles[task.task_id]

            duration = time.monotonic() - start
            errors = self._report_errors(report)
            if self._report_success(report) and not errors:
                result = TaskResult(
                    task_id=task.task_id,
                    success=True,
                    summary=getattr(report, "summary", None),
                    artifact_ids=list(
                        getattr(report, "artifacts", None)
                        or getattr(report, "files_changed", [])
                        or []
                    ),
                    duration_seconds=duration,
                )
                mark_task_completed(self.state, task.task_id, result=result)
                await self.events.publish(
                    TaskEvent(
                        event_type=TaskEventType.TASK_COMPLETED,
                        task_id=task.task_id,
                        attempt=attempts,
                        data={"duration_seconds": duration},
                    )
                )
                return ExecutionOutcome(
                    task_id=task.task_id, success=True, attempts=attempts, result=result
                )

            outcome = await self._handle_failure(
                task, attempts, "; ".join(errors) or "Runner reported failure.", duration
            )
            if outcome is not None:
                return outcome
            continue

    async def _handle_failure(
        self, task: Task, attempts: int, error_message: str, duration: float
    ) -> ExecutionOutcome | None:
        """Apply retry/failure bookkeeping. Returns a terminal `ExecutionOutcome`
        once retries are exhausted, or None to signal "retry and loop again"."""

        current = self.state.tasks[task.task_id]
        can_retry = current.retries < current.max_retries

        updated = mark_task_failed(
            self.state, task.task_id, error_message=error_message, retry=can_retry
        )

        if updated.status == TaskStatus.RETRYING:
            await self.events.publish(
                TaskEvent(
                    event_type=TaskEventType.TASK_RETRYING,
                    task_id=task.task_id,
                    attempt=attempts,
                    message=error_message,
                    data={"retries": updated.retries, "max_retries": updated.max_retries},
                )
            )
            if self.retry_backoff_seconds:
                await asyncio.sleep(self.retry_backoff_seconds * updated.retries)
            update_task(self.state, task.task_id, status=TaskStatus.READY)
            return None

        await self.events.publish(
            TaskEvent(
                event_type=TaskEventType.TASK_FAILED,
                task_id=task.task_id,
                attempt=attempts,
                message=error_message,
            )
        )
        return ExecutionOutcome(
            task_id=task.task_id,
            success=False,
            attempts=attempts,
            result=updated.result,
        )
