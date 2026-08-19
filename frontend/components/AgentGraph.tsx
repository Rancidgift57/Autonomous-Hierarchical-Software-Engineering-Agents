"use client";

import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { AgentDefinition, Task } from "@/lib/types";
import { agentTypeStyle, taskStatusStyle, toneClasses } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";

const TYPE_LEVEL: Record<string, number> = { cto: 0, manager: 1, worker: 2, system_agent: 3 };

interface AgentNodeData {
  agent: AgentDefinition;
  currentTasks: Task[];
  completedCount: number;
  failedCount: number;
  totalCount: number;
}

function AgentNode({ data }: NodeProps<AgentNodeData>) {
  const { agent, currentTasks, completedCount, failedCount, totalCount } = data;
  const typeStyle = agentTypeStyle(agent.agent_type);
  const t = toneClasses(typeStyle.tone);
  const working = currentTasks.length > 0;

  return (
    <div
      className={`w-64 rounded-lg border bg-base-raised px-4 py-3 shadow-panel ${t.border}`}
      style={{ borderWidth: 1 }}
    >
      <Handle type="target" position={Position.Top} className="!bg-base-hairline" />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{agent.name}</p>
          <p className="truncate font-mono text-[11px] text-ink-faint">
            {agent.team_name ?? agent.system_kind ?? agent.agent_id}
          </p>
        </div>
        <StatusBadge label={typeStyle.label} tone={typeStyle.tone} pulse={working} />
      </div>

      {agent.role_description && (
        <p className="mt-2 line-clamp-2 text-[11px] text-ink-muted">{agent.role_description}</p>
      )}

      <div className="mt-3 flex items-center gap-3 font-mono text-[11px] text-ink-faint">
        <span title="Completed / total tasks">✓ {completedCount}/{totalCount}</span>
        {failedCount > 0 && <span className="text-signal-rose">✕ {failedCount}</span>}
      </div>

      {currentTasks.length > 0 && (
        <div className="mt-2 space-y-1 border-t border-base-hairline pt-2">
          <p className="font-mono text-[10px] uppercase tracking-wide text-ink-faint">
            Current work
          </p>
          {currentTasks.slice(0, 2).map((task) => {
            const s = taskStatusStyle(task.status);
            return (
              <div key={task.task_id} className="flex items-center gap-1.5">
                <span className={`h-1.5 w-1.5 rounded-full ${toneClasses(s.tone).dot} animate-pulseDot`} />
                <span className="truncate text-[11px] text-ink-muted">{task.title}</span>
              </div>
            );
          })}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-base-hairline" />
    </div>
  );
}

const nodeTypes = { agent: AgentNode };

export function AgentGraph({ agents, tasks }: { agents: AgentDefinition[]; tasks: Task[] }) {
  const { nodes, edges } = useMemo(() => buildGraph(agents, tasks), [agents, tasks]);

  return (
    <div className="h-[560px] w-full overflow-hidden rounded-lg border border-base-hairline bg-base-raised/40">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
      >
        <Background color="#242a33" gap={20} />
        <Controls showInteractive={false} className="!bg-base-panel !border-base-hairline" />
      </ReactFlow>
    </div>
  );
}

function buildGraph(agents: AgentDefinition[], tasks: Task[]): { nodes: Node[]; edges: Edge[] } {
  const levels = new Map<number, AgentDefinition[]>();
  for (const agent of agents) {
    const level = TYPE_LEVEL[agent.agent_type] ?? 3;
    const bucket = levels.get(level) ?? [];
    bucket.push(agent);
    levels.set(level, bucket);
  }

  const tasksByAgent = new Map<string, Task[]>();
  for (const task of tasks) {
    if (!task.assigned_agent_id) continue;
    const bucket = tasksByAgent.get(task.assigned_agent_id) ?? [];
    bucket.push(task);
    tasksByAgent.set(task.assigned_agent_id, bucket);
  }

  const X_SPACING = 290;
  const Y_SPACING = 190;

  const nodes: Node[] = [];
  const sortedLevels = [...levels.keys()].sort((a, b) => a - b);

  for (const level of sortedLevels) {
    const bucket = levels.get(level)!;
    const totalWidth = (bucket.length - 1) * X_SPACING;
    bucket.forEach((agent, i) => {
      const agentTasks = tasksByAgent.get(agent.agent_id) ?? [];
      const currentTasks = agentTasks.filter((t) => t.status === "running" || t.status === "retrying");
      const completedCount = agentTasks.filter((t) => t.status === "completed").length;
      const failedCount = agentTasks.filter((t) => t.status === "failed").length;

      nodes.push({
        id: agent.agent_id,
        type: "agent",
        position: { x: i * X_SPACING - totalWidth / 2, y: level * Y_SPACING },
        data: {
          agent,
          currentTasks,
          completedCount,
          failedCount,
          totalCount: agentTasks.length,
        },
        draggable: false,
      });
    });
  }

  const edges: Edge[] = agents
    .filter((a) => a.parent_agent_id)
    .map((a) => ({
      id: `${a.parent_agent_id}-${a.agent_id}`,
      source: a.parent_agent_id!,
      target: a.agent_id,
      style: { stroke: "#242a33", strokeWidth: 1.5 },
      type: "smoothstep",
    }));

  return { nodes, edges };
}
