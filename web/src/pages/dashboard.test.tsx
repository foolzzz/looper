// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "@/pages/dashboard";
import { fetchOverview, fetchTasks, fetchTaskDetail, postGateMessage, GATE_MESSAGE_MAX_CHARS,
  type Overview, type TaskDetail, type UserGate, type GatePosted } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchOverview: vi.fn(), fetchTasks: vi.fn(), fetchTaskDetail: vi.fn(), postGateMessage: vi.fn(),
}));
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
const task = (id: string, gates: UserGate[]) => ({ task_id: id, name: `Task ${id}`, group: "needs_you", gates } as TaskDetail);

describe("Dashboard waiting gates", () => {
  let container: HTMLDivElement;
  let root: Root;
  let details: TaskDetail[];
  let overview: Overview;
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.setSystemTime(new Date("2026-10-20T10:12:00Z"));
    vi.resetAllMocks();
    details = [];
    overview = {
      tasks: { needs_you: 1, other_statuses: {} }, agents: { manager: null, planners: [], workers: [], reviewers: [] },
      projects: [], idle_projects: 0, health: { unhealthy: [] },
      schedules: { total: 0, enabled: 0, next: [], failing: [], skipped_today: {} },
      spend_today: { since: "", provider_priced_usd: 0, equivalent_usd: 0, runs: 0, running: 0, unpriced_runs: 0 },
    } as unknown as Overview;
    vi.mocked(fetchOverview).mockImplementation(async () => ({ ...overview }));
    vi.mocked(fetchTasks).mockImplementation(async () => ({ tasks: details.slice(0, 10), next_before: details.length > 10 ? "next-page" : null, counts: { waiting_user: details.length } }));
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
  const show = async () => { await act(async () => root.render(<DashboardPage />)); };
  const section = () => container.querySelector('section[aria-label="Waiting for you"]');
  const answer = async (text: string) => {
    const input = section()!.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    return input;
  };

  it("counts the fleet's busy agents separately from waiting runs and links their tasks", async () => {
    overview.agents = {
      manager: { agent_id: "manager", state: "busy" },
      managers: [
        { agent_id: "manager", name: "Manager", state: "busy" },
        { agent_id: "manager-build", name: "Build manager", state: "busy" },
        { agent_id: "manager-review", name: "Review manager", state: "idle" },
        { agent_id: "manager-off", name: "Stopped manager", state: "stopped" },
      ],
      planners: [{ agent_id: "planner-a", task_id: "a", name: "Build parser", requirement: "Plan the parser",
        project_id: "project-a", project_name: "Compiler" }],
      workers: [{ agent_id: "worker-a", role: "worker", task_id: "a", task_name: "Build parser",
        project_id: "project-a", project_name: "Compiler", step: "parse", instruction: "Implement parsing\nMore detail",
        state: "running", elapsed_seconds: 120, task_role: { id: "developer", kind: "work", version: 1 } },
      { agent_id: "worker-b", role: "worker", task_id: "b", task_name: "Add tests",
        project_id: null, project_name: null, step: "test", instruction: "", state: "waiting", elapsed_seconds: 0 }],
      reviewers: [{ agent_id: "reviewer-a", role: "reviewer", task_id: "a", task_name: "Build parser",
        project_id: "project-a", project_name: "Compiler", step: "review", instruction: "Review parsing",
        state: "running", elapsed_seconds: 30 }],
    };
    await show();
    const agents = container.querySelector('section[aria-label="Agents running now"]')!;
    expect(agents.firstElementChild!.textContent).toContain("5 running · 1 waiting");
    const rows = agents.querySelector(".divide-y")!.children;
    expect(rows[0].firstElementChild!.textContent).toBe("Managers");
    expect(rows[0].textContent).toContain("2 busy");
    expect(rows[1].textContent).toContain("Planners1");
    expect(rows[2].textContent).toContain("1 running · 1 waiting");
    expect(rows[3].textContent).toContain("1 running");
    const managers = [...rows[0].querySelectorAll("a")];
    expect(managers.map((a) => a.textContent)).toEqual(["Managerbusy", "Build managerbusy", "Review manageridle"]);
    expect(managers.map((a) => a.getAttribute("href"))).toEqual([
      "#history/manager", "#history/manager-build", "#history/manager-review",
    ]);
    for (const row of [rows[1], rows[2], rows[3]]) {
      const link = row.querySelector('a[href="#tasks/a"]')!;
      expect(link.textContent).toBe("Compiler · Build parser");
      expect(link.getAttribute("title")).toBe("Compiler · Build parser");
    }
    expect(rows[1].textContent).toContain("Plan the parser");
    expect(rows[2].textContent).toContain("developer");
    expect(rows[2].textContent).toContain("Implement parsing");
    expect(rows[2].textContent).not.toContain("More detail");
    expect(rows[2].textContent).toContain("2 min");
    expect(rows[2].querySelector('a[href="#tasks/b"]')!.textContent).toBe("Add tests");
    expect(rows[2].textContent).toContain("testwaiting");
  });

  it.each(["busy", "idle", null] as const)("falls back to the single Manager: %s", async (state) => {
    overview.agents.manager = state ? { agent_id: "manager", state } : null;
    await show();
    const agents = container.querySelector('section[aria-label="Agents running now"]')!;
    expect(agents.firstElementChild!.textContent).toContain(`${state === "busy" ? 1 : 0} running`);
    expect(agents.firstElementChild!.textContent).not.toContain("waiting");
    const link = agents.querySelector('a[href="#history/manager"]');
    expect(agents.querySelector(".divide-y")!.firstElementChild!.firstElementChild!.textContent).toBe("Manager");
    if (state) expect(link!.textContent).toBe(`Manager${state}`);
    else expect(link).toBeNull();
  });

  it.each(["Plan the parser", ""])("avoids repeating a Planner's requirement as its name: %s", async (name) => {
    overview.agents.planners = [{ agent_id: "planner-a", task_id: "a", name,
      requirement: "Plan the parser\nAdditional details", project_id: null, project_name: null }];
    await show();
    const agents = container.querySelector('section[aria-label="Agents running now"]')!;
    expect(agents.querySelector('a[href="#tasks/a"]')!.textContent).toBe("Plan the parser");
    expect(agents.querySelector('a[href="#history/planner-a?task=a"]')!.textContent).toBe("planning");
  });

  it("keeps the task name beside a long project name on a narrow screen", async () => {
    vi.stubGlobal("innerWidth", 390);
    const project = "A very long project name that would fill the entire row on a phone";
    overview.agents.planners = [{ agent_id: "planner-a", task_id: "a", name: "Build parser",
      requirement: "Plan the parser", project_id: "project-a", project_name: project }];
    await show();
    const link = container.querySelector('section[aria-label="Agents running now"] a[href="#tasks/a"]')!;
    const projectSpan = link.querySelector(`span[title="${project}"]`)!;
    const taskSpan = link.querySelector('span[title="Build parser"]')!;
    expect(projectSpan.textContent).toBe(project);
    expect(taskSpan.textContent).toBe("Build parser");
    expect(projectSpan.className).toContain("max-w-[40%]");
    expect(projectSpan.className).toContain("shrink-0");
    expect(taskSpan.className).toContain("min-w-0");
    expect(taskSpan.className).toContain("truncate");
    expect(link.classList.contains("inline-block")).toBe(true);
    expect(link.classList.contains("max-w-full")).toBe(true);
    expect(link.classList.contains("block")).toBe(false);
  });

  it("treats an empty fleet as authoritative", async () => {
    overview.agents.manager = { agent_id: "manager", state: "busy" };
    overview.agents.managers = [];
    await show();
    const agents = container.querySelector('section[aria-label="Agents running now"]')!;
    expect(agents.firstElementChild!.textContent).toContain("0 running");
    expect(agents.querySelector('a[href="#history/manager"]')).toBeNull();
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
    const more = section()!.querySelector('a[href="#tasks?group=needs_you"]');
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

  it("does not reread waiting gates for an unchanged overview count", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("Draft");
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(fetchOverview).toHaveBeenCalledTimes(7);
    expect(fetchTasks).toHaveBeenCalledTimes(1);
    expect(fetchTaskDetail).toHaveBeenCalledTimes(1);
    overview.tasks.needs_you = 2;
    details.push(task("b", [gate("g2")]));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetchTasks).toHaveBeenCalledTimes(2);
    expect(section()!.querySelectorAll("article")).toHaveLength(2);
    expect(input.value).toBe("Draft");
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
    expect(container.querySelector('a[href="#tasks?group=needs_you"]')).toBeTruthy();
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
    expect(section()!.querySelector('a[href="#tasks?group=needs_you"]')?.textContent?.trim())
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
    expect(section()!.querySelector('a[href="#tasks?group=needs_you"]')?.textContent?.trim())
      .toBe("View 1 more task needing you");
  });

  it("keeps known extra gates separate from unread task counts", async () => {
    details = Array.from({ length: 12 }, (_, i) => task(`task-${i}`, [gate(`g${i}`)]));
    details[0].gates.push(gate("extra-1"), gate("extra-2"));
    overview.tasks.needs_you = 12;
    await show();
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    const links = [...section()!.querySelectorAll('a[href="#tasks?group=needs_you"]')];
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
    const links = [...section()!.querySelectorAll('a[href="#tasks?group=needs_you"]')];
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
