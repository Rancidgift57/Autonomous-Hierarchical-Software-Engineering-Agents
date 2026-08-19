"use client";

import { useState } from "react";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { deploymentStageStyle, relativeTime } from "@/lib/format";
import type { DeploymentStage } from "@/lib/types";

const PIPELINE: DeploymentStage[] = [
  "not_started",
  "preparing",
  "building",
  "awaiting_approval",
  "deploying",
  "verifying",
  "deployed",
];

export default function DeploymentPage({ params }: { params: { id: string } }) {
  const projectId = params.id;
  const { data: deployment, error, isLoading, mutate } = useSWR(
    ["deployment", projectId],
    () => api.getDeployment(projectId),
    { refreshInterval: 5000 },
  );

  const [approver, setApprover] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  async function handleApprove(e: React.FormEvent) {
    e.preventDefault();
    if (!approver.trim()) {
      setApproveError("Enter who is approving this deployment.");
      return;
    }
    setSubmitting(true);
    setApproveError(null);
    try {
      await api.approveDeployment(projectId, { approved_by: approver.trim(), notes: notes.trim() });
      setApprover("");
      setNotes("");
      mutate();
    } catch (err) {
      setApproveError(err instanceof ApiError ? err.message : "Failed to approve deployment.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        eyebrow="Ship it"
        title="Deployment"
        description="Pipeline stage, verification, and human approval gate before agents deploy to an environment."
      />

      {isLoading && <LoadingState label="Loading deployment state…" />}

      {error && !isLoading && (
        <ErrorState detail={error instanceof ApiError ? error.message : undefined} onRetry={() => mutate()} />
      )}

      {!isLoading && !error && deployment && (
        <>
          <Pipeline stage={deployment.stage} />

          <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <InfoCard label="Environment">
              <span className="font-mono text-sm text-ink">{deployment.environment}</span>
            </InfoCard>
            <InfoCard label="Verification">
              {deployment.verification_passed === null ? (
                <span className="text-sm text-ink-faint">Not run</span>
              ) : (
                <StatusBadge
                  label={deployment.verification_passed ? "Passed" : "Failed"}
                  tone={deployment.verification_passed ? "teal" : "rose"}
                />
              )}
            </InfoCard>
            <InfoCard label="Last deployed">
              <span className="text-sm text-ink">
                {deployment.last_deployed_at ? relativeTime(deployment.last_deployed_at) : "Never"}
              </span>
            </InfoCard>
          </div>

          {deployment.rollback_reason && (
            <div className="mt-4 rounded-lg border border-signal-rose/30 bg-signal-rose/[0.06] px-4 py-3">
              <p className="text-xs font-medium text-signal-rose">Rolled back</p>
              <p className="mt-1 text-xs text-ink-muted">{deployment.rollback_reason}</p>
            </div>
          )}

          {deployment.stage === "awaiting_approval" && (
            <form
              onSubmit={handleApprove}
              className="mt-6 rounded-lg border border-signal-amber/30 bg-signal-amber/[0.06] p-4"
            >
              <p className="mb-3 text-sm font-medium text-signal-amber">Awaiting your approval to deploy</p>
              <div className="flex flex-col gap-3 sm:flex-row">
                <input
                  className="input flex-1"
                  placeholder="Approved by (your name)"
                  value={approver}
                  onChange={(e) => setApprover(e.target.value)}
                  disabled={submitting}
                />
                <input
                  className="input flex-1"
                  placeholder="Notes (optional)"
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  disabled={submitting}
                />
                <button
                  type="submit"
                  disabled={submitting}
                  className="rounded-md bg-signal-amber px-4 py-2 text-sm font-medium text-base transition hover:bg-signal-amber/90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {submitting ? "Approving…" : "Approve deployment"}
                </button>
              </div>
              {approveError && <p className="mt-2 text-xs text-signal-rose">{approveError}</p>}
              <style jsx global>{`
                .input {
                  border-radius: 0.5rem;
                  border: 1px solid #242a33;
                  background-color: #12151b;
                  padding: 0.5rem 0.75rem;
                  font-size: 0.8125rem;
                  color: #e7eaee;
                  outline: none;
                }
                .input:focus {
                  border-color: rgba(232, 163, 61, 0.5);
                }
              `}</style>
            </form>
          )}

          {deployment.approved_by && (
            <p className="mt-4 font-mono text-[11px] text-ink-faint">
              approved by {deployment.approved_by}
              {deployment.approved_at ? ` · ${relativeTime(deployment.approved_at)}` : ""}
            </p>
          )}

          <div className="mt-8">
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">Deployment log</h3>
            <div className="max-h-80 overflow-y-auto rounded-lg border border-base-hairline bg-base-raised/40 p-3 font-mono text-xs text-ink-muted">
              {deployment.deployment_log.length === 0 ? (
                <p className="text-ink-faint">No log entries yet.</p>
              ) : (
                deployment.deployment_log.map((line, i) => (
                  <p key={i} className="whitespace-pre-wrap py-0.5">
                    {line}
                  </p>
                ))
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Pipeline({ stage }: { stage: DeploymentStage }) {
  if (stage === "failed" || stage === "rolled_back") {
    const s = deploymentStageStyle(stage);
    return (
      <div className="flex items-center gap-2">
        <StatusBadge label={s.label} tone={s.tone} />
      </div>
    );
  }

  const currentIndex = PIPELINE.indexOf(stage);
  return (
    <div className="flex items-center overflow-x-auto pb-2">
      {PIPELINE.map((step, i) => {
        const s = deploymentStageStyle(step);
        const done = i < currentIndex;
        const current = i === currentIndex;
        return (
          <div key={step} className="flex items-center">
            <div
              className={`flex items-center gap-2 whitespace-nowrap rounded-full border px-3 py-1.5 text-xs font-medium ${
                current
                  ? "border-signal-amber/40 bg-signal-amber/10 text-signal-amber"
                  : done
                    ? "border-signal-teal/40 bg-signal-teal/10 text-signal-teal"
                    : "border-base-hairline text-ink-faint"
              }`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${current ? "animate-pulseDot bg-signal-amber" : done ? "bg-signal-teal" : "bg-signal-slate"}`} />
              {s.label}
            </div>
            {i < PIPELINE.length - 1 && <div className="mx-1.5 h-px w-6 shrink-0 bg-base-hairline" />}
          </div>
        );
      })}
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
