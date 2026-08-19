"use client";

import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { LoadingState, ErrorState, EmptyState } from "@/components/States";
import { AgentGraph } from "@/components/AgentGraph";
import { StatusBadge } from "@/components/StatusBadge";
import { agentTypeStyle } from "@/lib/format";

export default function AgentsPage({ params }: { params: { id: string } }) {
  const projectId = params.id;

  const { data: agents, error: agentsError, isLoading: agentsLoading, mutate } = useSWR(
    ["agents", projectId],
    () => api.getAgents(projectId),
    { refreshInterval: 6000 },
  );
  const { data: tasks } = useSWR(["tasks", projectId], () => api.getTasks(projectId), {
    refreshInterval: 6000,
  });

  return (
    <div>
      <PageHeader
        eyebrow="Agent hierarchy"
        title="Agents"
        description="CTO plans, managers coordinate teams, workers execute tasks. Node borders show current activity."
      />

      {agentsLoading && <LoadingState label="Loading agent hierarchy…" />}

      {agentsError && !agentsLoading && (
        <ErrorState
          detail={agentsError instanceof ApiError ? agentsError.message : undefined}
          onRetry={() => mutate()}
        />
      )}

      {!agentsLoading && !agentsError && agents && agents.length === 0 && (
        <EmptyState
          title="No agents yet"
          detail="Agents are instantiated once the project run starts and the CTO produces a plan."
        />
      )}

      {!agentsLoading && !agentsError && agents && agents.length > 0 && (
        <>
          <AgentGraph agents={agents} tasks={tasks ?? []} />

          <div className="mt-8 overflow-hidden rounded-lg border border-base-hairline">
            <table className="w-full text-left text-sm">
              <thead className="bg-base-panel text-[11px] uppercase tracking-wide text-ink-faint">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Name</th>
                  <th className="px-4 py-2.5 font-medium">Type</th>
                  <th className="px-4 py-2.5 font-medium">Team</th>
                  <th className="px-4 py-2.5 font-medium">Capabilities</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-base-hairline">
                {agents.map((agent) => {
                  const s = agentTypeStyle(agent.agent_type);
                  return (
                    <tr key={agent.agent_id} className="bg-base-raised/40">
                      <td className="px-4 py-2.5">
                        <p className="font-medium text-ink">{agent.name}</p>
                        <p className="font-mono text-[11px] text-ink-faint">{agent.agent_id}</p>
                      </td>
                      <td className="px-4 py-2.5">
                        <StatusBadge label={s.label} tone={s.tone} />
                      </td>
                      <td className="px-4 py-2.5 text-ink-muted">
                        {agent.team_name ?? agent.system_kind ?? "—"}
                      </td>
                      <td className="px-4 py-2.5 text-ink-muted">
                        {agent.capabilities.length > 0 ? agent.capabilities.join(", ") : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
