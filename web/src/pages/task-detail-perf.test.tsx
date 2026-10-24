// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ActivityEvent, fetchTaskActivityWindow, fetchTaskConversation, fetchTaskRuns, streamTaskActivity } from "@/lib/api";
import { ConversationTab, SessionsTab, TaskDetailRoute } from "@/pages/task-detail";
import { useTaskRuns } from "@/lib/use-task-runs";
import { agentFact } from "@/lib/node-cards";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchTaskActivityWindow: vi.fn(),
  fetchTaskConversation: vi.fn(),
  fetchTaskRuns: vi.fn(),
  streamTaskActivity: vi.fn(() => () => {}),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const event = (seq: number, kind = "text"): ActivityEvent => ({
  seq, task_id: "task-a", worker: "worker-1", kind, summary: `event ${seq}`, metadata: null,
  input_tokens: null, output_tokens: null, created_at: "2026-10-17T08:00:00Z",
});
const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => event(from + i));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.mocked(streamTaskActivity).mockClear().mockImplementation(() => () => {});
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.mocked(fetchTaskActivityWindow).mockReset();
  vi.mocked(fetchTaskConversation).mockReset();
});

const settle = async () => {
  for (let i = 0; i < 5; i++) await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
};

/** happy-dom lays nothing out: give the scroller the heights a browser would (px per row, a 300px view). */
function layout(el: HTMLElement, rowHeight = 50) {
  Object.defineProperty(el, "clientHeight", { configurable: true, value: 300 });
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => el.children.length * rowHeight,
  });
}

describe("Sessions tab window", () => {
  it("reads the latest window, streams after its newest seq and scrolls to the bottom", async () => {
    vi.mocked(fetchTaskActivityWindow).mockResolvedValue({ events: range(501, 1500), hasMore: true });
    await act(async () => root.render(<SessionsTab taskId="task-a" status="running" />));
    await settle();
    expect(fetchTaskActivityWindow).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetchTaskActivityWindow).mock.calls[0][1]).toBeUndefined();
    expect(container.textContent).toContain("1000 events");
    expect(streamTaskActivity).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamTaskActivity).mock.calls[0][1]).toBe(1500);
  });

  it("loads the page before the oldest loaded seq near the top and keeps the reading place", async () => {
    vi.mocked(fetchTaskActivityWindow)
      .mockResolvedValueOnce({ events: range(101, 200), hasMore: true })
      .mockResolvedValueOnce({ events: range(1, 100), hasMore: false });
    await act(async () => root.render(<SessionsTab taskId="task-a" status="running" />));
    await settle();
    const scroller = container.querySelector<HTMLElement>(".overflow-y-auto")!;
    layout(scroller);
    scroller.scrollTop = 40;
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await settle();
    expect(fetchTaskActivityWindow).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetchTaskActivityWindow).mock.calls[1][1]).toBe(101);
    expect(container.textContent).toContain("200 events");
    expect(container.textContent).not.toContain("loading earlier");
    // 100 rows were put above: the view moved down by their height, so the same rows stay in view.
    expect(scroller.scrollTop).toBe(40 + 100 * 50);
    const seqs = [...container.querySelectorAll("p.whitespace-pre-wrap")].map((p) => p.textContent);
    expect(seqs[0]).toBe("event 1");
    expect(seqs[199]).toBe("event 200");
  });

  it("shows the loading line while the earlier page is read, and stops asking when has_more is false", async () => {
    let release!: (page: { events: ActivityEvent[]; hasMore: boolean }) => void;
    vi.mocked(fetchTaskActivityWindow)
      .mockResolvedValueOnce({ events: range(11, 20), hasMore: true })
      .mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    await act(async () => root.render(<SessionsTab taskId="task-a" status="running" />));
    await settle();
    const scroller = container.querySelector<HTMLElement>(".overflow-y-auto")!;
    layout(scroller);
    scroller.scrollTop = 0;
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    expect(container.textContent).toContain("loading earlier…");
    expect(fetchTaskActivityWindow).toHaveBeenCalledTimes(2);
    await act(async () => release({ events: range(1, 10), hasMore: false }));
    await settle();
    expect(container.textContent).not.toContain("loading earlier");
    for (let i = 0; i < 3; i++) await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await settle();
    expect(fetchTaskActivityWindow).toHaveBeenCalledTimes(2);
  });

  it("follows new events only while at the bottom", async () => {
    vi.mocked(fetchTaskActivityWindow).mockResolvedValue({ events: range(1, 20), hasMore: false });
    await act(async () => root.render(<SessionsTab taskId="task-a" status="running" />));
    await settle();
    const scroller = container.querySelector<HTMLElement>(".overflow-y-auto")!;
    layout(scroller);
    const push = vi.mocked(streamTaskActivity).mock.calls[0][2];
    scroller.scrollTop = 1000 - 300; // at the bottom
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await act(async () => push(event(21)));
    expect(scroller.scrollTop).toBe(21 * 50);
    scroller.scrollTop = 500; // reading above
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await act(async () => push(event(22)));
    expect(scroller.scrollTop).toBe(500);
  });
});

describe("Conversation tab window", () => {
  it("reads the latest window and the page before it on scroll to the top", async () => {
    const message = (seq: number) => ({ ...event(seq, "user_message") });
    vi.mocked(fetchTaskConversation)
      .mockResolvedValueOnce({ events: [message(11), message(12)], hasMore: true })
      .mockResolvedValueOnce({ events: [message(9), message(10)], hasMore: false });
    await act(async () => root.render(<ConversationTab taskId="task-a" />));
    await settle();
    const scroller = container.querySelector<HTMLElement>(".overflow-y-auto")!;
    layout(scroller);
    scroller.scrollTop = 0;
    await act(async () => { scroller.dispatchEvent(new Event("scroll")); });
    await settle();
    expect(vi.mocked(fetchTaskConversation).mock.calls.map((c) => c[1])).toEqual([undefined, 11]);
    expect([...container.querySelectorAll("p.whitespace-pre-wrap")].map((p) => p.textContent))
      .toEqual(["event 9", "event 10", "event 11", "event 12"]);
  });
});

describe("useTaskRuns", () => {
  it("reads the runs from the one compact endpoint, and the Agent line is the same", async () => {
    vi.mocked(fetchTaskRuns).mockResolvedValue([
      { id: 1, role: "planner", agent_id: "planner-task-a", step_id: null, status: "completed", created_at: "2026-10-17T08:00:00Z",
        runtime: { type: "claude-code", name: "Claude Code" }, model: "opus", effort: "high", profile: { name: "Fast", source: "project" } },
      { id: 2, role: "worker", agent_id: "worker-1", step_id: "step-1", status: "running", created_at: "2026-10-17T08:01:00Z",
        runtime: { type: "codex", name: "Codex" }, model: "gpt", effort: null, profile: { name: "W", source: "task_role" } },
    ]);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    let seen: ReturnType<typeof useTaskRuns> | undefined;
    function Probe() {
      seen = useTaskRuns({ task_id: "task-a", status: "running", planner_id: "p", steps: [] } as never);
      return null;
    }
    await act(async () => root.render(<Probe />));
    await settle();
    expect(fetchTaskRuns).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(seen?.runs?.planner.map((r) => r.id)).toEqual([1]);
    expect(seen?.runs?.worker.map((r) => r.id)).toEqual([2]);
    expect(seen?.runs?.reviewer).toEqual([]);
    expect(agentFact(seen!.runs!.planner)).toMatchObject({ label: "Agent", value: "Claude Code · opus · high" });
    expect(agentFact(seen!.runs!.worker, "step-1")).toMatchObject({ label: "Agent", value: "Codex · gpt" });
  });
});

describe("useTaskRuns refresh", () => {
  it("reads again when the Planner runs again without any other change", async () => {
    vi.mocked(fetchTaskRuns).mockClear();
    vi.mocked(fetchTaskRuns).mockResolvedValue([]);
    const cost = (runs: number) => ({ runs, running: 0, by_role: { planner: { runs }, worker: { runs: 0 }, reviewer: { runs: 0 } } });
    function Probe({ runs }: { runs: number }) {
      useTaskRuns({ task_id: "task-b", status: "running", planner_id: "p", steps: [], cost: cost(runs) } as never);
      return null;
    }
    await act(async () => root.render(<Probe runs={1} />));
    await settle();
    await act(async () => root.render(<Probe runs={2} />));
    await settle();
    expect(fetchTaskRuns).toHaveBeenCalledTimes(2);
  });
});

describe("task page polling", () => {
  const taskBody = JSON.stringify({
    task_id: "task-a", name: "t", requirement: "r", status: "running", group: "active", phase: "x", planner_id: null,
    steps: [], journal: [], progress: [], gates: [], roles: [], config: {}, requirement_versions: [], supplements: [],
    manager_actions: [], profile_overrides: {}, project_id: "p1",
  });
  const respond = (url: string) =>
    new Response(url.endsWith("/tasks/task-a") ? taskBody : JSON.stringify({ activity: [], conversation: [], runs: [] }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  const taskCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
    fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/tasks/task-a"));
  const visibility = (state: "visible" | "hidden") => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
    document.dispatchEvent(new Event("visibilitychange"));
  };

  beforeEach(() => {
    vi.useFakeTimers();
    visibility("visible");
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not overlap a slow poll, pauses while hidden, resumes at once and aborts on unmount", async () => {
    const releases: (() => void)[] = [];
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (!url.endsWith("/tasks/task-a")) return Promise.resolve(respond(url));
      if (init?.signal) signals.push(init.signal);
      return new Promise<Response>((resolve) => releases.push(() => resolve(respond(url))));
    });
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(taskCalls(fetchMock)).toHaveLength(1);
    await act(async () => { releases[0](); await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(taskCalls(fetchMock)).toHaveLength(2);

    await act(async () => { releases[1](); await vi.advanceTimersByTimeAsync(0); });
    visibility("hidden");
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(taskCalls(fetchMock)).toHaveLength(2);
    await act(async () => { visibility("visible"); await vi.advanceTimersByTimeAsync(0); });
    expect(taskCalls(fetchMock)).toHaveLength(3);

    act(() => root.unmount());
    expect(signals[signals.length - 1].aborted).toBe(true);
    root = createRoot(container);
  });
});
