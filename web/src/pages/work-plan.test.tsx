// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkPage } from "@/pages/work";
import type { TaskDetail, TaskStep, UserGate } from "@/lib/api";

const api = vi.hoisted(() => ({
  fetchTaskDetail: vi.fn(),
  fetchChatMessages: vi.fn(),
  fetchChatTargets: vi.fn(),
  fetchTaskActivityWindow: vi.fn(),
  streamTaskActivity: vi.fn(),
  fetchGateThread: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), ...api }));
vi.mock("@/components/work-list", () => ({ WorkList: () => <aside>Tasks</aside> }));
vi.mock("@/components/work-switcher", () => ({ WorkSwitcher: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function step(partial: Partial<TaskStep> & Pick<TaskStep, "id" | "step_key" | "step_index">): TaskStep {
  return {
    task_id: "task 1", kind: "gate", instruction: "Decide", acceptance_criteria: [], target_files: [],
    depends_on: [], status: "waiting_user", review: false, role_key: null, role_version: null, review_roles: [],
    review_stage: null, review_position: null, retries: 0, worker_id: null, reviewer_id: null, result: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", completed_at: null, started_at: null,
    ...partial,
  };
}

function gate(partial: Partial<UserGate> & Pick<UserGate, "step" | "step_id">): UserGate {
  return {
    state: "cancelled", topic: partial.step, agenda: [], findings: "", summary: null, resolution: "not now",
    ended_by_user: false, plan_change: null, depends_on: [], waiting_on: null, not_delivered: 0, retry_at: null,
    turns: 0, reminders: 0, opened_at: "2026-11-01T00:00:00Z", first_reply_at: null, closed_at: "2026-11-01T00:00:00Z",
    channel_posts: null, ...partial,
  };
}

function detail(): TaskDetail {
  return {
    task_id: "task 1", title: "Refund plan", group: "completed", status: "completed", project_name: "Demo",
    steps: [
      step({ id: "g1", step_key: "ask/earlier", step_index: 0 }),
      step({ id: "g2", step_key: "ask/later", step_index: 1 }),
    ],
    gates: [
      gate({ step: "ask/earlier", step_id: "g1", closed_at: "2026-11-01T00:00:00Z" }),
      gate({ step: "ask/later", step_id: "g2", closed_at: "2026-11-02T00:00:00Z" }),
    ],
  } as TaskDetail;
}

let host: HTMLDivElement;
let root: Root;

async function settle() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await Promise.resolve(); });
}

async function show(node: ReactNode) {
  await act(async () => { root.render(node); });
  await settle();
}

beforeEach(() => {
  api.fetchTaskDetail.mockReset();
  api.fetchChatMessages.mockReset();
  api.fetchChatTargets.mockReset();
  api.fetchTaskActivityWindow.mockReset();
  api.streamTaskActivity.mockReset();
  api.fetchGateThread.mockReset();
  api.fetchTaskDetail.mockResolvedValue(detail());
  api.fetchChatMessages.mockResolvedValue([]);
  api.fetchChatTargets.mockResolvedValue([]);
  api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
  api.streamTaskActivity.mockReturnValue(() => {});
  api.fetchGateThread.mockResolvedValue({ messages: [] });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { activity: [{ seq: 1 }] } })));
  window.innerWidth = 1280;
  window.innerHeight = 800;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Work plan on the page", () => {
  it("opens, scrolls to and focuses the one gate editor from a step", async () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
    await show(<WorkPage sub="task/task%201" onNavigate={() => {}} />);
    const earlier = document.getElementById("gate-ask/earlier") as HTMLDetailsElement;
    const later = document.getElementById("gate-ask/later") as HTMLDetailsElement;
    expect(earlier).toBeInstanceOf(HTMLDetailsElement);
    expect(earlier.open).toBe(false);
    expect(later.open).toBe(true);
    expect(document.querySelectorAll('[id="gate-ask/earlier"]')).toHaveLength(1);

    const button = host.querySelector('[data-step-key="ask/earlier"] button') as HTMLButtonElement;
    await act(async () => { button.click(); });

    expect(earlier.open).toBe(true);
    expect(scroll).toHaveBeenCalledWith({ block: "nearest" });
    expect(document.activeElement).toBe(earlier.querySelector("summary"));
    expect(document.querySelectorAll('[id="gate-ask/earlier"]')).toHaveLength(1);
    expect(earlier.querySelector("textarea")).toBeNull();
  });

  it("scrolls the composer and the plan together on a short landscape screen", async () => {
    window.innerWidth = 844;
    window.innerHeight = 390;
    await show(<WorkPage sub="task/task%201" onNavigate={() => {}} />);
    const column = host.querySelector("[data-work-scroll]") as HTMLElement;
    const detail = host.querySelector("[data-work-detail]") as HTMLElement;
    const steps = host.querySelector("[data-work-steps-pane]") as HTMLElement;
    const composer = host.querySelector('textarea[aria-label="Message"]') as HTMLElement;
    const title = host.querySelector("#work-steps-title") as HTMLElement;
    const conversation = host.querySelector('section[aria-label="Conversation"]') as HTMLElement;
    expect(column).not.toBeNull();
    expect(column.className).toContain("overflow-y-auto");
    expect(detail.getAttribute("data-layout")).toBe("scroll");
    expect(steps.className).not.toContain("40dvh");
    expect(conversation.parentElement?.className).toContain("work-short-conversation");
    expect(column.contains(composer)).toBe(true);
    expect(column.contains(title)).toBe(true);
    expect(steps.contains(composer)).toBe(false);
    expect(conversation.contains(title)).toBe(false);
    expect(title.textContent).toBe("计划与步骤");
  });

  it("keeps the side plan on a tall screen", async () => {
    window.innerWidth = 1280;
    window.innerHeight = 800;
    await show(<WorkPage sub="task/task%201" onNavigate={() => {}} />);
    expect(host.querySelector("[data-work-scroll]")).toBeNull();
    const detail = host.querySelector("[data-work-detail]") as HTMLElement;
    const steps = host.querySelector("[data-work-steps-pane]") as HTMLElement;
    expect(detail.getAttribute("data-layout")).toBe("split");
    expect(steps.className).toContain("max-lg:max-h-[40dvh]");
    expect(steps.className).toContain("lg:w-80");
  });
});
