import { useCallback, useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { Sidebar, SIDEBAR_ID, type Page } from "@/components/sidebar";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { TasksPage } from "@/pages/tasks";
import { AgentsPage } from "@/pages/agents";
import { SettingsPage } from "@/pages/settings";
import { AgentHistoryPage } from "@/pages/agent-history";

const PAGES = new Set<Page>(["dashboard", "chat", "tasks", "agents", "settings", "history"]);

interface Route {
  page: Page;
  /** Everything after "page/", including any "?query" of its own (#history/<agent>?task=…). */
  sub: string;
  /** The query right after the page name: "project=…" for #tasks?project=…. */
  query: string;
}

function readHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
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
        activeProjectId={route.page === "tasks" && !route.sub ? new URLSearchParams(route.query).get("project") : null}
        onNavigate={(p) => {
          setNavOpen(false);
          navigate(p);
        }}
        onOpenProject={(id) => {
          setNavOpen(false);
          navigateSub("tasks", "", `project=${encodeURIComponent(id)}`);
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
          <span className="text-sm font-semibold tracking-tight">LoopX</span>
        </header>
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
          {route.page === "agents" && (
            <div className="h-full overflow-y-auto">
              <AgentsPage
                sub={route.sub}
                onNavigate={(sub) => navigateSub("agents", sub)}
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
              <SettingsPage />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
