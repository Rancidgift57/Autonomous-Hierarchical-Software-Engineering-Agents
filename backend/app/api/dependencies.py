"""FastAPI dependency wiring (Phase 16, extended in Phase 17).

Routers depend on `get_project_service` (and, for persistence-aware
endpoints, `get_persistence_service`), never construct these themselves --
the app factory (`app.api.app.create_app`) is the one place that decides
*which* service instance (and therefore which orchestrator factory /
project store / database) a given app uses, which is what makes it
possible to swap in fakes for tests without touching a single route.
"""

from __future__ import annotations

from fastapi import Request

from app.api.services.project_service import ProjectService
from app.db.persistence_service import PersistenceService


def get_project_service(request: Request) -> ProjectService:
    service = getattr(request.app.state, "project_service", None)
    if service is None:  # pragma: no cover - defensive; app factory always sets this
        raise RuntimeError("ProjectService not configured on the FastAPI app.")
    return service


def get_persistence_service(request: Request) -> PersistenceService | None:
    """Return the app's `PersistenceService`, or `None` if this app instance
    was built without one (e.g. Phase 16-style in-memory-only tests)."""

    return getattr(request.app.state, "persistence_service", None)
