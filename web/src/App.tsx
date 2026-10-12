import { useEffect, useState } from "react";
import { Sidebar, type Page } from "@/components/sidebar";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { TasksPage } from "@/pages/tasks";
import { AgentsPage } from "@/pages/agents";

const PAGES = new Set<Page>(["dashboard", "chat", "tasks", "agents"]);

function readHash(): Page {
  const h = window.location.hash.replace("#", "") as Page;
  return PAGES.has(h) ? h : "dashboard";
}

function App() {
  const [page, setPage] = useState<Page>(readHash);

  useEffect(() => {
    const onHash = () => setPage(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = (p: Page) => {
    window.location.hash = p;
    setPage(p);
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <Sidebar activePage={page} onNavigate={navigate} />
      <main className="flex-1 overflow-hidden">
        {/* Chat stays mounted so state, SSE, and scroll position survive navigation */}
        <div className={page === "chat" ? "h-full" : "hidden"}>
          <ChatPage />
        </div>
        {page !== "chat" && (
          <div className="h-full overflow-y-auto p-6">{renderPage(page)}</div>
        )}
      </main>
    </div>
  );
}

function renderPage(page: Page) {
  switch (page) {
    case "dashboard":
      return <DashboardPage />;
    case "tasks":
      return <TasksPage />;
    case "agents":
      return <AgentsPage />;
    default:
      return null;
  }
}

export default App;
