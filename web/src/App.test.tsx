// @vitest-environment happy-dom
import { act, useEffect, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import App from "./App";
import { attentionSettingsGet, fetchAttention } from "@/lib/api-attention";

vi.mock("@/lib/api-attention", () => ({
  attentionSettingsGet: vi.fn(),
  fetchAttention: vi.fn().mockResolvedValue({ read_at: "2026-11-20T08:00:00Z", total: 0, sections: [] }),
  isAttentionOff: () => false,
}));

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchGates: vi.fn().mockResolvedValue({ counts: { waiting_user: 0 } }),
}));
vi.mock("@/components/sidebar", () => ({ Sidebar: () => null, SIDEBAR_ID: "sidebar" }));
vi.mock("@/components/claude-check-banner", () => ({ ClaudeCheckBanner: () => null }));
vi.mock("@/components/drain-banner", () => ({ DrainBanner: () => null }));
vi.mock("@/lib/session", () => ({ useSessionEnded: () => false }));
const chatMounted = vi.hoisted(() => vi.fn());
vi.mock("@/pages/chat", () => ({ ChatPage: () => {
  useEffect(() => { chatMounted(); }, []);
  return <div data-chat-mounted />;
} }));
vi.mock("@/lib/use-ledger", () => ({ LedgerProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/pages/ledger", () => ({ LedgerPage: ({ query, onQuery }: {
  query: string; onQuery: (query: string, replace?: boolean) => void;
}) => <div>账本 {query}<button onClick={() => onQuery("status=done")}>关闭详情</button></div> }));
vi.mock("@/pages/dashboard", () => ({ DashboardPage: () => <div>Dashboard view</div> }));
vi.mock("@/pages/agent-history", async (original) => ({
  ...await original<typeof import("@/pages/agent-history")>(),
  AgentHistoryPage: ({ sub, query }: { sub: string; query?: string }) => <div>History {sub}|{query ?? ""}</div>,
}));
vi.mock("@/pages/tasks", () => ({ TasksPage: ({ query }: { query: string }) => <div>Tasks {query}</div> }));
vi.mock("@/pages/settings", () => ({ SettingsPage: ({ sub }: { sub: string }) => <div>Settings {sub}</div> }));
vi.mock("@/pages/message-stream", () => ({ MessageStreamPage: ({ query }: { query?: string }) => <div>Messages {query ?? ""}</div> }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("opens explicit collection query without changing the default messages page", async () => {
  const host = document.createElement("div");
  const root = createRoot(host);
  try {
    window.history.replaceState(null, "", "#messages?collection=synthetic-source");
    await act(async () => root.render(<App />));
    expect(host.textContent).toContain("Messages collection=synthetic-source");
    await act(async () => { window.location.hash = "#messages"; window.dispatchEvent(new Event("hashchange")); });
    expect(host.textContent).toContain("Messages ");
    expect(host.textContent).not.toContain("collection=");
  } finally { act(() => root.unmount()); }
});

it("opens #todo directly with attention off without requesting a summary", async () => {
  let resolveSettings!: (settings: { enabled: boolean }) => void;
  vi.mocked(attentionSettingsGet).mockReturnValue(new Promise((resolve) => { resolveSettings = resolve; }));
  window.history.replaceState(null, "", "#todo");
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<App />));
    expect(fetchAttention).not.toHaveBeenCalled();
    await act(async () => { resolveSettings({ enabled: false }); });
    expect(attentionSettingsGet).toHaveBeenCalledOnce();
    expect(container.querySelector('[role="status"]')?.textContent).toContain("This summary is off");
    expect(fetchAttention).not.toHaveBeenCalled();
    expect(container.querySelector("section")).toBeNull();
  } finally {
    act(() => root.unmount());
    vi.clearAllMocks();
  }
});

it.each([
  ["tasks?project=p1&manager=m1", "Tasks project=p1&manager=m1", "tasks?project=p1&manager=m1"],
  ["settings/providers?focus=p1", "Settings providers?focus=p1", "settings/providers?focus=p1"],
  ["settings/unknown", "Settings roles", "settings/roles"],
  ["settings/provider/old", "Settings roles", "settings/roles"],
  ["settings/defaults", "Settings roles", "settings/roles"],
  ["settings/mcp/old", "Settings roles", "settings/roles"],
  ["settings/channel/old", "Settings roles", "settings/roles"],
  ["agents/manager/sessions", "Dashboard view", "agents/manager/sessions"],
])("routes #%s", async (hash, content, finalHash) => {
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

it("opens ledger links without changing Needs you or unmounting chat", async () => {
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
  chatMounted.mockClear();
  window.history.replaceState(null, "", "#ledger?status=done&item=L-0012");
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<App />));
    expect(container.textContent).toContain("账本 status=done&item=L-0012");
    const chat = container.querySelector("[data-chat-mounted]");
    expect(chatMounted).toHaveBeenCalledOnce();
    await act(async () => container.querySelector<HTMLButtonElement>("button:last-child")!.click());
    expect(window.location.hash).toBe("#ledger?status=done");
    expect(container.querySelector("[data-chat-mounted]")).toBe(chat);
    await act(async () => {
      window.location.hash = "todo";
      window.dispatchEvent(new Event("hashchange"));
    });
    expect(container.textContent).toContain("This summary is off");
    expect(window.location.hash).toBe("#todo");
    expect(container.querySelector("[data-chat-mounted]")).toBe(chat);
    expect(chatMounted).toHaveBeenCalledOnce();
    await act(async () => {
      window.location.hash = "ledger?status=done&item=L-0012";
      window.dispatchEvent(new Event("hashchange"));
    });
    expect(container.textContent).toContain("账本 status=done&item=L-0012");
  } finally { act(() => root.unmount()); }
});

it.each([
  ["runs", "History |", "history"],
  ["runs/", "History |", "history"],
  ["runs?task=t1", "History |", "history"],
  ["runs/not-a-role", "History |", "history"],
  ["runs/manager", "History |role=manager", "history?role=manager"],
  ["runs/manager?task=t1", "History |role=manager", "history?role=manager"],
  ["runs/planner/extra", "History |role=planner", "history?role=planner"],
  ["runs/processor", "History |role=processor", "history?role=processor"],
])("redirects #%s to History", async (hash, content, finalHash) => {
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
