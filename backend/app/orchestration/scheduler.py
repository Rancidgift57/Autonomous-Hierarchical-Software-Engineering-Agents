"""`TaskScheduler` (Phase 10).

Drives the task DAG (Phase 6, `app.tasks.dag`) to completion: repeatedly
finds every currently-READY task, launches each one concurrently through
a `TaskExecutor` (bounded by a `ConcurrencyController` for *task*
concurrency -- how many managers/workers may be actively working at
once), and reacts to completions by recomputing readiness for the rest
of the graph. LLM inference itself is a separate, much tighter bottleneck
(`MAX_LLM_CONCURRENCY`, default 1) enforced independently by
`LLMRequestQueue` -- a scheduler running 8 tasks "concurrently" just means
8 tasks are each free to *ask* for inference at once; only one of those
requests is actually running against the GPU at any moment. That's the
whole point of the layering:

    multiple agents -> TaskScheduler -> LLMRequestQueue -> controlled inference

`TaskScheduler` never talks to the LLM queue directly -- it only knows
about `Task`/`TaskGraph`. Whatever `runner` it's given (typically wired to
managers that themselves hold an `LLMRequestQueue`) is where that
constraint actually bites.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.orchestration.concurrency import ConcurrencyController
from app.orchestration.events import EventBus, TaskEvent, TaskEventType
from app.orchestration.executor import ExecutionOutcome, TaskExecutor, TaskRunner
from app.state.enums import TaskStatus
from app.state.models import AHSEAState, Task
from app.tasks.dag import TaskGraph, create_graph, validate_graph

logger = logging.getLogger("ahsea.orchestration.scheduler")

DEFAULT_MAX_TASK_CONCURRENCY = 4


@dataclass
class SchedulerRun:
    """Summary of one `TaskScheduler.run()` pass over a graph."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    outcomes: dict[str, ExecutionOutcome] = field(default_factory=dict)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed and not self.cancelled


class TaskScheduler:
    """Executes every task in a `TaskGraph`, respecting dependencies, in parallel.

    Independent tasks (no dependency relationship between them) run
    concurrently, up to `max_task_concurrency`; dependents only ever
    start once every dependency has reached a terminal COMPLETED state.
    A dependency ending in FAILED/CANCELLED propagates BLOCKED status to
    its dependents (via `TaskExecutor` -> `app.state.operations.
    mark_task_failed`), so they are never scheduled.
    """

    def __init__(
        self,
        state: AHSEAState,
        runner: TaskRunner,
        max_task_concurrency: int = DEFAULT_MAX_TASK_CONCURRENCY,
        events: EventBus | None = None,
        executor: TaskExecutor | None = None,
    ):
        self.state = state
        self.runner = runner
        self.events = events or EventBus()
        self.controller = ConcurrencyController(max_task_concurrency)
        self.executor = executor or TaskExecutor(state=state, runner=runner, events=self.events)
        #: task_id -> in-flight asyncio.Task, so `cancel_task` and
        #: `wait_idle` can find/await currently-scheduled work.
        self._inflight: dict[str, asyncio.Task[ExecutionOutcome]] = {}

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task if it is currently running under this scheduler."""

        return self.executor.cancel(task_id)

    # ------------------------------------------------------------------
    # Core scheduling loop
    # ------------------------------------------------------------------

    async def _launch(self, task: Task, run: SchedulerRun) -> None:
        async with self.controller.acquire():
            outcome = await self.executor.execute(task)
        run.outcomes[task.task_id] = outcome
        if outcome.cancelled:
            run.cancelled.append(task.task_id)
        elif outcome.success:
            run.completed.append(task.task_id)
        else:
            run.failed.append(task.task_id)
        self._inflight.pop(task.task_id, None)

    def _ready_tasks(self, graph: TaskGraph, scheduled: set[str]) -> list[Task]:
        ready = [
            t
            for t in graph.tasks.values()
            if t.status == TaskStatus.READY and t.task_id not in scheduled
        ]
        return sorted(ready, key=lambda t: (-t.priority, t.created_at))

    async def run(self, graph: TaskGraph | None = None) -> SchedulerRun:
        """Run every task in `graph` (default: the full current `state.tasks`)
        to a terminal state, launching independent tasks in parallel."""

        graph = graph or create_graph(self.state.tasks.values())
        validate_graph(graph)

        run = SchedulerRun()
        scheduled: set[str] = set()
        previously_blocked: set[str] = set()

        def terminal() -> bool:
            live_ids = set(graph.tasks)
            done = set(run.completed) | set(run.failed) | set(run.cancelled)
            # A task is "settled" once it's terminal or nothing further can
            # unblock it (BLOCKED tasks whose dependency chain has ended).
            for tid in live_ids - done:
                task = self.state.tasks.get(tid, graph.tasks[tid])
                if task.status not in (TaskStatus.BLOCKED,):
                    return False
            return True

        while not terminal():
            for task in self._ready_tasks(graph, scheduled):
                scheduled.add(task.task_id)
                await self.events.publish(
                    TaskEvent(event_type=TaskEventType.TASK_READY, task_id=task.task_id)
                )
                self._inflight[task.task_id] = asyncio.ensure_future(self._launch(task, run))

            newly_blocked = {
                t.task_id
                for t in graph.tasks.values()
                if self.state.tasks.get(t.task_id, t).status == TaskStatus.BLOCKED
            } - previously_blocked
            for blocked_id in newly_blocked:
                await self.events.publish(
                    TaskEvent(event_type=TaskEventType.TASK_BLOCKED, task_id=blocked_id)
                )
            previously_blocked |= newly_blocked

            if self._inflight:
                await asyncio.wait(
                    list(self._inflight.values()), return_when=asyncio.FIRST_COMPLETED
                )
            elif not terminal():
                # Nothing running and nothing ready: every remaining task is
                # permanently BLOCKED (a dependency failed/was cancelled).
                # Avoid a busy-loop -- there is nothing left this scheduler
                # run can do.
                break

        if self._inflight:
            await asyncio.gather(*self._inflight.values())

        return run
