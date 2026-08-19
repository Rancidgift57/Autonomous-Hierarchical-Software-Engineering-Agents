"""Dependency-aware task graph (Phase 6).

Builds a directed acyclic graph over `app.state.models.Task` objects, using
each task's existing `depends_on_task_ids` field as the edge list (no
separate DAG-specific task representation is introduced -- the same `Task`
produced by the CTO agent, or hand-built by orchestration code, is the
graph's node type).

Design notes on where each check lives:
    * Duplicate task IDs make it structurally impossible to build an
      unambiguous `{task_id: Task}` mapping, so `create_graph()` fails fast
      on them rather than silently keeping the last-seen task.
    * Missing dependencies, self-dependencies, and cycles don't prevent
      *building* the graph (the referencing task and its `depends_on_task_ids`
      list are still well-formed data) -- they're structural problems with
      the graph's edges, so they're all caught together by `validate_graph()`,
      which collects every problem found rather than failing on the first.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.state.enums import TaskStatus
from app.state.models import Task


class TaskGraphError(Exception):
    """Raised for structurally invalid graph construction/lookup requests."""


class TaskGraphValidationError(Exception):
    """Raised by `validate_graph` when the graph's edges are invalid.

    `errors` contains every problem found (missing dependencies,
    self-dependencies, cycles) so callers can report a complete diagnostic
    instead of fixing issues one at a time.
    """

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class TaskGraph:
    """An in-memory dependency graph over a fixed set of `Task`s.

    `dependents` is the reverse-edge index (`task_id -> {ids of tasks that
    depend on it}`), built once at construction time for O(1) lookups.
    """

    tasks: dict[str, Task] = field(default_factory=dict)
    dependents: dict[str, set[str]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.tasks)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self.tasks

    def get_task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskGraphError(f"Task id '{task_id}' is not in the graph.")
        return task


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def create_graph(tasks: Iterable[Task]) -> TaskGraph:
    """Build a `TaskGraph` from an iterable of `Task`s.

    Raises:
        TaskGraphError: if two tasks share the same `task_id`.
    """

    graph = TaskGraph()

    for task in tasks:
        if task.task_id in graph.tasks:
            raise TaskGraphError(
                f"Duplicate task id detected: '{task.task_id}'. Task IDs must be unique."
            )
        graph.tasks[task.task_id] = task
        graph.dependents.setdefault(task.task_id, set())

    for task in graph.tasks.values():
        for dep_id in task.depends_on_task_ids:
            graph.dependents.setdefault(dep_id, set())
            graph.dependents[dep_id].add(task.task_id)

    return graph


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_graph(graph: TaskGraph) -> None:
    """Validate a `TaskGraph`'s edges, raising `TaskGraphValidationError` on failure.

    Checks performed:
        * self-dependencies (a task listing its own `task_id` as a dependency)
        * missing dependencies (a `depends_on_task_ids` entry with no
          matching task in the graph)
        * circular dependency chains
    """

    errors: list[str] = []

    for task_id, task in graph.tasks.items():
        for dep_id in task.depends_on_task_ids:
            if dep_id == task_id:
                errors.append(f"Task '{task_id}' cannot depend on itself.")
            elif dep_id not in graph.tasks:
                errors.append(
                    f"Task '{task_id}' depends on missing task '{dep_id}'."
                )

    # Cycle detection via three-colour DFS (WHITE=unvisited, GRAY=in
    # progress, BLACK=finished), same approach as app.agents.registry.
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(graph.tasks, WHITE)
    reported_cycles: set[frozenset[str]] = set()

    def visit(task_id: str, path: list[str]) -> None:
        colour[task_id] = GRAY
        task = graph.tasks.get(task_id)
        dep_ids = task.depends_on_task_ids if task else []
        for dep_id in dep_ids:
            if dep_id not in graph.tasks:
                continue  # already reported as a missing dependency above
            if colour.get(dep_id) == GRAY:
                cycle_nodes = path[path.index(dep_id) :] + [dep_id]
                key = frozenset(cycle_nodes)
                if key not in reported_cycles:
                    reported_cycles.add(key)
                    errors.append(
                        f"Circular task dependency detected: {' -> '.join(cycle_nodes)}."
                    )
            elif colour.get(dep_id) == WHITE:
                visit(dep_id, path + [dep_id])
        colour[task_id] = BLACK

    for task_id in list(graph.tasks.keys()):
        if colour.get(task_id) == WHITE:
            visit(task_id, [task_id])

    if errors:
        raise TaskGraphValidationError(errors)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def topological_sort(graph: TaskGraph) -> list[str]:
    """Return a valid topological ordering of task IDs (Kahn's algorithm).

    Raises:
        TaskGraphValidationError: if the graph has a cycle or a task
            references a missing dependency.
    """

    validate_graph(graph)

    in_degree: dict[str, int] = {tid: 0 for tid in graph.tasks}
    for task in graph.tasks.values():
        for _dep_id in task.depends_on_task_ids:
            in_degree[task.task_id] += 1

    # Stable, deterministic ordering: process the current "ready" frontier
    # sorted by (-priority, task_id) each round.
    ready = deque(
        sorted(
            (tid for tid, deg in in_degree.items() if deg == 0),
            key=lambda tid: (-graph.tasks[tid].priority, tid),
        )
    )
    order: list[str] = []

    while ready:
        task_id = ready.popleft()
        order.append(task_id)
        newly_ready = []
        for dependent_id in sorted(graph.dependents.get(task_id, ())):
            in_degree[dependent_id] -= 1
            if in_degree[dependent_id] == 0:
                newly_ready.append(dependent_id)
        for tid in sorted(newly_ready, key=lambda t: (-graph.tasks[t].priority, t)):
            ready.append(tid)

    if len(order) != len(graph.tasks):
        # Shouldn't happen since validate_graph() already rejected cycles,
        # but guard defensively rather than silently returning a partial order.
        remaining = set(graph.tasks) - set(order)
        raise TaskGraphValidationError(
            [f"Could not topologically sort task(s): {sorted(remaining)}."]
        )

    return order


def get_parallel_groups(graph: TaskGraph) -> list[list[str]]:
    """Group task IDs into ordered "levels" that can each execute concurrently.

    Level 0 contains every task with no dependencies; level N contains every
    task whose dependencies all finish by level N-1. This is exactly what
    lets e.g. database schema, backend service, and frontend UI tasks (which
    don't depend on each other) run in parallel while still respecting
    cross-cutting dependencies.

    Raises:
        TaskGraphValidationError: if the graph has a cycle or a task
            references a missing dependency.
    """

    validate_graph(graph)

    in_degree: dict[str, int] = {tid: 0 for tid in graph.tasks}
    for task in graph.tasks.values():
        for _dep_id in task.depends_on_task_ids:
            in_degree[task.task_id] += 1

    remaining = dict(in_degree)
    groups: list[list[str]] = []
    current = sorted(tid for tid, deg in remaining.items() if deg == 0)

    while current:
        groups.append(current)
        next_level: set[str] = set()
        for task_id in current:
            for dependent_id in graph.dependents.get(task_id, ()):
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    next_level.add(dependent_id)
        current = sorted(next_level)

    return groups


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_dependencies(graph: TaskGraph, task_id: str) -> list[str]:
    """Return the direct (one-hop) dependency IDs of `task_id`."""

    return list(graph.get_task(task_id).depends_on_task_ids)


def get_dependents(graph: TaskGraph, task_id: str) -> list[str]:
    """Return the direct (one-hop) dependent IDs of `task_id`."""

    if task_id not in graph.tasks:
        raise TaskGraphError(f"Task id '{task_id}' is not in the graph.")
    return sorted(graph.dependents.get(task_id, ()))


def get_ready_tasks(
    graph: TaskGraph, completed_task_ids: Iterable[str] | None = None
) -> list[Task]:
    """Return tasks that are unblocked: not completed/cancelled, and every
    dependency is either completed or present in `completed_task_ids`.

    Highest `priority` first, then earliest `created_at`, matching
    `app.state.operations.get_ready_tasks`'s ordering.
    """

    completed = set(completed_task_ids or ())
    for task in graph.tasks.values():
        if task.status == TaskStatus.COMPLETED:
            completed.add(task.task_id)

    ready: list[Task] = []
    for task in graph.tasks.values():
        if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            continue
        if task.task_id in completed:
            continue
        if all(dep_id in completed for dep_id in task.depends_on_task_ids):
            ready.append(task)

    return sorted(ready, key=lambda t: (-t.priority, t.created_at))
