"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { LiveIndicator } from "@/components/LiveIndicator";
import { eventLevelStyle } from "@/lib/format";
import { useProjectEvents } from "@/lib/useProjectEvents";
import type { EventLevel } from "@/lib/types";
import clsx from "clsx";

const LEVELS: (EventLevel | "all")[] = ["all", "info", "warning", "error", "critical", "debug"];

export default function LogsPage({ params }: { params: { id: string } }) {
  const projectId = params.id;
  const [level, setLevel] = useState<EventLevel | "all">("all");
  const [scope, setScope] = useState<"all" | "agent" | "project">("all");

  const { data: events, error, isLoading, mutate } = useSWR(
    ["events", projectId],
    () => api.getEvents(projectId),
    { refreshInterval: 5000 },
  );

  // Phase 19: the websocket carries finer-grained events (agent_started,
  // agent_tool_call, ...) than the REST `EventOut` list does, so rather
  // than try to merge two different shapes, every socket message just
  // triggers an immediate revalidation of the REST log -- the log stays
  // one consistent shape, but updates as fast as the socket does.
  const { connectionState } = useProjectEvents(projectId, () => mutate());

  const filtered = useMemo(() => {
    return (events ?? [])
      .filter((e) => level === "all" || e.level === level)
      .filter((e) => scope === "all" || e.scope === scope)
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  }, [events, level, scope]);

  const failureCount = (events ?? []).filter((e) => e.level === "error" || e.level === "critical").length;

  return (
    <div>
      <PageHeader
        eyebrow="Observability"
        title="Logs"
        description="Agent and project events, most recent first. Failures and self-healing repair attempts surface here as error/warning events."
        actions={
          <div className="flex items-center gap-3">
            <LiveIndicator state={connectionState} />
            {failureCount > 0 && (
              <div className="rounded-md border border-signal-rose/30 bg-signal-rose/[0.08] px-3 py-1.5 text-xs text-signal-rose">
                {failureCount} failure{failureCount === 1 ? "" : "s"} recorded
              </div>
            )}
          </div>
        }
      />

      {isLoading && <LoadingState label="Loading event stream…" />}

      {error && !isLoading && (
        <ErrorState detail={error instanceof ApiError ? error.message : undefined} onRetry={() => mutate()} />
      )}

      {!isLoading && !error && events && events.length === 0 && (
        <EmptyState title="No events yet" detail="Agent and project events will stream in here as the run progresses." />
      )}

      {!isLoading && !error && events && events.length > 0 && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-4">
            <div className="flex flex-wrap gap-2">
              {LEVELS.map((l) => (
                <Pill key={l} active={level === l} onClick={() => setLevel(l)}>
                  {l === "all" ? "All levels" : eventLevelStyle(l).label}
                </Pill>
              ))}
            </div>
            <div className="h-4 w-px bg-base-hairline" />
            <div className="flex gap-2">
              {(["all", "project", "agent"] as const).map((s) => (
                <Pill key={s} active={scope === s} onClick={() => setScope(s)}>
                  {s === "all" ? "All scopes" : s === "project" ? "Project" : "Agent"}
                </Pill>
              ))}
            </div>
          </div>

          <div className="max-h-[640px] overflow-y-auto rounded-lg border border-base-hairline bg-base-raised/40">
            <div className="divide-y divide-base-hairline">
              {filtered.map((event) => {
                const s = eventLevelStyle(event.level);
                return (
                  <div key={event.event_id} className="flex items-start gap-3 px-4 py-2.5 font-mono text-xs">
                    <span className="shrink-0 text-ink-faint">
                      {new Date(event.created_at).toLocaleTimeString()}
                    </span>
                    <StatusBadge label={s.label} tone={s.tone} />
                    {event.agent_id && (
                      <span className="shrink-0 rounded bg-white/[0.04] px-1.5 py-0.5 text-ink-faint">
                        {event.agent_id}
                      </span>
                    )}
                    <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-ink-muted">
                      {event.message}
                    </span>
                  </div>
                );
              })}
              {filtered.length === 0 && (
                <p className="px-4 py-6 text-center text-xs text-ink-faint">No events match these filters.</p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Pill({
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
        "rounded-full border px-3 py-1 text-xs font-medium transition",
        active
          ? "border-signal-teal/40 bg-signal-teal/10 text-signal-teal"
          : "border-base-hairline text-ink-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}
