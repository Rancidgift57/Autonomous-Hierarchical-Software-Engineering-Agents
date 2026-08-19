"""FastAPI application factory (Phase 16, extended with persistence in
Phase 17).

`create_app` is the single place that decides which `ProjectService`
(and therefore which orchestrator factory / project store / persistence
layer) an app instance uses -- production code calls it with no arguments
(or a real `orchestrator_factory` and `persistence_service`), tests call it
with fakes. Routers never construct their own service, orchestrator, or
database session.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":  # pragma: no cover - platform-specific, no Windows CI runner
    # `asyncio.create_subprocess_exec` needs the Proactor event loop on
    # Windows; the Selector loop (which `uvicorn --reload` forces on
    # Windows for its reloader/multiprocessing compatibility) does not
    # implement subprocess support at all and raises `NotImplementedError`
    # for every child-process call. AHSEA's shell tools
    # (run_pytest/run_lint/run_typecheck/run_command, git tools) all go
    # through subprocess, so without this every one of them would be
    # unable to actually run -- they'd degrade to a failed ToolResult
    # (see the NotImplementedError handling in app/tools/shell.py) instead
    # of working. Setting this here, before any event loop exists, makes
    # them work for real rather than merely fail without crashing the run.
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routers.projects import router as projects_router
from app.api.routers.metrics import router as metrics_router
from app.api.routers.websocket import router as websocket_router
from app.api.services.project_service import (
    InvalidProjectStateError,
    OrchestratorFactory,
    ProjectNotFoundError,
    ProjectService,
    ProjectStore,
    default_orchestrator_factory,
)
from app.db.persistence_service import PersistenceService
from app.db.session import init_models
from app.realtime.manager import ConnectionManager


def create_app(
    orchestrator_factory: OrchestratorFactory | None = None,
    store: ProjectStore | None = None,
    persistence_service: PersistenceService | None = None,
    auto_create_tables: bool = False,
    realtime_manager: ConnectionManager | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `persistence_service` is optional (`None` by default), matching Phase
    16 behavior exactly for callers that don't pass one: state lives only
    in the in-memory `ProjectStore` for the process lifetime. Pass a real
    `PersistenceService` (e.g. `PersistenceService()`, reading
    `DATABASE_URL` from the environment) to durably persist projects, LLM
    telemetry, and everything else listed in the Phase 17 spec.

    `auto_create_tables=True` runs `Base.metadata.create_all` on startup --
    a convenience for local SQLite development/tests. Production
    deployments should apply the Alembic migrations under
    `alembic/versions/` ahead of time instead and leave this `False`.

    `realtime_manager` (Phase 19) is the `ConnectionManager` backing
    `/ws/projects/{project_id}`. One is always created if not given -- the
    endpoint always works -- but it's only ever fed real events when
    `orchestrator_factory` is left `None` (so the default factory can wire
    a `RealtimeEmitter` into the orchestrator it builds) or when a custom
    factory does the same wiring itself. Tests that pass a fake
    orchestrator factory get a working, empty socket, which is correct:
    a fake orchestrator has no real events to emit.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if auto_create_tables:
            await init_models()
        yield

    app = FastAPI(
        title="AHSEA Control Plane",
        description=(
            "FastAPI control plane for the Autonomous Hierarchical Software "
            "Engineering Agent system."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    realtime_manager = realtime_manager or ConnectionManager()

    app.state.project_service = ProjectService(
        orchestrator_factory=orchestrator_factory
        or default_orchestrator_factory(
            persistence=persistence_service, realtime_manager=realtime_manager
        ),
        store=store,
        persistence=persistence_service,
        realtime_manager=realtime_manager,
    )
    app.state.persistence_service = persistence_service
    app.state.realtime_manager = realtime_manager

    app.include_router(projects_router)
    app.include_router(metrics_router)
    app.include_router(websocket_router)

    @app.exception_handler(ProjectNotFoundError)
    async def _not_found(_: Request, exc: ProjectNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidProjectStateError)
    async def _conflict(_: Request, exc: InvalidProjectStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


#: Default app instance for `uvicorn app.api.app:app`. Uses the real
#: `DefaultProjectOrchestrator` (talks to Ollama) -- not imported/used by
#: the test suite, which builds its own app via `create_app(...)` with a
#: fake orchestrator factory.
app = create_app()
