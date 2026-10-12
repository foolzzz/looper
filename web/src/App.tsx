import { useCallback, useEffect, useState } from "react";
import { Sidebar, type Page } from "@/components/sidebar";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { TasksPage } from "@/pages/tasks";
import { AgentsPage } from "@/pages/agents";

const PAGES = new Set<Page>(["dashboard", "chat", "tasks", "agents"]);

interface Route {
  page: Page;
  sub: string;
}

function readHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const slash = raw.indexOf("/");
  const pagePart = (slash >= 0 ? raw.slice(0, slash) : raw) as Page;
  const sub = slash >= 0 ? raw.slice(slash + 1) : "";
  return { page: PAGES.has(pagePart) ? pagePart : "dashboard", sub };
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

  const navigateSub = useCallback((p: Page, sub: string) => {
    window.location.hash = sub ? `${p}/${sub}` : p;
  }, []);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <Sidebar activePage={route.page} onNavigate={navigate} />
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
              onNavigate={(sub) => navigateSub("tasks", sub)}
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
      </main>
    </div>
  );
}

export default App;
