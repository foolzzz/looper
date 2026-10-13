import { useState } from "react";
import { LayoutDashboard, MessageSquare, ListTodo, Bot, Sliders, Folder, Loader2, Plus } from "lucide-react";
import { ProjectDialog } from "@/components/project-dialog";
import { useProjects } from "@/lib/use-projects";
import { cn } from "@/lib/utils";

/** "history" is an agent's session history; it is reached from a task or agent, not the nav. */
export type Page = "dashboard" | "chat" | "tasks" | "agents" | "settings" | "history";

const NAV_ITEMS: { page: Page; label: string; icon: React.ElementType }[] = [
  { page: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { page: "chat", label: "Chat", icon: MessageSquare },
  { page: "tasks", label: "Tasks", icon: ListTodo },
  { page: "agents", label: "Agents", icon: Bot },
  { page: "settings", label: "Settings", icon: Sliders },
];

interface SidebarProps {
  activePage: Page;
  /** The project the Tasks list is filtered by in the current route, if any. */
  activeProjectId: string | null;
  onNavigate: (page: Page) => void;
  onOpenProject: (projectId: string) => void;
}

export function Sidebar({ activePage, activeProjectId, onNavigate, onOpenProject }: SidebarProps) {
  return (
    <aside className="flex h-screen w-56 flex-col border-r border-border bg-card">
      <div className="flex h-14 shrink-0 items-center gap-2 px-4 border-b border-border">
        <Bot className="h-6 w-6 text-primary" />
        <span className="text-lg font-semibold tracking-tight">LoopX</span>
      </div>

      <nav className="shrink-0 px-2 py-3 space-y-0.5">
        {NAV_ITEMS.map(({ page, label, icon: Icon }) => (
          <button
            key={page}
            type="button"
            onClick={() => onNavigate(page)}
            className={cn(
              "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              activePage === page
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </nav>

      <ProjectsSection activeProjectId={activeProjectId} onOpenProject={onOpenProject} />

      <div className="shrink-0 border-t border-border px-4 py-3">
        <p className="text-xs text-muted-foreground">LoopX Agent System</p>
      </div>
    </aside>
  );
}

function ProjectsSection({
  activeProjectId,
  onOpenProject,
}: {
  activeProjectId: string | null;
  onOpenProject: (projectId: string) => void;
}) {
  const { projects, error } = useProjects();
  const [creating, setCreating] = useState(false);

  return (
    <section aria-label="Projects" className="flex min-h-0 flex-1 flex-col border-t border-border">
      <div className="flex shrink-0 items-center justify-between pl-5 pr-3 pt-3 pb-1">
        <span className="font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Projects
        </span>
        <button
          type="button"
          onClick={() => setCreating(true)}
          aria-label="New project"
          title="New project"
          className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pb-3 space-y-0.5">
        {projects === null ? (
          error && <p className="px-3 py-1.5 text-xs text-destructive">{error}</p>
        ) : projects.length === 0 ? (
          <p className="px-3 py-1.5 text-xs text-muted-foreground">No projects yet</p>
        ) : (
          projects.map((p) => {
            const active = p.project_id === activeProjectId;
            return (
              <button
                key={p.project_id}
                type="button"
                onClick={() => onOpenProject(p.project_id)}
                aria-current={active ? "page" : undefined}
                title={`${p.name}${p.work_dir ? ` — ${p.work_dir}` : ""}\n${p.task_count} ${p.task_count === 1 ? "task" : "tasks"}, ${p.active_count} active`}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                <Folder className="h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1 truncate text-left">{p.name}</span>
                {p.active_count > 0 ? (
                  <span className="flex shrink-0 items-center gap-1 text-xs tabular-nums text-primary">
                    <Loader2 className="h-3 w-3 motion-safe:animate-spin" />
                    {p.active_count}
                    <span className="sr-only"> active</span>
                  </span>
                ) : (
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                    {p.task_count}
                    <span className="sr-only">{p.task_count === 1 ? " task" : " tasks"}</span>
                  </span>
                )}
              </button>
            );
          })
        )}
      </div>

      {creating && (
        <ProjectDialog
          onClose={() => setCreating(false)}
          onCreated={(project) => {
            setCreating(false);
            onOpenProject(project.project_id);
          }}
        />
      )}
    </section>
  );
}
