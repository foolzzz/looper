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
    vi.mocked(fetchTasks).mockImplementation(async () => ({ tasks: details, next_before: null, counts: { waiting_user: details.length } }));
    vi.mocked(fetchTaskDetail).mockImplementation(async (id) => details.find((t) => t.task_id === id)!);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
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

  it("removes a gate after a reply hands the turn to the Planner", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("My answer");
    details = [task("a", [gate("g1", { state: "discussing", waiting_on: "planner" })])];
    vi.mocked(postGateMessage).mockResolvedValue({ gate: details[0].gates[0] } as GatePosted);
    await act(async () => input.form!.requestSubmit());
    expect(section()).toBeNull();
  });

  it("refreshes waiting gates on the dashboard poll without losing a draft", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("Draft");
    details = [task("a", [gate("g1", { topic: "New topic" })])];
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(section()!.textContent).toContain("New topic");
    expect(input.value).toBe("Draft");
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

  it("bounds per-task reads and labels unseen tasks as a lower bound on more gates", async () => {
    details = Array.from({ length: 12 }, (_, i) => task(`task-${i}`, [gate(`g${i}`)]));
    overview.tasks.needs_you = 12;
    await show();
    expect(fetchTaskDetail).toHaveBeenCalledTimes(10);
    expect(section()!.querySelectorAll("article")).toHaveLength(10);
    expect(section()!.querySelector('a[href="#tasks?group=needs_you"]')?.textContent?.trim())
      .toBe("and at least 2 more");
  });

  it("ignores a read started before a successful reply", async () => {
    details = [task("a", [gate("g1")])];
    await show();
    const input = await answer("Answer");
    const stale = task("a", [gate("g1", { topic: "Stale topic" })]);
    let release!: (task: TaskDetail) => void;
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
