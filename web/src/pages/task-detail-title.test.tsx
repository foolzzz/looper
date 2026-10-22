// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailRoute } from "@/pages/task-detail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A failed task (a page that stops polling, so only a save refreshes it).
function detail(title: string) {
  return {
    task_id: "task-a", name: title, title, requirement: "Build the A parser.", goal_id: "", status: "failed",
    group: "attention", phase: "failed", planner_id: null, correlation_id: "", created_at: "2026-10-17T08:00:00Z",
    updated_at: "2026-10-17T08:01:00Z", completed_at: "2026-10-17T08:01:00Z", project_id: "p1", project_name: "P",
    work_dir: "/w", cost: null, schedule_id: null, schedule_name: null, continues_schedule_session: false, config: {},
    origin_channel_id: "", result_summary: null, error: "it failed", fail_kind: null, task_budget_usd: null,
    own_budget_usd: null, steps: [], journal: [], progress: [], gates: [], roles: [], builtin_role_stats: null,
    agents: null, requirement_versions: [], supplements: [], manager_actions: [], profile_overrides: {},
  };
}

describe("the task page heading", () => {
  let container: HTMLDivElement;
  let root: Root;
  let title: string;
  let puts: { url: string; body: unknown }[];
  let refusal: string | null;

  beforeEach(() => {
    title = "Parser";
    puts = [];
    refusal = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PUT" && url.endsWith("/api/tasks/task-a/title")) {
        const body = JSON.parse(String(init.body));
        puts.push({ url, body });
        if (refusal) return Response.json({ error: refusal }, { status: 400 });
        title = body.title;
        return Response.json({ task_id: "task-a", title });
      }
      if (url.endsWith("/api/tasks/task-a")) return Response.json(detail(title));
      if (url.includes("/activity")) return Response.json({ activity: [] });
      if (url.includes("/conversation")) return Response.json({ conversation: [] });
      if (url.endsWith("/projects")) return Response.json({ projects: [] });
      return Response.json({});
    }));
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
  const heading = () => container.querySelector("h1")?.textContent;
  const input = () => container.querySelector<HTMLInputElement>("input[aria-label='Task title']");
  const open = async () => {
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
    await settle();
    await act(async () => container.querySelector<HTMLButtonElement>("button[aria-label='Rename task']")!.click());
  };
  const type = async (value: string) => {
    await act(async () => {
      const field = input()!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const key = async (name: string) => {
    await act(async () => { input()!.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true })); });
    await settle();
  };

  it("prefills the title and saves a new one with the Save button, then shows it", async () => {
    await open();
    expect(input()!.value).toBe("Parser");
    await type("Better parser");
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Save")!.click());
    await settle();
    expect(puts).toEqual([{ url: "/api/tasks/task-a/title", body: { title: "Better parser" } }]);
    expect(input()).toBeNull();
    expect(heading()).toBe("Better parser");
  });

  it("saves with Enter", async () => {
    await open();
    await type("Entered");
    await key("Enter");
    expect(puts.map((p) => p.body)).toEqual([{ title: "Entered" }]);
    expect(heading()).toBe("Entered");
  });

  it("cancels with Esc and sends nothing", async () => {
    await open();
    await type("Never saved");
    await key("Escape");
    expect(input()).toBeNull();
    expect(puts).toEqual([]);
    expect(heading()).toBe("Parser");
  });

  it("shows the server's error inline and keeps the input", async () => {
    refusal = "title must not be empty";
    await open();
    await type("Rejected");
    await key("Enter");
    expect(container.querySelector("[role='alert']")?.textContent).toBe("title must not be empty");
    expect(input()!.value).toBe("Rejected");
    expect(heading() ?? "").not.toBe("Rejected");
  });
});
