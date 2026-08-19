"""Unit tests for app.tools.docker's DOCKER_NOT_AVAILABLE sentinel logic.

This is the mechanism `DeploymentManager` relies on to tell "docker isn't
usable on this host -- skip deployment" apart from "docker ran and the
build genuinely failed -- this is a real defect". Before this file, that
distinction had no direct test coverage at all (see test_deployment.py
for the pipeline-level tests this complements).
"""

from __future__ import annotations

from app.tools.docker import DOCKER_NOT_AVAILABLE, _looks_like_daemon_unreachable, _tool_result


def test_returncode_127_is_not_available():
    """The `_run_subprocess` sentinel for "executable not found at all"."""
    result = _tool_result("docker_build", 127, "", "'docker' is not recognized...", "docker build failed.")
    assert result.success is False
    assert result.error == DOCKER_NOT_AVAILABLE


def test_real_nonzero_returncode_is_a_genuine_failure():
    result = _tool_result("docker_build", 1, "", "Dockerfile:3: syntax error", "docker build failed.")
    assert result.success is False
    assert result.error == "docker build failed."


def test_returncode_zero_is_success():
    result = _tool_result("docker_build", 0, "Successfully built abc123", "", "docker build failed.")
    assert result.success is True
    assert result.error is None


def test_daemon_unreachable_stderr_maps_to_not_available_despite_real_returncode():
    """The actual bug fix: Docker installed (so the CLI runs -> a real
    non-127 returncode) but no daemon behind it -- must be treated the
    same as 'not installed', not as a genuine build failure."""
    result = _tool_result(
        "docker_build",
        1,
        "",
        "error during connect: this error may indicate that the docker daemon is not running.",
        "docker build failed.",
    )
    assert result.success is False
    assert result.error == DOCKER_NOT_AVAILABLE


def test_windows_named_pipe_daemon_unreachable_maps_to_not_available():
    """The Windows-specific wording Docker Desktop uses when it isn't
    running: a named-pipe connection failure, not the Linux socket
    wording -- both must be recognized."""
    result = _tool_result(
        "docker_build",
        1,
        "",
        "error during connect: Get \"http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.24/...\": "
        "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.",
        "docker build failed.",
    )
    assert result.success is False
    assert result.error == DOCKER_NOT_AVAILABLE


def test_daemon_unreachable_check_is_case_insensitive():
    assert _looks_like_daemon_unreachable("", "Cannot Connect To The Docker Daemon") is True


def test_daemon_unreachable_check_does_not_false_positive_on_normal_output():
    assert _looks_like_daemon_unreachable("Successfully built abc123", "") is False
    assert _looks_like_daemon_unreachable("", "COPY failed: file not found in build context") is False
