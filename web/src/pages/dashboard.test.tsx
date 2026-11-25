// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "@/pages/dashboard";
import { Sidebar } from "@/components/sidebar";
import { WaitingGatesProvider } from "@/lib/use-waiting-gates";
import { fetchManagerMetrics, type ManagerMetrics } from "@/lib/manager-metrics";
import { fetchStatusCard, type StatusSection } from "@/lib/status-card";
import { V1Error } from "@/lib/v1";
import { taskConversationsGet } from "@/lib/work";
import { fetchGates, fetchOverview, fetchProjectsList, fetchTasks, fetchTaskDetail, postGateMessage, GATE_MESSAGE_MAX_CHARS,
  type GateList, type AgentProcess, type Overview, type TaskDetail, type UserGate, type GatePosted } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchOverview: vi.fn(), fetchTasks: vi.fn(), fetchTaskDetail: vi.fn(), postGateMessage: vi.fn(),
  fetchGates: vi.fn(), fetchProjectsList: vi.fn(),
}));
vi.mock("@/lib/manager-metrics", () => ({ fetchManagerMetrics: vi.fn() }));
vi.mock("@/lib/status-card", async (original) => ({
  ...await original<typeof import("@/lib/status-card")>(), fetchStatusCard: vi.fn(),
}));
vi.mock("@/components/manager-sessions", () => ({ ManagerSessions: () => null }));
vi.mock("@/components/task-quality-section", () => ({ TaskQualitySection: () => null }));
vi.mock("@/components/sign-out", () => ({ SignOut: () => null }));
vi.mock("@/lib/api-cost-guard", () => ({
  fetchCostGuard: vi.fn(async () => ({
    mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, counters: [], recent: [],
    recommended_daily_usd: null, recommended_daily_equivalent_usd: null, problem: null,
  })),
  fetchCostHolds: vi.fn(async () => ({ holds: [] })),
}));
vi.mock("@/components/project-dialog", () => ({ ProjectDialog: () => null }));
vi.mock("@/lib/work", () => ({ taskConversationsGet: vi.fn(async () => ({ enabled: false })) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const gate = (step: string, extra: Partial<UserGate> = {}): UserGate => ({
  step, step_id: step, state: "open", topic: `Topic ${step}`, findings: "", summary: null, resolution: "",
  agenda: [
    { id: "q1", question: "Already answered", options: [], status: "resolved", answer: "Yes" },
    { id: "q2", question: `Question ${step}`, options: [], status: "open", answer: "" },
    { id: "q3", question: "Later question", options: [], status: "open", answer: "" },
  ],
  ended_by_user: false, plan_change: null, depends_on: [], waiting_on: "user", not_delivered: 0,
  retry_at: null, turns: 0, reminders: 0, opened_at: "2026-10-20T10:00:00Z", first_reply_at: null,
  closed_at: null, channel_posts: null, ...extra,
});
const METRICS_OFF: ManagerMetrics = {
  enabled: false, window_days: 7, turns: { by_delivery: {} },
  ack: { p50_ms: null, p95_ms: null, by_chat: {} }, first_reply: { p50_ms: null, p95_ms: null, by_chat: {} },
  empty: { count: 0, ratio: null }, proactive: { per_day: [], reply_rate: { all: null, by_source: {} } },
};
const task = (id: string, gates: UserGate[]) => ({ task_id: id, name: `Task ${id}`, group: "needs_you", gates } as TaskDetail);

describe("Dashboard waiting gates", () => {
  let container: HTMLDivElement;
  let root: Root;
  let details: TaskDetail[];
  let overview: Overview;
  let waitingGates: number;
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.setSystemTime(new Date("2026-10-20T10:12:00Z"));
    vi.resetAllMocks();
    vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: false });
    vi.mocked(fetchManagerMetrics).mockResolvedValue(METRICS_OFF);
    vi.mocked(fetchStatusCard).mockRejectedValue(new V1Error(409, "conflict", "off"));
    window.localStorage.clear();
    details = [];
    waitingGates = 1;
    overview = {
      tasks: { needs_you: 1, other_statuses: {} }, agents: { manager: null, planners: [], workers: [], reviewers: [] },
      projects: [], idle_projects: 0, health: { unhealthy: [] },
      schedules: { total: 0, enabled: 0, next: [], failing: [], skipped_today: {} },
      spend_today: { since: "", provider_priced_usd: 0, equivalent_usd: 0, runs: 0, running: 0, unpriced_runs: 0 },
    processes: [], tpm: { total: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, window_seconds: 60 },
    } as unknown as Overview;
    vi.mocked(fetchOverview).mockImplementation(async () => ({ ...overview }));
    vi.mocked(fetchGates).mockImplementation(async () => ({ counts: { waiting_user: waitingGates } } as GateList));
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], projects_root: "" });
    vi.mocked(fetchTasks).mockImplementation(async () => ({ tasks: details.slice(0, 10), next_before: details.length > 10 ? "next-page" : null, counts: { waiting_user: details.length }, hidden_scheduled: 0 }));
    vi.mocked(fetchTaskDetail).mockImplementation(async (id) => details.find((t) => t.task_id === id)!);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
  const show = async (withSidebar = false) => { await act(async () => root.render(<WaitingGatesProvider>
    {withSidebar && <Sidebar activePage="dashboard" activeSettingsPage={null} activeProjectId={null}
      onNavigate={vi.fn()} onOpenProject={vi.fn()} onOpenSettings={vi.fn()} open onClose={vi.fn()} />}
    <DashboardPage />
  </WaitingGatesProvider>)); };
  const section = () => container.querySelector('section[aria-label="Waiting for you"]');
  const answer = async (text: string) => {
    const input = section()!.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    return input;
  };

  const proc = (extra: Partial<AgentProcess>): AgentProcess => ({
    agent_id: "worker-a", role: "worker", name: null, task_id: "a", task_name: "Build parser",
    project_id: "project-a", project_name: "Compiler", step: "parse", instruction: "Implement parsing\nMore detail",
    runtime: "Claude Code", model: "sonnet", pid: 4242, state: "running", elapsed_seconds: 120,
    tpm: { total: 12_340, input: 1000, output: 2000, cache_read: 9000, cache_creation: 340 }, ...extra,
  });
  const agentsSection = () => container.querySelector('section[aria-label="Agents running now"]')!;

  it("lists a running process with its task, runtime, pid, time and TPM, and the total in the header", async () => {
    overview.processes = [proc({ task_role: { id: "developer", kind: "work", version: 1 } })];
    overview.tpm = { total: 15_000, input: 1, output: 2, cache_read: 3, cache_creation: 4, window_seconds: 60 };
    await show();
    const text = agentsSection().textContent!;
    expect(agentsSection().firstElementChild!.textContent).toBe("Agents running now1 running · 15k TPM");
    for (const part of ["Worker", "developer", "Claude Code · sonnet", "4242", "2 min", "12.3k", "Implement parsing"]) expect(text).toContain(part);
    expect(text).not.toContain("More detail");
    expect(agentsSection().querySelector('a[href="#tasks/a"]')!.textContent).toBe("Compiler · Build parser");
    expect(agentsSection().querySelector('a[href="#history/worker-a?task=a"]')).not.toBeNull();
    expect(agentsSection().querySelector('[title*="Cache read 9,000"]')).not.toBeNull();
  });

  it("shows no Status card while the switch is off and keeps the other sections", async () => {
    await show();
    expect(container.querySelector('section[aria-label="Status"]')).toBeNull();
    expect(fetchStatusCard).toHaveBeenCalledTimes(1);
    expect(container.querySelector('nav[aria-label="Tasks by group"]')).not.toBeNull();
  });

  it.each(["tasks", "schedules"] as const)("shows the first status snapshot when %s breaks the overview, and retries", async (source) => {
    vi.mocked(fetchOverview).mockRejectedValue(new Error(`${source} unavailable`));
    const sections: StatusSection[] = [
      { name: "manager", state: "ok", error: null, managers: [{ manager_id: "main", queued: 0, sessions: [] }] },
      { name: "tasks", state: "ok", error: null, count: 2 },
      { name: "schedules", state: "ok", error: null, running: [], next: [] },
    ];
    vi.mocked(fetchStatusCard).mockResolvedValue({ read_at: "2026-10-20T10:12:00Z", text: "",
      sections: sections.map((s) => s.name === source ? { ...s, state: "error", error: `${source} unavailable` } : s) });
    await show();
    const card = container.querySelector('section[aria-label="Status"]')!;
    expect(card).not.toBeNull();
    expect(card.querySelector('[role="alert"]')!.textContent).toContain(`${source} unavailable`);
    expect(card.textContent).toContain("Idle");
    expect(card.querySelector(`a[href="#${source === "tasks" ? "schedules" : "tasks"}"]`)).not.toBeNull();

    vi.mocked(fetchOverview).mockResolvedValue(overview);
    vi.mocked(fetchStatusCard).mockResolvedValue({ read_at: "2026-10-20T10:12:15Z", text: "", sections });
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(container.querySelector('section[aria-label="Status"]')).toBe(card);
    expect(card.querySelector('[role="alert"]')).toBeNull();
    expect(card.textContent).toContain("2 running");
    expect(container.querySelector('nav[aria-label="Tasks by group"]')).not.toBeNull();
    expect(fetchStatusCard).toHaveBeenCalledTimes(2);
  });

  it("shows each error on first load when all status sources fail", async () => {
    vi.mocked(fetchOverview).mockRejectedValue(new Error("sources unavailable"));
    vi.mocked(fetchStatusCard).mockResolvedValue({ read_at: "2026-10-20T10:12:00Z", text: "",
      sections: ["manager", "waiting", "reminders", "tasks", "schedules"].map((name) =>
        ({ name, state: "error", error: `${name} unavailable` }) as StatusSection) });
    await show();
    const card = container.querySelector('section[aria-label="Status"]')!;
    expect(card).not.toBeNull();
    expect(card.querySelectorAll('[role="alert"]')).toHaveLength(5);
    for (const name of ["manager", "waiting", "reminders", "tasks", "schedules"])
      expect(card.textContent).toContain(`${name} unavailable`);
    expect(card.textContent).not.toContain("0 running");
  });

  it("shows a status error even when both first requests reject", async () => {
    vi.mocked(fetchOverview).mockRejectedValue(new Error("overview unavailable"));
    vi.mocked(fetchStatusCard).mockRejectedValue(new Error("snapshot unavailable"));
    await show();
    const card = container.querySelector('section[aria-label="Status"]')!;
    expect(card).not.toBeNull();
    expect(card.querySelector('[role="alert"]')!.textContent).toContain("snapshot unavailable");
  });

  it("reads the status while the first overview is still pending", async () => {
    vi.mocked(fetchOverview).mockReturnValue(new Promise(() => {}));
    vi.mocked(fetchStatusCard).mockRejectedValue(new Error("snapshot unavailable"));
    await show();
    expect(container.textContent).toContain("Connecting...");
    expect(container.querySelector('section[aria-label="Status"] [role="alert"]')!.textContent)
      .toContain("snapshot unavailable");
  });

  it("keeps the off card hidden without restarting its polling when the overview recovers", async () => {
    vi.mocked(fetchOverview).mockRejectedValueOnce(new Error("overview unavailable"));
    await show();
    expect(container.textContent).toContain("Connection failed");
    expect(fetchStatusCard).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(container.querySelector('section[aria-label="Status"]')).toBeNull();
    expect(fetchStatusCard).toHaveBeenCalledTimes(1);
    expect(container.querySelector('nav[aria-label="Tasks by group"]')).not.toBeNull();
  });

  const metricsSection = () => container.querySelector('section[aria-label="Manager"]')!;
  /** The browser-local date `back` days before now, as the group names days. */
  const day = (back: number) => {
    const now = new Date();
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  it("shows the Manager's metrics with a dash for what was not measured, and refetches for 24 hours", async () => {
    vi.mocked(fetchManagerMetrics).mockResolvedValue({
      ...METRICS_OFF, enabled: true, turns: { by_delivery: { reply: 38, empty: 2 } },
      ack: { p50_ms: 1234, p95_ms: 3400, by_chat: {} }, empty: { count: 2, ratio: 0.05 },
      proactive: {
        per_day: [{ date: day(1), by_source: { task_notice: 3 } },
                  { date: day(0), by_source: { task_notice: 2, gate: 2 } }],
        reply_rate: { all: 0.5, by_source: { task_notice: 0.6, gate: null } },
      },
    });
    await show();
    const text = metricsSection().textContent!;
    for (const part of ["1.2 s", "P95 3.4 s", "2 · 5%", "turns that delivered no reply, of 40", "1.0 / day", "reply rate 50%",
                        "task notices 2 · gates 2", "task notices 60% · gates —"]) expect(text).toContain(part);
    // The first reply was not measured: a dash, never 0.
    const tiles = [...metricsSection().querySelectorAll("p")].map((p) => p.textContent);
    expect(tiles.slice(tiles.indexOf("First reply (P50)") + 1, tiles.indexOf("First reply (P50)") + 3))
      .toEqual(["—", "P95 —"]);
    // Every local day of the 7-day window, today and 7 back, a day without messages too.
    const metricsTable = [...metricsSection().querySelectorAll("table")].find((table) => table.textContent?.includes(day(0)))!;
    const rows = [...metricsTable.querySelectorAll("tbody tr")].map((r) => [...r.children].map((c) => c.textContent));
    expect(rows.map((r) => r[0])).toEqual([0, 1, 2, 3, 4, 5, 6, 7].map(day));
    expect(rows[1]).toEqual([day(1), "task notices 3", "3"]);
    expect(rows[2]).toEqual([day(2), "—", "0"]);
    expect(vi.mocked(fetchManagerMetrics).mock.calls.at(-1)![0]).toBe(7);
    await act(async () => (metricsSection().querySelector('button[aria-pressed="false"]') as HTMLButtonElement).click());
    expect(vi.mocked(fetchManagerMetrics).mock.calls.at(-1)![0]).toBe(1);
    expect(window.localStorage.getItem("looper.dashboard.managerMetricsDays")).toBe("1");
  });

  it("shows the writes by category with the failed ones", async () => {
    vi.mocked(fetchManagerMetrics).mockResolvedValue({
      ...METRICS_OFF, enabled: true,
      actions: { by_category: { create: { ok: 3 }, control: { ok: 2, error: 1 } } },
    });
    await show();
    expect(metricsSection().querySelector('[aria-label="Writes"]')!.textContent)
      .toBe("Writes: created 3 · controlled 2 (1 failed)");
  });

  it("shows no writes line while no write has a category", async () => {
    vi.mocked(fetchManagerMetrics).mockResolvedValue({ ...METRICS_OFF, enabled: true, actions: { by_category: {} } });
    await show();
    expect(metricsSection().querySelector('[aria-label="Writes"]')).toBeNull();
    expect(metricsSection().textContent).toContain("Ack");
  });

  it("says the metrics are off, with the way to Settings → Chat, and shows no figures", async () => {
    await show();
    expect(metricsSection().textContent).toContain("Metrics are off");
    expect(metricsSection().querySelector('a[href="#settings/chat"]')).not.toBeNull();
    expect(metricsSection().textContent).not.toContain("Ack");
    expect(metricsSection().querySelector('[aria-label="Metrics window"]')).toBeNull();  // nothing to window
  });

  it("keeps the rest of the page when the metrics cannot be read", async () => {
    vi.mocked(fetchManagerMetrics).mockRejectedValue(new Error("forbidden"));
    await show();
    expect(metricsSection().querySelector('[role="alert"]')!.textContent).toContain("forbidden");
    expect(agentsSection().textContent).toContain("No agent is running now");
  });

  it("links each count to the Tasks list with scheduled tasks included, so the list matches the count", async () => {
    await show();
    const tiles = [...container.querySelectorAll<HTMLAnchorElement>('a[href^="#tasks?group="]')];
    expect(tiles.length).toBeGreaterThan(0);
    for (const tile of tiles) expect(new URLSearchParams(tile.getAttribute("href")!.split("?")[1]).get("scheduled")).toBe("include");
  });

  it("shows a dash for a running process without usage, and the waiting badge for a waiting one", async () => {
    overview.processes = [
      proc({ tpm: null }),
      proc({ agent_id: "worker-b", state: "waiting", runtime: null, model: null, pid: null, tpm: null, elapsed_seconds: 0 }),
    ];
    await show();
    const [running, waiting] = [...agentsSection().querySelectorAll('[role="rowgroup"] [role="row"]')];
    expect(running.querySelector('[title="Usage is reported when a turn ends"]')!.textContent).toBe("—");
    expect(waiting.textContent).toContain("waiting");
    expect(waiting.querySelector('[title="Usage is reported when a turn ends"]')).toBeNull();
    expect(waiting.textContent).toContain("—");
    expect(agentsSection().firstElementChild!.textContent).toContain("1 running · 1 waiting");
  });

  it("says when no agent is running", async () => {
    overview.processes = [];
    await show();
    expect(agentsSection().textContent).toContain("No agent is running now");
    expect(agentsSection().querySelector('[role="table"]')).toBeNull();
  });

  it("keeps the task name beside a long project name on a narrow screen", async () => {
    vi.stubGlobal("innerWidth", 390);
    const project = "A very long project name that would fill the entire row on a phone";
    overview.processes = [proc({ agent_id: "planner-a", role: "planner", project_name: project, step: null, instruction: null })];
    await show();
    const link = agentsSection().querySelector('a[href="#tasks/a"]')!;
    const projectSpan = link.querySelector(`span[title="${project}"]`)!;
    const taskSpan = link.querySelector('span[title="Build parser"]')!;
    expect(projectSpan.className).toContain("max-w-[40%]");
    expect(projectSpan.className).toContain("shrink-0");
    expect(taskSpan.className).toContain("min-w-0");
    expect(taskSpan.className).toContain("truncate");
  });

  it("names the busy and idle Managers without a row below the table, not one with a row or a stopped one", async () => {
    overview.agents.managers = [
      { agent_id: "manager", name: "Manager", state: "busy" },
      { agent_id: "manager-s", name: "Starting manager", state: "busy" },
      { agent_id: "manager-a", name: "Review manager", state: "idle" },
      { agent_id: "manager-b", name: "Old manager", state: "stopped" },
    ];
    overview.processes = [proc({ agent_id: "manager", role: "manager", name: "Manager", task_id: null, task_name: null,
      project_id: null, project_name: null, step: null, instruction: null })];
    await show();
    expect(agentsSection().querySelector("p.mt-3")!.textContent).toBe("Busy: Starting manager · Idle: Review manager");
    expect(agentsSection().querySelector('a[href="#history/manager"]')).not.toBeNull();
  });

  it.each([false, true])("keeps the overlap warning distinct and hides absent alerts: %s", async (alert) => {
    overview.schedules = { total: 1, enabled: 1, next: [], skipped_today: {}, failing: alert ? [{
      schedule_id: "schedule", name: "Check", project_id: "project", project_name: "Example", timezone: "UTC",
      consecutive_failures: 0, overlap_since: "2026-10-20T10:00:00Z", deadline_since: "2026-10-20T10:00:00Z",
    }] : [] } as unknown as Overview["schedules"];
    await show();
    const overlap = [...container.querySelectorAll("span")].find((el) => el.textContent?.startsWith("Skipping: the previous run"));
    if (alert) {
      expect(overlap?.className).toContain("text-warning");
      expect([...container.querySelectorAll("span")].find((el) => el.textContent?.startsWith("Missed its success deadline"))?.className).toContain("text-destructive");
    } else {
      expect(overlap).toBeUndefined();
      expect(container.textContent).toContain("No failing schedules");
    }
  });

  it.each(["no tasks", "planner", "closed"])("hides the section with no user-waiting gate: %s", async (state) => {
    if (state === "no tasks") overview.tasks.needs_you = 0;
    else details = [task("a", [gate("g1", state === "planner"
      ? { state: "discussing", waiting_on: "planner" } : { state: "passed", waiting_on: null })])];
    await show();
    expect(section()).toBeNull();
    if (state === "no tasks") expect(fetchTasks).not.toHaveBeenCalled();
    else expect(fetchTaskDetail).toHaveBeenCalledWith("a");
  });

  it("shows each waiting gate, newest first, with only its first open question", async () => {
    details = [task("a", [gate("older"), gate("pending", { state: "pending" })]),
      task("b", [gate("newer", { state: "discussing", opened_at: "2026-10-20T10:10:00Z" })])];
    await show();
    expect(fetchTasks).toHaveBeenCalledWith({ group: "needs_you", limit: 10 });
    const cards = section()!.querySelectorAll("article");
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain("Topic newer");
    expect(cards[0].textContent).toContain("Question newer");
    expect(cards[1].textContent).toContain("12 min");
    expect(section()!.textContent).not.toContain("Already answered");
    expect(section()!.textContent).not.toContain("Later question");
    expect(cards[0].querySelector('a')?.getAttribute("href")).toBe("#tasks/b");
    expect([...cards[0].querySelectorAll("a")].find((a) => a.textContent === "Open the discussion")?.getAttribute("href"))
      .toBe("#tasks/b?gate=newer");
    expect(section()!.textContent).not.toContain("End discussion");
  });

  it("sends the task, step and unmodified text, disables controls, then refreshes the card", async () => {
    details = [task("a", [gate("g1")])];
    let resolve!: (posted: GatePosted) => void;
    vi.mocked(postGateMessage).mockImplementation(() => new Promise((done) => { resolve = done; }));
    await show();
    const input = await answer("  Use SQLite  ");
    expect(input.maxLength).toBe(GATE_MESSAGE_MAX_CHARS);
    const calls = vi.mocked(fetchTaskDetail).mock.calls.length;
    await act(async () => input.form!.requestSubmit());
    expect(postGateMessage).toHaveBeenCalledWith("a", "g1", { text: "  Use SQLite  " });
    expect(input.disabled).toBe(true);
    expect(section()!.querySelector("button")!.disabled).toBe(true);
    details = [task("a", [gate("g1", { topic: "Updated topic" })])];
    await act(async () => resolve({ gate: details[0].gates[0] } as GatePosted));
    expect(fetchTaskDetail).toHaveBeenCalledTimes(calls + 1);
    expect(section()!.textContent).toContain("Updated topic");
    expect(input.value).toBe("");
  });

  it("keeps the draft and shows a send error inside its card", async () => {
    details = [task("a", [gate("g1"), gate("g2")])];
    vi.mocked(postGateMessage).mockRejectedValue(new Error("Could not send"));
    await show();
    const input = await answer("My answer");
    await act(async () => input.form!.requestSubmit());
    expect(input.closest("article")!.querySelector('[role="alert"]')?.textContent).toContain("Could not send");
    expect(section()!.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(input.value).toBe("My answer");
    expect(input.disabled).toBe(false);
  });

  it("limits cards to 10 gates and links to the remaining ones", async () => {
    details = [task("a", Array.from({ length: 13 }, (_, i) => gate(`g${i}`)))];
    await show();
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    const more = section()!.querySelector('a[href="#tasks?group=needs_you&scheduled=include"]');
    expect(more?.textContent).toBe("and 3 more");
  });

  it("keeps the sent reply until the Planner answers, even outside the needs-you page", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("My answer");
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());
    expect(section()!.textContent).toContain("You replied: My answer");
    expect(section()!.textContent).toContain("Planner answering");
    const pending = details[0];
    details = [];
    overview.tasks.needs_you = 0;
    vi.mocked(fetchTaskDetail).mockResolvedValue(pending);
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(section()!.textContent).toContain("You replied: My answer");
    expect(fetchTaskDetail).toHaveBeenLastCalledWith("a");
    vi.mocked(fetchTaskDetail).mockResolvedValue(task("a", [gate("g1", {
      state: "passed", waiting_on: null, turns: 1,
    })]));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()).toBeNull();
  });

  it.each(["opened", "answered"])("rereads once when a gate is %s without changing the needs-you task count", async (change) => {
    details = [task("a", change === "opened" ? [gate("g1")] : [gate("g1"), gate("g2")])];
    waitingGates = details[0].gates.length;
    await show(true);
    expect(fetchGates).toHaveBeenCalledTimes(1);
    expect(fetchGates).toHaveBeenCalledWith({ state: "open", waitingOn: "user", limit: 1 });
    expect(fetchTasks).toHaveBeenCalledTimes(1);
    expect(section()!.querySelectorAll("article")).toHaveLength(waitingGates);

    details = [task("a", change === "opened" ? [gate("g1"), gate("g2")] : [gate("g1")])];
    waitingGates = details[0].gates.length;
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchGates).toHaveBeenCalledTimes(2);
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(2);
    expect(section()!.querySelectorAll("article")).toHaveLength(waitingGates);
    expect(container.querySelector(`[aria-label="${waitingGates} ${waitingGates === 1 ? "gate" : "gates"} waiting for you"]`))
      .toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchGates).toHaveBeenCalledTimes(3);
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(2);
  });

  it("does not reread waiting gates for unchanged task and gate counts", async () => {
    details = [task("a", [gate("g1")])];
    await show(true);
    const input = await answer("Draft");
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(fetchOverview).toHaveBeenCalledTimes(8); // The sidebar also reads the version once.
    expect(fetchGates).toHaveBeenCalledTimes(4);
    expect(fetchTasks).toHaveBeenCalledTimes(1);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(1);
    overview.tasks.needs_you = 2;
    details.push(task("b", [gate("g2")]));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelectorAll("article")).toHaveLength(2);
    expect(input.value).toBe("Draft");
  });

  it("falls back to a full read only every 30 s when the waiting-gate count is unavailable", async () => {
    vi.mocked(fetchGates).mockRejectedValue(new Error("Offline"));
    details = [task("a", [gate("g1")])];
    await show(true);
    details[0].gates.push(gate("g2"));
    await act(async () => vi.advanceTimersByTimeAsync(25_000));
    expect(fetchTasks).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelectorAll("article")).toHaveLength(2);
    await act(async () => vi.advanceTimersByTimeAsync(25_000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(3);
    overview.tasks.needs_you = 0;
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(fetchTasks).toHaveBeenCalledTimes(3);
    expect(section()).toBeNull();
  });

  it("keeps a fallback error visible until the next 30 s read succeeds", async () => {
    vi.mocked(fetchGates).mockRejectedValue(new Error("Count unavailable"));
    details = [task("a", [gate("g1")])];
    await show();
    vi.mocked(fetchTasks).mockRejectedValueOnce(new Error("Offline"));
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelector('[role="alert"]')?.textContent).toContain("Offline");
    await act(async () => vi.advanceTimersByTimeAsync(25_000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelector('[role="alert"]')?.textContent).toContain("Offline");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(3);
    expect(section()!.querySelector('[role="alert"]')).toBeNull();
  });

  it.each(["list", "detail"])("retries a failed $read read after only the waiting-gate count changes", async (read) => {
    details = [task("a", [gate("g1")])];
    await show();
    details[0].gates.push(gate("g2"));
    waitingGates++;
    if (read === "list") vi.mocked(fetchTasks).mockRejectedValueOnce(new Error("Offline"));
    else vi.mocked(fetchTaskDetail).mockRejectedValueOnce(new Error("Offline"));
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelector('[role="alert"]')?.textContent).toContain("Offline");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(3);
    expect(section()!.querySelectorAll("article")).toHaveLength(2);
    expect(section()!.querySelector('[role="alert"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchTasks).toHaveBeenCalledTimes(3);
  });

  it.each([
    { read: "list", previouslyLoaded: false },
    { read: "detail", previouslyLoaded: false },
    { read: "list", previouslyLoaded: true },
    { read: "detail", previouslyLoaded: true },
  ])("retries a failed $read read on the next overview poll (previously loaded: $previouslyLoaded)", async ({ read, previouslyLoaded }) => {
    if (previouslyLoaded) {
      details = [task("a", [gate("g1")])];
      await show();
      overview.tasks.needs_you = 2;
    }
    details.push(task("b", [gate("g2")]));
    if (read === "list") vi.mocked(fetchTasks).mockRejectedValueOnce(new Error("Offline"));
    else vi.mocked(fetchTaskDetail).mockRejectedValueOnce(new Error("Offline"));
    if (previouslyLoaded) await act(async () => vi.advanceTimersByTimeAsync(5000));
    else await show();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Offline");
    const listCalls = vi.mocked(fetchTasks).mock.calls.length;
    const detailCalls = vi.mocked(fetchTaskDetail).mock.calls.length;

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(listCalls + 1);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(detailCalls + details.length);
    expect(section()!.querySelectorAll("article")).toHaveLength(details.length);
    for (const detail of details) expect(section()!.textContent).toContain(detail.gates[0].topic);
    expect(container.querySelector('[role="alert"]')).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchTasks).toHaveBeenCalledTimes(listCalls + 1);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(detailCalls + details.length);
  });

  it("pauses overview reads while hidden and refreshes once on visibility", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    await show();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(fetchOverview).not.toHaveBeenCalled();
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("does not overlap a slow overview request", async () => {
    vi.mocked(fetchOverview).mockImplementation(() => new Promise(() => {}));
    await show();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });

  it.each(["deleted", "attention", "cancelled", "completed", "ready", "other"] as const)(
    "removes a %s task's pending reply while other gates keep refreshing", async (state) => {
      details = [task("a", [gate("g1")]), task("b", [gate("g2")])];
      await show();
      const input = await answer("My answer");
      const pending = task("a", [gate("g1", { state: "discussing", waiting_on: "planner", not_delivered: 1 })]);
      details[0] = pending;
      vi.mocked(postGateMessage).mockResolvedValue({ gate: pending.gates[0] } as GatePosted);
      await act(async () => input.form!.requestSubmit());
      expect(section()!.textContent).toContain("You replied: My answer");

      overview.tasks.needs_you++;
      details = [task("b", [gate("g2", { topic: "Updated other gate" })])];
      const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
      vi.mocked(fetchTaskDetail).mockImplementation(async (id) => id === "a"
        ? state === "deleted" ? actual.fetchTaskDetail(id) : { ...pending, group: state }
        : details.find((t) => t.task_id === id)!);
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(section()!.textContent).toContain("Updated other gate");
      expect(section()!.querySelectorAll("article")).toHaveLength(1);
      expect(section()!.textContent).not.toContain("You replied:");
      expect(container.querySelector('[role="alert"]')).toBeNull();

      vi.mocked(fetchTaskDetail).mockClear();
      overview.tasks.needs_you++;
      details = [task("b", [gate("g2", { topic: "Next other gate" })])];
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(section()!.textContent).toContain("Next other gate");
      expect(fetchTaskDetail).not.toHaveBeenCalledWith("a");
    },
  );

  it.each(["server", "network"])("isolates a %s failure and retries the pending reply without losing its receipt", async (failure) => {
    details = [task("a", [gate("g1")]), task("b", [gate("g2")])];
    await show();
    const input = await answer("My answer");
    details[0] = task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })]);
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());

    overview.tasks.needs_you++;
    details = [task("b", [gate("g2", { topic: "Updated other gate" })])];
    const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const fetch = vi.fn();
    if (failure === "server") fetch.mockResolvedValue(new Response(null, { status: 500 }));
    else fetch.mockRejectedValue(new TypeError("Offline"));
    vi.stubGlobal("fetch", fetch);
    vi.mocked(fetchTaskDetail).mockImplementation(async (id) => id === "a"
      ? actual.fetchTaskDetail(id) : details.find((t) => t.task_id === id)!);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("Updated other gate");
    expect(section()!.textContent).toContain("You replied: My answer");
    expect(section()!.querySelectorAll("article")).toHaveLength(2);
    expect(section()!.querySelector('[role="alert"]')?.textContent).toContain(
      failure === "server" ? "500" : "Offline",
    );

    vi.mocked(fetchTaskDetail).mockClear();
    vi.mocked(fetchTaskDetail).mockImplementation(async (id) => id === "a"
      ? task("a", [gate("g1", { topic: "Planner follow-up", turns: 1 })])
      : details.find((t) => t.task_id === id)!);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTaskDetail).toHaveBeenCalledWith("a");
    expect(section()!.textContent).toContain("Planner follow-up");
    expect(section()!.textContent).not.toContain("You replied:");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows a load error without pretending there are no waiting gates", async () => {
    vi.mocked(fetchTasks).mockRejectedValue(new Error("Offline"));
    await show();
    expect(section()).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Offline");
    expect(container.querySelector('a[href="#tasks?group=needs_you&scheduled=include"]')).toBeTruthy();
  });

  it("does not send blank or over-limit replies", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    for (const text of ["   ", "x".repeat(GATE_MESSAGE_MAX_CHARS + 1)]) {
      const input = await answer(text);
      expect(section()!.querySelector("button")!.disabled).toBe(true);
      await act(async () => input.form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    }
    expect(postGateMessage).not.toHaveBeenCalled();
  });

  it.each(["user", "planner"] as const)("labels unread tasks separately from gates, even if they all wait for %s", async (waiting_on) => {
    details = Array.from({ length: 12 }, (_, i) => task(`task-${i}`, [gate(`g${i}`, { state: "discussing", waiting_on: i < 10 ? "user" : waiting_on })]));
    overview.tasks.needs_you = 12;
    await show();
    expect(fetchTaskDetail).toHaveBeenCalledTimes(10);
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    expect(section()!.querySelector('a[href="#tasks?group=needs_you&scheduled=include"]')?.textContent?.trim())
      .toBe("View 2 more tasks needing you");
  });

  it("keeps the more-tasks entry when the first ten tasks all wait for the Planner", async () => {
    details = Array.from({ length: 11 }, (_, i) => task(`task-${i}`, [gate(`g${i}`, {
      state: "discussing", waiting_on: i < 10 ? "planner" : "user",
    })]));
    overview.tasks.needs_you = 11;
    await show();
    expect(fetchTaskDetail).toHaveBeenCalledTimes(10);
    expect(section()!.querySelectorAll("article")).toHaveLength(0);
    expect(section()!.querySelector('a[href="#tasks?group=needs_you&scheduled=include"]')?.textContent?.trim())
      .toBe("View 1 more task needing you");
  });

  it("keeps known extra gates separate from unread task counts", async () => {
    details = Array.from({ length: 12 }, (_, i) => task(`task-${i}`, [gate(`g${i}`)]));
    details[0].gates.push(gate("extra-1"), gate("extra-2"));
    overview.tasks.needs_you = 12;
    await show();
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    const links = [...section()!.querySelectorAll('a[href="#tasks?group=needs_you&scheduled=include"]')];
    expect(links.map((link) => link.textContent?.trim())).toEqual([
      "and 2 more", "View 2 more tasks needing you",
    ]);
  });

  it("publishes slow reads and coalesces polls instead of invalidating the in-flight read", async () => {
    details = [task("a", [gate("g1")])];
    const releases: ((task: TaskDetail) => void)[] = [];
    vi.mocked(fetchTaskDetail).mockImplementation(() => new Promise((resolve) => releases.push(resolve)));
    await show();
    for (let poll = 0; poll < 3; poll++) {
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(fetchTaskDetail).toHaveBeenCalledTimes(1);
    }
    await act(async () => releases[0](task("a", [gate("g1", { topic: "Slow result" })])));
    expect(section()!.textContent).toContain("Slow result");
    expect(fetchTaskDetail).toHaveBeenCalledTimes(1);
    overview.tasks.needs_you++;
    for (let poll = 0; poll < 2; poll++) {
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(fetchTaskDetail).toHaveBeenCalledTimes(2);
    }
    await act(async () => releases[1](task("a", [gate("g1", { topic: "Next result" })])));
    expect(section()!.textContent).toContain("Next result");
    expect(fetchTaskDetail).toHaveBeenCalledTimes(2);
  });

  it("keeps a reply sent while the Planner is answering after that turn raises the count", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const first = await answer("First answer");
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => first.form!.requestSubmit());
    const second = await answer("Still pending");
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => second.form!.requestSubmit());
    // The turn already running finishes. It took the earlier message, not this one.
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner", turns: 1 })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.querySelectorAll("article")).toHaveLength(1);
    expect(section()!.textContent).toContain("You replied: Still pending");
    expect(section()!.textContent).not.toContain("First answer");
    // The new message's turn fails: the count has already risen, and the gate waits for the user again.
    details = [task("a", [gate("g1", { state: "discussing", turns: 1, not_delivered: 1 })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("You replied: Still pending");
    details = [task("a", [gate("g1", { state: "discussing", turns: 2, topic: "Follow-up" })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("Follow-up");
    expect(section()!.textContent).not.toContain("You replied:");
  });

  it("keeps a sent reply after a failed Planner turn and clears it after a successful answer", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("My answer");
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());
    details = [task("a", [gate("g1", { state: "discussing", not_delivered: 1 })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("You replied: My answer");
    details = [task("a", [gate("g1", { state: "discussing", turns: 1, topic: "Follow-up" })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("Follow-up");
    expect(section()!.textContent).not.toContain("You replied:");
  });

  it("keeps a sent card when newer tasks fill the page and excludes its read task from the unread count", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("My answer");
    const pending = task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })]);
    details = [pending];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: pending.gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());
    details = [
      ...Array.from({ length: 10 }, (_, i) => task(`new-${i}`, [gate(`new-g${i}`, {
        opened_at: "2026-10-20T10:15:00Z",
      })])),
      pending, task("hidden", [gate("hidden", { state: "discussing", waiting_on: "planner" })]),
    ];
    overview.tasks.needs_you = 12;
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    expect(section()!.textContent).toContain("You replied: My answer");
    expect(fetchTaskDetail).toHaveBeenLastCalledWith("a");
    const links = [...section()!.querySelectorAll('a[href="#tasks?group=needs_you&scheduled=include"]')];
    expect(links.map((link) => link.textContent?.trim())).toEqual([
      "and 1 more", "View 1 more task needing you",
    ]);
  });

  it("ignores a read started before a successful reply", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("Answer");
    const stale = task("a", [gate("g1", { topic: "Stale topic" })]);
    let release!: (task: TaskDetail) => void;
    overview.tasks.needs_you++;
    vi.mocked(fetchTaskDetail).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    details = [task("a", [gate("g1", { topic: "Fresh topic" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());
    await act(async () => release(stale));
    expect(section()!.textContent).toContain("Fresh topic");
    expect(section()!.textContent).not.toContain("Stale topic");
  });
});
