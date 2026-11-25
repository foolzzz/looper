// @vitest-environment happy-dom
import { act, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { Sidebar } from "@/components/sidebar";
import { WaitingGatesProvider } from "@/lib/use-waiting-gates";
import { fetchGates, fetchOverview, fetchProjectsList, fetchTasks } from "@/lib/api";
import { intentsSettingsGet } from "@/lib/api-leads";
import { attentionSettingsGet, fetchAttention } from "@/lib/api-attention";
import { V1Error } from "@/lib/v1";
import { useLedger } from "@/lib/use-ledger";
import { useProjects } from "@/lib/use-projects";
import { WorkPage } from "@/pages/work";
import type { WorkTask } from "@/lib/work";
import frozenNav from "./work-nav-off.html?raw";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchGates: vi.fn(), fetchProjectsList: vi.fn(), fetchOverview: vi.fn(), fetchTasks: vi.fn(),
}));
vi.mock("@/lib/api-leads", () => ({ intentsSettingsGet: vi.fn() }));
vi.mock("@/lib/api-attention", () => ({
  attentionSettingsGet: vi.fn(), fetchAttention: vi.fn(),
  isAttentionOff: (err: unknown) => err instanceof V1Error && err.status === 409,
}));
vi.mock("@/lib/use-ledger", () => ({
  LedgerProvider: ({ children }: { children: ReactNode }) => children,
  useLedger: vi.fn(),
}));
vi.mock("@/lib/use-projects", () => ({ useProjects: vi.fn() }));
vi.mock("@/components/sign-out", () => ({ SignOut: () => null }));
vi.mock("@/components/project-dialog", () => ({ ProjectDialog: () => null }));
vi.mock("@/components/claude-check-banner", () => ({ ClaudeCheckBanner: () => null }));
vi.mock("@/components/drain-banner", () => ({ DrainBanner: () => null }));
vi.mock("@/lib/session", async (original) => ({
  ...await original<typeof import("@/lib/session")>(),
  useSessionEnded: () => false,
}));
vi.mock("@/pages/chat", () => ({ ChatPage: () => null }));
vi.mock("@/pages/dashboard", () => ({ DashboardPage: () => <div>Dashboard view</div> }));
vi.mock("@/pages/gates", () => ({ GatesPage: () => null }));
vi.mock("@/pages/schedules", () => ({ SchedulesPage: () => null }));
vi.mock("@/pages/message-stream", () => ({ MessageStreamPage: () => null }));
vi.mock("@/pages/intents", () => ({ IntentsPage: () => null }));
vi.mock("@/pages/memory", () => ({ MemoryPage: () => null }));
vi.mock("@/pages/needs-you", () => ({ NeedsYouPage: () => null }));
vi.mock("@/pages/ledger", () => ({ LedgerPage: () => null }));
vi.mock("@/pages/settings", () => ({ SettingsPage: () => null }));
vi.mock("@/pages/project", () => ({ ProjectPage: () => null }));
vi.mock("@/pages/agent-history", () => ({
  AgentHistoryPage: () => null,
  historyHref: () => "#history",
  isHistoryRole: () => false,
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FROZEN_NAV = frozenNav.trim();

interface Answer { status: number; body: unknown }
type Handler = (url: string, method: string, body: Record<string, unknown>) => Answer | Promise<Answer>;

let handler: Handler = () => ({ status: 200, body: { status: "ok", result: {} } });
const calls: { url: string; body: Record<string, unknown> }[] = [];

function task(partial: Partial<WorkTask> & Pick<WorkTask, "task_id">): WorkTask {
  return {
    title: "Beta new", project_id: "p-beta", project_name: "Beta", status: "running", group: "in_progress",
    last_activity_at: "2026-11-24T00:00:00Z", last_seq: 7, waiting_gates: 0, needs_you: false, unseen: true, ...partial,
  };
}

const OPEN = [
  task({ task_id: "t-b1" }),
  task({
    task_id: "t-a1", title: "Alpha wait", project_id: "p-alpha", project_name: "Alpha",
    status: "waiting_user", group: "needs_you", needs_you: true, waiting_gates: 2, unseen: false, last_seq: 3,
  }),
];

function workHandler(url: string, method: string, body: Record<string, unknown>): Answer {
  if (method === "GET") return { status: 405, body: { status: "error", error: { code: "invalid", message: "use POST" } } };
  if (url.includes("task_conversations/get")) return { status: 200, body: { status: "ok", result: { enabled: false } } };
  if (url.includes("tasks/work_list")) {
    return { status: 200, body: { status: "ok", result: body.section === "open" ? { items: OPEN, next_cursor: null } : { items: [], schedules: [], next_cursor: null } } };
  }
  if (url.includes("tasks/detail")) return { status: 200, body: { status: "ok", result: OPEN.find(task => task.task_id === body.task_id) } };
  if (url.includes("tasks/activity")) return { status: 200, body: { status: "ok", result: { activity: [{ seq: 7 }] } } };
  if (url.includes("tasks/mark_seen")) return { status: 200, body: { status: "ok", result: {} } };
  if (url.includes("tasks/select")) return { status: 200, body: { status: "ok", result: { kind: "none", searched: {} } } };
  return { status: 200, body: { status: "ok", result: {} } };
}

let host: HTMLDivElement;
let root: Root;

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await Promise.resolve(); });
}
async function render(node: ReactNode) {
  await act(async () => { root.render(node); });
  await settle();
}

beforeEach(() => {
  calls.length = 0;
  handler = workHandler;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url: String(url), body });
    const answer = await handler(String(url), method, body);
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { "Content-Type": "application/json" } });
  }));
  vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], projects_root: "" });
  vi.mocked(fetchGates).mockResolvedValue({
    gates: [], total: 0, limit: 1, offset: 0, next_cursor: null, has_more: false,
    counts: { open: 0, waiting_user: 0, waiting_planner: 0, closed: 0, all: 0 },
  });
  vi.mocked(fetchOverview).mockResolvedValue({} as never);
  vi.mocked(fetchTasks).mockResolvedValue({ tasks: [], next_before: null, counts: {}, hidden_scheduled: 0 });
  vi.mocked(intentsSettingsGet).mockResolvedValue({ enabled: false, subscriptions: [] });
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
  vi.mocked(fetchAttention).mockResolvedValue({ read_at: "", total: 0, sections: [] });
  vi.mocked(useLedger).mockReturnValue({
    enabled: false, count: null, stale: false, error: null, revision: 0, refresh: vi.fn(), changed: vi.fn(),
  });
  vi.mocked(useProjects).mockReturnValue({
    projects: [{ project_id: "p-beta", name: "Beta", work_dir: "", task_count: 1, active_count: 0 } as never],
    projectsRoot: "", error: "", reload: vi.fn(),
  });
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#dashboard");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("Work page shell", () => {
  it("nav hidden when off", async () => {
    const show = async () => {
      await render(
        <WaitingGatesProvider>
          <Sidebar activePage="dashboard" activeSettingsPage={null} activeProjectId={null} onNavigate={vi.fn()}
            onOpenProject={vi.fn()} onOpenSettings={vi.fn()} open onClose={vi.fn()} />
        </WaitingGatesProvider>,
      );
      const nav = host.querySelector("nav");
      expect(nav).not.toBeNull();
      expect(nav!.innerHTML).toBe(FROZEN_NAV);
      expect(nav!.textContent).not.toContain("Work");
    };
    await show();
    act(() => root.unmount());
    root = createRoot(host);
    handler = () => { throw new Error("offline"); };
    await show();
    act(() => root.unmount());
    root = createRoot(host);
    handler = (url) => ({
      status: 404,
      body: { status: "error", error: { code: "not_found", message: `no POST ${url}` } },
    });
    await show();
  });

  it("route redirects when off", async () => {
    let release: ((answer: Answer) => void) | null = null;
    handler = (url, method) => {
      if (url.includes("task_conversations/get")) {
        return new Promise<Answer>((resolve) => { release = resolve; });
      }
      return workHandler(url, method, {});
    };
    window.history.replaceState(null, "", "#work/task/x");
    await render(<App />);
    expect(window.location.hash).toBe("#work/task/x");
    expect(host.textContent).not.toContain("New task");
    await act(async () => { release?.({ status: 200, body: { status: "ok", result: { enabled: false } } }); });
    await settle();
    expect(window.location.hash).toBe("#dashboard");
    expect(host.textContent).toContain("Dashboard view");
    expect(host.querySelector("nav")!.textContent).not.toContain("Work");
  });

  it("selection", async () => {
    const navigated: { sub: string; replace?: boolean }[] = [];
    function Harness({ initial }: { initial: string }) {
      const [sub, setSub] = useState(initial);
      return (
        <WorkPage sub={sub} onNavigate={(next, replace) => {
          navigated.push({ sub: next, replace });
          const hash = next ? `#work/${next}` : "#work";
          if (replace) window.history.replaceState(null, "", hash);
          else window.location.hash = hash;
          setSub(next);
        }} />
      );
    }
    await render(<Harness initial="" />);
    const scroller = host.querySelector('[aria-label="Work tasks"]') as HTMLElement;
    scroller.scrollTop = 120;
    const link = [...host.querySelectorAll("a")].find((a) => a.textContent?.includes("Beta new"))!;
    await act(async () => { link.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    await settle();
    expect(window.location.hash).toBe("#work/task/t-b1");
    expect(scroller.scrollTop).toBe(120);
    const seen = calls.filter((call) => call.url.includes("mark_seen"));
    expect(seen.map((call) => call.body)).toEqual([{ task_id: "t-b1", seq: 7 }]);
    expect(window.sessionStorage.getItem("looper.work.scroll")).toBe("120");
    expect(window.sessionStorage.getItem("looper.work.selection")).toBe("t-b1");

    act(() => root.unmount());
    root = createRoot(host);
    navigated.length = 0;
    await render(<Harness initial="task/t-b1" />);
    expect(host.querySelector("h1")!.textContent).toBe("Beta new");
    expect(host.querySelector('a[href="#tasks/t-b1"]')!.textContent).toBe("Open the task page");
    expect(host.querySelector('a[href="#chat?target=planner-t-b1"]')!.textContent).toBe("Talk to its Planner");
    expect((host.querySelector('[aria-label="Work tasks"]') as HTMLElement).scrollTop).toBe(120);

    act(() => root.unmount());
    root = createRoot(host);
    navigated.length = 0;
    window.history.replaceState(null, "", "#work");
    await render(<Harness initial="" />);
    expect(navigated).toContainEqual({ sub: "task/t-b1", replace: true });
  });

  it("accessibility", async () => {
    await render(<WorkPage sub="" onNavigate={() => {}} />);
    const open = host.querySelector('[aria-label="Open tasks"]')!;
    const row = [...open.querySelectorAll("a")].find((a) => a.textContent?.includes("Beta new")) as HTMLAnchorElement;
    const button = [...host.querySelectorAll("button")].find((item) => item.textContent === "New task") as HTMLButtonElement;
    for (const el of [row, button]) {
      el.focus();
      expect(document.activeElement).toBe(el);
      expect(el.tabIndex).toBeGreaterThanOrEqual(0);
      expect(el.className).toContain("focus-visible:ring-ring");
    }
    expect(row.textContent).toContain("Running");
    expect(row.textContent).toContain("Unseen");
    expect(row.querySelector("svg")).not.toBeNull();
    button.focus();
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }));
    });
    const dialog = host.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog).not.toBeNull();
    const name = dialog.getAttribute("aria-labelledby");
    expect(name).toBeTruthy();
    expect(host.querySelector(`#${name}`)!.textContent).toBe("Find a task");
  });
});

