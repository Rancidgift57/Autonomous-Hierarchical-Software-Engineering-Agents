"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import useSWR from "swr";
import { api } from "@/lib/api";
import { projectStatusStyle } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";

const NAV_ITEMS = [
  { href: "", label: "Overview", icon: "◆" },
  { href: "/agents", label: "Agents", icon: "⌂" },
  { href: "/tasks", label: "Tasks", icon: "⌗" },
  { href: "/artifacts", label: "Artifacts", icon: "▤" },
  { href: "/logs", label: "Logs", icon: "≋" },
  { href: "/qa", label: "QA", icon: "✓" },
  { href: "/deployment", label: "Deployment", icon: "▲" },
];

export function ProjectSidebar({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  const { data: project } = useSWR(["project", projectId], () => api.getProject(projectId), {
    refreshInterval: 10000,
  });

  const base = `/projects/${projectId}`;

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-base-hairline bg-base-raised/60">
      <div className="border-b border-base-hairline px-4 py-4">
        <Link href="/projects" className="font-mono text-[11px] uppercase tracking-widest text-ink-faint hover:text-ink-muted">
          ← All projects
        </Link>
        <h2 className="mt-2 truncate text-sm font-semibold text-ink">
          {project?.name ?? "…"}
        </h2>
        {project && (
          <div className="mt-2">
            <StatusBadge
              label={projectStatusStyle(project.status).label}
              tone={projectStatusStyle(project.status).tone}
              pulse={project.status === "running"}
            />
          </div>
        )}
      </div>

      <nav className="flex flex-col gap-0.5 p-2">
        {NAV_ITEMS.map((item) => {
          const href = `${base}${item.href}`;
          const active = pathname === href;
          return (
            <Link
              key={item.href}
              href={href}
              className={clsx(
                "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition",
                active
                  ? "bg-signal-teal/10 text-signal-teal"
                  : "text-ink-muted hover:bg-white/[0.04] hover:text-ink",
              )}
            >
              <span className="w-4 text-center text-xs opacity-70">{item.icon}</span>
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
