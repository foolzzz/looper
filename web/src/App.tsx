import { useState } from "react";
import { Sidebar, type Page } from "@/components/sidebar";
import { DashboardPage } from "@/pages/dashboard";
import { ChatPage } from "@/pages/chat";
import { TasksPage } from "@/pages/tasks";

function App() {
  const [page, setPage] = useState<Page>("dashboard");

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <Sidebar activePage={page} onNavigate={setPage} />
      <main className="flex-1 overflow-y-auto p-6">{renderPage(page)}</main>
    </div>
  );
}

function renderPage(page: Page) {
  switch (page) {
    case "dashboard":
      return <DashboardPage />;
    case "chat":
      return <ChatPage />;
    case "tasks":
      return <TasksPage />;
  }
}

export default App;
