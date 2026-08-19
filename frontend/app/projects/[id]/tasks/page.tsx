"use client";

import { useState } from "react";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";
import { TaskDagGraph } from "@/components/TaskDagGraph";
import { StatusBadge } from "@/components/StatusBadge";
import { LiveIndicator } from "@/components/LiveIndicator";
import { LiveActivityFeed } from "@/components/LiveActivityFeed";
import { taskStatusStyle, relativeTime } from "@/lib/format";
import { useProjectEvents } from "@/lib/useProjectEvents";
import type { TaskStatus } from "@/lib/types";
import clsx from "clsx";

const FILTERS: { value: TaskStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "blocked", label: "Blocked" },
  { value: "failed", label: "Failed" },
  { value: "completed", label: "Completed" },
];

//: Realtime event types worth an immediate task-list refetch. Agent-level
//: events (agent_started/agent_tool_call) don't change a Task's own
//: status field, so they're left to the normal poll interval.
const TASK_AFFECTING_EVENTS = new Set(["task_started", "task_completed", "task_failed"]);

export default function TasksPage({ params }: { params: { id: string } }) {
  const projectId = params.id;
  const [filter, setFilter] = useState<TaskStatus | "all">("all");
  const [view, setView] = useState<"graph" | "table">("graph");

  const { data: tasks, error, isLoading, mutate } = useSWR(
    ["tasks", projectId],
    () => api.getTasks(projectId),
    { refreshInterval: 6000 },
  );

  const { connectionState, events } = useProjectEvents(projectId, (event) => {
    if (TASK_AFFECTING_EVENTS.has(event.event_type)) mutate();
  });

  const filtered = tasks?.filter((t) => filter === "all" || t.status === filter) ?? [];

  return (
    <div>
      <PageHeader
        eyebrow="Task DAG"
        title="Tasks"
        description="Dependency-aware task graph. Amber, animated edges show work currently in flight."
        actions={
          <div className="flex items-center gap-3">
            <LiveIndicator state={connectionState} />
            <div className="flex overflow-hidden rounded-md border border-base-hairline">
              <ViewToggle active={view === "graph"} onClick={() => setView("graph")}>
                Graph
              </ViewToggle>
              <ViewToggle active={view === "table"} onClick={() => setView("table")}>
                Table
              </ViewToggle>
            </div>
          </div>
        }
      />

      {isLoading && <LoadingState label="Loading task DAG…" />}

      {error && !isLoading && (
        <ErrorState detail={error instanceof ApiError ? error.message : undefined} onRetry={() => mutate()} />
      )}

      {!isLoading && !error && tasks && tasks.length === 0 && (
        <EmptyState
          title="No tasks yet"
          detail="Tasks appear once the CTO agent finishes planning the project into a DAG."
        />
      )}

      {!isLoading && !error && tasks && tasks.length > 0 && (
        <>
          <div className="mb-4 flex flex-wrap gap-2">
            {FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => setFilter(f.value)}
                className={clsx(
                  "rounded-full border px-3 py-1 text-xs font-medium transition",
                  filter === f.value
                    ? "border-signal-teal/40 bg-signal-teal/10 text-signal-teal"
                    : "border-base-hairline text-ink-muted hover:text-ink",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>

          {view === "graph" ? (
            <TaskDagGraph tasks={filtered} />
          ) : (
            <div className="overflow-hidden rounded-lg border border-base-hairline">
              <table className="w-full text-left text-sm">
                <thead className="bg-base-panel text-[11px] uppercase tracking-wide text-ink-faint">
                  <tr>
                    <th className="px-4 py-2.5 font-medium">Task</th>
                    <th className="px-4 py-2.5 font-medium">Status</th>
                    <th className="px-4 py-2.5 font-medium">Owner</th>
                    <th className="px-4 py-2.5 font-medium">Retries</th>
                    <th className="px-4 py-2.5 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-base-hairline">
                  {filtered.map((task) => {
                    const s = taskStatusStyle(task.status);
                    return (
                      <tr key={task.task_id} className="bg-base-raised/40">
                        <td className="px-4 py-2.5">
                          <p className="font-medium text-ink">{task.title}</p>
                          <p className="line-clamp-1 text-xs text-ink-faint">{task.description}</p>
                        </td>
                        <td className="px-4 py-2.5">
                          <StatusBadge label={s.label} tone={s.tone} />
                        </td>
                        <td className="px-4 py-2.5 text-ink-muted">{task.owner_manager ?? "—"}</td>
                        <td className="px-4 py-2.5 font-mono text-xs text-ink-muted">
                          {task.retries}/{task.max_retries}
                        </td>
                        <td className="px-4 py-2.5 text-xs text-ink-muted">
                          {relativeTime(task.updated_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-6">
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              Live activity
            </h3>
            <LiveActivityFeed events={events} />
          </div>
        </>
      )}
    </div>
  );
}

function ViewToggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "px-3 py-1.5 text-xs font-medium transition",
        active ? "bg-signal-teal/10 text-signal-teal" : "text-ink-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}
