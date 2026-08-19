"use client";

import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState } from "@/components/States";
import { RunControls } from "@/components/RunControls";
import { StatusBadge } from "@/components/StatusBadge";
import { LiveIndicator } from "@/components/LiveIndicator";
import { projectStatusStyle, taskStatusStyle, relativeTime } from "@/lib/format";
import { useProjectEvents } from "@/lib/useProjectEvents";

export default function ProjectOverviewPage({ params }: { params: { id: string } }) {
  const projectId = params.id;

  const {
    data: project,
    error: projectError,
    isLoading: projectLoading,
    mutate: mutateProject,
  } = useSWR(["project", projectId], () => api.getProject(projectId), { refreshInterval: 6000 });

  const {
    data: status,
    error: statusError,
    isLoading: statusLoading,
    mutate: mutateStatus,
  } = useSWR(["status", projectId], () => api.getStatus(projectId), { refreshInterval: 6000 });

  // Phase 19: the socket is a fast-path invalidation signal, not a
  // second source of truth -- every event just triggers an immediate
  // revalidation of the REST data above, instead of trying to
  // reconstruct project/task state from the event payload alone.
  const { connectionState } = useProjectEvents(projectId, () => {
    mutateProject();
    mutateStatus();
  });

  if (projectLoading || statusLoading) return <LoadingState label="Loading project…" />;

  if (projectError || !project) {
    return (
      <ErrorState
        title="Couldn't load this project"
        detail={projectError instanceof ApiError ? projectError.message : undefined}
        onRetry={() => mutateProject()}
      />
    );
  }

  const style = projectStatusStyle(project.status);

  return (
    <div className="max-w-4xl">
      <PageHeader
        eyebrow="Project"
        title={project.name}
        description={project.description || undefined}
        actions={
          <div className="flex items-center gap-3">
            <LiveIndicator state={connectionState} />
            <RunControls
              projectId={projectId}
              status={project.status}
              onChanged={() => {
                mutateProject();
                mutateStatus();
              }}
            />
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <InfoCard label="Status">
          <StatusBadge label={style.label} tone={style.tone} pulse={project.status === "running"} />
        </InfoCard>
        <InfoCard label="Tasks">
          <span className="text-lg font-semibold text-ink">{project.task_count}</span>
        </InfoCard>
        <InfoCard label="Last updated">
          <span className="text-sm text-ink">{relativeTime(project.updated_at)}</span>
        </InfoCard>
      </div>

      {project.error && (
        <div className="mt-4 rounded-lg border border-signal-rose/30 bg-signal-rose/[0.06] px-4 py-3">
          <p className="text-xs font-medium text-signal-rose">Run error</p>
          <p className="mt-1 font-mono text-xs text-ink-muted">{project.error}</p>
        </div>
      )}

      {statusError ? (
        <ErrorState
          title="Couldn't load task breakdown"
          detail={statusError instanceof ApiError ? statusError.message : undefined}
          onRetry={() => mutateStatus()}
        />
      ) : (
        status && (
          <div className="mt-6">
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              Task breakdown
            </h3>
            <div className="flex flex-wrap gap-2">
              {Object.entries(status.task_counts).length === 0 && (
                <p className="text-xs text-ink-muted">No tasks planned yet.</p>
              )}
              {Object.entries(status.task_counts).map(([key, count]) => {
                const s = taskStatusStyle(key);
                return (
                  <div
                    key={key}
                    className="flex items-center gap-2 rounded-md border border-base-hairline bg-base-raised px-3 py-1.5"
                  >
                    <StatusBadge label={s.label} tone={s.tone} />
                    <span className="font-mono text-sm text-ink">{count}</span>
                  </div>
                );
              })}
            </div>
          </div>
        )
      )}

      <div className="mt-8 rounded-lg border border-base-hairline bg-base-raised p-4">
        <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
          Idea prompt
        </h3>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
          {project.idea_prompt}
        </p>
        {project.repo_url && (
          <p className="mt-3 font-mono text-xs text-ink-faint">
            repo: <a className="text-signal-teal hover:underline" href={project.repo_url} target="_blank" rel="noreferrer">{project.repo_url}</a>
          </p>
        )}
      </div>
    </div>
  );
}

function InfoCard({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-base-hairline bg-base-raised p-4">
      <p className="mb-1.5 font-mono text-[11px] uppercase tracking-wide text-ink-faint">{label}</p>
      {children}
    </div>
  );
}
