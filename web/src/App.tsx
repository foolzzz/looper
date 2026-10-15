import { useCallback, useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { Sidebar, SIDEBAR_ID, type Page } from "@/components/sidebar";
import { ClaudeCheckBanner } from "@/components/claude-check-banner";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { TasksPage } from "@/pages/tasks";
import { RunsPage } from "@/pages/runs";
import { SchedulesPage } from "@/pages/schedules";
import { SettingsPage } from "@/pages/settings";
import { AgentHistoryPage } from "@/pages/agent-history";
import { ProjectPage } from "@/pages/project";
import { DEFAULT_SETTINGS_PAGE, isSettingsPage, type SettingsPageId } from "@/lib/settings-pages";

const PAGES = new Set<Page>(["dashboard", "chat", "tasks", "projects", "runs", "schedules", "settings", "history"]);

interface Route {
  page: Page;
  /** Everything after "page/", including any "?query" of its own (#history/<agent>?task=…). */
  sub: string;
  /** The query right after the page name: "project=…" for #tasks?project=…. */
  query: string;
}

/** Where the links from before the Settings pages and the Runs page go now (the hash without #). */
function currentHash(raw: string): string {
  // #agents/<id>[/sessions]: the role's runs.
  const agents = /^agents(?:\/([^/?]+))?(?:\/sessions)?(\?.*)?$/.exec(raw);
  if (agents) return `runs${agents[1] ? `/${agents[1]}` : ""}${agents[2] ?? ""}`;
  const settings = /^settings(?:\/([^/?]*))?(?:\/([^?]*))?/.exec(raw);
  if (!settings) return raw;
  const [, page = "", rest = ""] = settings;
  // The import's links from before: #settings/provider|mcp|channel/<id>.
  const moved: Record<string, string> = { provider: "providers", mcp: "catalog/mcp", channel: "channels", defaults: "roles" };
  if (page in moved) return `settings/${moved[page]}${rest ? `/${rest}` : ""}`;
  return isSettingsPage(page) ? raw : `settings/${DEFAULT_SETTINGS_PAGE}`;
}

function readHash(): Route {
  const shown = window.location.hash.replace(/^#\/?/, "");
  const raw = currentHash(shown);
  // An old link is replaced in place, so Back does not return to it.
  if (raw !== shown) window.history.replaceState(null, "", `#${raw}`);
  const cut = raw.search(/[/?]/);
  const pagePart = (cut >= 0 ? raw.slice(0, cut) : raw) as Page;
  const rest = cut >= 0 ? raw.slice(cut) : "";
  const sub = rest.startsWith("/") ? rest.slice(1) : "";
  const query = rest.startsWith("?") ? rest.slice(1) : "";
  return { page: PAGES.has(pagePart) ? pagePart : "dashboard", sub, query };
}

function App() {
  const [route, setRoute] = useState<Route>(readHash);

  useEffect(() => {
    const onHash = () => setRoute(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Below the lg breakpoint the sidebar collapses into a drawer; choosing a page or project closes it.
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const navigate = useCallback((p: Page) => {
    window.location.hash = p;
  }, []);

  const navigateSub = useCallback((p: Page, sub: string, query = "") => {
    window.location.hash = (sub ? `${p}/${sub}` : p) + (query ? `?${query}` : "");
  }, []);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <Sidebar
        activePage={route.page}
        activeSettingsPage={route.page === "settings" ? (route.sub.split("/")[0] as SettingsPageId) : null}
        activeProjectId={
          route.page === "projects"
            ? route.sub.split("/")[0] || null
            : route.page === "tasks" && !route.sub
              ? new URLSearchParams(route.query).get("project")
              : null
        }
        onNavigate={(p) => {
          setNavOpen(false);
          navigate(p);
        }}
        onOpenProject={(id) => {
          setNavOpen(false);
          navigateSub("projects", id);
        }}
        onOpenSettings={(id) => {
          setNavOpen(false);
          navigateSub("settings", id);
        }}
        open={navOpen}
        onClose={() => setNavOpen(false)}
      />
      {navOpen && (
        <div aria-hidden="true" onClick={() => setNavOpen(false)} className="fixed inset-0 z-30 bg-black/50 lg:hidden" />
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-card px-2 lg:hidden">
          <button
            type="button"
            onClick={() => setNavOpen(true)}
            aria-label="Open navigation"
            aria-controls={SIDEBAR_ID}
            aria-expanded={navOpen}
            className="flex h-11 w-11 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <Menu className="h-5 w-5" />
          </button>
          <span className="text-sm font-semibold tracking-tight">Looper</span>
        </header>
        <ClaudeCheckBanner />
        <main className="min-h-0 flex-1 overflow-hidden">
          <div className={route.page === "chat" ? "h-full" : "hidden"}>
            <ChatPage />
          </div>
          {route.page === "dashboard" && (
            <div className="h-full overflow-y-auto p-6"><DashboardPage /></div>
          )}
          {route.page === "tasks" && (
            <div className="h-full overflow-y-auto">
              <TasksPage
                sub={route.sub}
                query={route.query}
                onNavigate={(sub, query) => navigateSub("tasks", sub, query)}
              />
            </div>
          )}
          {route.page === "projects" && (
            <div className="h-full overflow-y-auto">
              <ProjectPage
                key={route.sub.split("/")[0]}
                sub={route.sub}
                onNavigate={(sub) => navigateSub("projects", sub)}
              />
            </div>
          )}
          {route.page === "runs" && (
            <div className="h-full overflow-y-auto">
              <RunsPage
                sub={route.sub}
                onNavigate={(sub) => navigateSub("runs", sub)}
              />
            </div>
          )}
          {route.page === "schedules" && (
            <div className="h-full overflow-y-auto">
              <SchedulesPage
                sub={route.sub}
                query={route.query}
                onNavigate={(sub, query) => navigateSub("schedules", sub, query)}
              />
            </div>
          )}
          {route.page === "history" && (
            <div className="h-full overflow-y-auto">
              <AgentHistoryPage sub={route.sub} />
            </div>
          )}
          {route.page === "settings" && (
            <div className="h-full overflow-y-auto">
              <SettingsPage sub={route.sub} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
