// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatesPage } from "@/pages/gates";
import { fetchGates, fetchProjects, postGateMessage, type GateList, type GateListRow, type UserGate } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchGates: vi.fn(), fetchProjects: vi.fn(), postGateMessage: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const gate = (step: string, extra: Partial<UserGate> = {}): UserGate => ({
  step, step_id: step, state: "open", topic: `Topic ${step}`, findings: "", summary: null, resolution: "",
  agenda: [{ id: "q1", question: `Question ${step}`, options: [], status: "open", answer: "" }],
  ended_by_user: false, plan_change: null, depends_on: [], waiting_on: "user", not_delivered: 0,
  retry_at: null, turns: 0, reminders: 0, opened_at: "2026-10-20T10:00:00Z", first_reply_at: null,
  closed_at: null, channel_posts: null, ...extra,
});
const row = (id: string, g: UserGate, group: GateListRow["task"]["group"] = "needs_you"): GateListRow => ({
  task: { task_id: id, name: `Task ${id}`, status: "waiting_user", group, project_id: "p1", project_name: "P" },
  gate: g, agenda_open: 1, last_message: { role: "user", author: null, via: "", created_at: "2026-10-20T10:07:00Z" },
});
const list = (gates: GateListRow[], extra: Partial<GateList> = {}): GateList => ({
  gates, total: gates.length, limit: 50, offset: 0,
  counts: { open: 3, waiting_user: 2, waiting_planner: 1, closed: 4, all: 7 }, ...extra,
});

describe("GatesPage", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-20T10:12:00Z"));
    vi.resetAllMocks();
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(fetchGates).mockResolvedValue(list([]));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });
  const show = async (query = "", onQuery = vi.fn()) => {
    await act(async () => root.render(<GatesPage query={query} onQuery={onQuery} />));
    return onQuery;
  };
  const button = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith(text))!;
  const click = async (el: Element) => { await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); }); };

  it("gives the header and every row the same fixed columns, so they line up", async () => {
    vi.mocked(fetchGates).mockResolvedValue(list([
      row("t1", gate("g1")), row("t2", gate("g2", { state: "passed", waiting_on: null, closed_at: "2026-10-20T10:10:00Z" }), "completed"),
    ]));
    await show();
    const grids = [...container.querySelectorAll("[class*='xl:grid-cols-']")];
    expect(grids).toHaveLength(3);
    const columns = grids.map((g) => g.className.match(/xl:grid-cols-\[[^\]]+\]/)![0]);
    expect(new Set(columns).size).toBe(1);
    expect(columns[0]).not.toMatch(/auto|max-content|min-content/);
  });

  it("lists the open gates with the counts", async () => {
    vi.mocked(fetchGates).mockResolvedValue(list([row("t1", gate("g1"))]));
    await show();
    expect(fetchGates).toHaveBeenCalledWith(expect.objectContaining({ state: "open" }));
    expect(container.querySelector("a[href='#tasks/t1']")?.textContent).toBe("Task t1");
    const text = container.textContent!;
    expect(text).toContain("Topic g1");
    expect(text).toContain("Waiting for you");
    expect(text).toContain("1 open question");
    expect(text).toContain("You · 5 min");
    expect(container.querySelector("h1")?.textContent).toBe("User Gates");
    expect(text).toContain("3 open · 2 waiting for you");
  });

  it("filters through the query", async () => {
    const onQuery = await show();
    await click(button("Closed"));
    expect(onQuery).toHaveBeenCalledWith("state=closed");
    await show("state=closed");
    expect(fetchGates).toHaveBeenLastCalledWith(expect.objectContaining({ state: "closed" }));
    expect(container.querySelector("[role=switch]")).toBeNull();
    await show("waiting=user");
    expect(fetchGates).toHaveBeenLastCalledWith(expect.objectContaining({ state: "open", waitingOn: "user" }));
  });

  it("says what is empty, loading and failing", async () => {
    let resolve!: (l: GateList) => void;
    vi.mocked(fetchGates).mockReturnValueOnce(new Promise((r) => { resolve = r; }));
    await show();
    expect(container.textContent).toContain("Loading gates…");
    await act(async () => resolve(list([])));
    expect(container.textContent).toContain("No gate waits for an answer.");
    await show("waiting=user");
    expect(container.textContent).toContain("No gate waits for you.");
    await show("state=closed");
    expect(container.textContent).toContain("No gate has closed yet.");
    await show("state=all");
    expect(container.textContent).toContain("No task has opened a gate yet.");

    vi.mocked(fetchGates).mockResolvedValueOnce(list([row("t1", gate("g1"))]));
    await show("");
    vi.mocked(fetchGates).mockRejectedValueOnce(new Error("boom"));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Cannot load gates (boom)");
    expect(container.textContent).toContain("Topic g1");
    const calls = vi.mocked(fetchGates).mock.calls.length;
    await click(button("Retry"));
    expect(vi.mocked(fetchGates).mock.calls.length).toBe(calls + 1);
  });

  it("offers no Answer on an unclosed gate of a completed task", async () => {
    vi.mocked(fetchGates).mockResolvedValue(list([row("t1", gate("g1"), "completed")]));
    await show("state=all");
    expect([...container.querySelectorAll("button")].some((b) => b.textContent === "Answer")).toBe(false);
  });

  it("refreshes every loaded row, not only the first 200", async () => {
    const all = Array.from({ length: 250 }, (_, i) => row(`t${i}`, gate(`g${i}`)));
    vi.mocked(fetchGates).mockImplementation(async (params) => {
      const { limit = 50, offset = 0 } = params ?? {};
      return list(all.slice(offset, offset + limit), { total: 250, offset, limit });
    });
    await show();
    while (container.querySelectorAll("li").length < 250) {
      await click(button("Show more"));
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(container.querySelectorAll("li")).toHaveLength(250);
  });

  it("offers Answer only on rows waiting for the user, and sends the reply", async () => {
    vi.mocked(fetchGates).mockResolvedValue(list([
      row("t1", gate("g1")),
      row("t2", gate("g2", { state: "discussing", waiting_on: "planner" })),
      row("t3", gate("g3", { state: "passed", waiting_on: null, closed_at: "2026-10-20T10:10:00Z" }), "completed"),
    ]));
    vi.mocked(postGateMessage).mockResolvedValue({ gate: gate("g1") } as never);
    const changed = vi.fn();
    window.addEventListener("looper:gates-changed", changed);
    await show();
    const answers = [...container.querySelectorAll("button")].filter((b) => b.textContent === "Answer");
    expect(answers).toHaveLength(1);
    await click(answers[0]);
    expect(answers[0].getAttribute("aria-expanded")).toBe("true");
    const input = container.querySelector("textarea")!;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(input, "Yes");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const calls = vi.mocked(fetchGates).mock.calls.length;
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postGateMessage).toHaveBeenCalledWith("t1", "g1", { text: "Yes" });
    expect(vi.mocked(fetchGates).mock.calls.length).toBe(calls + 1);
    expect(changed).toHaveBeenCalled();
    window.removeEventListener("looper:gates-changed", changed);
  });

  it("shows more and refreshes on a timer, never while one is pending", async () => {
    vi.mocked(fetchGates).mockResolvedValue(list(
      Array.from({ length: 50 }, (_, i) => row("t1", gate(`g${i}`))), { total: 80 }));
    await show();
    await click(button("Show more"));
    expect(fetchGates).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50, limit: 50 }));

    vi.mocked(fetchGates).mockReturnValue(new Promise(() => {}));
    const calls = vi.mocked(fetchGates).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(vi.mocked(fetchGates).mock.calls.length).toBe(calls + 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(vi.mocked(fetchGates).mock.calls.length).toBe(calls + 1);
  });
});
