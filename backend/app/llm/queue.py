"""`LLMRequestQueue` (Phase 10).

`LLMGateway` (Phase 4) already enforces `MAX_LLM_CONCURRENCY` internally
via its own semaphore, which is sufficient in isolation. Phase 10 adds a
*queue* in front of it because the topology grows a layer: many worker
agents, running concurrently under `TaskScheduler`, must not each race to
invoke the gateway the moment they're ready -- every request funnels
through one shared queue, drained by a small, fixed pool of consumers
sized to `MAX_LLM_CONCURRENCY`:

    multiple agents -> TaskScheduler -> LLMRequestQueue -> controlled inference

This gives three things a bare semaphore on the gateway doesn't:
    * FIFO/priority ordering visible and testable independent of asyncio's
      internal semaphore wait-queue order.
    * A single choke point that can be paused/drained/inspected
      (`pending_count`, `outstanding_count`) as one object, useful for
      shutdown and for tests asserting "no more than N inferences ever
      overlapped".
    * A second, independent enforcement of the concurrency cap (via
      `ConcurrencyController`) so that even if a future change ever let a
      caller reach the gateway directly, this queue still won't let more
      than `MAX_LLM_CONCURRENCY` requests it drains run at once.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from app.llm.gateway import LLMGateway
from app.llm.models import TaskType
from app.orchestration.concurrency import ConcurrencyController

logger = logging.getLogger("ahsea.llm.queue")

ModelT = TypeVar("ModelT", bound=BaseModel)

_counter = itertools.count()


@dataclass(order=True)
class _QueueItem:
    """Priority-ordered queue entry. Lower `sort_key` is drained first."""

    sort_key: tuple[int, int] = field(compare=True)
    request_id: str = field(compare=False)
    coro_factory: Any = field(compare=False)
    future: asyncio.Future[Any] = field(compare=False)
    enqueued_at: float = field(compare=False)


class LLMRequestQueue:
    """The single funnel every agent's LLM call is submitted through.

    Agents/managers/workers should hold a reference to this queue (not to
    `LLMGateway` directly) once a project is running under
    `TaskScheduler`, and call `submit_json`/`submit_text` instead of
    `gateway.generate_json`/`gateway.generate`. A fixed pool of
    `num_consumers` background tasks (default: `gateway.settings.
    max_llm_concurrency`) drains the queue, each holding the shared
    `ConcurrencyController` slot for the duration of one inference call.
    """

    def __init__(
        self,
        gateway: LLMGateway,
        num_consumers: int | None = None,
        controller: ConcurrencyController | None = None,
    ):
        self.gateway = gateway
        self.num_consumers = num_consumers or gateway.settings.max_llm_concurrency
        #: Independent second enforcement of the concurrency cap -- see
        #: module docstring. Defaults to the same MAX_LLM_CONCURRENCY the
        #: gateway itself already enforces.
        self.controller = controller or ConcurrencyController(gateway.settings.max_llm_concurrency)
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._consumers: list[asyncio.Task[None]] = []
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the consumer pool. Idempotent."""

        if self._consumers:
            return
        self._stopped = False
        self._consumers = [
            asyncio.ensure_future(self._consume_loop(i)) for i in range(self.num_consumers)
        ]

    async def stop(self, drain: bool = True) -> None:
        """Stop consuming. If `drain`, waits for in-flight/queued work first."""

        if drain:
            await self._queue.join()
        self._stopped = True
        for consumer in self._consumers:
            consumer.cancel()
        for consumer in self._consumers:
            try:
                await consumer
            except asyncio.CancelledError:
                pass
        self._consumers = []

    async def __aenter__(self) -> LLMRequestQueue:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop(drain=False)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Requests enqueued but not yet picked up by a consumer."""

        return self._queue.qsize()

    @property
    def in_flight_count(self) -> int:
        """Requests currently executing an inference call."""

        return self.controller.active_count

    @property
    def peak_in_flight(self) -> int:
        return self.controller.peak_active

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def _consume_loop(self, consumer_index: int) -> None:
        while not self._stopped:
            item = await self._queue.get()
            try:
                async with self.controller.acquire():
                    if item.future.cancelled():
                        continue
                    try:
                        result = await item.coro_factory()
                    except Exception as exc:  # noqa: BLE001 - propagate to the caller's future
                        if not item.future.cancelled():
                            item.future.set_exception(exc)
                    else:
                        if not item.future.cancelled():
                            item.future.set_result(result)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Submission API
    # ------------------------------------------------------------------

    async def _submit(self, coro_factory: Any, priority: int) -> Any:
        if not self._consumers:
            self.start()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        item = _QueueItem(
            sort_key=(-priority, next(_counter)),
            request_id=str(uuid.uuid4()),
            coro_factory=coro_factory,
            future=future,
            enqueued_at=time.monotonic(),
        )
        await self._queue.put(item)
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def submit_text(
        self,
        task_type: TaskType,
        prompt: str,
        priority: int = 0,
        **kwargs: Any,
    ) -> str:
        """Enqueue a plain-text `LLMGateway.generate` call and await its result."""

        return await self._submit(
            lambda: self.gateway.generate(task_type=task_type, prompt=prompt, **kwargs),
            priority=priority,
        )

    async def submit_json(
        self,
        task_type: TaskType,
        prompt: str,
        response_model: type[ModelT],
        priority: int = 0,
        **kwargs: Any,
    ) -> ModelT:
        """Enqueue a structured `LLMGateway.generate_json` call and await its result."""

        return await self._submit(
            lambda: self.gateway.generate_json(
                task_type=task_type, prompt=prompt, response_model=response_model, **kwargs
            ),
            priority=priority,
        )
