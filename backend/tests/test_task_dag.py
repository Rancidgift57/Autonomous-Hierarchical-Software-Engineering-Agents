"""Unit tests for app.tasks (Phase 6 -- dependency-aware task graph)."""

from __future__ import annotations

import pytest

from app.state.enums import TaskComplexity, TaskStatus
from app.state.models import Task
from app.tasks import (
    TaskGraphError,
    TaskGraphValidationError,
    create_graph,
    get_dependencies,
    get_dependents,
    get_parallel_groups,
    get_ready_tasks,
    topological_sort,
    validate_graph,
)


def make_task(task_id: str, depends_on: list[str] | None = None, priority: int = 0, **kw) -> Task:
    return Task(
        task_id=task_id,
        title=task_id,
        description=f"Task {task_id}",
        depends_on_task_ids=depends_on or [],
        priority=priority,
        **kw,
    )


# ---------------------------------------------------------------------------
# create_graph
# ---------------------------------------------------------------------------


def test_create_graph_basic():
    tasks = [make_task("a"), make_task("b", depends_on=["a"])]
    graph = create_graph(tasks)
    assert len(graph) == 2
    assert "a" in graph
    assert "b" in graph


def test_create_graph_duplicate_ids_rejected():
    tasks = [make_task("a"), make_task("a")]
    with pytest.raises(TaskGraphError):
        create_graph(tasks)


def test_get_task_missing_raises():
    graph = create_graph([make_task("a")])
    with pytest.raises(TaskGraphError):
        graph.get_task("missing")


# ---------------------------------------------------------------------------
# validate_graph
# ---------------------------------------------------------------------------


def test_validate_graph_valid_dag_passes():
    tasks = [
        make_task("db", []),
        make_task("backend", ["db"]),
        make_task("frontend", ["backend"]),
    ]
    graph = create_graph(tasks)
    validate_graph(graph)  # should not raise


def test_validate_graph_detects_self_dependency():
    graph = create_graph([make_task("a", depends_on=["a"])])
    with pytest.raises(TaskGraphValidationError) as exc_info:
        validate_graph(graph)
    assert any("cannot depend on itself" in e for e in exc_info.value.errors)


def test_validate_graph_detects_missing_dependency():
    graph = create_graph([make_task("a", depends_on=["ghost"])])
    with pytest.raises(TaskGraphValidationError) as exc_info:
        validate_graph(graph)
    assert any("missing task 'ghost'" in e for e in exc_info.value.errors)


def test_validate_graph_detects_cycle():
    tasks = [
        make_task("a", depends_on=["c"]),
        make_task("b", depends_on=["a"]),
        make_task("c", depends_on=["b"]),
    ]
    graph = create_graph(tasks)
    with pytest.raises(TaskGraphValidationError) as exc_info:
        validate_graph(graph)
    assert any("Circular task dependency" in e for e in exc_info.value.errors)


def test_validate_graph_collects_multiple_errors():
    tasks = [
        make_task("a", depends_on=["a"]),  # self-dep
        make_task("b", depends_on=["ghost"]),  # missing dep
    ]
    graph = create_graph(tasks)
    with pytest.raises(TaskGraphValidationError) as exc_info:
        validate_graph(graph)
    assert len(exc_info.value.errors) >= 2


# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


def test_topological_sort_respects_dependencies():
    tasks = [
        make_task("db", []),
        make_task("backend", ["db"]),
        make_task("frontend", ["backend"]),
    ]
    graph = create_graph(tasks)
    order = topological_sort(graph)
    assert order.index("db") < order.index("backend")
    assert order.index("backend") < order.index("frontend")
    assert set(order) == {"db", "backend", "frontend"}


def test_topological_sort_raises_on_cycle():
    tasks = [make_task("a", depends_on=["b"]), make_task("b", depends_on=["a"])]
    graph = create_graph(tasks)
    with pytest.raises(TaskGraphValidationError):
        topological_sort(graph)


def test_topological_sort_is_deterministic_with_priority():
    # Two independent roots; higher priority should sort first among ties.
    tasks = [make_task("low", priority=0), make_task("high", priority=10)]
    graph = create_graph(tasks)
    order = topological_sort(graph)
    assert order == ["high", "low"]


# ---------------------------------------------------------------------------
# get_parallel_groups
# ---------------------------------------------------------------------------


def test_parallel_groups_identifies_independent_tasks():
    # database schema, frontend UI, and backend service can run concurrently
    # once "design" is done; "integration" waits on all three.
    tasks = [
        make_task("design", []),
        make_task("database", ["design"]),
        make_task("backend", ["design"]),
        make_task("frontend", ["design"]),
        make_task("integration", ["database", "backend", "frontend"]),
    ]
    graph = create_graph(tasks)
    groups = get_parallel_groups(graph)

    assert groups[0] == ["design"]
    assert set(groups[1]) == {"backend", "database", "frontend"}
    assert groups[2] == ["integration"]


def test_parallel_groups_raises_on_cycle():
    tasks = [make_task("a", depends_on=["b"]), make_task("b", depends_on=["a"])]
    graph = create_graph(tasks)
    with pytest.raises(TaskGraphValidationError):
        get_parallel_groups(graph)


# ---------------------------------------------------------------------------
# get_dependencies / get_dependents
# ---------------------------------------------------------------------------


def test_get_dependencies_and_dependents():
    tasks = [
        make_task("db", []),
        make_task("backend", ["db"]),
        make_task("frontend", ["backend"]),
    ]
    graph = create_graph(tasks)

    assert get_dependencies(graph, "backend") == ["db"]
    assert get_dependencies(graph, "db") == []
    assert get_dependents(graph, "db") == ["backend"]
    assert get_dependents(graph, "frontend") == []


def test_get_dependents_missing_task_raises():
    graph = create_graph([make_task("a")])
    with pytest.raises(TaskGraphError):
        get_dependents(graph, "missing")


# ---------------------------------------------------------------------------
# get_ready_tasks
# ---------------------------------------------------------------------------


def test_get_ready_tasks_no_dependencies():
    tasks = [make_task("a"), make_task("b")]
    graph = create_graph(tasks)
    ready_ids = {t.task_id for t in get_ready_tasks(graph)}
    assert ready_ids == {"a", "b"}


def test_get_ready_tasks_respects_completed_set():
    tasks = [make_task("a"), make_task("b", depends_on=["a"])]
    graph = create_graph(tasks)

    ready_before = {t.task_id for t in get_ready_tasks(graph)}
    assert ready_before == {"a"}

    ready_after = {t.task_id for t in get_ready_tasks(graph, completed_task_ids=["a"])}
    assert ready_after == {"b"}


def test_get_ready_tasks_excludes_completed_and_cancelled():
    tasks = [
        make_task("a", status=TaskStatus.COMPLETED),
        make_task("b", status=TaskStatus.CANCELLED),
        make_task("c"),
    ]
    graph = create_graph(tasks)
    ready_ids = {t.task_id for t in get_ready_tasks(graph)}
    assert ready_ids == {"c"}


def test_get_ready_tasks_orders_by_priority():
    tasks = [make_task("low", priority=1), make_task("high", priority=5)]
    graph = create_graph(tasks)
    ready = get_ready_tasks(graph)
    assert [t.task_id for t in ready] == ["high", "low"]


def test_task_complexity_default_and_new_fields():
    task = make_task(
        "a",
        owner_manager="Backend",
        worker_type="api_worker",
        expected_outputs=["A working /health endpoint"],
        complexity=TaskComplexity.HIGH,
    )
    assert task.owner_manager == "Backend"
    assert task.worker_type == "api_worker"
    assert task.expected_outputs == ["A working /health endpoint"]
    assert task.complexity == TaskComplexity.HIGH
