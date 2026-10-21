// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const task = {
  task_id: "task-a", name: "task-a", requirement: "Build the A parser.", goal_id: "", status: "failed", group: "attention",
  phase: "failed", planner_id: null, correlation_id: "", created_at: "2026-10-17T08:00:00Z",
  updated_at: "2026-10-17T08:01:00Z", completed_at: "2026-10-17T08:01:00Z", project_id: "p1", project_name: "P",
  work_dir: "/w", cost: null, schedule_id: null, schedule_name: null, continues_schedule_session: false, config: {},
  origin_channel_id: "", result_summary: null, error: "it failed", fail_kind: null, task_budget_usd: null,
  own_budget_usd: null, steps: [{ id: "choice", step_key: "choice", kind: "gate", step_index: 0, status: "verified",
    instruction: "Decide", depends_on: [] }],
  journal: [], progress: [], gates: [], roles: [], builtin_role_stats: null, agents: null, requirement_versions: [],
  supplements: [], manager_actions: [], profile_overrides: {},
};
const planner = (id: string) => ({ kind: "planner", id: `planner-${id}`, can_send: true, task_id: id, name: `Task ${id}`, status: "running" });
const web = (id: string) => ({ session_id: id, channel: "web", message_count: 1, first_message_at: "", last_message_at: "", last_role: "user", last_content: "hi" });
const lark = (id: string) => ({ ...web(id), channel: "lark", last_content: "from lark" });

let requests: string[] = [];
function answer(url: string): unknown {
  requests.push(url);
  if (/\/api\/tasks\/task-a$/.test(url)) return task;
  if (/\/api\/tasks(\?|$)/.test(url)) return { tasks: [], counts: { failed: 2 }, next_before: null };
  if (url.includes("/chat/targets")) return { targets: [{ kind: "manager", id: "manager", can_send: true }, planner("task-a")] };
  if (url.includes("/chat/sessions")) return { sessions: [web("web-1"), lark("lark-1")] };
  if (url.includes("/chat/messages")) return [];
  if (url.includes("/activity")) return { activity: [] };
  if (url.includes("/conversation")) return { conversation: [] };
  if (url.endsWith("/projects")) return { projects: [{ project_id: "p1", name: "Proj", description: "", work_dir: "", task_count: 1, active_count: 0 }] };
  if (url.includes("/manager/session")) return { effective: { profile: null }, choices: [], session: null };
  if (url.includes("/api/gates")) return { gates: [], total: 0, limit: 50, offset: 0, counts: { open: 0, waiting_user: 0, waiting_planner: 0, closed: 0, all: 0 } };
  if (url.includes("/agent-settings")) return undefined; // a 404: the card says it could not load
  if (url.includes("/knowledge")) return { documents: [], skills: [], total_bytes: 0 };
  if (url.includes("/schedules")) return { schedules: [] };
  if (url.includes("/room")) return { enabled: true, seats: [], by_step: {}, more: false, counts: { messages: 0, by_kind: {}, questions: 0, unanswered: 0 }, messages: [] };
  return {};
}

describe("the dashboard keeps its place in the URL", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    requests = [];
    window.location.hash = "";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const body = answer(String(input));
      return body === undefined ? new Response("{}", { status: 404 }) : Response.json(body);
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
  const open = async (hash: string) => {
    window.location.hash = hash;
    await act(async () => root.render(<App />));
    await settle();
  };
  const goto = async (hash: string) => {
    await act(async () => {
      window.location.hash = hash;
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await settle();
  };
  const button = (label: string) => [...container.querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === label || b.textContent?.trim().startsWith(label));
  const click = async (label: string) => {
    const el = button(label);
    expect(el, label).toBeTruthy();
    await act(async () => el!.click());
    await settle();
  };
  const selectedTab = () => container.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.trim();
  const hash = () => window.location.hash;

  describe("a task's page", () => {
    it.each(["Pipeline", "Room", "Agents", "Sessions", "Conversation"])("opens the %s tab from the hash", async (label) => {
      await open(`#tasks/task-a?tab=${label.toLowerCase()}`);
      expect(selectedTab()).toBe(label);
    });

    it("opens the Overview with no tab, and writes no default", async () => {
      await open("#tasks/task-a");
      expect(selectedTab()).toBe("Overview");
      await click("Room");
      expect(hash()).toBe("#tasks/task-a?tab=room");
      await click("Overview");
      expect(hash()).toBe("#tasks/task-a");
    });

    it("falls back to the Overview for an unknown tab", async () => {
      await open("#tasks/task-a?tab=nonsense");
      expect(selectedTab()).toBe("Overview");
    });

    it("moves between tabs with Back and Forward", async () => {
      await open("#tasks/task-a");
      await click("Pipeline");
      await click("Agents");
      expect(selectedTab()).toBe("Agents");
      await goto("#tasks/task-a?tab=pipeline");
      expect(selectedTab()).toBe("Pipeline");
      await goto("#tasks/task-a");
      expect(selectedTab()).toBe("Overview");
    });

    it("still opens the gate a link names, on the Overview", async () => {
      vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
      await open("#tasks/task-a?gate=choice");
      expect(selectedTab()).toBe("Overview");
      await click("Pipeline");
      expect(hash()).toBe("#tasks/task-a?tab=pipeline");
      expect(selectedTab()).toBe("Pipeline");
    });

    it("keeps the Overview when a gate link names a tab too", async () => {
      vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
      await open("#tasks/task-a?gate=choice&tab=pipeline");
      expect(selectedTab()).toBe("Overview");
    });
  });

  describe("the tasks list", () => {
    it("opens the status filter from the hash and asks the server for it", async () => {
      await open("#tasks?status=failed");
      expect(requests.some((u) => u.includes("/api/tasks?") && u.includes("status=failed"))).toBe(true);
    });

    it("writes the chosen status, keeps the other filters, and writes no default", async () => {
      await open("#tasks?project=p1");
      await click("Failed");
      expect(new URLSearchParams(hash().split("?")[1]).get("status")).toBe("failed");
      expect(new URLSearchParams(hash().split("?")[1]).get("project")).toBe("p1");
      await click("All");
      expect(hash()).toBe("#tasks?project=p1");
    });

    it("lists every task for an unknown status", async () => {
      await open("#tasks?status=bogus");
      expect(requests.filter((u) => u.includes("/api/tasks?")).every((u) => !u.includes("status="))).toBe(true);
    });

    it("returns from a task to the list it was opened from, status included", async () => {
      await open("#tasks?status=failed");
      await goto("#tasks/task-a");
      await click("Back to tasks");
      expect(hash()).toBe("#tasks?status=failed");
    });
  });

  describe("the chat", () => {
    const heading = () => container.textContent ?? "";

    it("opens an agent from the hash, and the Manager when the hash drops it", async () => {
      await open("#chat?target=planner-task-a");
      expect(heading()).toContain("Planner · Task task-a");
      await goto("#chat");
      expect(heading()).not.toContain("Planner · Task task-a");
    });

    it("falls back to the Manager for an unknown agent", async () => {
      await open("#chat?target=planner-nope");
      expect(heading()).not.toContain("Planner · ");
    });

    it("opens a read-only channel conversation from the hash, and falls back when it is unknown", async () => {
      await open("#chat?conversation=lark-1");
      expect(container.querySelector("[data-viewing]")?.getAttribute("data-viewing")).toBe("lark-1");
      await goto("#chat?conversation=missing");
      expect(container.querySelector("[data-viewing]")).toBeNull();
    });
  });

  describe("pages that already keep their place", () => {
    it("opens a project's tab from the hash", async () => {
      await open("#projects/p1/schedules");
      expect(selectedTab()).toBe("Schedules");
      await goto("#projects/p1/nonsense");
      expect(selectedTab()).toBe("Overview");
    });

    it("opens the user gates filters from the hash", async () => {
      await open("#gates?state=closed");
      expect(requests.some((u) => u.includes("/gates") && u.includes("state=closed"))).toBe(true);
    });

    it("opens a schedule's list filter from the hash", async () => {
      await open("#schedules?enabled=true");
      expect(requests.some((u) => u.includes("schedules"))).toBe(true);
    });
  });
});
