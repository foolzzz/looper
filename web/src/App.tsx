import { useCallback, useEffect, useState } from "react";
import { Sidebar, type Page } from "@/components/sidebar";
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
        onNavigate={navigate}
        onOpenProject={(id) => navigateSub("tasks", "", `project=${encodeURIComponent(id)}`)}
      />
      <main className="flex-1 overflow-hidden">
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
  );
}

export default App;
