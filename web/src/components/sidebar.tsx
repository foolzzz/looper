import { useState } from "react";
import {
  LayoutDashboard,
  MessageSquare,
  MessagesSquare,
  ListTodo,
  Bot,
  ChevronDown,
  History,
  Sliders,
  Folder,
  Loader2,
  Plus,
  X,
} from "lucide-react";
import { ProjectDialog } from "@/components/project-dialog";
import { SETTINGS_PAGES, type SettingsPageId } from "@/lib/settings-pages";
import { useProjects } from "@/lib/use-projects";
import { cn } from "@/lib/utils";

/** "history" is an agent's session history, reached from a task or a run; "projects" is one project, from the
 * sidebar's project list; "settings" is one of the Settings pages, from the group at the bottom. */
export type Page =
  | "dashboard"
  | "chat"
  | "conversations"
  | "tasks"
  | "projects"
  | "runs"
  | "settings"
  | "history";

const NAV_ITEMS: { page: Page; label: string; icon: React.ElementType }[] = [
  { page: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { page: "chat", label: "Chat", icon: MessageSquare },
  { page: "conversations", label: "Conversations", icon: MessagesSquare },
  { page: "tasks", label: "Tasks", icon: ListTodo },
  { page: "runs", label: "Runs", icon: History },
];

interface SidebarProps {
  activePage: Page;
  /** The Settings page open in the current route, if any. */
  activeSettingsPage: SettingsPageId | null;
  /** The project open in the current route (its page, or the Tasks list filtered by it), if any. */
  activeProjectId: string | null;
  onNavigate: (page: Page) => void;
  onOpenProject: (projectId: string) => void;
  onOpenSettings: (page: SettingsPageId) => void;
  /** Below the lg breakpoint the sidebar is a drawer: shown only while open. */
  open: boolean;
  onClose: () => void;
}

export const SIDEBAR_ID = "app-sidebar";

export function Sidebar({
  activePage,
  activeSettingsPage,
  activeProjectId,
  onNavigate,
  onOpenProject,
  onOpenSettings,
  open,
  onClose,
}: SidebarProps) {
  return (
    <aside
      id={SIDEBAR_ID}
      aria-label="Navigation"
      className={cn(
        "fixed inset-y-0 left-0 z-40 flex h-screen w-56 flex-col border-r border-border bg-card",
        "transition-[transform,visibility] duration-200 motion-reduce:transition-none",
        "lg:visible lg:static lg:z-auto lg:translate-x-0",
        open ? "visible translate-x-0" : "invisible -translate-x-full",
      )}
    >
      <div className="flex h-14 shrink-0 items-center gap-2 px-4 border-b border-border">
        <Bot className="h-6 w-6 text-primary" />
        <span className="flex-1 text-lg font-semibold tracking-tight">Looper</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close navigation"
          className="-mr-2 flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring lg:hidden"
        >
          <X className="h-4 w-4" />
        </button>
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

      <SettingsGroup activePage={activeSettingsPage} onOpen={onOpenSettings} />

      <div className="shrink-0 border-t border-border px-4 py-3">
        <p className="text-xs text-muted-foreground">Looper Agent System</p>
      </div>
    </aside>
  );
}

const SETTINGS_OPEN_KEY = "looper.sidebar.settings-open";
const SETTINGS_LIST_ID = "sidebar-settings-pages";

function storedOpen(): boolean | null {
  try {
    const value = window.localStorage.getItem(SETTINGS_OPEN_KEY);
    return value === null ? null : value === "true";
  } catch {
    return null;
  }
}

/** The Settings pages, one item each, in a group that folds; it opens by itself on a Settings page. */
function SettingsGroup({
  activePage,
  onOpen,
}: {
  activePage: SettingsPageId | null;
  onOpen: (page: SettingsPageId) => void;
}) {
  const [expanded, setExpanded] = useState(() => storedOpen() ?? activePage !== null);
  // Opening a Settings page (from a link too) opens the group, so the page's item shows.
  const [shownFor, setShownFor] = useState(activePage);
  if (activePage !== shownFor) {
    setShownFor(activePage);
    if (activePage !== null) setExpanded(true);
  }
  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    try {
      window.localStorage.setItem(SETTINGS_OPEN_KEY, String(next));
    } catch {
      // A per-viewer convenience: without storage the group starts as the route says.
    }
  };

  return (
    <nav aria-label="Settings" className="shrink-0 border-t border-border px-2 py-2">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        aria-controls={SETTINGS_LIST_ID}
        className={cn(
          "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          activePage !== null && !expanded
            ? "bg-primary/10 text-primary"
            : "text-muted-foreground hover:bg-accent hover:text-foreground",
        )}
      >
        <Sliders className="h-4 w-4" />
        <span className="flex-1 text-left">Settings</span>
        <ChevronDown
          aria-hidden="true"
          className={cn("h-4 w-4 transition-transform duration-150 motion-reduce:transition-none", !expanded && "-rotate-90")}
        />
      </button>
      <ul id={SETTINGS_LIST_ID} hidden={!expanded} className="mt-0.5 space-y-0.5">
        {SETTINGS_PAGES.map(({ id, label, icon: Icon }) => {
          const active = id === activePage;
          return (
            <li key={id}>
              <button
                type="button"
                onClick={() => onOpen(id)}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex w-full items-center gap-2.5 rounded-md py-1.5 pl-9 pr-3 text-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="truncate">{label}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
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
