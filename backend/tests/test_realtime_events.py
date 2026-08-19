"""Tests for Phase 19: real-time events (`app.realtime.*` +
`/ws/projects/{project_id}`).

Mirrors `tests/test_api.py`'s style: a `ScriptedOrchestrator` fake so
these stay fast/offline, plus focused unit tests for `ConnectionManager`
and `sanitize_payload` in isolation.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.security import APISecuritySettings, get_current_principal
from app.api.services.project_service import ProjectStore
from app.orchestration.project_runner import ProjectRunControl, ProjectRunStatus
from app.realtime.emitter import RealtimeEmitter
from app.realtime.manager import ConnectionManager
from app.realtime.redaction import sanitize_payload
from app.realtime.schemas import RealtimeEvent, RealtimeEventType
from app.state.enums import EventLevel
from app.state.models import ProjectEvent
from app.state.operations import add_event

# ---------------------------------------------------------------------------
# sanitize_payload
# ---------------------------------------------------------------------------


def test_sanitize_payload_drops_secret_shaped_keys():
    clean = sanitize_payload(
        {
            "api_key": "sk-abcdefghijklmnopqrstuvwx",
            "DATABASE_PASSWORD": "hunter2",
            "system_prompt": "You are a helpful assistant...",
            "env_vars": {"SECRET_TOKEN": "abc"},
            "task_title": "Implement login form",
        }
    )
    assert "api_key" not in clean
    assert "DATABASE_PASSWORD" not in clean
    assert "system_prompt" not in clean
    assert "env_vars" not in clean
    assert clean["task_title"] == "Implement login form"


def test_sanitize_payload_redacts_secret_shaped_values_in_strings():
    clean = sanitize_payload({"log_line": "AWS_KEY=AKIAABCDEFGHIJKLMNOP still works"})
    assert "AKIAABCDEFGHIJKLMNOP" not in clean["log_line"]


def test_sanitize_payload_truncates_long_strings_and_bounds_lists():
    clean = sanitize_payload({"blob": "x" * 5000, "items": list(range(200))})
    assert len(clean["blob"]) < 5000
    assert len(clean["items"]) <= 51  # 50 items + a "N more omitted" marker


def test_sanitize_payload_recurses_into_nested_dicts():
    clean = sanitize_payload({"outer": {"password": "x", "note": "fine"}})
    assert "password" not in clean["outer"]
    assert clean["outer"]["note"] == "fine"


# ---------------------------------------------------------------------------
# ConnectionManager (no WebSocket needed -- broadcast/replay directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_manager_replay_returns_full_history_with_no_after():
    manager = ConnectionManager()
    e1 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.PROJECT_STARTED)
    e2 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.TASK_STARTED)
    await manager.broadcast("p1", e1)
    await manager.broadcast("p1", e2)

    replayed = manager.replay("p1")
    assert [e.event_id for e in replayed] == [e1.event_id, e2.event_id]


@pytest.mark.asyncio
async def test_connection_manager_replay_after_known_event_id_returns_only_the_gap():
    manager = ConnectionManager()
    e1 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.PROJECT_STARTED)
    e2 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.TASK_STARTED)
    e3 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.TASK_COMPLETED)
    for e in (e1, e2, e3):
        await manager.broadcast("p1", e)

    replayed = manager.replay("p1", after_event_id=e1.event_id)
    assert [e.event_id for e in replayed] == [e2.event_id, e3.event_id]


@pytest.mark.asyncio
async def test_connection_manager_replay_unknown_after_id_returns_everything():
    manager = ConnectionManager()
    e1 = RealtimeEvent(project_id="p1", event_type=RealtimeEventType.PROJECT_STARTED)
    await manager.broadcast("p1", e1)

    replayed = manager.replay("p1", after_event_id="rtevt_doesnotexist")
    assert [e.event_id for e in replayed] == [e1.event_id]


@pytest.mark.asyncio
async def test_connection_manager_broadcast_is_isolated_per_project():
    manager = ConnectionManager()
    await manager.broadcast(
        "p1", RealtimeEvent(project_id="p1", event_type=RealtimeEventType.PROJECT_STARTED)
    )
    assert manager.replay("p1")
    assert manager.replay("p2") == []


@pytest.mark.asyncio
async def test_realtime_emitter_sanitizes_before_broadcast():
    manager = ConnectionManager()
    emitter = RealtimeEmitter(manager, project_id="p1")
    await emitter.emit(
        RealtimeEventType.AGENT_TOOL_CALL,
        agent_id="backend-manager",
        payload={"tool_name": "write_file", "api_key": "sk-shouldnotleak1234567890"},
    )
    [event] = manager.replay("p1")
    assert "api_key" not in event.payload
    assert event.payload["tool_name"] == "write_file"
    assert event.agent_id == "backend-manager"


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


class ScriptedOrchestrator:
    """Minimal well-behaved fake orchestrator (same shape as
    `tests.test_api.ScriptedOrchestrator`) -- doesn't itself emit realtime
    events, since these tests broadcast directly through the shared
    `ConnectionManager` to exercise the socket/replay path in isolation.
    """

    async def run(self, state, control: ProjectRunControl) -> None:
        control.status = ProjectRunStatus.RUNNING
        add_event(state, ProjectEvent(level=EventLevel.INFO, message="Run started."))
        await asyncio.sleep(0.01)
        control.status = ProjectRunStatus.COMPLETED
        add_event(state, ProjectEvent(level=EventLevel.INFO, message="Run completed."))


def make_app():
    def factory(state):
        return ScriptedOrchestrator()

    return create_app(orchestrator_factory=factory, store=ProjectStore())


@pytest.fixture
def client():
    app = make_app()
    with TestClient(app) as c:
        yield c


def create_project(client, name="My Project") -> str:
    response = client.post(
        "/api/projects",
        json={"name": name, "description": "d", "idea_prompt": "Build a todo app"},
    )
    assert response.status_code == 201
    return response.json()["project_id"]


def test_websocket_rejects_unknown_project(client):
    with pytest.raises(Exception):  # noqa: B017 - starlette raises on the abnormal close
        with client.websocket_connect("/ws/projects/does-not-exist"):
            pass


def test_websocket_connects_and_receives_broadcast_events(client):
    project_id = create_project(client)
    manager: ConnectionManager = client.app.state.realtime_manager

    with client.websocket_connect(f"/ws/projects/{project_id}") as ws:
        event = RealtimeEvent(
            project_id=project_id,
            event_type=RealtimeEventType.TASK_STARTED,
            task_id="task_abc",
            agent_id="backend-manager",
        )
        asyncio.run(manager.broadcast(project_id, event))

        received = ws.receive_json()
        assert received["event_type"] == "task_started"
        assert received["task_id"] == "task_abc"
        assert received["project_id"] == project_id


def test_websocket_reconnect_replays_missed_events(client):
    project_id = create_project(client)
    manager: ConnectionManager = client.app.state.realtime_manager

    e1 = RealtimeEvent(project_id=project_id, event_type=RealtimeEventType.PROJECT_STARTED)
    e2 = RealtimeEvent(project_id=project_id, event_type=RealtimeEventType.TASK_STARTED)
    asyncio.run(manager.broadcast(project_id, e1))
    asyncio.run(manager.broadcast(project_id, e2))

    # Reconnect claiming to have already seen e1 -- should replay only e2.
    with client.websocket_connect(f"/ws/projects/{project_id}?after={e1.event_id}") as ws:
        received = ws.receive_json()
        assert received["event_id"] == e2.event_id


def test_websocket_requires_api_key_when_enabled(monkeypatch):
    import app.api.security as security_module

    monkeypatch.setattr(
        security_module,
        "get_security_settings",
        lambda: APISecuritySettings(require_api_key=True, api_keys="secretkey"),
    )

    def factory(state):
        return ScriptedOrchestrator()

    app = create_app(orchestrator_factory=factory, store=ProjectStore())
    app.dependency_overrides[get_current_principal] = lambda: None
    with TestClient(app) as c:
        response = c.post(
            "/api/projects",
            json={"name": "P", "description": "d", "idea_prompt": "Build a todo app"},
            headers={"X-API-Key": "secretkey"},
        )
        project_id = response.json()["project_id"]

        with pytest.raises(Exception):  # noqa: B017
            with c.websocket_connect(f"/ws/projects/{project_id}"):
                pass

        with c.websocket_connect(f"/ws/projects/{project_id}?api_key=secretkey"):
            pass
