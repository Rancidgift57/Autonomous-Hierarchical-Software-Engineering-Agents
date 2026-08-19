"use client";

import { useState } from "react";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";
import { relativeTime } from "@/lib/format";
import type { ArtifactType } from "@/lib/types";
import clsx from "clsx";

const TYPE_LABELS: Record<ArtifactType, string> = {
  source_file: "Source",
  test_file: "Test",
  config_file: "Config",
  documentation: "Docs",
  diagram: "Diagram",
  report: "Report",
  other: "Other",
};

export default function ArtifactsPage({ params }: { params: { id: string } }) {
  const projectId = params.id;
  const [typeFilter, setTypeFilter] = useState<ArtifactType | "all">("all");

  const { data: artifacts, error, isLoading, mutate } = useSWR(
    ["artifacts", projectId],
    () => api.getArtifacts(projectId),
    { refreshInterval: 8000 },
  );

  const types = Array.from(new Set((artifacts ?? []).map((a) => a.artifact_type)));
  const filtered = (artifacts ?? []).filter((a) => typeFilter === "all" || a.artifact_type === typeFilter);

  return (
    <div>
      <PageHeader
        eyebrow="Build output"
        title="Artifacts"
        description="Files and outputs produced by agents. Contents live in the workspace/repo — only metadata is shown here."
      />

      {isLoading && <LoadingState label="Loading artifacts…" />}

      {error && !isLoading && (
        <ErrorState detail={error instanceof ApiError ? error.message : undefined} onRetry={() => mutate()} />
      )}

      {!isLoading && !error && artifacts && artifacts.length === 0 && (
        <EmptyState title="No artifacts yet" detail="Workers publish artifacts here as tasks complete." />
      )}

      {!isLoading && !error && artifacts && artifacts.length > 0 && (
        <>
          <div className="mb-4 flex flex-wrap gap-2">
            <FilterPill active={typeFilter === "all"} onClick={() => setTypeFilter("all")}>
              All ({artifacts.length})
            </FilterPill>
            {types.map((t) => (
              <FilterPill key={t} active={typeFilter === t} onClick={() => setTypeFilter(t)}>
                {TYPE_LABELS[t]} ({artifacts.filter((a) => a.artifact_type === t).length})
              </FilterPill>
            ))}
          </div>

          <div className="overflow-hidden rounded-lg border border-base-hairline">
            <table className="w-full text-left text-sm">
              <thead className="bg-base-panel text-[11px] uppercase tracking-wide text-ink-faint">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Path</th>
                  <th className="px-4 py-2.5 font-medium">Type</th>
                  <th className="px-4 py-2.5 font-medium">Description</th>
                  <th className="px-4 py-2.5 font-medium">Produced</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-base-hairline">
                {filtered.map((artifact) => (
                  <tr key={artifact.artifact_id} className="bg-base-raised/40">
                    <td className="px-4 py-2.5 font-mono text-xs text-ink">{artifact.path}</td>
                    <td className="px-4 py-2.5 text-ink-muted">{TYPE_LABELS[artifact.artifact_type]}</td>
                    <td className="px-4 py-2.5 text-ink-muted">{artifact.description ?? "—"}</td>
                    <td className="px-4 py-2.5 text-xs text-ink-faint">{relativeTime(artifact.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function FilterPill({
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
