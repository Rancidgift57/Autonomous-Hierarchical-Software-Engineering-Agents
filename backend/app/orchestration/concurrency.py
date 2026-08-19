"""`ConcurrencyController` (Phase 10).

A small, reusable semaphore-based limiter used in two places:

    * `TaskScheduler` uses one instance to cap how many *tasks* run at
      once (independent worker/tool concurrency -- I/O bound, cheap).
    * `LLMRequestQueue` (see `app.llm.queue`) uses one instance seeded
      from `MAX_LLM_CONCURRENCY` to cap how many *LLM inference calls*
      run at once (GPU bound, expensive, default 1).

Keeping this generic (not GPU/LLM-specific) means the same primitive
enforces both limits without a task scheduler needing to know anything
about LLM inference, and without the LLM queue needing to know anything
about the task graph.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ConcurrencyController:
    """Caps the number of concurrently-running units of work at `max_concurrency`.

    Backed by an `asyncio.Semaphore`, plus a small amount of bookkeeping
    (`active_count`, `waiting_count`, `peak_active`) that tests and
    dashboards can inspect without needing access to the semaphore's
    private internals.
    """

    def __init__(self, max_concurrency: int):
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}.")
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active = 0
        self._waiting = 0
        self._peak_active = 0
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def waiting_count(self) -> int:
        return self._waiting

    @property
    def peak_active(self) -> int:
        """Highest `active_count` ever observed -- used by concurrency tests
        to assert the limit was actually respected (not just never hit)."""

        return self._peak_active

    async def _enter(self) -> None:
        async with self._lock:
            self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            async with self._lock:
                self._waiting -= 1
        async with self._lock:
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)

    async def _exit(self) -> None:
        async with self._lock:
            self._active -= 1
        self._semaphore.release()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """`async with controller.acquire(): ...` -- blocks until a slot is free."""

        await self._enter()
        try:
            yield
        finally:
            await self._exit()

    def resize(self, new_max_concurrency: int) -> None:
        """Adjust the limit at runtime (e.g. operator lowers MAX_LLM_CONCURRENCY).

        Implemented by rebuilding the semaphore; safe to call between
        acquisitions but does not forcibly evict already-running holders.
        """

        if new_max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {new_max_concurrency}.")
        self.max_concurrency = new_max_concurrency
        self._semaphore = asyncio.Semaphore(new_max_concurrency)
