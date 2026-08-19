"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { ProjectRunStatus } from "@/lib/types";

const RUNNABLE: ProjectRunStatus[] = ["pending"];
const PAUSABLE: ProjectRunStatus[] = ["running", "planning"];
const RESUMABLE: ProjectRunStatus[] = ["paused"];
const CANCELLABLE: ProjectRunStatus[] = ["pending", "planning", "running", "paused"];

export function RunControls({
  projectId,
  status,
  onChanged,
}: {
  projectId: string;
  status: ProjectRunStatus;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(action: "run" | "pause" | "resume" | "cancel", fn: () => Promise<unknown>) {
    setPending(action);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : `Failed to ${action} project.`);
    } finally {
      setPending(null);
    }
  }

  const btnClass =
    "rounded-md border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <button
          className={`${btnClass} border-signal-teal/40 bg-signal-teal/10 text-signal-teal hover:bg-signal-teal/20`}
          disabled={!RUNNABLE.includes(status) || pending !== null}
          onClick={() => act("run", () => api.runProject(projectId))}
        >
          {pending === "run" ? "Starting…" : "Run"}
        </button>
        <button
          className={`${btnClass} border-signal-amber/40 bg-signal-amber/10 text-signal-amber hover:bg-signal-amber/20`}
          disabled={!PAUSABLE.includes(status) || pending !== null}
          onClick={() => act("pause", () => api.pauseProject(projectId))}
        >
          {pending === "pause" ? "Pausing…" : "Pause"}
        </button>
        <button
          className={`${btnClass} border-signal-teal/40 bg-signal-teal/10 text-signal-teal hover:bg-signal-teal/20`}
          disabled={!RESUMABLE.includes(status) || pending !== null}
          onClick={() => act("resume", () => api.resumeProject(projectId))}
        >
          {pending === "resume" ? "Resuming…" : "Resume"}
        </button>
        <button
          className={`${btnClass} border-signal-rose/40 bg-signal-rose/10 text-signal-rose hover:bg-signal-rose/20`}
          disabled={!CANCELLABLE.includes(status) || pending !== null}
          onClick={() => act("cancel", () => api.cancelProject(projectId))}
        >
          {pending === "cancel" ? "Cancelling…" : "Cancel"}
        </button>
      </div>
      {error && <p className="text-[11px] text-signal-rose">{error}</p>}
    </div>
  );
}
