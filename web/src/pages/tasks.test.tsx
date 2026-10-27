// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TasksPage } from "@/pages/tasks";
import { fetchTasks, type TaskInfo, type TasksPage as TaskPage } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchTasks: vi.fn(), fetchManagers: vi.fn(async () => []),
}));
vi.mock("@/lib/use-projects", () => ({
  useProjects: () => ({ projects: [], reload: vi.fn(async () => {}) }),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const task = (id: string, day: number): TaskInfo => ({
  task_id: id, name: id, requirement: "Synthetic task", goal_id: "", status: "completed", group: "completed",
  phase: "completed", planner_id: null, correlation_id: "", created_at: `2026-10-${day}T00:00:00Z`,
  completed_at: null, project_id: "demo", project_name: "Demo", work_dir: "",
  schedule_id: null, schedule_name: null, continues_schedule_session: false,
});
const page = (tasks: TaskInfo[], next_before: string | null = null): TaskPage => ({
  tasks, next_before, counts: { completed: 5 },
});
const newest = task("newest", 25);
const first = task("first", 24);
const boundary = task("boundary", 23);
const oldest = task("oldest", 22);

describe("task list polling and pagination", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: {
    filters: Parameters<typeof fetchTasks>[0];
    resolve: (value: TaskPage) => void;
    reject: (error: Error) => void;
  }[];

  beforeEach(() => {
    vi.useFakeTimers();
    requests = [];
    vi.mocked(fetchTasks).mockImplementation((filters) => new Promise((resolve, reject) => {
      requests.push({ filters, resolve, reject });
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  const show = async (query = "") => { await act(async () => root.render(<TasksPage query={query} />)); };
  const tick = async (ms: number) => { await act(async () => vi.advanceTimersByTimeAsync(ms)); };
  const finish = async (index: number, value: TaskPage) => {
    await act(async () => requests[index].resolve(value));
  };
  const button = (label: string) => [...container.querySelectorAll("button")]
    .find((el) => el.textContent?.trim() === label || el.getAttribute("title") === label);
  const click = async (label: string) => {
    const el = button(label);
    expect(el).toBeDefined();
    await act(async () => el!.click());
  };
  const rows = () => [...container.querySelectorAll(".divide-y p.truncate")].map((el) => el.textContent);

  it.each(["initial", "refresh"])("applies a slow %s response and waits until it settles before polling again", async (pending) => {
    await show();
    if (pending === "refresh") {
      await finish(0, page([first]));
      await tick(3000);
    }
    const current = requests.length - 1;
    await tick(9000);
    await finish(current, page([newest]));
    expect(rows()).toEqual(["newest"]);
    expect(requests).toHaveLength(current + 1);
    await tick(2999);
    expect(requests).toHaveLength(current + 1);
    await tick(1);
    expect(requests).toHaveLength(current + 2);
    await finish(current + 1, page([newest, first]));
    expect(rows()).toEqual(["newest", "first"]);
  });

  it("retries three seconds after a slow failure", async () => {
    await show();
    await tick(7000);
    await act(async () => requests[0].reject(new Error("List unavailable")));
    expect(container.textContent).toContain("List unavailable");
    expect(requests).toHaveLength(1);
    await tick(3000);
    await finish(1, page([first]));
    expect(rows()).toEqual(["first"]);
    expect(container.textContent).not.toContain("List unavailable");
  });

  it.each(["older first", "newer first"])("orders overlapping manual refreshes by applied response: %s", async (order) => {
    await show();
    await click("Refresh");
    if (order === "older first") {
      await finish(0, page([first]));
      expect(rows()).toEqual(["first"]);
      await finish(1, page([newest]));
    } else {
      await finish(1, page([newest]));
      await finish(0, page([first]));
    }
    expect(rows()).toEqual(["newest"]);
  });

  it.each(["first", null])("drops a pending older page when polling changes its cursor to %s", async (cursor) => {
    await show();
    await finish(0, page([first, boundary], "boundary"));
    await click("Load more");
    expect(requests[1].filters?.before).toBe("boundary");
    await tick(3000);
    await finish(2, page([newest, first], cursor));
    await finish(1, page([oldest]));
    expect(rows()).toEqual(["newest", "first"]);
    if (cursor) {
      // Retry from the new boundary: the displaced row must still be reachable exactly once.
      await click("Load more");
      expect(requests[3].filters?.before).toBe("first");
      await finish(3, page([boundary, oldest]));
      expect(rows()).toEqual(["newest", "first", "boundary", "oldest"]);
    } else {
      expect(button("Load more")).toBeUndefined();
    }
  });

  it("accepts an older page if the poll keeps its cursor, deduplicates it and preserves it on later polls", async () => {
    await show();
    await finish(0, page([first, boundary], "boundary"));
    await click("Load more");
    await tick(3000);
    await finish(2, page([newest, first, boundary], "boundary"));
    await finish(1, page([boundary, oldest]));
    expect(rows()).toEqual(["newest", "first", "boundary", "oldest"]);
    await tick(3000);
    await finish(3, page([newest, first], "first"));
    expect(rows()).toEqual(["newest", "first", "boundary", "oldest"]);
    expect(button("Load more")).toBeUndefined();
  });

  it.each(["initial", "refresh", "more"])("rejects a late %s response after a filter change, even after returning to that filter", async (pending) => {
    await show("manager=alpha");
    if (pending !== "initial") {
      await finish(0, page([first], "first"));
      if (pending === "refresh") await tick(3000);
      else await click("Load more");
    }
    const old = requests.length - 1;
    await show("manager=beta");
    const current = requests.length - 1;
    expect(requests[current].filters?.managerId).toBe("beta");
    await finish(current, page([newest]));
    await show("manager=alpha");
    const returned = requests.length - 1;
    await finish(returned, page([boundary]));
    await finish(old, page([oldest], "oldest"));
    expect(rows()).toEqual(["boundary"]);
    expect(button("Load more")).toBeUndefined();
    await tick(3000);
    await finish(returned + 1, page([newest]));
    expect(rows()).toEqual(["newest"]);
  });
});
