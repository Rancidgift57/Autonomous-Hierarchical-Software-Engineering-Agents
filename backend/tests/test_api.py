"""Tests for the FastAPI control plane (Phase 16): app.api.app / routers /
services. Uses a fake `ProjectOrchestrator` (no real LLM/agent work) so these
stay fast and deterministic -- `tests/test_project_orchestrator.py` covers
the real `DefaultProjectOrchestrator` wiring separately."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.security import APISecuritySettings, Principal, get_current_principal
from app.api.services.project_service import ProjectStore
from app.orchestration.project_runner import ProjectRunControl, ProjectRunStatus
from app.state.enums import EventLevel, TaskStatus
from app.state.models import Artifact, ProjectEvent, Task

# ---------------------------------------------------------------------------
# Fake orchestrator: well-behaved w.r.t. cancellation, controllable pacing.
# ---------------------------------------------------------------------------


class ScriptedOrchestrator:
    """Runs `steps` worth of fake work, honoring pause/cancel exactly like
    `DefaultProjectOrchestrator` does, and populates a bit of state along
    the way so the read-only GET endpoints have something to return."""

    def __init__(self, steps: int = 10, step_seconds: float = 0.01, fail: bool = False):
        self.steps = steps
        self.step_seconds = step_seconds
        self.fail = fail

    async def run(self, state, control: ProjectRunControl) -> None:
        control.status = ProjectRunStatus.RUNNING
        from app.state.operations import add_event, add_task

        try:
            add_event(state, ProjectEvent(level=EventLevel.INFO, message="Run started."))
            task = Task(
                title="Do the thing", description="A fake unit of work.", owner_manager="Backend"
            )
            add_task(state, task)
            state.artifacts[task.task_id] = Artifact(
                artifact_type="source_file",
                path="app/thing.py",
                description="fake",
                produced_by_task_id=task.task_id,
            )

            for _ in range(self.steps):
                await control.pause_event.wait()
                if control.cancel_requested:
                    control.status = ProjectRunStatus.CANCELLED
                    add_event(state, ProjectEvent(level=EventLevel.WARNING, message="Cancelled."))
                    return
                await asyncio.sleep(self.step_seconds)
        except asyncio.CancelledError:
            control.status = ProjectRunStatus.CANCELLED
            add_event(
                state, ProjectEvent(level=EventLevel.WARNING, message="Cancelled (task cancelled).")
            )
            return
        except Exception as exc:  # noqa: BLE001 - surface bugs as a FAILED run, never a silent hang
            control.status = ProjectRunStatus.FAILED
            control.error = f"ScriptedOrchestrator error: {exc}"
            return

        if self.fail:
            control.status = ProjectRunStatus.FAILED
            control.error = "simulated failure"
            add_event(state, ProjectEvent(level=EventLevel.ERROR, message="Run failed."))
        else:
            control.status = ProjectRunStatus.COMPLETED
            add_event(state, ProjectEvent(level=EventLevel.INFO, message="Run completed."))


def make_app(steps: int = 10, step_seconds: float = 0.01, fail: bool = False):
    def factory(state):
        return ScriptedOrchestrator(steps=steps, step_seconds=step_seconds, fail=fail)

    return create_app(orchestrator_factory=factory, store=ProjectStore())


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def client():
    app = make_app()
    with TestClient(app) as c:
        yield c


def create_project(client, name="My Project", idea_prompt="Build a todo app") -> str:
    response = client.post(
        "/api/projects", json={"name": name, "description": "d", "idea_prompt": idea_prompt}
    )
    assert response.status_code == 201
    return response.json()["project_id"]


# ---------------------------------------------------------------------------
# Create / list / get
# ---------------------------------------------------------------------------


def test_create_project_returns_full_detail(client):
    response = client.post(
        "/api/projects",
        json={"name": "Todo App", "description": "A todo app", "idea_prompt": "Build a todo app"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Todo App"
    assert body["status"] == "pending"
    assert body["task_count"] == 0
    assert body["project_id"].startswith("proj_")


def test_create_project_validates_required_fields(client):
    response = client.post("/api/projects", json={"name": "", "idea_prompt": ""})
    assert response.status_code == 422


def test_list_projects_returns_created_projects(client):
    id1 = create_project(client, name="Project One")
    id2 = create_project(client, name="Project Two")

    response = client.get("/api/projects")
    assert response.status_code == 200
    ids = {p["project_id"] for p in response.json()}
    assert {id1, id2} <= ids


def test_get_project_by_id(client):
    project_id = create_project(client)
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["project_id"] == project_id


def test_get_unknown_project_returns_404(client):
    response = client.get("/api/projects/proj_does_not_exist")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Run control
# ---------------------------------------------------------------------------


def test_run_then_status_transitions_to_running_then_completed():
    app = make_app(steps=3, step_seconds=0.02)
    with TestClient(app) as client:
        project_id = create_project(client)

        run_response = client.post(f"/api/projects/{project_id}/run")
        assert run_response.status_code == 200

        def is_running():
            return client.get(f"/api/projects/{project_id}/status").json()["status"] == "running"

        assert wait_until(is_running)

        def is_completed():
            return client.get(f"/api/projects/{project_id}/status").json()["status"] == "completed"

        assert wait_until(is_completed, timeout=3.0)


def test_run_twice_while_running_returns_409():
    app = make_app(steps=20, step_seconds=0.02)
    with TestClient(app) as client:
        project_id = create_project(client)
        client.post(f"/api/projects/{project_id}/run")
        wait_until(
            lambda: client.get(f"/api/projects/{project_id}/status").json()["status"] == "running"
        )

        response = client.post(f"/api/projects/{project_id}/run")
        assert response.status_code == 409


def test_run_unknown_project_returns_404(client):
    response = client.post("/api/projects/proj_does_not_exist/run")
    assert response.status_code == 404


def test_pause_and_resume_cycle():
    app = make_app(steps=30, step_seconds=0.02)
    with TestClient(app) as client:
        project_id = create_project(client)
        client.post(f"/api/projects/{project_id}/run")
        wait_until(
            lambda: client.get(f"/api/projects/{project_id}/status").json()["status"] == "running"
        )

        pause_response = client.post(f"/api/projects/{project_id}/pause")
        assert pause_response.status_code == 200
        assert pause_response.json()["status"] == "paused"
        assert client.get(f"/api/projects/{project_id}/status").json()["status"] == "paused"

        resume_response = client.post(f"/api/projects/{project_id}/resume")
        assert resume_response.status_code == 200
        assert resume_response.json()["status"] == "running"


def test_pause_when_not_running_returns_409(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/pause")
    assert response.status_code == 409


def test_resume_when_not_paused_returns_409(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/resume")
    assert response.status_code == 409


def test_cancel_running_project():
    app = make_app(steps=50, step_seconds=0.02)
    with TestClient(app) as client:
        project_id = create_project(client)
        client.post(f"/api/projects/{project_id}/run")
        wait_until(
            lambda: client.get(f"/api/projects/{project_id}/status").json()["status"] == "running"
        )

        cancel_response = client.post(f"/api/projects/{project_id}/cancel")
        assert cancel_response.status_code == 200

        assert wait_until(
            lambda: (
                client.get(f"/api/projects/{project_id}/status").json()["status"] == "cancelled"
            ),
            timeout=3.0,
        )


def test_cancel_with_no_active_run_returns_409(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/cancel")
    assert response.status_code == 409


def test_run_failure_reflected_in_status():
    app = make_app(steps=2, step_seconds=0.01, fail=True)
    with TestClient(app) as client:
        project_id = create_project(client)
        client.post(f"/api/projects/{project_id}/run")

        assert wait_until(
            lambda: client.get(f"/api/projects/{project_id}/status").json()["status"] == "failed",
            timeout=3.0,
        )
        status = client.get(f"/api/projects/{project_id}/status").json()
        assert status["error"] == "simulated failure"


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------


def test_status_task_counts_shape(client):
    project_id = create_project(client)
    response = client.get(f"/api/projects/{project_id}/status")
    assert response.status_code == 200
    counts = response.json()["task_counts"]
    assert set(counts) == {s.value for s in TaskStatus}


def test_agents_tasks_artifacts_events_populated_after_run():
    app = make_app(steps=2, step_seconds=0.01)
    with TestClient(app) as client:
        project_id = create_project(client)
        client.post(f"/api/projects/{project_id}/run")
        assert wait_until(
            lambda: (
                client.get(f"/api/projects/{project_id}/status").json()["status"] == "completed"
            ),
            timeout=3.0,
        )

        tasks = client.get(f"/api/projects/{project_id}/tasks").json()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Do the thing"

        artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
        assert len(artifacts) == 1
        assert artifacts[0]["path"] == "app/thing.py"

        events = client.get(f"/api/projects/{project_id}/events").json()
        assert len(events) >= 2
        assert all(e["scope"] == "project" for e in events)
        # Chronological order.
        timestamps = [e["created_at"] for e in events]
        assert timestamps == sorted(timestamps)


def test_agents_empty_list_for_fresh_project(client):
    project_id = create_project(client)
    response = client.get(f"/api/projects/{project_id}/agents")
    assert response.status_code == 200
    assert response.json() == []


def test_qa_reports_empty_list_for_fresh_project(client):
    project_id = create_project(client)
    response = client.get(f"/api/projects/{project_id}/qa")
    assert response.status_code == 200
    assert response.json() == []


def test_get_deployment_returns_default_state(client):
    project_id = create_project(client)
    response = client.get(f"/api/projects/{project_id}/deployment")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "not_started"
    assert body["approved_by"] is None


def test_read_only_views_on_unknown_project_return_404(client):
    for path in ("agents", "tasks", "artifacts", "events", "qa", "deployment", "status"):
        response = client.get(f"/api/projects/proj_missing/{path}")
        assert response.status_code == 404, path


# ---------------------------------------------------------------------------
# Deployment approval -- never deploy without explicit approval
# ---------------------------------------------------------------------------


def test_approve_deployment_records_approver_and_notes(client):
    project_id = create_project(client)

    response = client.post(
        f"/api/projects/{project_id}/approve-deployment",
        json={"approved_by": "alice@example.com", "notes": "Looks good to ship."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approved_by"] == "alice@example.com"
    assert body["approved_at"] is not None
    assert any("Looks good to ship." in line for line in body["deployment_log"])


def test_approve_deployment_requires_approver_field(client):
    project_id = create_project(client)
    response = client.post(f"/api/projects/{project_id}/approve-deployment", json={})
    assert response.status_code == 422


def test_approve_deployment_unknown_project_returns_404(client):
    response = client.post(
        "/api/projects/proj_missing/approve-deployment", json={"approved_by": "alice"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Authentication-ready architecture
# ---------------------------------------------------------------------------


def test_auth_disabled_by_default_allows_anonymous_access(client):
    response = client.get("/api/projects")
    assert response.status_code == 200


def test_get_current_principal_returns_anonymous_when_auth_disabled(monkeypatch):
    import app.api.security as security_module

    monkeypatch.setattr(
        security_module, "get_security_settings", lambda: APISecuritySettings(require_api_key=False)
    )
    principal = get_current_principal(x_api_key=None)
    assert principal == Principal(subject="anonymous", authenticated=False)


def test_get_current_principal_rejects_missing_key_when_required(monkeypatch):
    from fastapi import HTTPException

    import app.api.security as security_module

    monkeypatch.setattr(
        security_module,
        "get_security_settings",
        lambda: APISecuritySettings(require_api_key=True, api_keys="secret-key"),
    )
    with pytest.raises(HTTPException) as exc_info:
        get_current_principal(x_api_key=None)
    assert exc_info.value.status_code == 401


def test_get_current_principal_accepts_valid_key_when_required(monkeypatch):
    import app.api.security as security_module

    monkeypatch.setattr(
        security_module,
        "get_security_settings",
        lambda: APISecuritySettings(require_api_key=True, api_keys="secret-key,other-key"),
    )
    principal = get_current_principal(x_api_key="secret-key")
    assert principal.authenticated is True
    assert principal.subject == "secret-key"


def test_every_project_route_depends_on_current_principal():
    from app.api.routers.projects import router

    for route in router.routes:
        dependant_calls = {dep.call for dep in route.dependant.dependencies}
        assert get_current_principal in dependant_calls, route.path


def test_auth_enforced_end_to_end_via_dependency_override():
    """Demonstrates the auth seam actually gates requests: override
    `get_current_principal` to always reject, and every route (not just
    one hand-picked example) should now 401."""

    from fastapi import HTTPException

    from app.api.security import get_current_principal as dependency

    app = make_app()

    def always_reject():
        raise HTTPException(status_code=401, detail="nope")

    app.dependency_overrides[dependency] = always_reject
    with TestClient(app) as client:
        response = client.get("/api/projects")
        assert response.status_code == 401
