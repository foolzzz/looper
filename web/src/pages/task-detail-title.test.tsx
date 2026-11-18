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
  let writes: { url: string; body: unknown }[];
  let refusal: string | null;

  beforeEach(() => {
    title = "Parser";
    writes = [];
    refusal = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST" && url.endsWith("/api/v1/ops/task_update")) {
        const body = JSON.parse(String(init.body));
        writes.push({ url, body });
        if (refusal) return Response.json({ status: "error", error: { code: "invalid", message: refusal } }, { status: 400 });
        title = body.title;
        return Response.json({ status: "ok", result: { task_id: "task-a", changed: ["title"], title } });
      }
      if (url === "/api/v1/tasks/detail") return Response.json({ status: "ok", result: detail(title) });
      if (url === "/api/v1/tasks/activity") return Response.json({ status: "ok", result: { activity: [] } });
      if (url === "/api/v1/tasks/conversation") return Response.json({ status: "ok", result: { rows: [], has_more: false } });
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
    expect(writes).toEqual([{ url: "/api/v1/ops/task_update", body: { task_id: "task-a", title: "Better parser" } }]);
    expect(input()).toBeNull();
    expect(heading()).toBe("Better parser");
  });

  it("saves with Enter", async () => {
    await open();
    await type("Entered");
    await key("Enter");
    expect(writes.map((p) => p.body)).toEqual([{ task_id: "task-a", title: "Entered" }]);
    expect(heading()).toBe("Entered");
  });

  it("cancels with Esc and sends nothing", async () => {
    await open();
    await type("Never saved");
    await key("Escape");
    expect(input()).toBeNull();
    expect(writes).toEqual([]);
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

  it("keeps the title, status and actions in the header and folds the rest behind Details below lg", async () => {
    await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
    await settle();
    const button = (name: string) =>
      [...container.querySelectorAll("button")].find((b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === name);
    const idLine = [...container.querySelectorAll("span")].find((s) => s.textContent === "task-a")!;
    const project = container.querySelector("select[aria-label='Project']")!.closest(".contents")!;
    const header = button("Back to tasks")!.closest(".border-b")!;
    expect(heading()).toBe("Parser");
    expect(header.textContent).toContain("Failed");
    expect(button("Start")).toBeDefined();
    expect(button("Start over")).toBeDefined();
    const details = button("Details")!;
    expect(details.getAttribute("aria-expanded")).toBe("false");
    expect(idLine.classList).toContain("max-lg:hidden");
    expect(project.classList).toContain("max-lg:hidden");

    await act(async () => details.click());
    expect(details.getAttribute("aria-expanded")).toBe("true");
    expect(idLine.classList).not.toContain("max-lg:hidden");
    expect(project.classList).not.toContain("max-lg:hidden");
  });

  it.each([
    { strip: "wider than the screen", width: 600, revealed: true },
    { strip: "that fits", width: 0, revealed: false },
  ])("scrolls the newly selected tab into view only in a tab strip $strip", async ({ width, revealed }) => {
    // The test DOM lays nothing out: the tab strip reports the width its tabs would take.
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) { return this.getAttribute("role") === "tablist" ? width : 0; },
    });
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
    try {
      await act(async () => root.render(<TaskDetailRoute taskId="task-a" onBack={() => {}} />));
      await settle();
      const tab = (name: string) =>
        [...container.querySelectorAll<HTMLElement>("[role='tab']")].find((t) => t.textContent?.trim() === name)!;
      await act(async () => tab("Conversation").click());
      expect(tab("Conversation").getAttribute("aria-selected")).toBe("true");
      if (revealed) {
        expect(scroll.mock.contexts.at(-1)).toBe(tab("Conversation"));
        expect(scroll).toHaveBeenLastCalledWith({ block: "nearest", inline: "nearest" });
      } else {
        expect(scroll.mock.contexts.filter((el) => (el as HTMLElement).getAttribute("role") === "tab")).toEqual([]);
      }
    } finally {
      delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth;
      scroll.mockRestore();
    }
  });
});
