// Mirrors backend/app/state/enums.py and backend/app/api/schemas.py.
// Keep in sync with the FastAPI control plane — these are structural
// types only, not re-validated client-side.

export type TaskStatus =
  | "pending"
  | "ready"
  | "running"
  | "blocked"
  | "completed"
  | "failed"
  | "retrying"
  | "cancelled";

export type AgentStatus =
  | "idle"
  | "planning"
  | "working"
  | "waiting"
  | "blocked"
  | "completed"
  | "failed";

export type AgentType = "cto" | "manager" | "worker" | "system_agent";

export type SystemAgentKind =
  | "integration"
  | "qa"
  | "error_analyzer"
  | "deployment"
  | "self_healing";

export type ArtifactType =
  | "source_file"
  | "test_file"
  | "config_file"
  | "documentation"
  | "diagram"
  | "report"
  | "other";

export type DeploymentStage =
  | "not_started"
  | "preparing"
  | "building"
  | "awaiting_approval"
  | "deploying"
  | "verifying"
  | "deployed"
  | "failed"
  | "rolled_back";

export type EventLevel = "debug" | "info" | "warning" | "error" | "critical";

export type TestOutcome = "passed" | "failed" | "error" | "skipped";

export type TaskComplexity = "low" | "medium" | "high";

export type ProjectRunStatus =
  | "pending"
  | "planning"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface ProjectSummary {
  project_id: string;
  name: string;
  description: string;
  status: ProjectRunStatus;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends ProjectSummary {
  idea_prompt: string;
  repo_url: string | null;
  task_count: number;
  error: string | null;
}

export interface ProjectCreateRequest {
  name: string;
  description?: string;
  idea_prompt: string;
  repo_url?: string | null;
}

export interface RunControlResponse {
  project_id: string;
  status: ProjectRunStatus;
  message: string;
}

export interface ProjectStatusResponse {
  project_id: string;
  status: ProjectRunStatus;
  error: string | null;
  task_counts: Record<string, number>;
  updated_at: string;
}

export interface AgentDefinition {
  agent_id: string;
  name: string;
  agent_type: AgentType;
  system_kind: SystemAgentKind | null;
  parent_agent_id: string | null;
  team_name: string | null;
  role_description: string;
  capabilities: string[];
  allowed_tools: string[];
  created_at: string;
}

export interface TaskResult {
  task_id: string;
  success: boolean;
  summary: string | null;
  artifact_ids: string[];
  logs: string | null;
  error_message: string | null;
  duration_seconds: number | null;
  completed_at: string;
}

export interface Task {
  task_id: string;
  title: string;
  description: string;
  status: TaskStatus;
  assigned_agent_id: string | null;
  owner_manager: string | null;
  worker_type: string | null;
  module_id: string | null;
  requirement_ids: string[];
  depends_on_task_ids: string[];
  expected_outputs: string[];
  priority: number;
  complexity: TaskComplexity;
  retries: number;
  max_retries: number;
  result: TaskResult | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface Artifact {
  artifact_id: string;
  artifact_type: ArtifactType;
  path: string;
  description: string | null;
  produced_by_agent_id: string | null;
  produced_by_task_id: string | null;
  content_hash: string | null;
  created_at: string;
}

export interface EventOut {
  event_id: string;
  scope: "agent" | "project";
  agent_id: string | null;
  level: EventLevel;
  message: string;
  task_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export interface TestResult {
  test_id: string;
  name: string;
  outcome: TestOutcome;
  module_id: string | null;
  task_id: string | null;
  duration_seconds: number | null;
  message: string | null;
  ran_at: string;
}

export interface QAReport {
  report_id: string;
  test_results: TestResult[];
  lint_passed: boolean | null;
  type_check_passed: boolean | null;
  coverage_percent: number | null;
  summary: string | null;
  generated_at: string;
}

export interface DeploymentState {
  stage: DeploymentStage;
  environment: string;
  approved_by: string | null;
  approved_at: string | null;
  last_deployed_at: string | null;
  deployment_log: string[];
  verification_passed: boolean | null;
  rollback_reason: string | null;
}

export interface DeploymentApprovalRequest {
  approved_by: string;
  notes?: string;
}

export interface ApiErrorBody {
  detail: string;
}

// ---------------------------------------------------------------------------
// Phase 19: real-time events (mirrors backend/app/realtime/schemas.py)
// ---------------------------------------------------------------------------

export type RealtimeEventType =
  | "project_started"
  | "agent_started"
  | "agent_completed"
  | "agent_tool_call"
  | "task_started"
  | "task_completed"
  | "task_failed"
  | "integration_failed"
  | "qa_started"
  | "qa_failed"
  | "repair_started"
  | "repair_completed"
  | "deployment_started"
  | "deployment_completed";

export interface RealtimeEvent {
  event_id: string;
  project_id: string;
  timestamp: string;
  event_type: RealtimeEventType;
  agent_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
}
