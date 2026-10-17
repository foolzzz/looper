// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailRoute } from "@/pages/task-detail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Two failed tasks (a page that stops polling), each with a requirement the Overview alone shows.
const REQUIREMENTS: Record<string, string> = { "task-a": "Build the A parser.", "task-b": "Write the B docs." };

function detail(id: string) {
  return {
    task_id: id, name: id, requirement: REQUIREMENTS[id], goal_id: "", status: "failed", group: "attention",
    phase: "failed", planner_id: null, correlation_id: "", created_at: "2026-10-17T08:00:00Z",
    updated_at: "2026-10-17T08:01:00Z", completed_at: "2026-10-17T08:01:00Z", project_id: "p1", project_name: "P",
    work_dir: "/w", cost: null, schedule_id: null, schedule_name: null, config: {}, origin_channel_id: "",
    result_summary: null, error: "it failed", fail_kind: null, task_budget_usd: null, own_budget_usd: null,
    steps: [], journal: [], progress: [], gates: [], roles: [], builtin_role_stats: null, agents: null,
    requirement_versions: [], supplements: [], manager_actions: [], profile_overrides: {},
  };
}

function answer(url: string): unknown {
  const task = url.match(/\/api\/tasks\/(task-[a-z]+)$/);
  if (task) return detail(task[1]);
  if (url.includes("/activity")) return { activity: [] };
  if (url.includes("/conversation")) return { conversation: [] };
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

  it("opens another task on its Overview, with none of the previous task's page", async () => {
    await show("task-a");
    expect(container.textContent).toContain("Build the A parser.");
    await openTab("Conversation");
    expect(container.textContent).not.toContain("Build the A parser."); // the Overview is not shown

    await show("task-b"); // the hash moved to another task
    expect(container.textContent).toContain("Write the B docs."); // its Overview
    expect(container.textContent).not.toContain("Build the A parser.");
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
});
