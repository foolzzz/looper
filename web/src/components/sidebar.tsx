import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  LayoutDashboard,
  MessageSquare,
  ListTodo,
  BookOpen,
  MessageCircleQuestion,
  Bot,
  CalendarClock,
  Radio,
  Layers,
  ListChecks,
  BellRing,
  BrainCircuit,
  ChevronDown,
  Sliders,
  Folder,
  Loader2,
  Plus,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { SignOut } from "@/components/sign-out";
import { ProjectDialog } from "@/components/project-dialog";
import { SETTINGS_PAGES, type SettingsPageId } from "@/lib/settings-pages";
import { fetchOverview } from "@/lib/api";
import { attentionSettingsGet, fetchAttention, isAttentionOff } from "@/lib/api-attention";
import { intentsSettingsGet } from "@/lib/api-leads";
import { useProjects } from "@/lib/use-projects";
import { useWaitingGates } from "@/lib/use-waiting-gates";
import { useLedger } from "@/lib/use-ledger";
import { startPolling } from "@/lib/polling";
import { isMissingOperation, taskConversationsGet } from "@/lib/work";
import { cn } from "@/lib/utils";

/** "history" is one agent's session, or every run of one role; "projects" is one project, from the
 * sidebar's project list; "settings" is one of the Settings pages, from the group at the bottom. */
export type Page = "dashboard" | "todo" | "chat" | "work" | "tasks" | "ledger" | "gates" | "projects" | "schedules" | "messages" | "intents" | "memory" | "settings" | "history";

const NAV_ITEMS: { page: Page; label: string; icon: React.ElementType }[] = [
  { page: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { page: "chat", label: "Chat", icon: MessageSquare },
  { page: "tasks", label: "Tasks", icon: ListTodo },
  { page: "gates", label: "User Gates", icon: MessageCircleQuestion },
  { page: "schedules", label: "Schedules", icon: CalendarClock },
  { page: "messages", label: "Message stream", icon: Radio },
  { page: "memory", label: "Memory 记忆", icon: BrainCircuit },
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
  /** `null` until the conversations switch has been read. Off, or a failed read, is false. */
  onWorkEnabled?: (enabled: boolean | null) => void;
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
  onWorkEnabled,
}: SidebarProps) {
  const waitingGates = useWaitingGates();
  const ledger = useLedger();
  const [version, setVersion] = useState<string | null>(null);
  const [intentsOn, setIntentsOn] = useState(false);
  useEffect(() => {
    let live = true;
    intentsSettingsGet().then(
      (settings) => { if (live) setIntentsOn(settings.enabled === true); },
      () => { if (live) setIntentsOn(false); },
    );
    return () => { live = false; };
  }, []);
  const [todoOn, setTodoOn] = useState(false);
  const [todoCount, setTodoCount] = useState<number | null>(null);
  useEffect(() => startPolling(async (signal) => {
    try {
      // Check the switch before collecting a summary: off must not provoke a 409.
      const settings = await attentionSettingsGet();
      if (signal.aborted) return;
      setTodoOn(settings.enabled === true);
      if (!settings.enabled) { setTodoCount(null); return; }
      const summary = await fetchAttention(signal);
      if (signal.aborted) return;
      // total counts only readable sections; an incomplete total is not an exact count.
      setTodoCount(summary.sections.every((section) => section.state === "ok") ? summary.total : null);
    } catch (err) {
      if (signal.aborted) return;
      setTodoCount(null);
      if (isAttentionOff(err)) setTodoOn(false);
    }
  }, 30_000), []);
  const [workOn, setWorkOn] = useState<boolean | null>(null);
  useEffect(() => startPolling(async (signal) => {
    try {
      const settings = await taskConversationsGet(signal);
      if (signal.aborted) return;
      setWorkOn(settings.enabled === true);
    } catch (err) {
      if (signal.aborted) return;
      setWorkOn(false);
      if (isMissingOperation(err)) return true;
    }
  }, 30_000), []);
  useEffect(() => { onWorkEnabled?.(workOn); }, [workOn, onWorkEnabled]);
  const navItems = [
    ...NAV_ITEMS.slice(0, 1),
    ...(todoOn ? [{ page: "todo" as const, label: "Needs you", icon: BellRing }] : []),
    ...NAV_ITEMS.slice(1, 2),
    ...(workOn === true ? [{ page: "work" as const, label: "Work", icon: Layers }] : []),
    ...NAV_ITEMS.slice(2, 3),
    ...(ledger.enabled ? [{ page: "ledger" as const, label: "Ledger 账本", icon: BookOpen }] : []),
    ...NAV_ITEMS.slice(3),
    ...(intentsOn ? [{ page: "intents" as const, label: "Intents 意图", icon: ListChecks }] : []),
  ];
  useEffect(() => {
    let live = true;
    fetchOverview()
      .then((overview) => {
        if (live && overview.version) setVersion(overview.version);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  return (
    <aside
      id={SIDEBAR_ID}
      aria-label="Navigation"
      className={cn(
        // The whole column scrolls when it is taller than the screen (a phone on its side, Settings open).
        "fixed inset-y-0 left-0 z-40 flex h-dvh w-56 flex-col overflow-y-auto overscroll-contain border-r border-border bg-card",
        "transition-[transform,visibility] duration-200 motion-reduce:transition-none",
        "lg:visible lg:static lg:z-auto lg:translate-x-0",
        open ? "visible translate-x-0" : "invisible -translate-x-full",
      )}
    >
      <div className="flex h-14 shrink-0 items-center gap-2 px-4 border-b border-border">
        <Bot className="h-6 w-6 text-primary" />
        <span className="text-lg font-semibold tracking-tight">Looper</span>
        {version && (
          <span title={version} className="whitespace-nowrap text-xs text-muted-foreground">
            v{version}
          </span>
        )}
        <span className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close navigation"
          className="-mr-2 flex h-9 w-9 shrink-0 pointer-coarse:h-11 pointer-coarse:w-11 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring lg:hidden"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <nav className="shrink-0 px-2 py-3 space-y-0.5">
        {navItems.map(({ page, label, icon: Icon }) => (
          <button
            key={page}
            type="button"
            onClick={() => onNavigate(page)}
            className={cn(
              "flex w-full items-center gap-3 rounded-md px-3 py-2 pointer-coarse:py-3 text-sm font-medium transition-colors",
              page === "work" && "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              activePage === page
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            <Icon className="h-4 w-4" />
            <span className="min-w-0 text-left">{label}
              {page === "ledger" && ledger.stale && <span className="block text-xs">暂无法刷新</span>}
            </span>
            {page === "todo" && todoCount === null && (
              <span className="ml-auto" aria-label="待处理数量未知">?</span>
            )}
            {page === "todo" && todoCount !== null && todoCount > 0 && (
              <Badge variant="warning" aria-label={`${todoCount} 项待你处理`} className="ml-auto tabular-nums">
                {todoCount > 99 ? "99+" : todoCount}
              </Badge>
            )}
            {page === "ledger" && ledger.count === null && (
              <span className="ml-auto" aria-label="待处理数量未知">?</span>
            )}
            {page === "ledger" && ledger.count !== null && ledger.count > 0 && (
              <Badge variant="warning" aria-label={`${ledger.count} 项待你处理`} className="ml-auto tabular-nums">
                {ledger.count > 99 ? "99+" : ledger.count}
              </Badge>
            )}
            {page === "gates" && waitingGates !== null && waitingGates > 0 && (
              <Badge
                variant="warning"
                aria-label={`${waitingGates} ${waitingGates === 1 ? "gate" : "gates"} waiting for you`}
                className="ml-auto tabular-nums"
              >
                {waitingGates > 99 ? "99+" : waitingGates}
              </Badge>
            )}
          </button>
        ))}
      </nav>

      <ProjectsSection activeProjectId={activeProjectId} onOpenProject={onOpenProject} />

      <SettingsGroup activePage={activeSettingsPage} onOpen={onOpenSettings} />

      <div className="shrink-0 space-y-1 border-t border-border px-2 py-3">
        <SignOut />
        <p className="px-2 text-xs text-muted-foreground">Looper Agent System</p>
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
          "flex w-full items-center gap-3 rounded-md px-3 py-2 pointer-coarse:py-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
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
                  "flex w-full items-center gap-2.5 rounded-md py-1.5 pointer-coarse:py-3 pl-9 pr-3 text-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
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
  const { projects, projectsRoot, error } = useProjects();
  const [creating, setCreating] = useState(false);

  return (
    // Below lg the list grows with its projects and the drawer scrolls; from lg up it scrolls on its own, and keeps
    // a few rows when the window is short.
    <section aria-label="Projects" className="flex flex-1 flex-col border-t border-border lg:min-h-32">
      <div className="flex shrink-0 items-center justify-between pl-5 pr-3 pt-3 pb-1">
        <span className="font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Projects
        </span>
        <button
          type="button"
          onClick={() => setCreating(true)}
          aria-label="New project"
          title="New project"
          className="flex h-6 w-6 pointer-coarse:-my-2.5 pointer-coarse:-mr-2.5 pointer-coarse:h-11 pointer-coarse:w-11 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="space-y-0.5 px-2 pb-3 lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:overscroll-contain">
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
                  "flex w-full items-center gap-2 rounded-md px-3 py-1.5 pointer-coarse:py-3 text-sm transition-colors",
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

      {/* On document.body: the sidebar's translate would otherwise confine the fixed dialog to the sidebar. */}
      {creating && createPortal(
        <ProjectDialog
          projectsRoot={projectsRoot}
          onClose={() => setCreating(false)}
          onCreated={(project) => {
            setCreating(false);
            onOpenProject(project.project_id);
          }}
        />,
        document.body,
      )}
    </section>
  );
}
