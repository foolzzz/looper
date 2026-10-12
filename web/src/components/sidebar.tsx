import { LayoutDashboard, MessageSquare, ListTodo, Bot, Sliders } from "lucide-react";
import { cn } from "@/lib/utils";

export type Page = "dashboard" | "chat" | "tasks" | "agents" | "settings";

const NAV_ITEMS: { page: Page; label: string; icon: React.ElementType }[] = [
  { page: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { page: "chat", label: "Chat", icon: MessageSquare },
  { page: "tasks", label: "Tasks", icon: ListTodo },
  { page: "agents", label: "Agents", icon: Bot },
  { page: "settings", label: "Settings", icon: Sliders },
];

interface SidebarProps {
  activePage: Page;
  onNavigate: (page: Page) => void;
}

export function Sidebar({ activePage, onNavigate }: SidebarProps) {
  return (
    <aside className="flex h-screen w-56 flex-col border-r border-border bg-card">
      <div className="flex h-14 items-center gap-2 px-4 border-b border-border">
        <Bot className="h-6 w-6 text-primary" />
        <span className="text-lg font-semibold tracking-tight">LoopX</span>
      </div>

      <nav className="flex-1 px-2 py-3 space-y-0.5">
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

      <div className="border-t border-border px-4 py-3">
        <p className="text-xs text-muted-foreground">LoopX Agent System</p>
      </div>
    </aside>
  );
}
