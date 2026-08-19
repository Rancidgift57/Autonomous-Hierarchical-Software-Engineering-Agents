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
import type { Task } from "@/lib/types";
import { taskStatusStyle, toneClasses, isActiveTask } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";

interface TaskNodeData {
  task: Task;
}

function TaskNode({ data }: NodeProps<TaskNodeData>) {
  const { task } = data;
  const s = taskStatusStyle(task.status);
  const t = toneClasses(s.tone);
  return (
    <div className={`w-56 rounded-lg border bg-base-raised px-3 py-2.5 shadow-panel ${t.border}`}>
      <Handle type="target" position={Position.Left} className="!bg-base-hairline" />
      <div className="flex items-start justify-between gap-2">
        <p className="line-clamp-2 text-xs font-medium text-ink">{task.title}</p>
      </div>
      <div className="mt-2 flex items-center justify-between">
        <StatusBadge label={s.label} tone={s.tone} pulse={isActiveTask(task.status)} />
        {task.retries > 0 && (
          <span className="font-mono text-[10px] text-signal-amber">
            retry {task.retries}/{task.max_retries}
          </span>
        )}
      </div>
      {task.owner_manager && (
        <p className="mt-1.5 truncate font-mono text-[10px] text-ink-faint">{task.owner_manager}</p>
      )}
      <Handle type="source" position={Position.Right} className="!bg-base-hairline" />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

export function TaskDagGraph({ tasks }: { tasks: Task[] }) {
  const { nodes, edges } = useMemo(() => buildDag(tasks), [tasks]);

  return (
    <div className="h-[600px] w-full overflow-hidden rounded-lg border border-base-hairline bg-base-raised/40">
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

function buildDag(tasks: Task[]): { nodes: Node[]; edges: Edge[] } {
  const byId = new Map(tasks.map((t) => [t.task_id, t]));

  // Longest-path layering so a task always sits to the right of every
  // task it depends on, even across multiple dependency hops.
  const depthCache = new Map<string, number>();
  function depthOf(taskId: string, guard = new Set<string>()): number {
    if (depthCache.has(taskId)) return depthCache.get(taskId)!;
    if (guard.has(taskId)) return 0; // cycle guard — shouldn't happen in a valid DAG
    guard.add(taskId);
    const task = byId.get(taskId);
    if (!task || task.depends_on_task_ids.length === 0) {
      depthCache.set(taskId, 0);
      return 0;
    }
    const deps = task.depends_on_task_ids.filter((id) => byId.has(id));
    const depth = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((id) => depthOf(id, guard)));
    depthCache.set(taskId, depth);
    return depth;
  }

  const columns = new Map<number, Task[]>();
  for (const task of tasks) {
    const depth = depthOf(task.task_id);
    const bucket = columns.get(depth) ?? [];
    bucket.push(task);
    columns.set(depth, bucket);
  }

  const X_SPACING = 300;
  const Y_SPACING = 120;

  const nodes: Node[] = [];
  for (const [depth, colTasks] of columns) {
    colTasks
      .sort((a, b) => a.title.localeCompare(b.title))
      .forEach((task, i) => {
        nodes.push({
          id: task.task_id,
          type: "task",
          position: { x: depth * X_SPACING, y: i * Y_SPACING },
          data: { task },
          draggable: false,
        });
      });
  }

  const edges: Edge[] = tasks.flatMap((task) =>
    task.depends_on_task_ids
      .filter((depId) => byId.has(depId))
      .map((depId) => {
        const active = isActiveTask(task.status);
        return {
          id: `${depId}-${task.task_id}`,
          source: depId,
          target: task.task_id,
          type: "smoothstep",
          animated: active,
          style: {
            stroke: active ? "#E8A33D" : "#242a33",
            strokeWidth: active ? 2 : 1.5,
          },
        } satisfies Edge;
      }),
  );

  return { nodes, edges };
}
