"use client";

import Link from "next/link";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { ProjectCard } from "@/components/ProjectCard";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";

export default function ProjectsPage() {
  const { data, error, isLoading, mutate } = useSWR(
    "projects",
    () => api.listProjects(),
    { refreshInterval: 8000 },
  );

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <PageHeader
        eyebrow="AHSEA Control Plane"
        title="Projects"
        description="Every project the agent hierarchy — CTO, managers, and workers — is building or has built."
        actions={
          <Link
            href="/projects/new"
            className="rounded-md bg-signal-teal px-4 py-2 text-sm font-medium text-base transition hover:bg-signal-teal/90"
          >
            New project
          </Link>
        }
      />

      {isLoading && <LoadingState label="Loading projects…" />}

      {error && !isLoading && (
        <ErrorState
          detail={error instanceof ApiError ? error.message : "Could not reach the control plane."}
          onRetry={() => mutate()}
        />
      )}

      {!isLoading && !error && data && data.length === 0 && (
        <EmptyState
          title="No projects yet"
          detail="Kick off the agent hierarchy by describing a project idea."
          action={
            <Link
              href="/projects/new"
              className="rounded-md border border-signal-teal/40 bg-signal-teal/10 px-3 py-1.5 text-xs font-medium text-signal-teal transition hover:bg-signal-teal/20"
            >
              Create your first project
            </Link>
          }
        />
      )}

      {!isLoading && !error && data && data.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((p) => (
            <ProjectCard key={p.project_id} project={p} />
          ))}
        </div>
      )}
    </div>
  );
}
