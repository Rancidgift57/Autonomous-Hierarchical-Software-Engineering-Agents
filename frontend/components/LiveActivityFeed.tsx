import { StatusBadge } from "@/components/StatusBadge";
import { realtimeEventTypeStyle } from "@/lib/format";
import type { RealtimeEvent } from "@/lib/types";

/**
 * A raw, low-level feed of `/ws/projects/{project_id}` events -- the same
 * agent_started/agent_tool_call/task_* granularity the backend emits,
 * shown as-is rather than folded into the REST `EventOut` shape (see
 * `logs/page.tsx`, which stays on the REST `GET .../events` list as its
 * source of truth and just uses the socket to trigger faster revalidation).
 */
export function LiveActivityFeed({ events }: { events: RealtimeEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="rounded-lg border border-base-hairline bg-base-raised/40 px-4 py-6 text-center text-xs text-ink-faint">
        Waiting for live activity…
      </div>
    );
  }

  return (
    <div className="max-h-80 overflow-y-auto rounded-lg border border-base-hairline bg-base-raised/40">
      <div className="divide-y divide-base-hairline">
        {events.slice(0, 50).map((event) => {
          const s = realtimeEventTypeStyle(event.event_type);
          return (
            <div
              key={event.event_id}
              className="flex items-start gap-3 px-4 py-2 font-mono text-xs"
            >
              <span className="shrink-0 text-ink-faint">
                {new Date(event.timestamp).toLocaleTimeString()}
              </span>
              <StatusBadge label={s.label} tone={s.tone} />
              {event.agent_id && (
                <span className="shrink-0 rounded bg-white/[0.04] px-1.5 py-0.5 text-ink-faint">
                  {event.agent_id}
                </span>
              )}
              {event.task_id && (
                <span className="min-w-0 flex-1 truncate text-ink-muted">{event.task_id}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
