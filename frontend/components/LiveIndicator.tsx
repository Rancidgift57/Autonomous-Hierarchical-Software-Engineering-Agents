import { StatusBadge } from "@/components/StatusBadge";
import type { ConnectionState } from "@/lib/useProjectEvents";

const STYLE: Record<ConnectionState, { label: string; tone: "teal" | "amber" | "slate" }> = {
  open: { label: "Live", tone: "teal" },
  connecting: { label: "Connecting…", tone: "amber" },
  reconnecting: { label: "Reconnecting…", tone: "amber" },
  closed: { label: "Offline", tone: "slate" },
};

export function LiveIndicator({ state }: { state: ConnectionState }) {
  const s = STYLE[state];
  return <StatusBadge label={s.label} tone={s.tone} pulse={state === "open"} />;
}
