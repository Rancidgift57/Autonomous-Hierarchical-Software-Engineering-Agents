"""End-to-end integration test for Phase 17: `create_app(persistence_service=...)`
actually persists projects created (and runs completed) through the real
HTTP API, using the API -> Service -> Repository -> Database stack exactly
as production would.

Uses the same `ScriptedOrchestrator` fake as `tests/test_api.py` so this
stays fast/deterministic; the only thing under test here is the persistence
wiring, not orchestration itself.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.services.project_service import ProjectStore
from app.db.config import DatabaseSettings
from app.db.persistence_service import PersistenceService
from app.db.session import get_engine, init_models, reset_engine_cache
from tests.test_api import ScriptedOrchestrator


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def persisted_client(tmp_path):
    settings = DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path}/api_test.db")
    reset_engine_cache()
    engine = get_engine(settings)

    import asyncio

    asyncio.run(init_models(engine))

    persistence = PersistenceService(settings)

    def factory(state):
        return ScriptedOrchestrator(steps=3, step_seconds=0.01)

    app = create_app(
        orchestrator_factory=factory, store=ProjectStore(), persistence_service=persistence
    )
    with TestClient(app) as client:
        yield client, persistence

    reset_engine_cache()


def test_created_project_is_persisted(persisted_client):
    client, persistence = persisted_client

    response = client.post(
        "/api/projects",
        json={"name": "Durable App", "description": "d", "idea_prompt": "Build a todo app"},
    )
    assert response.status_code == 201
    project_id = response.json()["project_id"]

    import asyncio

    async def _check():
        return await persistence.list_project_ids()

    assert wait_until(lambda: project_id in asyncio.run(_check()))

    loaded = asyncio.run(persistence.load_state(project_id))
    assert loaded is not None
    assert loaded.project.name == "Durable App"


def test_completed_run_is_persisted_with_its_tasks(persisted_client):
    client, persistence = persisted_client

    response = client.post(
        "/api/projects",
        json={"name": "Runs App", "description": "d", "idea_prompt": "Build a todo app"},
    )
    project_id = response.json()["project_id"]

    run_response = client.post(f"/api/projects/{project_id}/run")
    assert run_response.status_code == 200

    def _is_completed() -> bool:
        status = client.get(f"/api/projects/{project_id}").json()["status"]
        return status == "completed"

    assert wait_until(_is_completed, timeout=5.0)

    import asyncio

    async def _load():
        # Give the fire-and-forget persistence task a moment to land.
        for _ in range(50):
            loaded = await persistence.load_state(project_id)
            if loaded is not None and loaded.tasks:
                return loaded
            await asyncio.sleep(0.05)
        return await persistence.load_state(project_id)

    loaded = asyncio.run(_load())
    assert loaded is not None
    assert len(loaded.tasks) == 1
    assert any(e.message == "Run completed." for e in loaded.project_events)
