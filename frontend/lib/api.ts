import type {
  Artifact,
  AgentDefinition,
  DeploymentApprovalRequest,
  DeploymentState,
  EventOut,
  ProjectCreateRequest,
  ProjectDetail,
  ProjectStatusResponse,
  ProjectSummary,
  QAReport,
  RunControlResponse,
  Task,
} from "./types";

// Every call goes to our own Next.js route (/api/proxy/...), which forwards
// to the real AHSEA backend server-side. The browser never sees the
// backend's base URL or API key.
const PROXY_PREFIX = "/api/proxy";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PROXY_PREFIX}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // response wasn't JSON — fall back to statusText
    }
    throw new ApiError(res.status, detail || "Request failed");
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // Projects
  listProjects: () => request<ProjectSummary[]>("/api/projects"),
  getProject: (id: string) => request<ProjectDetail>(`/api/projects/${id}`),
  createProject: (body: ProjectCreateRequest) =>
    request<ProjectDetail>("/api/projects", { method: "POST", body: JSON.stringify(body) }),

  // Run control
  runProject: (id: string) =>
    request<RunControlResponse>(`/api/projects/${id}/run`, { method: "POST" }),
  pauseProject: (id: string) =>
    request<RunControlResponse>(`/api/projects/${id}/pause`, { method: "POST" }),
  resumeProject: (id: string) =>
    request<RunControlResponse>(`/api/projects/${id}/resume`, { method: "POST" }),
  cancelProject: (id: string) =>
    request<RunControlResponse>(`/api/projects/${id}/cancel`, { method: "POST" }),

  // Read-only views
  getStatus: (id: string) => request<ProjectStatusResponse>(`/api/projects/${id}/status`),
  getAgents: (id: string) => request<AgentDefinition[]>(`/api/projects/${id}/agents`),
  getTasks: (id: string) => request<Task[]>(`/api/projects/${id}/tasks`),
  getArtifacts: (id: string) => request<Artifact[]>(`/api/projects/${id}/artifacts`),
  getEvents: (id: string) => request<EventOut[]>(`/api/projects/${id}/events`),
  getQaReports: (id: string) => request<QAReport[]>(`/api/projects/${id}/qa`),
  getDeployment: (id: string) => request<DeploymentState>(`/api/projects/${id}/deployment`),

  // Deployment approval
  approveDeployment: (id: string, body: DeploymentApprovalRequest) =>
    request<DeploymentState>(`/api/projects/${id}/approve-deployment`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
