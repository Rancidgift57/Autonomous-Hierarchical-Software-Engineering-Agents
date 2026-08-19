import Link from "next/link";
import type { ProjectSummary } from "@/lib/types";
import { projectStatusStyle, relativeTime } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";

export function ProjectCard({ project }: { project: ProjectSummary }) {
  const style = projectStatusStyle(project.status);
  return (
    <Link
      href={`/projects/${project.project_id}`}
      className="group flex flex-col gap-3 rounded-lg border border-base-hairline bg-base-raised p-4 shadow-panel transition hover:border-signal-teal/40 hover:bg-base-panel"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="truncate text-sm font-semibold text-ink group-hover:text-signal-teal">
          {project.name}
        </h3>
        <StatusBadge label={style.label} tone={style.tone} pulse={project.status === "running"} />
      </div>
      <p className="line-clamp-2 text-xs text-ink-muted">
        {project.description || "No description provided."}
      </p>
      <div className="mt-auto flex items-center justify-between pt-2 font-mono text-[11px] text-ink-faint">
        <span>{project.project_id}</span>
        <span>updated {relativeTime(project.updated_at)}</span>
      </div>
    </Link>
  );
}
