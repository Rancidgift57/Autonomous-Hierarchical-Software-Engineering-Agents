"use client";

import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { testOutcomeStyle, relativeTime } from "@/lib/format";

export default function QaPage({ params }: { params: { id: string } }) {
  const projectId = params.id;

  const { data: reports, error, isLoading, mutate } = useSWR(
    ["qa", projectId],
    () => api.getQaReports(projectId),
    { refreshInterval: 8000 },
  );

  return (
    <div>
      <PageHeader
        eyebrow="Quality assurance"
        title="QA"
        description="Aggregated test, lint, and type-check results from the QA system agent."
      />

      {isLoading && <LoadingState label="Loading QA reports…" />}

      {error && !isLoading && (
        <ErrorState detail={error instanceof ApiError ? error.message : undefined} onRetry={() => mutate()} />
      )}

      {!isLoading && !error && reports && reports.length === 0 && (
        <EmptyState title="No QA reports yet" detail="Reports are generated once tasks reach integration and QA." />
      )}

      {!isLoading && !error && reports && reports.length > 0 && (
        <div className="flex flex-col gap-6">
          {[...reports]
            .sort((a, b) => new Date(b.generated_at).getTime() - new Date(a.generated_at).getTime())
            .map((report) => (
              <div key={report.report_id} className="rounded-lg border border-base-hairline bg-base-raised p-5">
                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="font-mono text-[11px] text-ink-faint">{report.report_id}</p>
                    <p className="text-xs text-ink-muted">{relativeTime(report.generated_at)}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <CheckBadge label="Lint" passed={report.lint_passed} />
                    <CheckBadge label="Type check" passed={report.type_check_passed} />
                    {report.coverage_percent !== null && (
                      <span className="rounded-full border border-base-hairline px-2.5 py-0.5 font-mono text-[11px] text-ink-muted">
                        {report.coverage_percent.toFixed(1)}% coverage
                      </span>
                    )}
                  </div>
                </div>

                {report.summary && <p className="mb-4 text-sm text-ink-muted">{report.summary}</p>}

                <div className="mb-3 flex items-center gap-4 font-mono text-xs text-ink-faint">
                  <span className="text-signal-teal">{countOutcome(report.test_results, "passed")} passed</span>
                  <span className="text-signal-rose">{countOutcome(report.test_results, "failed")} failed</span>
                  <span>{report.test_results.length} total</span>
                </div>

                {report.test_results.length > 0 ? (
                  <div className="overflow-hidden rounded-md border border-base-hairline">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-base-panel text-[11px] uppercase tracking-wide text-ink-faint">
                        <tr>
                          <th className="px-3 py-2 font-medium">Test</th>
                          <th className="px-3 py-2 font-medium">Outcome</th>
                          <th className="px-3 py-2 font-medium">Duration</th>
                          <th className="px-3 py-2 font-medium">Message</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-base-hairline">
                        {report.test_results.map((t) => {
                          const s = testOutcomeStyle(t.outcome);
                          return (
                            <tr key={t.test_id} className="bg-base-raised/60">
                              <td className="px-3 py-2 text-ink">{t.name}</td>
                              <td className="px-3 py-2">
                                <StatusBadge label={s.label} tone={s.tone} />
                              </td>
                              <td className="px-3 py-2 font-mono text-xs text-ink-muted">
                                {t.duration_seconds != null ? `${t.duration_seconds.toFixed(2)}s` : "—"}
                              </td>
                              <td className="px-3 py-2 text-xs text-ink-muted">{t.message ?? "—"}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-xs text-ink-faint">No individual test results recorded.</p>
                )}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

function countOutcome(results: { outcome: string }[], outcome: string) {
  return results.filter((r) => r.outcome === outcome).length;
}

function CheckBadge({ label, passed }: { label: string; passed: boolean | null }) {
  if (passed === null) {
    return (
      <span className="rounded-full border border-base-hairline px-2.5 py-0.5 font-mono text-[11px] text-ink-faint">
        {label} — n/a
      </span>
    );
  }
  return <StatusBadge label={`${label} ${passed ? "passed" : "failed"}`} tone={passed ? "teal" : "rose"} />;
}
