// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkGates } from "./work-gates";
import { WorkPage } from "@/pages/work";
import type { TaskDetail, UserGate } from "@/lib/api";
import { gateAnchor } from "@/lib/gates";

const api = vi.hoisted(() => ({ fetchTaskDetail: vi.fn(), fetchTaskActivityWindow: vi.fn(),
  streamTaskActivity: vi.fn(), fetchGateThread: vi.fn(), postGateMessage: vi.fn(),
  endGateDiscussion: vi.fn(), fetchChatMessages: vi.fn(), fetchChatTargets: vi.fn(), sendChat: vi.fn() }));
vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), ...api }));
vi.mock("@/components/work-list", () => ({ WorkList: () => <aside>Tasks</aside> }));
vi.mock("@/components/work-switcher", () => ({ WorkSwitcher: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const gate = (extra: Partial<UserGate> = {}): UserGate => ({
  step: "decision", step_id: "gate-step", state: "open", topic: "Choose a direction", findings: "", summary: null,
  resolution: "", agenda: [], ended_by_user: false, plan_change: null, depends_on: [], waiting_on: "user",
  not_delivered: 0, retry_at: null, turns: 0, reminders: 0, opened_at: "2026-11-01T10:00:00Z",
  first_reply_at: null, closed_at: null, channel_posts: null, ...extra,
});
const task = (extra: Partial<TaskDetail> = {}) => ({ task_id: "A", title: "Task A", status: "running",
  group: "in_progress", gates: [gate()], ...extra }) as TaskDetail;
let host: HTMLDivElement, root: Root;
const show = async (node: ReactNode) => { await act(async () => root.render(node)); };
const click = async (el: Element | null) => { expect(el).toBeTruthy(); await act(async () => (el as HTMLElement).click()); };
const key = async (el: Element, value: string) => { await act(async () => {
  el.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true }));
}); };
const button = (text: string) => [...host.querySelectorAll("button")].find(el => el.textContent === text)!;
beforeEach(() => {
  vi.resetAllMocks();
  api.fetchTaskDetail.mockResolvedValue(task());
  api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
  api.fetchGateThread.mockResolvedValue({ messages: [] });
  api.fetchChatMessages.mockResolvedValue([]); api.fetchChatTargets.mockResolvedValue([]);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { activity: [], seq: 0 } })));
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Work gate summaries", () => {
  it("shows an empty section without gate reads or writes", async () => {
    await show(<WorkGates task={task({ gates: [] })} onOpenGate={vi.fn()} />);
    expect(host.textContent).toContain("关卡"); expect(host.textContent).toContain("没有关卡");
    expect(host.querySelector("textarea, form, [id]" )).toBeNull();
    expect(api.fetchGateThread).not.toHaveBeenCalled(); expect(api.postGateMessage).not.toHaveBeenCalled();
  });
  it.each([
    ["open", "user", "Waiting for you"], ["discussing", "user", "Your turn"],
    ["discussing", "planner", "Planner answering"], ["passed", null, "Passed"],
    ["cancelled", null, "Cancelled"], ["pending", null, "Not open yet"],
    ["skipped", null, "Skipped"], ["future", null, "状态未知"],
  ])("uses the gate fields for %s/%s", async (state, waiting_on, label) => {
    const value = gate({ state, waiting_on } as Partial<UserGate>);
    await show(<WorkGates task={task({ gates: [value] })} onOpenGate={vi.fn()} />);
    expect(host.textContent).toContain(label); expect(host.textContent).toContain(value.step);
    expect(host.textContent).toContain(value.topic);
    expect(api.fetchGateThread).not.toHaveBeenCalled();
  });
  it("mouse, Enter and Space locate exactly the same step, and missing steps have no target", async () => {
    const open = vi.fn();
    await show(<WorkGates task={task()} onOpenGate={open} />);
    const row = host.querySelector("button")!;
    await click(row); await key(row, "Enter"); await key(row, " ");
    expect(open.mock.calls).toEqual([["decision"], ["decision"], ["decision"]]);
    await show(<WorkGates task={task({ gates: [gate({ step: "" })] })} onOpenGate={open} />);
    await click(host.querySelector("button"));
    expect(open).toHaveBeenCalledTimes(3); expect(host.textContent).toContain("关卡已更新，请刷新");
  });
  it.each(["pending", "skipped", "future"])("does not offer navigation for an unmounted %s gate", async state => {
    const open = vi.fn();
    await show(<WorkGates task={task({ gates: [gate({ state } as Partial<UserGate>)] })} onOpenGate={open} />);
    const row = host.querySelector("button")!;
    expect(row.disabled).toBe(true);
    expect(row.textContent).toContain("没有对话卡片");
    await click(row); await key(row, "Enter"); await key(row, " ");
    expect(open).not.toHaveBeenCalled();
  });
  it("expands and scrolls the non-latest closed gate inside the current conversation", async () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
    api.fetchTaskDetail.mockResolvedValue(task({ gates: [
      gate({ state: "passed", closed_at: "2026-11-01T00:00:00Z" }),
      gate({ step: "later", step_id: "later-id", state: "cancelled", closed_at: "2026-11-02T00:00:00Z" }),
    ] }));
    const foreign = document.createElement("details"); foreign.id = gateAnchor("decision");
    foreign.innerHTML = "<summary>Another conversation</summary>"; document.body.prepend(foreign);
    try {
      await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
      const anchor = host.querySelector(`[id="${gateAnchor("decision")}"]`) as HTMLDetailsElement;
      expect(anchor).toBeInstanceOf(HTMLDetailsElement);
      expect(anchor.open).toBe(false);
      expect((host.querySelector("#gate-later") as HTMLDetailsElement).open).toBe(true);
      await click(button("任务详情")); scroll.mockClear();
      await click(host.querySelector('[aria-label="关卡"] button'));
      expect(anchor.open).toBe(true);
      expect(scroll.mock.contexts).toEqual([anchor]);
      expect(scroll).toHaveBeenCalledWith({ block: "nearest" });
      expect(document.activeElement).toBe(anchor.querySelector("summary"));
      expect(foreign.open).toBe(false);
      expect(host.querySelectorAll(`[id="${gateAnchor("decision")}"]`)).toHaveLength(1);
    } finally { foreign.remove(); }
  });
  it("closes mobile details before focusing the single editor and restores the opener on dismissal", async () => {
    vi.useFakeTimers();
    api.fetchTaskDetail.mockResolvedValue(task({ gates: [gate({ state: "discussing", waiting_on: "planner" })] }));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    const opener = button("任务详情"); opener.focus(); await click(opener);
    expect(host.querySelector('[role="dialog"]')?.contains(document.activeElement)).toBe(true);
    expect(host.querySelectorAll("#gate-decision")).toHaveLength(1);
    expect(host.querySelectorAll("#gate-decision-text")).toHaveLength(1);
    expect(api.fetchGateThread).toHaveBeenCalledTimes(1);
    await key(document.activeElement!, "Escape"); expect(document.activeElement).toBe(opener);
    await click(opener); await click(host.querySelector('[aria-label="关卡"] button'));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement?.id).toBe("gate-decision-text");
    expect(host.querySelectorAll("#gate-decision")).toHaveLength(1);
    expect(host.querySelectorAll("#gate-decision-text")).toHaveLength(1);
    expect(api.fetchGateThread).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(api.fetchGateThread).toHaveBeenCalledTimes(2);
    expect(api.postGateMessage).not.toHaveBeenCalled(); expect(api.endGateDiscussion).not.toHaveBeenCalled();
    expect(api.sendChat).not.toHaveBeenCalled();
  });
  it("shows one desktop summary and focuses the existing editor without mounting a dialog", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(button("任务详情")).toBeUndefined();
    await key(host.querySelector('[aria-label="关卡"] button')!, "Enter");
    expect(document.activeElement?.id).toBe("gate-decision-text");
    expect(host.querySelectorAll("#gate-decision")).toHaveLength(1);
    expect(api.fetchGateThread).toHaveBeenCalledTimes(1);
  });
  it.each(["passed", "cancelled", "completed"])("locates a read-only %s gate/task without posting", async state => {
    api.fetchTaskDetail.mockResolvedValue(task(state === "completed" ? { status: state, group: "completed" }
      : { gates: [gate({ state: state as "passed" | "cancelled" })] }));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    await click(button("任务详情")); await click(host.querySelector('[aria-label="关卡"] button'));
    const anchor = host.querySelector("#gate-decision")!;
    expect(anchor.contains(document.activeElement) || anchor === document.activeElement).toBe(true);
    expect(anchor.querySelector("textarea, form")).toBeNull();
    expect(api.postGateMessage).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
  });
  it("does not locate another task's same-named step when the current target is removed", async () => {
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    await click(button("任务详情"));
    host.querySelector("#gate-decision")!.removeAttribute("id");
    const foreign = document.createElement("textarea"); foreign.id = "gate-decision"; document.body.append(foreign);
    try {
      await click(host.querySelector('[aria-label="关卡"] button'));
      expect(host.textContent).toContain("关卡已更新，请刷新"); expect(document.activeElement).not.toBe(foreign);
      expect(host.querySelector('[role="dialog"]')).toBeNull();
      expect(document.activeElement).toBe(button("任务详情"));
    } finally { foreign.remove(); }
  });
  it.each(["pending", "skipped"])("gives truthful feedback and restores focus for a %s step entry", async state => {
    api.fetchTaskDetail.mockResolvedValue(task({ gates: [gate({ state } as Partial<UserGate>)], steps: [
      { id: "gate-step", task_id: "A", step_key: "decision", step_index: 0, kind: "gate", instruction: "Decide",
        depends_on: [], acceptance_criteria: [], target_files: [], status: "pending", review: false,
        role_key: null, role_version: null, review_roles: [], review_stage: null, review_position: null,
        retries: 0, worker_id: null, reviewer_id: null, result: null, created_at: "", updated_at: "",
        completed_at: null, started_at: null },
    ] }));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    await click(button("任务详情"));
    await click(host.querySelector('[data-step-key="decision"] button'));
    expect(host.textContent).toContain("没有对话卡片");
    expect(host.textContent).not.toContain("关卡已更新，请刷新");
    expect(document.activeElement).toBe(button("任务详情"));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });
  it("rejects a detail response for another task before showing its gates", async () => {
    api.fetchTaskDetail.mockResolvedValue(task({ task_id: "B" }));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.textContent).toContain("任务详情与当前任务不匹配，请刷新");
    expect(host.querySelector('[aria-label="关卡"], #gate-decision')).toBeNull();
    expect(api.fetchGateThread).not.toHaveBeenCalled();
  });
});
