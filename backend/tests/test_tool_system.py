"""Unit tests for app.tools (Phase 9 -- Agent Tool System)."""

from __future__ import annotations

import subprocess

import pytest

from app.tools.audit import AuditLog
from app.tools.exceptions import (
    CommandNotAllowedError,
    GitOperationError,
    PathValidationError,
    PermissionDeniedError,
    ToolTimeoutError,
)
from app.tools.permissions import READ_ONLY, WORKER_DEFAULT
from app.tools.registry import build_default_registry, make_executor
from app.tools.sandbox import WorkspaceSandbox
from app.tools.shell import validate_command


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n")
    return tmp_path


@pytest.fixture
def worker_executor(workspace):
    return make_executor(
        agent_id="api_worker_1",
        workspace_root=workspace,
        permissions=WORKER_DEFAULT,
    )


@pytest.fixture
def readonly_executor(workspace):
    audit_log = AuditLog()
    return make_executor(
        agent_id="backend_manager",
        workspace_root=workspace,
        permissions=READ_ONLY,
        audit_log=audit_log,
    )


# ---------------------------------------------------------------------------
# Sandbox / path validation
# ---------------------------------------------------------------------------


def test_sandbox_blocks_absolute_path(workspace):
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve("/etc/passwd")


def test_sandbox_blocks_traversal(workspace):
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve("../../etc/passwd")


def test_sandbox_blocks_git_internals(workspace):
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve(".git/config")


def test_sandbox_resolves_valid_relative_path(workspace):
    sandbox = WorkspaceSandbox(workspace)
    resolved = sandbox.resolve("app/main.py", must_exist=True)
    assert resolved == (workspace / "app" / "main.py").resolve()


def test_sandbox_normalizes_backslashes_before_resolving(workspace):
    """A backslash-separated path (Windows-style, or accidentally produced
    by a model/caller regardless of host OS) must resolve the same nested
    file a forward-slash path would -- not a literal single filename
    containing a backslash character, which is what `Path` does with an
    un-normalized backslash string on POSIX."""
    sandbox = WorkspaceSandbox(workspace)
    resolved = sandbox.resolve("app\\main.py", must_exist=True)
    assert resolved == (workspace / "app" / "main.py").resolve()


def test_sandbox_blocks_posix_rooted_path_even_where_pathlib_calls_it_relative(workspace):
    """`Path("/etc/passwd").is_absolute()` is False on Windows (Windows
    "absolute" requires a drive letter/UNC root) even though the same
    string is unambiguously an escape attempt. This must be rejected
    outright rather than relying only on the downstream `relative_to`
    escape check."""
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve("/etc/passwd")


@pytest.mark.parametrize("blocked_variant", [".git", ".GIT", ".Git", "NODE_MODULES", "Venv"])
def test_sandbox_blocks_git_internals_case_insensitively(workspace, blocked_variant):
    """On Windows/macOS the filesystem itself is case-insensitive, so a
    blocklist checked with exact-case comparison only isn't actually a
    security boundary there -- `.GIT/config` resolves to the very same
    directory `.git/config` does."""
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve(f"{blocked_variant}/config")


@pytest.mark.parametrize(
    "reserved_path", ["con.py", "CON.py", "nul.txt", "com1.md", "app/prn.json"]
)
def test_sandbox_blocks_windows_reserved_device_names(workspace, reserved_path):
    """Windows reserves these names in every directory regardless of
    extension/case. Rejecting them here (on every platform, not only when
    actually running on Windows) means a project behaves identically
    wherever it runs, instead of only failing -- with a raw, uncaught
    OSError -- once someone runs it on Windows."""
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathValidationError):
        sandbox.resolve(reserved_path)


def test_sandbox_allows_names_that_merely_contain_a_reserved_word(workspace):
    """The reserved-name check must match the exact base name (before the
    first '.'), not merely check whether a reserved word appears as a
    substring -- `console.py`, `nullable.py`, `iconfig.py` are all
    perfectly ordinary, legal filenames."""
    sandbox = WorkspaceSandbox(workspace)
    resolved = sandbox.resolve("console.py")
    assert resolved == (workspace / "console.py").resolve()


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file(worker_executor):
    result = await worker_executor.run("read_file", path="app/main.py")
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_read_file_outside_sandbox_denied(worker_executor):
    with pytest.raises(PathValidationError):
        await worker_executor.run("read_file", path="../outside.py")


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(worker_executor):
    write_result = await worker_executor.run(
        "write_file", path="app/new_module.py", content="x = 1\n"
    )
    assert write_result.success

    read_result = await worker_executor.run("read_file", path="app/new_module.py")
    assert read_result.output == "x = 1\n"


@pytest.mark.asyncio
async def test_write_file_degrades_gracefully_on_os_error(worker_executor, monkeypatch):
    """Regression test: any platform-level failure writing the file
    (locked by another process on Windows, a path-length limit, a
    permissions error) must degrade to a normal failed ToolResult, not an
    unhandled exception that would crash the whole worker task the same
    way the un-caught subprocess NotImplementedError used to (see
    app/tools/shell.py)."""
    from pathlib import Path

    def _raise_os_error(self, *args, **kwargs):
        raise OSError("simulated platform-level write failure")

    monkeypatch.setattr(Path, "write_text", _raise_os_error)

    result = await worker_executor.run("write_file", path="app/blocked.py", content="x = 1\n")

    assert result.success is False
    assert "simulated platform-level write failure" in result.error


@pytest.mark.asyncio
async def test_read_file_degrades_gracefully_on_os_error(worker_executor, monkeypatch):
    from pathlib import Path

    def _raise_os_error(self, *args, **kwargs):
        raise OSError("simulated platform-level read failure")

    monkeypatch.setattr(Path, "read_text", _raise_os_error)

    result = await worker_executor.run("read_file", path="app/main.py")

    assert result.success is False
    assert "simulated platform-level read failure" in result.error


@pytest.mark.asyncio
async def test_list_files_degrades_gracefully_on_os_error(worker_executor, monkeypatch):
    from pathlib import Path

    def _raise_os_error(self, *args, **kwargs):
        raise OSError("simulated platform-level listing failure")

    monkeypatch.setattr(Path, "iterdir", _raise_os_error)

    result = await worker_executor.run("list_files", path="app")

    assert result.success is False
    assert "simulated platform-level listing failure" in result.error


@pytest.mark.asyncio
async def test_edit_file_requires_unique_match(worker_executor):
    await worker_executor.run("write_file", path="app/dup.py", content="x = 1\nx = 1\n")
    result = await worker_executor.run(
        "edit_file", path="app/dup.py", old_str="x = 1", new_str="x = 2"
    )
    assert not result.success
    assert "not unique" in result.error


@pytest.mark.asyncio
async def test_edit_file_applies_unique_replacement(worker_executor):
    await worker_executor.run("write_file", path="app/single.py", content="x = 1\ny = 2\n")
    result = await worker_executor.run(
        "edit_file", path="app/single.py", old_str="x = 1", new_str="x = 42"
    )
    assert result.success
    read_result = await worker_executor.run("read_file", path="app/single.py")
    assert "x = 42" in read_result.output


@pytest.mark.asyncio
async def test_worker_cannot_delete_without_admin(worker_executor):
    # WORKER_DEFAULT has no ADMIN permission, so delete_file must be denied.
    with pytest.raises(PermissionDeniedError):
        await worker_executor.run("delete_file", path="app/main.py")


@pytest.mark.asyncio
async def test_list_files(worker_executor):
    result = await worker_executor.run("list_files", path="app")
    assert result.success
    assert any("main.py" in entry for entry in result.output)


@pytest.mark.asyncio
async def test_search_files(worker_executor):
    result = await worker_executor.run("search_files", query="hello", path="app")
    assert result.success
    assert len(result.output) == 1
    assert result.output[0]["path"] == "app/main.py"


def test_sandbox_relative_always_uses_forward_slashes(tmp_path):
    """Regression test: `WorkspaceSandbox.relative()` used to return
    `str(Path)`, which is backslash-separated on Windows. Every caller
    (search_files/list_files output, artifact identifiers, WorkerFileChange
    path comparisons) needs a stable, OS-independent separator."""
    sandbox = WorkspaceSandbox(tmp_path)
    nested = tmp_path / "app" / "sub" / "module.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x = 1\n")

    rel = sandbox.relative(nested)

    assert rel == "app/sub/module.py"
    assert "\\" not in rel


async def test_list_files_returns_forward_slash_paths(workspace):
    executor = make_executor(
        agent_id="api_worker_1", workspace_root=workspace, permissions=WORKER_DEFAULT
    )
    result = await executor.run("list_files", path="app")
    assert result.success
    assert all("\\" not in entry for entry in result.output)


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_agent_cannot_write(readonly_executor):
    with pytest.raises(PermissionDeniedError):
        await readonly_executor.run("write_file", path="app/hack.py", content="evil")


@pytest.mark.asyncio
async def test_readonly_agent_cannot_run_commands(readonly_executor):
    with pytest.raises(PermissionDeniedError):
        await readonly_executor.run("run_command", argv=["python", "-m", "pytest"])


@pytest.mark.asyncio
async def test_readonly_agent_cannot_git_commit(readonly_executor):
    with pytest.raises(PermissionDeniedError):
        await readonly_executor.run("git_commit", message="test")


def test_available_tools_reflects_permissions(readonly_executor, worker_executor):
    assert "write_file" not in readonly_executor.available_tools()
    assert "read_file" in readonly_executor.available_tools()
    assert "write_file" in worker_executor.available_tools()
    assert "delete_file" not in worker_executor.available_tools()  # ADMIN only


@pytest.mark.asyncio
async def test_denied_call_is_audited(readonly_executor):
    with pytest.raises(PermissionDeniedError):
        await readonly_executor.run("write_file", path="x.py", content="x")
    denied = readonly_executor.context.audit_log.denied()
    assert len(denied) == 1
    assert denied[0].tool_name == "write_file"
    assert denied[0].agent_id == "backend_manager"


# ---------------------------------------------------------------------------
# Audit log never stores raw content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_redacts_file_content(worker_executor):
    secret = "super-secret-api-key-xyz"
    await worker_executor.run("write_file", path="app/secret.py", content=secret)
    entries = worker_executor.context.audit_log.for_agent("api_worker_1")
    write_entries = [e for e in entries if e.tool_name == "write_file"]
    assert write_entries
    assert secret not in str(write_entries[0].arguments_summary)


# ---------------------------------------------------------------------------
# Command allowlist / untrusted-suggestion validation
# ---------------------------------------------------------------------------


def test_validate_command_rejects_unknown_program():
    with pytest.raises(CommandNotAllowedError):
        validate_command(["rm", "-rf", "/"])


def test_validate_command_rejects_shell_metacharacters():
    with pytest.raises(CommandNotAllowedError):
        validate_command(["python", "-m", "pytest; rm -rf /"])


def test_validate_command_rejects_bad_subcommand():
    with pytest.raises(CommandNotAllowedError):
        validate_command(["python", "-c", "import os; os.system('evil')"])


def test_validate_command_rejects_dependency_installation():
    with pytest.raises(CommandNotAllowedError):
        validate_command(["pip", "install", "untrusted-package"])
    with pytest.raises(CommandNotAllowedError):
        validate_command(["npm", "install"])


def test_validate_command_rejects_docker_in_generic_runner():
    with pytest.raises(CommandNotAllowedError):
        validate_command(["docker", "compose", "up"])


def test_validate_command_accepts_allowlisted_command():
    validate_command(["pytest", "-q"])
    validate_command(["python", "-m", "pytest"])


@pytest.mark.asyncio
async def test_run_command_rejects_disallowed_program(worker_executor):
    # "curl" is allowlisted (Phase 15 smoke tests); "wget" deliberately isn't.
    with pytest.raises(CommandNotAllowedError):
        await worker_executor.run("run_command", argv=["wget", "http://evil.example"])


@pytest.mark.asyncio
async def test_run_command_executes_allowlisted_program(worker_executor):
    result = await worker_executor.run("run_command", argv=["python", "-m", "pytest", "--version"])
    assert result.success


@pytest.mark.asyncio
async def test_run_command_timeout(worker_executor, monkeypatch):
    # Force a tiny timeout so a real (but slow) allowlisted call trips it.
    from app.tools import shell as shell_module

    monkeypatch.setattr(shell_module.RunCommandTool, "default_timeout", 0.001)
    with pytest.raises(ToolTimeoutError):
        await worker_executor.run("run_command", argv=["python", "-m", "pytest", "--version"])


@pytest.mark.asyncio
async def test_run_command_degrades_gracefully_when_subprocess_unsupported(
    worker_executor, monkeypatch
):
    """Regression test for the Windows bug: `asyncio.create_subprocess_exec`
    raises `NotImplementedError` when the running event loop is a Selector
    loop (Windows without the Proactor policy -- e.g. `uvicorn --reload` on
    Windows, or some pytest-asyncio loop configurations). Before the fix,
    this exception was not a `ToolError`, so it wasn't caught anywhere in
    the call chain (`BaseTool.__call__` -> `_test()` -> `WorkerAgent.run()`)
    and instead propagated all the way up, failing the entire project run
    over what should have been "this one subprocess-based step couldn't
    run." It must now degrade to a normal failed `ToolResult` instead."""
    from app.tools import shell as shell_module

    async def _raise_not_implemented(*args, **kwargs):
        raise NotImplementedError("subprocess support is not implemented")

    monkeypatch.setattr(
        shell_module.asyncio, "create_subprocess_exec", _raise_not_implemented
    )

    result = await worker_executor.run("run_command", argv=["python", "-m", "pytest", "--version"])

    assert result.success is False
    assert result.output["returncode"] == 127
    assert "not supported" in result.error.lower() or "not supported" in result.output["stderr"].lower()


@pytest.mark.asyncio
async def test_run_pytest_degrades_gracefully_when_subprocess_unsupported(
    workspace, monkeypatch
):
    """Same regression as above, but through the actual `run_pytest` tool
    a worker's TEST stage calls -- this is the exact path that crashed
    whole orchestrator runs on Windows before the fix."""
    from app.tools import shell as shell_module

    (workspace / "tests").mkdir()

    async def _raise_not_implemented(*args, **kwargs):
        raise NotImplementedError("subprocess support is not implemented")

    monkeypatch.setattr(
        shell_module.asyncio, "create_subprocess_exec", _raise_not_implemented
    )

    executor = make_executor(
        agent_id="api_worker_1", workspace_root=workspace, permissions=WORKER_DEFAULT
    )
    result = await executor.run("run_pytest", path="tests")

    assert result.success is False
    assert result.output["returncode"] == 127


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------


@pytest.fixture
def git_workspace(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def git_executor(git_workspace):
    return make_executor(
        agent_id="api_worker_1",
        workspace_root=git_workspace,
        permissions=WORKER_DEFAULT,
    )


@pytest.mark.asyncio
async def test_git_status(git_executor):
    result = await git_executor.run("git_status")
    assert result.success


@pytest.mark.asyncio
async def test_git_add_commit_flow(git_executor, git_workspace):
    (git_workspace / "new.py").write_text("x = 1\n")
    add_result = await git_executor.run("git_add", paths=["new.py"])
    assert add_result.success
    commit_result = await git_executor.run("git_commit", message="add new.py")
    assert commit_result.success


@pytest.mark.asyncio
async def test_git_commit_rejects_empty_message(git_executor):
    with pytest.raises(GitOperationError):
        await git_executor.run("git_commit", message="   ")


@pytest.mark.asyncio
async def test_git_branch_and_checkout(git_executor):
    create_result = await git_executor.run("git_checkout", branch_name="feature/x", create=True)
    assert create_result.success
    branch_result = await git_executor.run("git_branch")
    assert "feature/x" in branch_result.output


@pytest.mark.asyncio
async def test_git_checkout_rejects_unsafe_branch_name(git_executor):
    with pytest.raises(GitOperationError):
        await git_executor.run("git_checkout", branch_name="--upload-pack=evil")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_default_registry_contains_all_required_tools():
    registry = build_default_registry()
    expected = {
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_files",
        "search_files",
        "run_command",
        "run_pytest",
        "run_lint",
        "run_typecheck",
        "git_status",
        "git_diff",
        "git_branch",
        "git_checkout",
        "git_add",
        "git_commit",
    }
    assert expected.issubset(set(registry.names()))
