"""Unit/concurrency tests for app.orchestration + app.llm.queue (Phase 10)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from app.llm.config import LLMSettings
from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.llm.queue import LLMRequestQueue
from app.orchestration.concurrency import ConcurrencyController
from app.orchestration.events import EventBus, TaskEventType
from app.orchestration.executor import TaskExecutor
from app.orchestration.scheduler import TaskScheduler
from app.state.enums import TaskStatus
from app.state.models import AHSEAState, ProjectMetadata, Task
from app.state.operations import add_task
from app.tasks.dag import create_graph


def make_task(task_id: str, depends_on: list[str] | None = None, priority: int = 0, **kw) -> Task:
    return Task(
        task_id=task_id,
        title=task_id,
        description=f"Task {task_id}",
        depends_on_task_ids=depends_on or [],
        priority=priority,
        **kw,
    )


def make_state(tasks: list[Task]) -> AHSEAState:
    state = AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )
    for t in tasks:
        add_task(state, t)
    return state


class Echo(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# ConcurrencyController
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_controller_caps_active_count():
    controller = ConcurrencyController(max_concurrency=2)
    active_snapshots: list[int] = []

    async def worker():
        async with controller.acquire():
            active_snapshots.append(controller.active_count)
            await asyncio.sleep(0.02)

    await asyncio.gather(*(worker() for _ in range(10)))

    assert max(active_snapshots) <= 2
    assert controller.peak_active <= 2
    assert controller.active_count == 0


def test_concurrency_controller_rejects_invalid_max():
    with pytest.raises(ValueError):
        ConcurrencyController(0)


# ---------------------------------------------------------------------------
# LLMRequestQueue
# ---------------------------------------------------------------------------


class FakeProvider:
    """Ollama-shaped fake that records how many calls overlap."""

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.active = 0
        self.peak_active = 0
        self.calls = 0

    async def generate(self, model, prompt, **kwargs):
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
            return '{"text": "ok"}'
        finally:
            self.active -= 1

    async def stream(self, *a, **kw):  # pragma: no cover - unused here
        yield "ok"

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_llm_request_queue_enforces_max_concurrency_of_one():
    provider = FakeProvider(delay=0.03)
    gateway = LLMGateway(provider=provider, settings=LLMSettings(max_llm_concurrency=1))
    queue = LLMRequestQueue(gateway)
    queue.start()

    results = await asyncio.gather(
        *(queue.submit_json(TaskType.REASONING, f"prompt {i}", Echo) for i in range(6))
    )

    assert len(results) == 6
    assert all(r.text == "ok" for r in results)
    # The whole point of MAX_LLM_CONCURRENCY=1: no two inferences ever
    # overlapped, no matter how many callers submitted at once.
    assert provider.peak_active == 1
    assert provider.calls == 6

    await queue.stop()


@pytest.mark.asyncio
async def test_llm_request_queue_respects_higher_concurrency_limit():
    provider = FakeProvider(delay=0.03)
    gateway = LLMGateway(provider=provider, settings=LLMSettings(max_llm_concurrency=3))
    queue = LLMRequestQueue(gateway, num_consumers=3)
    queue.start()

    await asyncio.gather(*(queue.submit_json(TaskType.REASONING, f"p{i}", Echo) for i in range(9)))

    assert provider.peak_active <= 3
    assert provider.peak_active >= 2  # sanity: concurrency was actually exercised
    await queue.stop()


# ---------------------------------------------------------------------------
# TaskExecutor: retry / timeout / cancellation / failure propagation
# ---------------------------------------------------------------------------


class FakeReport(BaseModel):
    status: str
    summary: str = ""
    artifacts: list[str] = []
    errors: list[str] = []


@pytest.mark.asyncio
async def test_executor_marks_success_and_completes_task():
    state = make_state([make_task("a")])

    async def runner(task: Task):
        return FakeReport(status="accepted", summary="done")

    events = EventBus()
    seen = []
    events.subscribe(lambda e: seen.append(e.event_type))
    executor = TaskExecutor(state=state, runner=runner, events=events)

    outcome = await executor.execute(state.tasks["a"])

    assert outcome.success
    assert state.tasks["a"].status == TaskStatus.COMPLETED
    assert TaskEventType.TASK_STARTED in seen
    assert TaskEventType.TASK_COMPLETED in seen


@pytest.mark.asyncio
async def test_executor_retries_then_succeeds():
    state = make_state([make_task("a", max_retries=2)])
    attempts = {"n": 0}

    async def runner(task: Task):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeReport(status="failed", errors=["boom"])
        return FakeReport(status="accepted", summary="ok")

    events = EventBus()
    executor = TaskExecutor(state=state, runner=runner, events=events, retry_backoff_seconds=0)

    outcome = await executor.execute(state.tasks["a"])

    assert outcome.success
    assert attempts["n"] == 3
    retry_events = [e for e in events.history if e.event_type == TaskEventType.TASK_RETRYING]
    assert len(retry_events) == 2


@pytest.mark.asyncio
async def test_executor_exhausts_retries_and_fails():
    state = make_state([make_task("a", max_retries=1)])

    async def runner(task: Task):
        return FakeReport(status="failed", errors=["always broken"])

    events = EventBus()
    executor = TaskExecutor(state=state, runner=runner, events=events, retry_backoff_seconds=0)

    outcome = await executor.execute(state.tasks["a"])

    assert not outcome.success
    assert state.tasks["a"].status == TaskStatus.FAILED
    failed_events = [e for e in events.history if e.event_type == TaskEventType.TASK_FAILED]
    assert len(failed_events) == 1


@pytest.mark.asyncio
async def test_executor_timeout_counts_as_failure_and_can_retry():
    state = make_state([make_task("a", max_retries=1)])

    async def runner(task: Task):
        await asyncio.sleep(10)
        return FakeReport(status="accepted")  # pragma: no cover

    executor = TaskExecutor(
        state=state, runner=runner, timeout_seconds=0.01, retry_backoff_seconds=0
    )
    outcome = await executor.execute(state.tasks["a"])

    assert not outcome.success
    assert state.tasks["a"].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_executor_failure_propagates_to_dependent_as_blocked():
    state = make_state([make_task("a", max_retries=0), make_task("b", depends_on=["a"])])

    async def runner(task: Task):
        if task.task_id == "a":
            return FakeReport(status="failed", errors=["a broke"])
        return FakeReport(status="accepted")  # pragma: no cover - b never runs

    executor = TaskExecutor(state=state, runner=runner, retry_backoff_seconds=0)
    await executor.execute(state.tasks["a"])

    assert state.tasks["a"].status == TaskStatus.FAILED
    assert state.tasks["b"].status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_executor_cancel_marks_task_cancelled():
    state = make_state([make_task("a")])
    started = asyncio.Event()

    async def runner(task: Task):
        started.set()
        await asyncio.sleep(5)
        return FakeReport(status="accepted")  # pragma: no cover

    executor = TaskExecutor(state=state, runner=runner)
    run_coro = asyncio.ensure_future(executor.execute(state.tasks["a"]))
    await started.wait()
    cancelled = executor.cancel("a")
    outcome = await run_coro

    assert cancelled
    assert outcome.cancelled
    assert state.tasks["a"].status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# TaskScheduler: parallel independent execution + dependency ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_runs_independent_tasks_in_parallel():
    # a, b, c are mutually independent; d depends on all three.
    tasks = [
        make_task("a"),
        make_task("b"),
        make_task("c"),
        make_task("d", depends_on=["a", "b", "c"]),
    ]
    state = make_state(tasks)
    order: list[tuple[str, str]] = []  # (task_id, "start"/"end")
    concurrent_peak = {"n": 0, "active": 0}

    async def runner(task: Task):
        concurrent_peak["active"] += 1
        concurrent_peak["n"] = max(concurrent_peak["n"], concurrent_peak["active"])
        order.append((task.task_id, "start"))
        await asyncio.sleep(0.02)
        order.append((task.task_id, "end"))
        concurrent_peak["active"] -= 1
        return FakeReport(status="accepted", summary="ok")

    scheduler = TaskScheduler(state=state, runner=runner, max_task_concurrency=3)
    run = await scheduler.run(create_graph(state.tasks.values()))

    assert run.all_succeeded
    assert set(run.completed) == {"a", "b", "c", "d"}
    # a/b/c should have overlapped (all three concurrently active at once).
    assert concurrent_peak["n"] >= 2
    # d must not start until a, b, and c have all ended.
    d_start_index = order.index(("d", "start"))
    for dep in ("a", "b", "c"):
        assert order.index((dep, "end")) < d_start_index


@pytest.mark.asyncio
async def test_scheduler_respects_max_task_concurrency():
    tasks = [make_task(str(i)) for i in range(8)]
    state = make_state(tasks)
    active = {"n": 0, "peak": 0}

    async def runner(task: Task):
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        await asyncio.sleep(0.02)
        active["n"] -= 1
        return FakeReport(status="accepted")

    scheduler = TaskScheduler(state=state, runner=runner, max_task_concurrency=2)
    run = await scheduler.run(create_graph(state.tasks.values()))

    assert run.all_succeeded
    assert active["peak"] <= 2


@pytest.mark.asyncio
async def test_scheduler_blocks_dependents_on_failure():
    tasks = [make_task("a", max_retries=0), make_task("b", depends_on=["a"])]
    state = make_state(tasks)

    async def runner(task: Task):
        if task.task_id == "a":
            return FakeReport(status="failed", errors=["nope"])
        return FakeReport(status="accepted")  # pragma: no cover

    scheduler = TaskScheduler(state=state, runner=runner)
    run = await scheduler.run(create_graph(state.tasks.values()))

    assert "a" in run.failed
    assert "b" not in run.completed
    assert state.tasks["b"].status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_scheduler_emits_ready_and_blocked_events():
    tasks = [make_task("a", max_retries=0), make_task("b", depends_on=["a"])]
    state = make_state(tasks)

    async def runner(task: Task):
        if task.task_id == "a":
            return FakeReport(status="failed", errors=["nope"])
        return FakeReport(status="accepted")  # pragma: no cover

    events = EventBus()
    scheduler = TaskScheduler(state=state, runner=runner, events=events)
    await scheduler.run(create_graph(state.tasks.values()))

    types_seen = [e.event_type for e in events.history]
    assert TaskEventType.TASK_READY in types_seen
    assert TaskEventType.TASK_BLOCKED in types_seen
    assert TaskEventType.TASK_FAILED in types_seen
