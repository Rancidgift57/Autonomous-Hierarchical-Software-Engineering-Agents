"""Tests for app.git_workflow (Phase 14 -- Git Workflow), against a real,
temporary Git repository (not mocked) so branch/commit/merge/refuse-to-merge
behavior is exercised against actual `git`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.workers.schemas import WorkerScope
from app.git_workflow.engine import GitWorkflowEngine
from app.git_workflow.schemas import (
    ArchitectureReviewDecision,
    GitWorkflowStage,
    is_valid_branch_name,
    is_valid_commit_message,
    make_branch_name,
    make_commit_message,
)
from app.llm.models import TaskType
from app.qa.schemas import QACheckCategory, QACheckResult, QAPipelineReport
from app.state.models import AHSEAState, ProjectMetadata, Task
from app.tools.audit import AuditLog
from app.tools.permissions import WORKER_DEFAULT
from app.tools.registry import build_default_registry, make_executor


def test_make_branch_name_has_agent_prefix():
    branch = make_branch_name("Backend", "task_9f2c1a")
    assert branch.startswith("agent/")
    assert is_valid_branch_name(branch)


def test_make_commit_message_format():
    message = make_commit_message("Backend", "fix login null check")
    assert message == "agent(backend): fix login null check"
    assert is_valid_commit_message(message)


def test_invalid_commit_message_rejected():
    assert not is_valid_commit_message("fixed the bug")
    assert not is_valid_commit_message("agent: missing scope parens")


# ---------------------------------------------------------------------------
# Temporary real Git repository fixture
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, initialized git repo on branch 'main' with one committed file."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello\n")
    _run_git(["add", "README.md"], repo)
    _run_git(["commit", "-m", "initial commit"], repo)
    return repo


def make_tools(repo: Path, permissions=WORKER_DEFAULT):
    return make_executor(
        agent_id="git-workflow-test",
        workspace_root=repo,
        permissions=permissions,
        registry=build_default_registry(),
        audit_log=AuditLog(),
    )


def make_state() -> AHSEAState:
    return AHSEAState(
        project=ProjectMetadata(name="p", description="d", idea_prompt="build something")
    )


class FakeWorker:
    """Writes one file through the same ToolExecutor as the engine, then
    reports success -- exercises the real git_add/git_commit path."""

    agent_id = "fake-worker"

    def __init__(self, tools, path: str = "app/feature.py", content: str = "x = 1\n"):
        self.tools = tools
        self.path = path
        self.content = content

    async def run(self, task: Task, context, metadata=None):
        result = await self.tools.run("write_file", path=self.path, content=self.content)
        assert result.success
        return SimpleNamespace(
            status="success",
            summary=f"Implemented {task.title}.",
            files_changed=[self.path],
            errors=[],
        )


class FakeFailingWorker:
    agent_id = "fake-failing-worker"

    async def run(self, task: Task, context, metadata=None):
        return SimpleNamespace(status="failed", summary="", files_changed=[], errors=["boom"])


class FakeQAManager:
    """Scriptable stand-in for app.qa.manager.QAManager."""

    def __init__(self, gate_passed: bool = True, extra_checks: list[QACheckResult] | None = None):
        self.gate_passed = gate_passed
        self.extra_checks = extra_checks or []
        self.calls = 0

    async def run_pipeline(
        self,
        state,
        contract_registry=None,
        code_summary="",
        files_changed=None,
        diff_or_content="",
        metadata=None,
    ):
        self.calls += 1
        base_checks = [
            QACheckResult(
                check_name="unit_tests",
                category=QACheckCategory.UNIT_TEST,
                agent="UnitTestAgent",
                passed=self.gate_passed,
            ),
            QACheckResult(
                check_name="integration_tests",
                category=QACheckCategory.INTEGRATION_TEST,
                agent="IntegrationTestAgent",
                passed=True,
            ),
            QACheckResult(
                check_name="code_review",
                category=QACheckCategory.CODE_REVIEW,
                agent="CodeReviewAgent",
                passed=self.gate_passed,
            ),
        ] + self.extra_checks
        passed = [c for c in base_checks if c.passed]
        failed = [c for c in base_checks if not c.passed]
        return QAPipelineReport(
            passed_checks=passed,
            failed_checks=failed,
            warnings=[],
            gate_passed=self.gate_passed,
        )


class FakeGateway:
    def __init__(self, architecture_decision: str = "pass"):
        self.architecture_decision = architecture_decision
        self.calls: list[TaskType] = []

    async def generate_json(self, task_type, prompt, response_model, metadata=None, **_):
        self.calls.append(task_type)
        assert response_model is ArchitectureReviewDecision
        assert task_type == TaskType.REASONING  # architecture review -> Qwen3
        return ArchitectureReviewDecision(
            decision=self.architecture_decision,
            concerns=[] if self.architecture_decision == "pass" else ["circular dependency risk"],
        )


def read_file(repo: Path, relative_path: str) -> str | None:
    path = repo / relative_path
    return path.read_text() if path.exists() else None


def current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Happy path: full pipeline merges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_workflow_merges_when_all_gates_pass(git_repo: Path):
    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    worker = FakeWorker(tools)
    qa_manager = FakeQAManager(gate_passed=True)
    gateway = FakeGateway(architecture_decision="pass")
    scope = WorkerScope(allowed_path_prefixes=["app/"])

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, worker, scope=scope)

    assert report.stage_reached == GitWorkflowStage.MERGED
    assert report.merged is True
    assert report.merge_target == "main"
    assert report.branch_name.startswith("agent/backend-")
    assert is_valid_commit_message(report.commit_message)
    assert report.all_gates_passed
    assert report.unauthorized_files == []

    # The merge actually happened in the real repo: main now has the file,
    # and we're sitting on main with a real merge commit.
    assert current_branch(git_repo) == "main"
    assert read_file(git_repo, "app/feature.py") == "x = 1\n"

    log = subprocess.run(
        ["git", "log", "--oneline", "-5"], cwd=git_repo, capture_output=True, text=True
    ).stdout
    assert "agent(backend):" in log

    # Architecture review used TaskType.REASONING (Qwen3) exclusively.
    assert gateway.calls == [TaskType.REASONING]


# ---------------------------------------------------------------------------
# Never merge failing work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_merges_when_unit_tests_fail(git_repo: Path):
    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    worker = FakeWorker(tools)
    qa_manager = FakeQAManager(gate_passed=False)
    gateway = FakeGateway()

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, worker)

    assert report.merged is False
    assert report.stage_reached == GitWorkflowStage.ABORTED
    assert not report.all_gates_passed
    failed_gate_names = {c.name for c in report.checks if not c.passed}
    assert "tests_pass" in failed_gate_names

    # A rework task was created instead of merging.
    assert report.rework_task_id in state.tasks
    assert state.tasks[report.rework_task_id].owner_manager == "Backend"

    # The file never made it into main.
    assert current_branch(git_repo) != "main" or read_file(git_repo, "app/feature.py") is None
    log = subprocess.run(
        ["git", "log", "--all", "--oneline"], cwd=git_repo, capture_output=True, text=True
    ).stdout
    assert "Merge" not in log


@pytest.mark.asyncio
async def test_worker_failure_aborts_before_any_commit(git_repo: Path):
    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    qa_manager = FakeQAManager(gate_passed=True)
    gateway = FakeGateway()

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, FakeFailingWorker())

    assert report.merged is False
    assert report.stage_reached == GitWorkflowStage.BRANCH_CREATED
    assert qa_manager.calls == 0  # pipeline never even ran


# ---------------------------------------------------------------------------
# No unauthorized files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthorized_file_blocks_merge(git_repo: Path):
    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    # Worker writes outside its declared scope (deploy/ instead of app/).
    worker = FakeWorker(tools, path="deploy/secrets.yaml", content="key: value\n")
    qa_manager = FakeQAManager(gate_passed=True)
    gateway = FakeGateway()
    scope = WorkerScope(allowed_path_prefixes=["app/"])

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, worker, scope=scope)

    assert report.merged is False
    assert "deploy/secrets.yaml" in report.unauthorized_files
    failed_gate_names = {c.name for c in report.checks if not c.passed}
    assert "no_unauthorized_files" in failed_gate_names


# ---------------------------------------------------------------------------
# Diff reviewed (architecture review gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_review_failure_blocks_merge(git_repo: Path):
    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    worker = FakeWorker(tools)
    qa_manager = FakeQAManager(gate_passed=True)
    gateway = FakeGateway(architecture_decision="needs_fix")

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, worker)

    assert report.merged is False
    failed_gate_names = {c.name for c in report.checks if not c.passed}
    assert "diff_reviewed" in failed_gate_names
    assert report.architecture_review.decision == "needs_fix"


# ---------------------------------------------------------------------------
# Never overwrite user changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_to_merge_over_uncommitted_target_changes(git_repo: Path):
    # Simulate a human editing README.md on main and never committing it.
    (git_repo / "README.md").write_text("hello\nuser's uncommitted edit\n")

    state = make_state()
    task = Task(title="Add feature", description="Add a small feature.", owner_manager="Backend")
    tools = make_tools(git_repo)
    worker = FakeWorker(tools)  # only touches app/feature.py, never README.md
    qa_manager = FakeQAManager(gate_passed=True)
    gateway = FakeGateway()

    engine = GitWorkflowEngine(gateway=gateway, tools=tools, qa_manager=qa_manager)
    report = await engine.run(state, task, worker)

    assert report.merged is False
    assert any("uncommitted changes" in e for e in report.errors)

    # The user's edit is untouched -- never discarded, never overwritten.
    assert read_file(git_repo, "README.md") == "hello\nuser's uncommitted edit\n"
    # And the agent's change never landed on main.
    assert current_branch(git_repo) == "main"
    assert read_file(git_repo, "app/feature.py") is None


# ---------------------------------------------------------------------------
# Underlying git tools (git_diff ref/name_only, git_current_branch, git_merge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_current_branch_tool(git_repo: Path):
    tools = make_tools(git_repo)
    result = await tools.run("git_current_branch")
    assert result.success
    assert result.output == "main"


@pytest.mark.asyncio
async def test_git_diff_name_only_between_branches(git_repo: Path):
    tools = make_tools(git_repo)
    await tools.run("git_checkout", branch_name="agent/backend-t1", create=True)
    await tools.run("write_file", path="app/x.py", content="1\n")
    await tools.run("git_add", paths=["app/x.py"])
    await tools.run("git_commit", message="agent(backend): add x")

    result = await tools.run("git_diff", ref="main..agent/backend-t1", name_only=True)

    assert result.success
    assert "app/x.py" in result.output


@pytest.mark.asyncio
async def test_git_merge_tool_no_ff_creates_merge_commit(git_repo: Path):
    tools = make_tools(git_repo)
    await tools.run("git_checkout", branch_name="agent/backend-t1", create=True)
    await tools.run("write_file", path="app/x.py", content="1\n")
    await tools.run("git_add", paths=["app/x.py"])
    await tools.run("git_commit", message="agent(backend): add x")
    await tools.run("git_checkout", branch_name="main")

    result = await tools.run(
        "git_merge", branch_name="agent/backend-t1", message="Merge agent/backend-t1", no_ff=True
    )

    assert result.success
    assert read_file(git_repo, "app/x.py") == "1\n"
    # --no-ff guarantees a distinct merge commit (2 parents), not a fast-forward.
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(parents) == 3  # commit hash + 2 parent hashes


@pytest.mark.asyncio
async def test_git_diff_rejects_unsafe_ref(git_repo: Path):
    from app.tools.exceptions import GitOperationError

    tools = make_tools(git_repo)
    with pytest.raises(GitOperationError):
        await tools.run("git_diff", ref="main; rm -rf /")
