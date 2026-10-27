// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import App from "./App";

vi.mock("@/components/sidebar", () => ({ Sidebar: () => null, SIDEBAR_ID: "sidebar" }));
vi.mock("@/components/claude-check-banner", () => ({ ClaudeCheckBanner: () => null }));
vi.mock("@/lib/session", () => ({ useSessionEnded: () => false }));
vi.mock("@/pages/chat", () => ({ ChatPage: () => null }));
vi.mock("@/pages/dashboard", () => ({ DashboardPage: () => <div>Dashboard view</div> }));
vi.mock("@/pages/runs", () => ({ RunsPage: ({ sub }: { sub: string }) => <div>Runs {sub}</div> }));
vi.mock("@/pages/tasks", () => ({ TasksPage: ({ query }: { query: string }) => <div>Tasks {query}</div> }));
vi.mock("@/pages/settings", () => ({ SettingsPage: ({ sub }: { sub: string }) => <div>Settings {sub}</div> }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it.each([
  ["runs/manager?task=t1", "Runs manager?task=t1", "runs/manager?task=t1"],
  ["tasks?project=p1&manager=m1", "Tasks project=p1&manager=m1", "tasks?project=p1&manager=m1"],
  ["settings/providers?focus=p1", "Settings providers?focus=p1", "settings/providers?focus=p1"],
  ["settings/unknown", "Settings roles", "settings/roles"],
  ["settings/provider/old", "Settings roles", "settings/roles"],
  ["settings/defaults", "Settings roles", "settings/roles"],
  ["settings/mcp/old", "Settings roles", "settings/roles"],
  ["settings/channel/old", "Settings roles", "settings/roles"],
  ["agents/manager/sessions", "Dashboard view", "agents/manager/sessions"],
])("routes #%s without retired aliases", async (hash, content, finalHash) => {
  window.history.replaceState(null, "", `#${hash}`);
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<App />));
    expect(container.textContent).toContain(content);
    expect(window.location.hash).toBe(`#${finalHash}`);
  } finally {
    act(() => root.unmount());
  }
});
