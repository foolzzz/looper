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
    work_dir: "/w", cost: null, schedule_id: null, schedule_name: null, continues_schedule_session: false, config: {}, origin_channel_id: "",
    result_summary: null, error: "it failed", fail_kind: null, task_budget_usd: null, own_budget_usd: null,
    steps: [], journal: [], progress: [], gates: [], roles: [], builtin_role_stats: null, agents: null,
    requirement_versions: [], supplements: [], manager_actions: [], profile_overrides: {},
  };
}


const TEMPLATE = {
  key: "pr", name: "PR review", updated_at: "2026-10-01T00:00:00Z",
  inputs: [{ key: "url", label: "Link", value: "https://github.com/o/r/pull/7" }],
  planner_instructions: "Read the diff.",
};

describe("TaskDetailRoute manager attribution", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const show = async (extra: Record<string, unknown>) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (/\/api\/tasks\/task-a$/.test(url)) return Response.json({ ...detail("task-a"), ...extra });
      if (url === "/api/v1/tasks/activity") return Response.json({ status: "ok", result: { activity: [] } });
      if (url === "/api/v1/tasks/conversation") return Response.json({ status: "ok", result: { rows: [], has_more: false } });
      return Response.json({});
    }));
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
    for (let i = 0; i < 5; i++) await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  };

  it("names the Manager and template and links the inputs", async () => {
    await show({ manager_id: "manager-review", manager_name: "Code review", template: TEMPLATE });
    expect(container.textContent).toContain("Created by Code review · template PR review");
    const link = container.querySelector('a[href="https://github.com/o/r/pull/7"]');
    expect(link?.getAttribute("rel")).toBe("noreferrer");
    expect(container.textContent).toContain("Read the diff.");
  });

  it("shows nothing new for a default Manager's task without a template", async () => {
    await show({ manager_id: "manager", manager_name: "Manager", template: null });
    expect(container.textContent).not.toContain("Created by");
  });

  const action = (extra: Record<string, unknown>) => ({
    manager_id: "manager", created_at: "2026-10-17T08:00:00Z", channel: "web", tool: "looper_task_control",
    arguments: { task_id: "task-a", action: "stop" }, risk: "write", status: "ok", confirmation_id: null, ...extra,
  });

  it("names the client of an action that came through loopercli", async () => {
    await show({ manager_actions: [action({ channel: "api", client_name: "laptop" })] });
    expect(container.textContent).toContain("loopercli · laptop");
    expect(container.textContent).not.toMatch(/\bapi ·/);
  });

  it("names the client when the Manager acted in a client's conversation", async () => {
    await show({ manager_actions: [action({ channel: "web", client_name: "laptop" })] });
    expect(container.textContent).toContain("loopercli · laptop");
  });

  it("shows the channel of an action without a client", async () => {
    await show({ manager_actions: [action({ channel: "telegram", client_name: null })] });
    expect(container.textContent).toContain("telegram ·");
    expect(container.textContent).not.toContain("loopercli");
  });
});
