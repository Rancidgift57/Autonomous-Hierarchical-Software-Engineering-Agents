import { formatDistanceToNow } from "date-fns";

export type Tone = "amber" | "teal" | "rose" | "violet" | "slate";

export interface ToneStyle {
  tone: Tone;
  label: string;
}

// Central place mapping every backend enum value to a display label and a
// color "tone". Keep this as the single source of truth for status colors
// so every page (agents, tasks, QA, deployment, logs) reads consistently.

const TASK_STATUS: Record<string, ToneStyle> = {
  pending: { tone: "slate", label: "Pending" },
  ready: { tone: "teal", label: "Ready" },
  running: { tone: "amber", label: "Running" },
  blocked: { tone: "rose", label: "Blocked" },
  completed: { tone: "teal", label: "Completed" },
  failed: { tone: "rose", label: "Failed" },
  retrying: { tone: "amber", label: "Retrying" },
  cancelled: { tone: "slate", label: "Cancelled" },
};

const AGENT_STATUS: Record<string, ToneStyle> = {
  idle: { tone: "slate", label: "Idle" },
  planning: { tone: "violet", label: "Planning" },
  working: { tone: "amber", label: "Working" },
  waiting: { tone: "slate", label: "Waiting" },
  blocked: { tone: "rose", label: "Blocked" },
  completed: { tone: "teal", label: "Completed" },
  failed: { tone: "rose", label: "Failed" },
};

const PROJECT_STATUS: Record<string, ToneStyle> = {
  pending: { tone: "slate", label: "Pending" },
  planning: { tone: "violet", label: "Planning" },
  running: { tone: "amber", label: "Running" },
  paused: { tone: "slate", label: "Paused" },
  completed: { tone: "teal", label: "Completed" },
  failed: { tone: "rose", label: "Failed" },
  cancelled: { tone: "slate", label: "Cancelled" },
};

const DEPLOYMENT_STAGE: Record<string, ToneStyle> = {
  not_started: { tone: "slate", label: "Not started" },
  preparing: { tone: "violet", label: "Preparing" },
  building: { tone: "amber", label: "Building" },
  awaiting_approval: { tone: "amber", label: "Awaiting approval" },
  deploying: { tone: "amber", label: "Deploying" },
  verifying: { tone: "violet", label: "Verifying" },
  deployed: { tone: "teal", label: "Deployed" },
  failed: { tone: "rose", label: "Failed" },
  rolled_back: { tone: "rose", label: "Rolled back" },
};

const EVENT_LEVEL: Record<string, ToneStyle> = {
  debug: { tone: "slate", label: "Debug" },
  info: { tone: "teal", label: "Info" },
  warning: { tone: "amber", label: "Warning" },
  error: { tone: "rose", label: "Error" },
  critical: { tone: "rose", label: "Critical" },
};

const TEST_OUTCOME: Record<string, ToneStyle> = {
  passed: { tone: "teal", label: "Passed" },
  failed: { tone: "rose", label: "Failed" },
  error: { tone: "rose", label: "Error" },
  skipped: { tone: "slate", label: "Skipped" },
};

const AGENT_TYPE: Record<string, ToneStyle> = {
  cto: { tone: "amber", label: "CTO" },
  manager: { tone: "violet", label: "Manager" },
  worker: { tone: "teal", label: "Worker" },
  system_agent: { tone: "slate", label: "System" },
};

// Phase 19: realtime event types (backend/app/realtime/schemas.py).
const REALTIME_EVENT_TYPE: Record<string, ToneStyle> = {
  project_started: { tone: "violet", label: "Project started" },
  agent_started: { tone: "amber", label: "Agent started" },
  agent_completed: { tone: "teal", label: "Agent completed" },
  agent_tool_call: { tone: "slate", label: "Tool call" },
  task_started: { tone: "amber", label: "Task started" },
  task_completed: { tone: "teal", label: "Task completed" },
  task_failed: { tone: "rose", label: "Task failed" },
  integration_failed: { tone: "rose", label: "Integration failed" },
  qa_started: { tone: "amber", label: "QA started" },
  qa_failed: { tone: "rose", label: "QA failed" },
  repair_started: { tone: "amber", label: "Repair started" },
  repair_completed: { tone: "teal", label: "Repair completed" },
  deployment_started: { tone: "amber", label: "Deployment started" },
  deployment_completed: { tone: "teal", label: "Deployment completed" },
};

export function taskStatusStyle(s: string): ToneStyle {
  return TASK_STATUS[s] ?? { tone: "slate", label: s };
}
export function agentStatusStyle(s: string): ToneStyle {
  return AGENT_STATUS[s] ?? { tone: "slate", label: s };
}
export function projectStatusStyle(s: string): ToneStyle {
  return PROJECT_STATUS[s] ?? { tone: "slate", label: s };
}
export function deploymentStageStyle(s: string): ToneStyle {
  return DEPLOYMENT_STAGE[s] ?? { tone: "slate", label: s };
}
export function eventLevelStyle(s: string): ToneStyle {
  return EVENT_LEVEL[s] ?? { tone: "slate", label: s };
}
export function testOutcomeStyle(s: string): ToneStyle {
  return TEST_OUTCOME[s] ?? { tone: "slate", label: s };
}
export function agentTypeStyle(s: string): ToneStyle {
  return AGENT_TYPE[s] ?? { tone: "slate", label: s };
}
export function realtimeEventTypeStyle(s: string): ToneStyle {
  return REALTIME_EVENT_TYPE[s] ?? { tone: "slate", label: s };
}

export function toneClasses(tone: Tone): { dot: string; text: string; bg: string; border: string } {
  switch (tone) {
    case "amber":
      return { dot: "bg-signal-amber", text: "text-signal-amber", bg: "bg-signal-amber/10", border: "border-signal-amber/30" };
    case "teal":
      return { dot: "bg-signal-teal", text: "text-signal-teal", bg: "bg-signal-teal/10", border: "border-signal-teal/30" };
    case "rose":
      return { dot: "bg-signal-rose", text: "text-signal-rose", bg: "bg-signal-rose/10", border: "border-signal-rose/30" };
    case "violet":
      return { dot: "bg-signal-violet", text: "text-signal-violet", bg: "bg-signal-violet/10", border: "border-signal-violet/30" };
    default:
      return { dot: "bg-signal-slate", text: "text-ink-muted", bg: "bg-white/[0.03]", border: "border-base-hairline" };
  }
}

const ACTIVE_TASK_STATUSES = new Set(["running", "retrying"]);
const ACTIVE_AGENT_STATUSES = new Set(["working", "planning"]);

export function isActiveTask(s: string): boolean {
  return ACTIVE_TASK_STATUSES.has(s);
}
export function isActiveAgent(s: string): boolean {
  return ACTIVE_AGENT_STATUSES.has(s);
}

export function relativeTime(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

export function shortId(id: string, keep = 8): string {
  if (id.length <= keep + 3) return id;
  return `${id.slice(0, keep)}…`;
}
