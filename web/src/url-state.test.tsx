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
let chatReads: { path: string; session_id?: string }[] = [];
function answer(url: string, payload: Record<string, string>): unknown {
  requests.push(url);
  if (/\/api\/tasks\/task-a$/.test(url)) return task;
  if (/\/api\/tasks(\?|$)/.test(url)) return { tasks: [], counts: { failed: 2 }, next_before: null };
  if (url === "/api/v1/operator_chat/targets") return { status: "ok", result: { targets: [{ kind: "manager", id: "manager", can_send: true }, planner("task-a")] } };
  if (url === "/api/v1/operator_chat/sessions") {
    const asked = payload.session_id;
    if (asked === "lark-old") return { status: "ok", result: { sessions: [lark("lark-old")] } }; // past the newest 50: only by its id
    return { status: "ok", result: { sessions: asked ? [] : [web("web-1"), lark("lark-1")] } };
  }
  if (url === "/api/v1/operator_chat/messages") {
    chatReads.push({ path: url, session_id: payload.session_id });
    return { status: "ok", result: { messages: payload.session_id === "web-1"
      ? [{ message_id: "a1", role: "user", content: "The original conversation", created_at: "2026-01-01T00:00:00Z" }] : [] } };
  }
  if (url === "/api/v1/tasks/activity") return { status: "ok", result: { activity: [] } };
  if (url === "/api/v1/tasks/conversation") return { status: "ok", result: { rows: [], has_more: false } };
  if (url.endsWith("/projects")) return { projects: [{ project_id: "p1", name: "Proj", description: "", work_dir: "", task_count: 1, active_count: 0 }] };
  if (url.includes("/manager/session")) return { effective: { profile: null }, choices: [], session: null };
  if (url.includes("/api/v1/ops/gates/list")) return { status: "ok", result: { gates: [], total: 0, limit: 50, offset: 0, next_cursor: null, has_more: false, counts: { open: 0, waiting_user: 0, waiting_planner: 0, closed: 0, all: 0 } } };
  if (url.includes("/agent-settings")) return undefined; // a 404: the card says it could not load
  if (url === "/api/v1/project_knowledge/list") return { status: "ok", result: { project_id: "p1", documents: [], count: 0, stored_bytes: 0, index: "", index_chars: 0, caps: { document_max_bytes: 2097152, project_max_documents: 100, project_max_bytes: 52428800, index_max_chars: 4000 } } };
  if (url === "/api/v1/project_knowledge/drafts") return { status: "ok", result: { project_id: "p1", drafts: [], count: 0 } };
  if (url === "/api/v1/schedule_admin/list" || url === "/api/v1/schedule_admin/project_list") return { status: "ok", result: { schedules: [] } };
  if (url.includes("/room")) return { status: "ok", result: { enabled: true, seats: [], by_step: {}, more: false, counts: { messages: 0, by_kind: {}, questions: 0, unanswered: 0 }, messages: [] } };
  return {};
}

describe("the dashboard keeps its place in the URL", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    requests = [];
    chatReads = [];
    window.location.hash = "";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const body = answer(String(input), init?.body ? JSON.parse(String(init.body)) : {});
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

    it("names the stored thread in a bare chat address before starting another", async () => {
      localStorage.setItem("looper-chat-session-id", "web-1");
      window.location.hash = "#chat";
      const entries = window.history.length;
      await open("#chat");
      const previous = hash();
      expect(previous).toBe("#chat?conversation=web-1");
      expect(window.history.length).toBe(entries);
      expect(heading()).toContain("The original conversation");
      const newChat = container.querySelector('button[title^="New conversation"]') as HTMLButtonElement;
      await act(async () => newChat.click());
      const next = hash();
      expect(next).not.toBe(previous);
      expect(heading()).not.toContain("The original conversation");
      await act(async () => window.history.back());
      await settle();
      expect(localStorage.getItem("looper-chat-session-id")).toBe("web-1");
      expect(hash()).toBe(previous);
      expect(heading()).toContain("The original conversation");
      await act(async () => window.history.forward());
      await settle();
      expect(localStorage.getItem("looper-chat-session-id")).toBe(new URLSearchParams(next.split("?")[1]).get("conversation"));
      expect(heading()).not.toContain("The original conversation");
    });

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

  describe("the chat conversations", () => {
    it("opens a conversation older than the list by its id", async () => {
      await open("#chat?conversation=lark-old");
      expect(container.querySelector("[data-viewing]")?.getAttribute("data-viewing")).toBe("lark-old");
    });

    it("keeps a conversation open while the list no longer holds it", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        await open("#chat?conversation=lark-old");
        await act(async () => vi.advanceTimersByTimeAsync(20000));
        await settle();
        expect(container.querySelector("[data-viewing]")?.getAttribute("data-viewing")).toBe("lark-old");
      } finally {
        vi.useRealTimers();
      }
    });

    it("opens the web thread the address names, and writes the one chosen", async () => {
      localStorage.setItem("looper-chat-session-id", "session-other");
      await open("#chat?conversation=web-1");
      expect(localStorage.getItem("looper-chat-session-id")).toBe("web-1");
      expect(chatReads).toContainEqual({ path: "/api/v1/operator_chat/messages", session_id: "web-1" });
      await act(async () => (container.querySelector('button[title^="New conversation"]') as HTMLButtonElement).click());
      await settle();
      expect(new URLSearchParams(window.location.hash.split("?")[1]).get("conversation")).toBe(localStorage.getItem("looper-chat-session-id"));
      expect(localStorage.getItem("looper-chat-session-id")).not.toBe("web-1");
    });
  });

  describe("the room's filters", () => {
    const pressed = (label: string) => button(label)?.getAttribute("aria-pressed");
    const roleSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="From or to a role"]');

    it("keeps the kind and role filters when selecting Room again", async () => {
      await open("#tasks/task-a?tab=room&filter=unanswered&role=planner");
      await click("Room");
      expect(hash()).toBe("#tasks/task-a?tab=room&filter=unanswered&role=planner");
      expect(pressed("Unanswered")).toBe("true");
      expect(roleSelect()?.value).toBe("planner");
    });

    it("opens the kind and role filters from the hash", async () => {
      await open("#tasks/task-a?tab=room&filter=unanswered&role=planner");
      expect(pressed("Unanswered")).toBe("true");
      expect(roleSelect()?.value).toBe("planner");
    });

    it("writes the chosen kind and keeps the role, and writes no default", async () => {
      await open("#tasks/task-a?tab=room&role=planner");
      await click("Interface");
      expect(hash()).toBe("#tasks/task-a?tab=room&filter=interface&role=planner");
      await click("All");
      expect(hash()).toBe("#tasks/task-a?tab=room&role=planner");
      await goto("#tasks/task-a?tab=room&filter=bogus");
      expect(pressed("All")).toBe("true");
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
      expect(vi.mocked(fetch).mock.calls.some(([url, options]) =>
        url === "/api/v1/ops/gates/list" && JSON.parse(String(options?.body)).state === "closed")).toBe(true);
    });

    it("opens a schedule's list filter from the hash", async () => {
      await open("#schedules?enabled=true");
      expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/v1/schedule_admin/list", expect.objectContaining({
        method: "POST", body: JSON.stringify({}),
      }));
      expect(container.querySelector<HTMLSelectElement>('select[id$="-status"]')?.value).toBe("enabled");
      expect(container.textContent).toContain("No schedules yet.");
    });
  });
});
