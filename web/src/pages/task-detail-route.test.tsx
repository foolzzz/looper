// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailRoute } from "@/pages/task-detail";
import { TasksPage } from "@/pages/tasks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Two failed tasks (a page that stops polling), each with a requirement the Overview alone shows.
const REQUIREMENTS: Record<string, string> = { "task-a": "Build the A parser.", "task-b": "Write the B docs." };

function detail(id: string) {
  return {
    task_id: id, name: id, requirement: REQUIREMENTS[id], goal_id: "", status: "failed", group: "attention",
    phase: "failed", planner_id: null, correlation_id: "", created_at: "2026-10-17T08:00:00Z",
    updated_at: "2026-10-17T08:01:00Z", completed_at: "2026-10-17T08:01:00Z", project_id: "p1", project_name: "P",
    work_dir: "/w", cost: null, schedule_id: null, schedule_name: null, continues_schedule_session: false, config: {}, origin_channel_id: "",
    result_summary: null, error: "it failed", fail_kind: null, task_budget_usd: null, own_budget_usd: null,
    steps: [], journal: [], progress: [], gates: [], roles: [], builtin_role_stats: null, agents: null,
    requirement_versions: [], supplements: [], manager_actions: [], profile_overrides: {},
  };
}

function answer(url: string): unknown {
  const task = url.match(/\/api\/tasks\/(task-[a-z]+)$/);
  if (task) return detail(task[1]);
  if (url === "/api/v1/tasks/activity") return { status: "ok", result: { activity: [] } };
  if (url === "/api/v1/tasks/conversation") return { status: "ok", result: { rows: [], has_more: false } };
  if (url.endsWith("/projects")) return { projects: [] };
  return {};
}

describe("TaskDetailRoute", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
      new Response(JSON.stringify(answer(String(input))), { status: 200, headers: { "Content-Type": "application/json" } })));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const settle = async () => {
    for (let i = 0; i < 5; i++) await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  };
  const show = async (taskId: string) => {
    await act(async () => root.render(<TaskDetailRoute taskId={taskId} onBack={() => {}} />));
    await settle();
  };
  const openTab = async (label: string) => {
    const tab = [...container.querySelectorAll("button")].find((button) => button.textContent?.trim() === label);
    expect(tab, label).toBeTruthy();
    await act(async () => tab!.click());
    await settle();
  };

  it.each(["stopped", "failed", "cancelled"])("resumes Sessions activity after starting a %s task", async (initialStatus) => {
    let status = initialStatus;
    let liveStream: ReadableStreamDefaultController<Uint8Array> | undefined;
    const activity = (seq: number) => ({
      seq, task_id: "task-a", worker: "worker-1", kind: "text", summary: `activity ${seq}`,
      metadata: null, input_tokens: null, output_tokens: null, created_at: "2026-10-24T00:00:00Z",
    });
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/tasks/task-a") return Response.json({ ...detail("task-a"), status });
      if (url === "/api/tasks/start") {
        status = "running";
        return Response.json({});
      }
      if (url === "/api/v1/tasks/activity") return Response.json({
        status: "ok", result: { activity: [activity(7)], has_more: false },
      });
      if (url === "/api/v1/tasks/activity_stream") return new Response(
        status === "running"
          ? new ReadableStream<Uint8Array>({ start(controller) { liveStream = controller; } })
          : "event: done\ndata: {}\n\n",
        { headers: { "Content-Type": "text/event-stream" } },
      );
      return Response.json(answer(url));
    });
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" tab="sessions" onTab={() => {}} onBack={() => {}} />));
    await settle();
    expect(container.textContent).toContain("activity 7");
    expect(container.textContent).not.toContain("Live"); // The terminal stream has delivered done.

    await openTab("Start"); // Stay on Sessions while the task starts again.
    expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Sessions");
    expect(liveStream).toBeDefined();
    expect(container.textContent).toContain("Live");
    await act(async () => {
      liveStream!.enqueue(new TextEncoder().encode(`event: activity\ndata: ${JSON.stringify(activity(8))}\n\n`));
    });
    await settle();
    expect(container.textContent).toContain("activity 7");
    expect(container.textContent).toContain("activity 8");
    expect(container.textContent).toContain("2 events");
    expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/tasks/activity")).toHaveLength(1);
  });

  it.each([false, true])("shows the schedule session note only after a confirmed continuation (%s)", async (continues) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.endsWith("/tasks/task-a")
        ? { ...detail("task-a"), schedule_id: "sched-a", schedule_name: "Daily check", continues_schedule_session: continues }
        : answer(url);
      return Response.json(data);
    }));
    await show("task-a");
    expect(container.textContent?.includes("continues the schedule's Planner session")).toBe(continues);
  });

  it("opens another task on its Overview, with none of the previous task's page", async () => {
    await show("task-a");
    expect(container.textContent).toContain("Build the A parser.");
    await openTab("Conversation");
    expect(container.textContent).not.toContain("Build the A parser."); // the Overview is not shown

    await show("task-b"); // the hash moved to another task
    expect(container.textContent).toContain("Write the B docs."); // its Overview
    expect(container.textContent).not.toContain("Build the A parser.");
  });

  const phaseCard = () =>
    [...container.querySelectorAll("p")].find((p) => p.textContent === "Phase" || p.textContent === "Last phase")?.parentElement;

  const taskWith = (status: string, phase: string, topic?: string) => ({
    ...detail("task-a"), status, group: status === "failed" ? "attention" : "in_progress", phase,
    waits: topic ? [{
      step: "w1", step_id: "step-w1", state: "waiting", outcome: null, topic,
      check: null, every_seconds: null, opened_at: "2026-10-20T08:00:00Z", due_at: "2026-10-21T08:00:00Z",
      deadline_at: "2026-10-27T08:00:00Z", next_check_at: null, last_checked_at: null,
      checks: 0, last_result: null, last_output: "", last_error: "", resolution: "", notice: null,
      closed_at: null, depends_on: [],
    }] : [],
  });
  const stubTask = (body: ReturnType<typeof taskWith>) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/tasks/task-a")) return Response.json(body);
      return Response.json(answer(url));
    }));
  };

  it("shows the waiting phase while the task is waiting", async () => {
    stubTask(taskWith("waiting", "executing", "CI finishes"));
    await show("task-a");
    expect(phaseCard()?.textContent).toContain("Waiting: CI finishes");
    expect(phaseCard()?.querySelector(".animate-spin")).toBeNull();
    // The header and the phase card both name the wait. The stored "executing" phase is not shown.
    expect(container.textContent?.split("Waiting: CI finishes").length).toBe(3);
    expect(container.textContent).not.toContain("Worker Executing");
  });

  it.each([
    ["running", "executing", "Worker Executing"],
    ["running", "planning", "Planning"],
    ["running", "verifying", "Verifying"],
    ["failed", "failed", "Failed"],
  ])("keeps a %s task's %s phase (%s)", async (status, phase, label) => {
    stubTask(taskWith(status, phase));
    await show("task-a");
    expect(phaseCard()?.textContent).toContain(label);
    expect(container.textContent).not.toContain("Waiting:");
  });

  it("renders a wait step with its own badge in the steps list", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/tasks/task-a") return Response.json({ ...detail("task-a"), steps: [{
        id: "step-w1", task_id: "task-a", step_key: "w1", step_index: 0, kind: "wait", status: "pending",
        instruction: "Wait for a revision", depends_on: [], acceptance_criteria: [], target_files: [], review_roles: [],
      }] });
      return Response.json(answer(String(input)));
    }));
    await show("task-a");
    const row = container.querySelector("#step-step-w1");
    expect(row?.textContent).toContain("wait w1");
    expect(row?.textContent).toContain("Not open yet");
    expect(row?.querySelector(".lucide-hourglass")).not.toBeNull();
  });

  it("does not show the previous task's data while the next one loads", async () => {
    await show("task-a");
    let release = () => {};
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => new Promise<Response>((resolve) => {
      release = () => resolve(new Response(JSON.stringify(answer(String(input))), { status: 200 }));
    })));
    await act(async () => root.render(<TaskDetailRoute taskId="task-b" onBack={() => {}} />));
    expect(container.textContent).not.toContain("Build the A parser.");
    release();
  });

  it("opens a dashboard discussion link on its task and focuses the requested gate", async () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
    const closed = (step: string) => ({
      step, step_id: step, state: "passed", waiting_on: null, topic: `Topic ${step}`,
      agenda: [], summary: { decisions: [], assumptions: [], open: [] }, plan_change: null,
      channel_posts: null, closed_at: null,
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/tasks/task-a")) return Response.json({ ...detail("task-a"),
        gates: [closed("choice"), closed("later")],
        steps: [{ id: "choice", step_key: "choice", kind: "gate", step_index: 0, status: "verified", instruction: "Decide", depends_on: [] }],
      });
      if (url === "/api/tasks") return Response.json({ tasks: [], counts: {}, next_before: null });
      return Response.json(answer(url));
    }));
    try {
      await act(async () => root.render(<TasksPage sub="task-a?gate=choice" />));
      await settle();
      const target = container.querySelector<HTMLDetailsElement>("#gate-choice");
      expect(target?.open).toBe(true);
      expect(document.activeElement).toBe(target!.querySelector("summary"));
      expect(scroll).toHaveBeenCalled();
      expect(container.textContent).toContain("Build the A parser.");
    } finally {
      scroll.mockRestore();
    }
  });

  it("clears the mounted room and reads from zero after Start over", async () => {
    let restarted = false;
    let releaseRoom = () => {};
    const reads: { task_id: string; after?: number }[] = [];
    const room = (id: number, body: string) => ({
      enabled: true, seats: [], by_step: {}, more: false,
      counts: { messages: 1, by_kind: { info: 1 }, questions: 0, unanswered: 0 },
      messages: [{ id, from: "worker@s1", to: "all", kind: "info", body, at: "2026-10-17T08:00:00Z",
                   cc_planner: false, answered: false }],
    });
    const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/start")) {
        restarted = true;
        return response({});
      }
      if (url.includes("/room")) {
        reads.push(JSON.parse(String(init?.body)));
        if (!restarted) return response({ status: "ok", result: room(7, "Previous run's message") });
        return new Promise<Response>((resolve) => {
          releaseRoom = () => resolve(response({ status: "ok", result: room(1, "Restarted run's message") }));
        });
      }
      if (url.endsWith("/tasks/task-a") && restarted) {
        return response({ ...detail("task-a"), status: "running", group: "in_progress" });
      }
      return response(answer(url));
    }));
    await show("task-a");
    await openTab("Room");
    expect(container.textContent).toContain("Previous run's message");

    await openTab("Start over"); // open the confirmation
    await openTab("Start over"); // restart while Room stays selected
    expect(container.textContent).not.toContain("Previous run's message");
    expect(reads.length).toBeGreaterThan(1);
    for (const body of reads.slice(1)) {
      expect(body).toMatchObject({ task_id: "task-a" });
      expect(body.after ?? 0).toBe(0);
    }
    await act(async () => releaseRoom());
    await settle();
    expect(container.textContent).toContain("Restarted run's message");
    expect(container.textContent).not.toContain("Previous run's message");
  });
});
