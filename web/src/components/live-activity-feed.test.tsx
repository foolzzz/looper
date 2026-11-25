// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveActivityFeed } from "@/components/live-activity-feed";
import { TaskDetailRoute } from "@/pages/task-detail";
import { fetchTaskActivity, streamTaskActivity, type ActivityEvent } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchTaskActivity: vi.fn(),
  streamTaskActivity: vi.fn(),
}));
vi.mock("@/components/task-metrics-card", () => ({ TaskMetricsCard: () => null }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const event = (seq: number, worker: string | null = "worker-1", kind = "text"): ActivityEvent => ({
  seq, task_id: "task-a", worker, kind, summary: `event ${seq}`, metadata: null,
  input_tokens: null, output_tokens: null, created_at: "2026-10-17T08:00:00Z",
});
const deferred = () => {
  let resolve!: (value: { activity: ActivityEvent[] }) => void;
  const promise = new Promise<{ activity: ActivityEvent[] }>((done) => { resolve = done; });
  return { promise, resolve };
};

describe("LiveActivityFeed", () => {
  let container: HTMLDivElement;
  let root: Root;
  let stop: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    stop = vi.fn();
    vi.mocked(fetchTaskActivity).mockReset().mockResolvedValue({ activity: [] });
    vi.mocked(streamTaskActivity).mockReset().mockReturnValue(stop);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const show = async (taskId = "task-a", isActive = true) => {
    await act(async () => root.render(<LiveActivityFeed taskId={taskId} isActive={isActive} />));
  };
  const push = async (value: ActivityEvent, subscription = 0) => {
    await act(async () => vi.mocked(streamTaskActivity).mock.calls[subscription][2](value));
  };

  it("reads before subscribing, deduplicates seqs and preserves the rendered rows and automatic scroll", async () => {
    const read = deferred();
    vi.mocked(fetchTaskActivity).mockReturnValue(read.promise);
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => {});
    await show();
    expect(fetchTaskActivity).toHaveBeenCalledExactlyOnceWith("task-a", 0, 200);
    expect(streamTaskActivity).not.toHaveBeenCalled();
    expect(container.textContent).toBe("Live Activity0 eventsWaiting for activity...");
    await act(async () => read.resolve({ activity: [event(7)] }));
    // Preserve the existing initial cursor, including its closure over the empty first render.
    expect(streamTaskActivity).toHaveBeenCalledExactlyOnceWith("task-a", 0, expect.any(Function), expect.any(Function));
    await push(event(7));
    await push(event(8, null, "error"));
    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(container.textContent).toContain("Live Activity2 eventsStreaming");
    expect(container.textContent).toContain("worker-1");
    expect(container.textContent).toContain("system");
    expect(container.textContent).toContain(new Date(event(7).created_at!).toLocaleString());
    expect(container.firstElementChild?.className).toBe("rounded-lg border border-border bg-card overflow-hidden");
    const scroller = container.querySelector(".overflow-y-auto")!;
    expect(scroller.className).toBe("max-h-[300px] overflow-y-auto p-3 space-y-1");
    expect(scroller.children[1].className).toBe("flex items-start gap-2 rounded px-2 py-1.5 text-xs bg-destructive/10");
    expect(scroll).toHaveBeenLastCalledWith({ top: scroller.scrollHeight, behavior: "smooth" });
    await act(async () => vi.mocked(streamTaskActivity).mock.calls[0][3]!());
    expect(container.textContent).not.toContain("Streaming");
    expect(container.textContent).toContain("2 events");
  });

  it.each([false, true])("renders inactive history without subscribing (history present: %s)", async (hasHistory) => {
    vi.mocked(fetchTaskActivity).mockResolvedValue({ activity: hasHistory ? [event(1)] : [] });
    await show("task-a", false);
    expect(fetchTaskActivity).toHaveBeenCalledExactlyOnceWith("task-a", 0, 200);
    expect(streamTaskActivity).not.toHaveBeenCalled();
    if (hasHistory) expect(container.textContent).toContain("Live Activity1 eventsworker-1");
    else expect(container.innerHTML).toBe("");
  });

  it.each([false, true])("keeps the existing initial-read failure behavior (active: %s)", async (isActive) => {
    vi.mocked(fetchTaskActivity).mockRejectedValue(new Error("unavailable"));
    await show("task-a", isActive);
    expect(streamTaskActivity).toHaveBeenCalledTimes(isActive ? 1 : 0);
    if (isActive) {
      expect(container.textContent).toBe("Live Activity0 eventsStreamingWaiting for activity...");
      await push(event(1));
      expect(container.textContent).toContain("event 1");
    } else expect(container.innerHTML).toBe("");
  });

  it("cleans up on task change and ignores late reads and activity callbacks", async () => {
    vi.mocked(fetchTaskActivity).mockResolvedValueOnce({ activity: [event(7)] });
    await show();
    const read = deferred();
    vi.mocked(fetchTaskActivity).mockReturnValueOnce(read.promise);
    await show("task-b");
    expect(stop).toHaveBeenCalledTimes(1);
    await push(event(8));
    expect(container.textContent).not.toContain("event 8");
    await show("task-c");
    await act(async () => read.resolve({ activity: [event(99)] }));
    expect(container.textContent).not.toContain("event 99");
    expect(vi.mocked(streamTaskActivity).mock.calls.map(([id]) => id)).toEqual(["task-a", "task-c"]);
    expect(vi.mocked(fetchTaskActivity).mock.calls).toEqual([
      ["task-a", 0, 200], ["task-b", 0, 200], ["task-c", 0, 200],
    ]);
  });

  it.each([false, true])("ignores late activity or a successful read after unmount (pending read: %s)", async (pending) => {
    const read = deferred();
    if (pending) vi.mocked(fetchTaskActivity).mockReturnValueOnce(read.promise);
    await show();
    await act(async () => root.render(null));
    if (pending) {
      await act(async () => read.resolve({ activity: [event(99)] }));
      expect(streamTaskActivity).not.toHaveBeenCalled();
    } else {
      expect(stop).toHaveBeenCalledTimes(1);
      await push(event(99));
    }
    await show("task-b", false);
    expect(container.innerHTML).toBe("");
  });

  it("stops the subscription when the task becomes inactive", async () => {
    vi.mocked(fetchTaskActivity).mockResolvedValue({ activity: [event(7)] });
    await show();
    await show("task-a", false);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(streamTaskActivity).toHaveBeenCalledTimes(1);
    await push(event(8));
    expect(container.textContent).not.toContain("event 8");
  });

  it("keeps the 200-event initial read and appends new streamed events", async () => {
    vi.mocked(fetchTaskActivity).mockResolvedValue({ activity: Array.from({ length: 200 }, (_, i) => event(i + 1)) });
    await show();
    await push(event(201));
    expect(container.querySelectorAll("p")).toHaveLength(201);
    expect(container.textContent).toContain("201 events");
  });

  it("mounts exactly one feed through the existing task detail", async () => {
    vi.mocked(fetchTaskActivity).mockResolvedValue({ activity: [event(7)] });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => Response.json({ status: "ok", result:
      String(input) === "/api/v1/tasks/detail" ? {
        task_id: "task-a", name: "Example task", requirement: "Example requirement", status: "running",
        group: "in_progress", phase: "executing", planner_id: null, project_id: "project-a", config: {},
        steps: [], journal: [], progress: [], gates: [], roles: [], requirement_versions: [],
        supplements: [], manager_actions: [], profile_overrides: {},
      } : { targets: [], runs: [], drafts: [] },
    })));
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
    expect(container.querySelector("h1")?.textContent).toBe("Example task");
    expect(container.textContent?.match(/Live Activity/g)).toHaveLength(1);
    // The separate overview timeline keeps its existing 500-event read.
    expect(vi.mocked(fetchTaskActivity).mock.calls).toEqual([["task-a", 0, 500], ["task-a", 0, 200]]);
    expect(streamTaskActivity).toHaveBeenCalledTimes(1);
  });
});
