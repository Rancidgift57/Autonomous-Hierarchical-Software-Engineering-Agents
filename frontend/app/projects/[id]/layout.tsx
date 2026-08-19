import { ProjectSidebar } from "@/components/ProjectSidebar";

export default function ProjectLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { id: string };
}) {
  return (
    <div className="flex min-h-screen">
      <ProjectSidebar projectId={params.id} />
      <main className="flex-1 overflow-x-hidden px-8 py-8">{children}</main>
    </div>
  );
}
