"""Tests for app.db.repositories (Phase 17).

Exercises each repository's CRUD behavior directly against a real SQLite
database (via the async engine), independent of PersistenceService /
converters -- those get their own test modules.
"""

from __future__ import annotations

import pytest

from app.db.config import DatabaseSettings
from app.db.models import (
    AgentORM,
    ArchitectureDecisionORM,
    ArtifactORM,
    ContractORM,
    DeploymentRunORM,
    ErrorORM,
    EventORM,
    LLMRequestORM,
    ProjectORM,
    RepairAttemptORM,
    TaskORM,
    TestResultORM,
)
from app.db.repositories import (
    AgentRepository,
    ArchitectureDecisionRepository,
    ArtifactRepository,
    ContractRepository,
    DeploymentRunRepository,
    ErrorRepository,
    EventRepository,
    LLMRequestRepository,
    ProjectRepository,
    RepairAttemptRepository,
    TaskRepository,
    TestResultRepository,
)
from app.db.session import session_scope


async def _make_project(settings: DatabaseSettings, project_id: str = "proj_1") -> None:
    async with session_scope(settings) as session:
        await ProjectRepository(session).add(
            ProjectORM(
                project_id=project_id,
                name="Demo",
                description="",
                idea_prompt="Build a thing.",
            )
        )


@pytest.mark.asyncio
async def test_project_repository_crud(db_settings: DatabaseSettings):
    async with session_scope(db_settings) as session:
        repo = ProjectRepository(session)
        await repo.add(
            ProjectORM(
                project_id="proj_1", name="Demo", description="d", idea_prompt="idea"
            )
        )

    async with session_scope(db_settings) as session:
        repo = ProjectRepository(session)
        row = await repo.get("proj_1")
        assert row is not None
        assert row.name == "Demo"
        assert await repo.list_ids() == ["proj_1"]

        row.name = "Renamed"
        await repo.upsert(row)

    async with session_scope(db_settings) as session:
        row = await ProjectRepository(session).get("proj_1")
        assert row.name == "Renamed"

    async with session_scope(db_settings) as session:
        deleted = await ProjectRepository(session).delete("proj_1")
        assert deleted is True

    async with session_scope(db_settings) as session:
        assert await ProjectRepository(session).get("proj_1") is None


@pytest.mark.asyncio
async def test_agent_repository_list_by_project(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = AgentRepository(session)
        await repo.add(
            AgentORM(agent_id="agent_1", project_id="proj_1", name="CTO", agent_type="cto")
        )
        await repo.add(
            AgentORM(
                agent_id="agent_2", project_id="proj_1", name="Backend Mgr", agent_type="manager"
            )
        )

    async with session_scope(db_settings) as session:
        agents = await AgentRepository(session).list_by_project("proj_1")
        assert {a.agent_id for a in agents} == {"agent_1", "agent_2"}


@pytest.mark.asyncio
async def test_task_repository_list_by_status(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = TaskRepository(session)
        await repo.add(
            TaskORM(task_id="task_1", project_id="proj_1", title="A", status="pending")
        )
        await repo.add(
            TaskORM(task_id="task_2", project_id="proj_1", title="B", status="completed")
        )

    async with session_scope(db_settings) as session:
        repo = TaskRepository(session)
        pending = await repo.list_by_status("proj_1", "pending")
        assert [t.task_id for t in pending] == ["task_1"]
        assert len(await repo.list_by_project("proj_1")) == 2


@pytest.mark.asyncio
async def test_artifact_and_contract_repositories(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        await ArtifactRepository(session).add(
            ArtifactORM(
                artifact_id="art_1",
                project_id="proj_1",
                artifact_type="source_file",
                path="app/main.py",
            )
        )
        await ContractRepository(session).add(
            ContractORM(
                contract_id="contract_1",
                project_id="proj_1",
                contract_type="api",
                name="Users API",
            )
        )

    async with session_scope(db_settings) as session:
        artifacts = await ArtifactRepository(session).list_by_project("proj_1")
        contracts = await ContractRepository(session).list_by_project("proj_1")
        assert len(artifacts) == 1
        assert len(contracts) == 1


@pytest.mark.asyncio
async def test_event_repository_orders_by_created_at(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = EventRepository(session)
        await repo.add(
            EventORM(event_id="evt_2", project_id="proj_1", scope="project", message="second")
        )
        await repo.add(
            EventORM(event_id="evt_1", project_id="proj_1", scope="agent", message="first")
        )

    async with session_scope(db_settings) as session:
        events = await EventRepository(session).list_by_project("proj_1")
        assert [e.event_id for e in events] == sorted(
            [e.event_id for e in events], key=lambda _: 0
        )
        assert len(events) == 2


@pytest.mark.asyncio
async def test_error_repository_unresolved_filter(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = ErrorRepository(session)
        await repo.add(
            ErrorORM(error_id="err_1", project_id="proj_1", source="worker", message="boom")
        )
        await repo.add(
            ErrorORM(
                error_id="err_2",
                project_id="proj_1",
                source="worker",
                message="fixed",
                resolved=True,
            )
        )

    async with session_scope(db_settings) as session:
        unresolved = await ErrorRepository(session).list_unresolved("proj_1")
        assert [e.error_id for e in unresolved] == ["err_1"]


@pytest.mark.asyncio
async def test_test_result_repository_group_by_report(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = TestResultRepository(session)
        await repo.add(
            TestResultORM(
                test_id="test_1",
                project_id="proj_1",
                report_id="qa_1",
                name="test_a",
                outcome="passed",
            )
        )
        await repo.add(
            TestResultORM(
                test_id="test_2",
                project_id="proj_1",
                report_id="qa_1",
                name="test_b",
                outcome="failed",
            )
        )

    async with session_scope(db_settings) as session:
        rows = await TestResultRepository(session).list_by_report("qa_1")
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_deployment_run_repository_latest(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        repo = DeploymentRunRepository(session)
        await repo.add(
            DeploymentRunORM(run_id="run_1", project_id="proj_1", stage="preparing")
        )
        await repo.add(
            DeploymentRunORM(run_id="run_2", project_id="proj_1", stage="deployed")
        )

    async with session_scope(db_settings) as session:
        latest = await DeploymentRunRepository(session).latest_for_project("proj_1")
        assert latest is not None
        assert latest.run_id == "run_2"
        assert len(await DeploymentRunRepository(session).list_by_project("proj_1")) == 2


@pytest.mark.asyncio
async def test_architecture_decision_repository(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        await ArchitectureDecisionRepository(session).add(
            ArchitectureDecisionORM(
                decision_id="adr_1",
                project_id="proj_1",
                title="Use Postgres",
                context="Need durability.",
                decision="Use PostgreSQL in production.",
            )
        )

    async with session_scope(db_settings) as session:
        decisions = await ArchitectureDecisionRepository(session).list_by_project("proj_1")
        assert decisions[0].title == "Use Postgres"


@pytest.mark.asyncio
async def test_repair_attempt_repository(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        await RepairAttemptRepository(session).add(
            RepairAttemptORM(
                attempt_id="repair_1",
                project_id="proj_1",
                task_id="task_1",
                attempt_number=1,
            )
        )

    async with session_scope(db_settings) as session:
        by_task = await RepairAttemptRepository(session).list_by_task("task_1")
        assert len(by_task) == 1


@pytest.mark.asyncio
async def test_llm_request_repository_never_stores_prompt_by_default(
    db_settings: DatabaseSettings,
):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        await LLMRequestRepository(session).add(
            LLMRequestORM(
                request_id="req_1",
                project_id="proj_1",
                task_type="CODING",
                model="qwen2.5-coder:7b",
                duration=1.23,
                status="success",
            )
        )

    async with session_scope(db_settings) as session:
        rows = await LLMRequestRepository(session).list_by_project("proj_1")
        assert len(rows) == 1
        assert rows[0].prompt_excerpt is None
        assert rows[0].model == "qwen2.5-coder:7b"

        by_type = await LLMRequestRepository(session).list_by_task_type("CODING")
        assert len(by_type) == 1


@pytest.mark.asyncio
async def test_project_cascade_delete_removes_children(db_settings: DatabaseSettings):
    await _make_project(db_settings)
    async with session_scope(db_settings) as session:
        await TaskRepository(session).add(
            TaskORM(task_id="task_1", project_id="proj_1", title="A")
        )
        await ArtifactRepository(session).add(
            ArtifactORM(
                artifact_id="art_1",
                project_id="proj_1",
                artifact_type="source_file",
                path="x.py",
            )
        )

    async with session_scope(db_settings) as session:
        await ProjectRepository(session).delete("proj_1")

    async with session_scope(db_settings) as session:
        assert await TaskRepository(session).get("task_1") is None
        assert await ArtifactRepository(session).get("art_1") is None
