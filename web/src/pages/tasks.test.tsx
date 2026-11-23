// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_STATUS_FILTERS, TasksPage } from "@/pages/tasks";
import { fetchTasks, fetchWorkflows, previewWorkflow, fetchProjectsList, type Workflow, type Project, type TaskInfo, type TasksPage as TaskPage } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchWorkflows: vi.fn(async () => []), previewWorkflow: vi.fn(), fetchTasks: vi.fn(), fetchProjectsList: vi.fn(), fetchManagers: vi.fn(async () => []),
}));
vi.mock("@/pages/task-detail", () => ({ TaskDetailRoute: () => <div>Task detail</div> }));
vi.mock("@/components/agent-profiles", () => ({ AgentProfileSelect: ({ id, value, disabled }: { id: string; value: string; disabled?: boolean }) => <select id={id} value={value} disabled={disabled} onChange={() => {}}><option value={value}>{value}</option></select> }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const task = (id: string, day: number): TaskInfo => ({
  task_id: id, name: id, requirement: "Synthetic task", goal_id: "", status: "completed", group: "completed",
  phase: "completed", planner_id: null, correlation_id: "", created_at: `2026-10-${day}T00:00:00Z`,
  completed_at: null, project_id: "demo", project_name: "Demo", work_dir: "",
  schedule_id: null, schedule_name: null, continues_schedule_session: false,
});
const page = (tasks: TaskInfo[], next_before: string | null = null): TaskPage => ({
  tasks, next_before, counts: { completed: 5 }, hidden_scheduled: 0,
});
const newest = task("newest", 25);
const first = task("first", 24);
const boundary = task("boundary", 23);
const oldest = task("oldest", 22);

describe("task list polling and pagination", () => {
  it("keeps a spend hold in the unfinished list and labels it in words", async () => {
    await showNavigating();
    const held = { ...task("held", 25), status: "awaiting_budget", group: "needs_you" as const };
    await finish(0, { ...page([held]), counts: { awaiting_budget: 1 } });
    expect(container.textContent).toContain("等你决定花费");
    expect(container.textContent).not.toContain("awaiting_budget");
  });

  let container: HTMLDivElement;
  let root: Root;
  let requests: {
    filters: Parameters<typeof fetchTasks>[0];
    resolve: (value: TaskPage) => void;
    reject: (error: Error) => void;
  }[];

  beforeEach(() => {
    vi.mocked(fetchWorkflows).mockResolvedValue([]);
    vi.mocked(previewWorkflow).mockResolvedValue(["- dev：Development（codex/model）；回落至 Backup（claude-code/sonnet）",
      "- 评审面板：必需席位：review_a、review_b；建议席位：suggest；验收：全部通过",
      "- verify_staging：Verification（codex/model）；验证：只读，最多 2 轮"]);
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    requests = [];
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], projects_root: "" });
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
    vi.unstubAllGlobals();
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

  it("does not read the hidden list and refreshes once when visible", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    await show();
    await tick(30_000);
    expect(requests).toHaveLength(0);
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(requests).toHaveLength(1);
  });

  it("pauses the list in detail and reloads once on return", async () => {
    await show();
    await finish(0, page([first]));
    await act(async () => root.render(<TasksPage sub="first" />));
    vi.mocked(fetchTasks).mockClear();
    await tick(30_000);
    expect(fetchTasks).not.toHaveBeenCalled();
    await show();
    expect(fetchTasks).toHaveBeenCalledTimes(1);
  });

  it.each([
    { rows: [], defaultKey: null, expected: null },
    { rows: [{ key: "dev", name: "Development", profiles: {}, roles: [], review_panel: null, verification: null }], defaultKey: null, expected: "none" },
    { rows: [{ key: "dev", name: "Development", profiles: { worker: "fixed" }, roles: [], review_panel: { members: ["review_a", "review_b"], accept_when: { all_of: ["review_a", "review_b"] } }, verification: { role: "verify_staging", max_rounds: 2 } }], defaultKey: "dev", expected: "dev" },
  ])("selects a workflow in the create form ($expected)", async ({ rows, defaultKey, expected }) => {
    vi.mocked(fetchWorkflows).mockResolvedValue(rows as Workflow[]);
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [{ project_id: "demo", name: "Demo", work_dir: "", default_workflow: defaultKey } as Project], projects_root: "" });
    await show("project=demo");
    await finish(0, page([]));
    await click("New Task");
    const select = container.querySelector<HTMLSelectElement>("#workflow-choice");
    expect(select?.value ?? null).toBe(expected);
    if (expected === "dev") {
      expect(previewWorkflow).toHaveBeenCalledWith("dev", "demo", {});
      expect(container.textContent).toContain("Development（codex/model）；回落至 Backup（claude-code/sonnet）");
      expect(container.textContent).toContain("必需席位：review_a、review_b；建议席位：suggest；验收：全部通过");
      expect(container.textContent).toContain("验证：只读，最多 2 轮");
      const fixed = container.querySelector<HTMLSelectElement>("#new-task-profile-worker")!;
      expect(fixed.disabled).toBe(true);
      expect(fixed.value).toBe("fixed");
    }
    if (expected === "none") {
      await act(async () => { select!.value = "dev"; select!.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(select!.value).toBe("dev");
    }
    const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json({ status: "ok", result: { task_id: "created", project_id: "demo" } }));
    vi.stubGlobal("fetch", fetcher);
    await fill('textarea[placeholder="Describe what this task should accomplish..."]', "Implement a small change");
    await fill('input[placeholder="~/projects/my-app"]', "/workspace");
    await click("Create");
    const body = JSON.parse(fetcher.mock.calls[0][1]!.body as string);
    expect(body.workflow).toBe(expected === null ? undefined : "dev");
    expect(body.profile_overrides).toBeUndefined();
  });

  const showNavigating = async (query = "") => {
    const render = (q: string): void => {
      root.render(<TasksPage query={q} onNavigate={(_sub, next) => render(next || "")} />);
    };
    await act(async () => render(query));
  };
  const pressed = (label: string) => button(label)?.getAttribute("aria-pressed");

  it("shows the Running filter count and requests it without changing an executing badge", async () => {
    const executing: TaskInfo = { ...first, status: "executing", group: "in_progress" };
    const result = { ...page([executing]), counts: { running: 3, waiting: 1 } };
    await showNavigating("status=all");
    await finish(0, result);
    expect(button("Running3")).toBeDefined();
    expect(button("All4")).toBeDefined();
    expect(pressed("All4")).toBe("true");
    await click("Running3");
    expect(requests[1].filters?.status).toBe("running");
    expect(requests[1].filters?.statuses).toBeUndefined();
    await finish(1, result);
    expect(rows()).toEqual(["first"]);
    expect(container.textContent).toContain("Executing");
    expect(button("Running3")).toBeDefined();
    expect(pressed("Running3")).toBe("true");
  });

  describe("several status filters", () => {
    it("selects every unfinished status when the address has no status, group or schedule", async () => {
      expect([...DEFAULT_STATUS_FILTERS]).toEqual(["init", "running", "waiting", "waiting_user", "awaiting_budget", "pending"]);
      await showNavigating();
      expect(requests[0].filters?.statuses).toEqual([...DEFAULT_STATUS_FILTERS]);
      expect(requests[0].filters?.status).toBeUndefined();
      expect(pressed("All")).toBe("false");
      expect(pressed("Ready")).toBe("true");
      expect(pressed("Running")).toBe("true");
      expect(pressed("Waiting")).toBe("true");
      expect(pressed("Pending")).toBe("true");
      expect(pressed("Failed")).toBe("false");
      // Waiting for you is in the default, and stays hidden while its count is 0.
      expect(button("Waiting for you")).toBeUndefined();
      expect(button("Ready")?.className).toContain("bg-primary");
      expect(button("Failed")?.className).toContain("bg-muted");
    });

    it("toggles a chip on and off the current selection", async () => {
      await showNavigating();
      await click("Failed");
      expect(requests[1].filters?.statuses).toEqual(["init", "running", "waiting", "waiting_user", "awaiting_budget", "pending", "failed"]);
      await click("Ready");
      expect(requests[2].filters?.statuses).toEqual(["running", "waiting", "waiting_user", "awaiting_budget", "pending", "failed"]);
      expect(requests[2].filters?.status).toBeUndefined();
    });

    it("selects only the clicked chip from All, and returns to All when the last chip is cleared", async () => {
      await showNavigating("status=all");
      expect(requests[0].filters?.status).toBeUndefined();
      expect(requests[0].filters?.statuses).toBeUndefined();
      expect(pressed("All")).toBe("true");
      await click("Failed");
      expect(requests[1].filters?.status).toBe("failed");
      expect(requests[1].filters?.statuses).toBeUndefined();
      expect(pressed("Failed")).toBe("true");
      expect(pressed("All")).toBe("false");
      await click("Failed");
      expect(requests[2].filters?.status).toBeUndefined();
      expect(requests[2].filters?.statuses).toBeUndefined();
      expect(pressed("All")).toBe("true");
    });

    it("reads a comma-separated list and a single legacy status", async () => {
      await showNavigating("status=pending,init,running,waiting");
      expect(requests[0].filters?.statuses).toEqual(["init", "running", "waiting", "pending"]);
      await showNavigating("status=failed");
      expect(requests.at(-1)?.filters?.status).toBe("failed");
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
      await showNavigating("status=failed,bogus");
      expect(requests.at(-1)?.filters?.status).toBe("failed");
      await showNavigating("status=all");
      expect(requests.at(-1)?.filters?.status).toBeUndefined();
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
    });

    it("hides an empty Waiting for you chip unless that filter is the only selection", async () => {
      await showNavigating();
      expect(requests[0].filters?.statuses).toContain("waiting_user");
      expect(button("Waiting for you")).toBeUndefined();
      await click("Failed");
      expect(requests[1].filters?.statuses).toEqual(["init", "running", "waiting", "waiting_user", "awaiting_budget", "pending", "failed"]);
      expect(button("Waiting for you")).toBeUndefined();
      await finish(1, { ...page([]), counts: { waiting_user: 0, failed: 1, init: 1 } });
      expect(button("Waiting for you")).toBeUndefined();
      expect(pressed("Failed1")).toBe("true");
      await showNavigating("project=p1");
      await finish(requests.length - 1, { ...page([]), counts: { waiting_user: 2, init: 1 } });
      expect(button("Waiting for you2")).toBeDefined();
      expect(pressed("Waiting for you2")).toBe("true");
      await showNavigating("status=waiting_user");
      expect(requests.at(-1)?.filters?.status).toBe("waiting_user");
      expect(button("Waiting for you")).toBeDefined();
      expect(pressed("Waiting for you")).toBe("true");
    });

    it("lists every task for a group link or a schedule link, and keeps the default on a project link", async () => {
      await showNavigating("group=needs_you&scheduled=include");
      expect(requests[0].filters).toMatchObject({ group: "needs_you" });
      expect(requests[0].filters?.status).toBeUndefined();
      expect(requests[0].filters?.statuses).toBeUndefined();
      expect(requests[0].filters?.excludeScheduled).toBeUndefined();
      expect(pressed("All")).toBe("true");
      expect(pressed("Ready")).toBe("false");

      await showNavigating("group=needs_you");
      expect(requests.at(-1)?.filters?.group).toBe("needs_you");
      expect(requests.at(-1)?.filters?.status).toBeUndefined();
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
      expect(requests.at(-1)?.filters?.excludeScheduled).toBe(true);
      expect(pressed("All")).toBe("true");

      await showNavigating("schedule=sched-1");
      expect(requests.at(-1)?.filters?.scheduleId).toBe("sched-1");
      expect(requests.at(-1)?.filters?.status).toBeUndefined();
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
      expect(requests.at(-1)?.filters?.excludeScheduled).toBeUndefined();
      expect(pressed("All")).toBe("true");

      await showNavigating("scheduled=include");
      expect(requests.at(-1)?.filters?.statuses).toEqual([...DEFAULT_STATUS_FILTERS]);
      expect(requests.at(-1)?.filters?.excludeScheduled).toBeUndefined();

      await showNavigating("project=p1");
      expect(requests.at(-1)?.filters?.projectId).toBe("p1");
      expect(requests.at(-1)?.filters?.statuses).toEqual([...DEFAULT_STATUS_FILTERS]);

      await showNavigating("group=attention&status=failed");
      expect(requests.at(-1)?.filters?.group).toBe("attention");
      expect(requests.at(-1)?.filters?.status).toBe("failed");
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
    });

    it("drops status when All is chosen on a group or a schedule, and applies the default once that chip is cleared", async () => {
      const queries: string[] = [];
      const render = (q: string): void => {
        queries.push(q);
        root.render(<TasksPage query={q} onNavigate={(_sub, next) => render(next || "")} />);
      };
      await act(async () => render("group=needs_you&status=failed"));
      await click("All");
      expect(queries.at(-1)).toBe("group=needs_you");
      expect(requests.at(-1)?.filters?.status).toBeUndefined();
      expect(requests.at(-1)?.filters?.statuses).toBeUndefined();
      expect(pressed("All")).toBe("true");

      await act(async () => render("schedule=sched-1&status=failed"));
      await click("All");
      expect(queries.at(-1)).toBe("schedule=sched-1");

      await act(async () => render("group=needs_you"));
      const clear = container.querySelector<HTMLButtonElement>('button[aria-label="Show all tasks, not only needs you"]');
      expect(clear).toBeTruthy();
      await act(async () => clear!.click());
      expect(queries.at(-1)).not.toContain("group=");
      expect(queries.at(-1)).not.toContain("status=");
      expect(requests.at(-1)?.filters?.group).toBeUndefined();
      expect(requests.at(-1)?.filters?.statuses).toEqual([...DEFAULT_STATUS_FILTERS]);
    });
  });

  describe("hiding tasks created by schedules", () => {
    const scheduled: TaskInfo = { ...task("from-schedule", 21), schedule_id: "sched-1", schedule_name: "Report" };
    const showNavigating = async (query = "") => {
      const render = (q: string): void => {
        root.render(<TasksPage query={q} onNavigate={(_sub, next) => render(next || "")} />);
      };
      await act(async () => render(query));
    };
    const checkbox = () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;

    it("hides them by default and says how many it hid", async () => {
      await showNavigating();
      expect(requests[0].filters?.excludeScheduled).toBe(true);
      await finish(0, { ...page([first]), hidden_scheduled: 3 });
      expect(checkbox().checked).toBe(true);
      expect(checkbox().disabled).toBe(false);
      expect(container.textContent).toContain("Hide tasks from schedules");
      expect(container.textContent).toContain("3 hidden");
    });

    it("shows them when unchecked, keeps that in the address and refetches without the flag", async () => {
      await showNavigating("project=demo");
      await finish(0, { ...page([first]), hidden_scheduled: 3 });
      await act(async () => checkbox().click());
      expect(requests[1].filters?.excludeScheduled).toBeUndefined();
      expect(requests[1].filters?.projectId).toBe("demo");
      await finish(1, page([first, scheduled]));
      expect(checkbox().checked).toBe(false);
      expect(rows()).toEqual(["first", "from-schedule"]);
      expect(container.textContent).not.toContain("hidden");
      // The address carries scheduled=include, so a reload keeps the choice.
      await act(async () => root.render(<TasksPage query="project=demo&scheduled=include" />));
      expect(checkbox().checked).toBe(false);
      expect(requests.at(-1)?.filters?.excludeScheduled).toBeUndefined();
    });

    it("lists a schedule's own tasks: the box is disabled and the flag is not sent", async () => {
      await showNavigating("schedule=sched-1");
      expect(requests[0].filters?.scheduleId).toBe("sched-1");
      expect(requests[0].filters?.excludeScheduled).toBeUndefined();
      await finish(0, page([scheduled]));
      expect(checkbox().disabled).toBe(true);
      expect(checkbox().checked).toBe(false);
      expect(rows()).toEqual(["from-schedule"]);
    });

    it("does not insert a scheduled task from a live update while hiding them", async () => {
      await showNavigating();
      await finish(0, page([first]));
      await tick(30_000);
      expect(requests).toHaveLength(2);
      await finish(1, page([scheduled, first]));
      expect(rows()).toEqual(["first"]);
    });
  });

  const fill = async (selector: string, value: string) => {
    const field = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    await act(async () => {
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };

  it.each(["", "/tmp/looper-projects"])("creates a new project with blank directories only when projects_root is set (%s)", async (projectsRoot) => {
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], projects_root: projectsRoot });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => Response.json({ status: "ok", result: {
      task_id: "new-task", status: "init", project_id: "new-project", created_at: "2026-10-25T00:00:00Z",
    } }));
    vi.stubGlobal("fetch", fetcher);
    await show();
    await finish(0, page([]));
    await click("New Task");
    expect(container.querySelector<HTMLSelectElement>("#new-task-project")!.selectedOptions[0].textContent)
      .toContain("New project");
    await fill("#new-task-project-name", "Example");
    await fill('textarea[placeholder="Describe what this task should accomplish..."]', "Build an example");
    const projectDir = container.querySelector<HTMLInputElement>("#new-task-project-dir")!;
    const taskDir = container.querySelector<HTMLInputElement>('input[placeholder="~/projects/my-app"], input[placeholder^="Default: a new directory"]')!;
    expect(projectDir.value).toBe("");
    expect(taskDir.value).toBe("");
    expect(button("Create")!.disabled).toBe(!projectsRoot);
    if (projectsRoot) {
      expect(taskDir.placeholder).toBe(`Default: a new directory under ${projectsRoot}`);
      expect(container.textContent).toContain(`Leave empty: Looper creates the new project's directory under ${projectsRoot}.`);
      await click("Create");
      expect(fetcher).toHaveBeenCalledOnce();
      expect(fetcher.mock.calls[0][0]).toBe("/api/v1/tasks/create");
      const body = JSON.parse((fetcher.mock.calls[0][1] as RequestInit).body as string);
      expect(body).toEqual({ requirement: "Build an example", project: { name: "Example" } });
      expect(body).not.toHaveProperty("work_dir");
      expect(body.project).not.toHaveProperty("work_dir");
    } else {
      expect(taskDir.placeholder).toBe("~/projects/my-app");
      expect(container.textContent).toContain("Required: the new project has no default directory.");
      await click("Create");
      expect(fetcher).not.toHaveBeenCalled();
    }
  });

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
