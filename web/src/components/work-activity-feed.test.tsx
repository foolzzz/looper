// @vitest-environment happy-dom
import { clearWorkConversationMemory } from "@/lib/use-work-conversation-state";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkActivityFeed, openWorkGate, type WorkFeedRecord } from "./work-activity-feed";
import { type ActivityEvent, type EventWindow, type TaskDetail, type UserGate } from "@/lib/api";
import type { WorkPaneMessage } from "./work-conversation";
import { WorkPage } from "@/pages/work";

const api = vi.hoisted(() => ({
  fetchTaskActivityWindow: vi.fn(), streamTaskActivity: vi.fn(), fetchGateThread: vi.fn(),
  postGateMessage: vi.fn(), endGateDiscussion: vi.fn(),
  fetchTaskDetail: vi.fn(), fetchChatMessages: vi.fn(), fetchChatTargets: vi.fn(), sendChat: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), ...api }));
vi.mock("@/components/work-list", () => ({ WorkList: () => <aside>Tasks</aside> }));
vi.mock("@/components/work-switcher", () => ({ WorkSwitcher: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const time = "2026-11-01T10:00:00Z";
const activity = (seq: number, extra: Partial<ActivityEvent> = {}): ActivityEvent => ({
  seq, task_id: "A", worker: "worker", kind: "text", summary: `Activity ${seq}`, created_at: time,
  metadata: null, input_tokens: null, output_tokens: null, ...extra,
});
const gate = (extra: Partial<UserGate> = {}): UserGate => ({
  step: "decision", step_id: "gate-step", state: "open", topic: "Choose a direction", findings: "", summary: null,
  resolution: "", agenda: [], ended_by_user: false, plan_change: null, depends_on: [], waiting_on: "user",
  not_delivered: 0, retry_at: null, turns: 0, reminders: 0, opened_at: time, first_reply_at: null,
  closed_at: null, channel_posts: null, ...extra,
});
const task = (id = "A", extra: Partial<TaskDetail> = {}) => ({
  task_id: id, status: "running", group: "in_progress", gates: [], ...extra,
}) as TaskDetail;
function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}

describe("Work activity feed", () => {
  let container: HTMLDivElement;
  let root: Root;
  let emit: (event: ActivityEvent) => void;
  let finish: () => void;
  let cleanup: ReturnType<typeof vi.fn>;
  const onGateChanged = vi.fn();
  const show = async (node: ReactNode) => { await act(async () => root.render(node)); };
  const feed = (detail = task(), messages: WorkPaneMessage[] = [], records: WorkFeedRecord[] = []) => (
    <WorkActivityFeed identity={{ kind: "planner", taskId: detail.task_id }} task={detail}
      messages={messages} records={records} onGateChanged={onGateChanged} />
  );
  const keys = () => [...container.querySelectorAll("[data-feed-key]")].map(el => el.getAttribute("data-feed-key"));
  const button = (text: string) => [...container.querySelectorAll("button")].find(el => el.textContent === text)!;
  const click = async (el: Element) => { expect(el).toBeTruthy(); await act(async () => { (el as HTMLElement).click(); }); };
  const type = (el: HTMLTextAreaElement, value: string) => act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  beforeEach(() => {
    clearWorkConversationMemory();
    vi.resetAllMocks();
    cleanup = vi.fn();
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
    api.streamTaskActivity.mockImplementation((_id, _cursor, callback, done) => { emit = callback; finish = done; return cleanup; });
    api.fetchGateThread.mockResolvedValue({ messages: [] });
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTargets.mockResolvedValue([]);
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("deduplicates history/live ids and keeps equal-time source and numeric sequence order", async () => {
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [activity(10), activity(2), activity(2)], hasMore: false });
    const messages = [
      { id: "b", role: "assistant", content: "old", created_at: time },
      { id: "a", role: "user", content: "Question", created_at: time },
      { id: "b", role: "assistant", content: "Final answer", created_at: time },
    ];
    await show(feed(task(), messages));
    expect(api.streamTaskActivity).toHaveBeenCalledWith("A", 10, expect.any(Function), expect.any(Function));
    const before = keys();
    expect(before).toEqual(["message:a", "message:b", "activity:2", "activity:10"]);
    await act(async () => { emit(activity(10)); emit(activity(2)); });
    await show(feed(task(), [...messages].reverse()));
    expect(keys()).toEqual(before);
    expect(container.querySelectorAll('[data-message-id="b"]')).toHaveLength(1);
    expect(container.querySelector('[data-message-id="b"]')?.textContent).toContain("old");
  });

  it("reaches earlier activity beyond 1000, merges overlap, and retries the same before cursor", async () => {
    api.fetchTaskActivityWindow.mockResolvedValueOnce({ events: Array.from({ length: 1000 }, (_, i) => activity(i + 1001)), hasMore: true });
    await show(feed());
    await act(async () => emit(activity(2001)));
    api.fetchTaskActivityWindow.mockRejectedValueOnce(new Error("offline"));
    await click(button("加载更早活动"));
    expect(container.textContent).toContain("活动读取失败");
    api.fetchTaskActivityWindow.mockResolvedValueOnce({ events: [activity(1), activity(1001)], hasMore: false });
    await click(button("重试"));
    expect(api.fetchTaskActivityWindow).toHaveBeenLastCalledWith("A", 1001, expect.any(AbortSignal));
    expect(keys()).toHaveLength(1002);
    expect(keys()[0]).toBe("activity:1");
    expect(button("加载更早活动")).toBeUndefined();
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(1);
  });

  it("shows loading, empty, error and retry without opening a stream after a failed history read", async () => {
    const read = pending<EventWindow>(); api.fetchTaskActivityWindow.mockReturnValueOnce(read.promise);
    await show(feed());
    expect(container.textContent).toContain("正在加载活动");
    await act(async () => read.resolve({ events: [], hasMore: false }));
    expect(container.textContent).toContain("暂无活动");
    expect(api.streamTaskActivity).toHaveBeenCalledWith("A", 0, expect.any(Function), expect.any(Function));
    api.fetchTaskActivityWindow.mockRejectedValueOnce(new Error("offline"));
    await show(feed(task("B")));
    expect(container.textContent).toContain("活动读取失败");
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(1);
    await click(button("重试"));
    expect(container.textContent).not.toContain("活动读取失败");
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(2);
  });

  it("ignores late history, earlier pages and stream callbacks after switching or unmounting", async () => {
    const read = pending<EventWindow>(); api.fetchTaskActivityWindow.mockReturnValueOnce(read.promise);
    await show(feed());
    api.fetchTaskActivityWindow.mockResolvedValueOnce({ events: [activity(4, { task_id: "B" })], hasMore: false });
    await show(feed(task("B")));
    expect(keys()).toEqual(["activity:4"]);
    await act(async () => read.resolve({ events: [activity(99)], hasMore: true }));
    expect(keys()).toEqual(["activity:4"]);
    await show(feed(task()));
    const oldEmit = emit; const oldFinish = finish;
    api.fetchTaskActivityWindow.mockResolvedValueOnce({ events: [activity(8, { task_id: "C" })], hasMore: true });
    await show(feed(task("C")));
    const page = pending<EventWindow>(); api.fetchTaskActivityWindow.mockReturnValueOnce(page.promise);
    await click(button("加载更早活动"));
    await show(feed(task("B")));
    await act(async () => { oldEmit(activity(99)); oldFinish(); });
    expect(keys()).toEqual([]);
    await act(async () => page.resolve({ events: [activity(7, { task_id: "C" })], hasMore: false }));
    expect(keys()).toEqual([]);
    expect(cleanup).toHaveBeenCalled();
    const calls = cleanup.mock.calls.length;
    act(() => root.unmount()); root = createRoot(container);
    expect(cleanup.mock.calls.length).toBe(calls + 1);
  });

  it("does not subscribe for inactive tasks or Manager, and cleans up when a task becomes inactive", async () => {
    await show(feed(task("A", { group: "completed" })));
    expect(api.streamTaskActivity).not.toHaveBeenCalled();
    await show(feed()); expect(api.streamTaskActivity).toHaveBeenCalledTimes(1);
    await show(feed(task("A", { group: "completed" })));
    expect(cleanup).toHaveBeenCalledTimes(1);
    await show(<WorkActivityFeed identity={{ kind: "manager" }} messages={[]} />);
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(1);
    expect(api.fetchTaskActivityWindow).toHaveBeenCalledTimes(3);
  });

  it("shows a stopped activity stream as a retryable read failure while its task is active", async () => {
    await show(feed());
    await act(async () => finish());
    expect(container.textContent).toContain("活动读取失败");
    await click(button("重试"));
    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(2);
  });

  it("keeps earlier loaded activity when the selected task finishes", async () => {
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [activity(5), activity(6)], hasMore: true });
    await show(feed());
    api.fetchTaskActivityWindow.mockResolvedValueOnce({ events: [activity(1), activity(5)], hasMore: false });
    await click(button("加载更早活动"));
    await show(feed(task("A", { group: "completed" })));
    expect(keys()).toEqual(["activity:1", "activity:5", "activity:6"]);
    expect(button("加载更早活动")).toBeUndefined();
    expect(cleanup).toHaveBeenCalledTimes(1);
  });

  it("updates a folded tool in place, treats HTML as text, and leaves unknown events without actions", async () => {
    const record: WorkFeedRecord = { source: "tool", id: "shell-1", created_at: time, name: "shell", args: { command: "echo ok" } };
    const unknown: WorkFeedRecord = { source: "task-event", id: "event-1", created_at: time, kind: "future.event", text: "<img src=x onerror=alert(1)>" };
    await show(feed(task(), [], [record, { ...record, result: "<b>done</b>" }, unknown]));
    expect(keys()).toEqual(["task-event:event-1", "tool:shell-1"]);
    const tool = container.querySelector('details[data-feed-key="tool:shell-1"]')!;
    expect(tool.hasAttribute("open")).toBe(false);
    await click(tool.querySelector("summary")!);
    expect(tool.hasAttribute("open")).toBe(true);
    expect(tool.textContent).toContain("<b>done</b>");
    expect(tool.textContent).toContain("echo ok");
    expect(container.querySelectorAll('[data-feed-key="tool:shell-1"]')).toHaveLength(1);
    expect(container.querySelector("img, b")).toBeNull();
    const event = container.querySelector('[data-feed-key="task-event:event-1"]')!;
    expect(event.textContent).toContain("future.event");
    expect(event.querySelector("button, a, textarea")).toBeNull();
    await show(feed(task(), [], [record, { ...record, result: "failed", isError: true }, unknown]));
    expect(container.querySelector('details[data-feed-key="tool:shell-1"]')?.hasAttribute("open")).toBe(true);
    expect(container.textContent).toContain("失败");
  });

  it("keeps unknown activity neutral even when its text describes a planner decision", async () => {
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [activity(1, { kind: "future.kind", summary: "planner decided to stop" })], hasMore: false });
    await show(feed());
    expect(container.textContent).toContain("planner decided to stop");
    expect(container.querySelector('[data-feed-key="activity:1"] button')).toBeNull();
  });

  it("collapses phase text repeated by a reply into an inspectable status line", async () => {
    const reply = "Planner 已收到需求，会按新的方向继续。";
    const summary = `Planner answered a change to the task (none): ${reply}`;
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [activity(1, { kind: "phase", summary, detail: "Request accepted" })], hasMore: false });
    await show(feed(task(), [{ id: "answer", role: "assistant", content: reply, created_at: time }]));
    const event = container.querySelector('[data-feed-key="activity:1"] details')!;
    expect(event.hasAttribute("open")).toBe(false);
    expect(event.querySelector("summary")?.textContent).toBe("进度更新");
    expect(event.querySelector("summary")?.textContent).not.toContain(reply);
    expect(container.querySelector('[data-message-id="answer"]')?.textContent).toContain(reply);
    await click(event.querySelector("summary")!);
    expect(event.hasAttribute("open")).toBe(true);
    expect(event.textContent).toContain(summary);
    expect(event.textContent).toContain("Request accepted");
  });

  it("keeps task event protocol text behind a short expandable status", async () => {
    const raw = "Planner answered: Here is the same answer";
    await show(feed(task(), [], [{ source: "task-event", id: "change", kind: "phase", text: raw }]));
    const event = container.querySelector('[data-feed-key="task-event:change"] details')!;
    expect(event).not.toBeNull();
    expect(event.hasAttribute("open")).toBe(false);
    expect(event.querySelector("summary")?.textContent).toBe("任务事件");
    await click(event.querySelector("summary")!);
    expect(event.textContent).toContain(raw);
  });

  it("mounts one gate editor/thread, keeps a failed reply draft, and uses gate APIs for reply and end", async () => {
    const detail = task("A", { gates: [gate()] });
    await show(feed(detail));
    const input = container.querySelector<HTMLTextAreaElement>("#gate-decision-text")!;
    expect(container.querySelectorAll("#gate-decision")).toHaveLength(1);
    expect(container.querySelectorAll("textarea")).toHaveLength(1);
    expect(api.fetchGateThread).toHaveBeenCalledTimes(1);
    type(input, "Keep this answer");
    api.postGateMessage.mockRejectedValueOnce(new Error("send failed"));
    await click(button("发送"));
    expect(input.value).toBe("Keep this answer");
    expect(container.textContent).toContain("send failed");
    const updated = gate({ state: "discussing", waiting_on: "planner" });
    const posted = { gate: updated, message: { id: 1, role: "user", text: "Keep this answer", created_at: time }, delivered: "sent" };
    api.postGateMessage.mockResolvedValueOnce(posted);
    await click(button("发送"));
    expect(api.postGateMessage).toHaveBeenLastCalledWith("A", "decision", { text: "Keep this answer" });
    expect(input.value).toBe(""); expect(onGateChanged).toHaveBeenCalledWith(updated);
    type(input, "Conclude now"); api.endGateDiscussion.mockResolvedValueOnce(posted);
    await click(button("结束讨论，按当前结论继续"));
    expect(api.endGateDiscussion).toHaveBeenLastCalledWith("A", "decision", "Conclude now");
    expect(input.value).toBe("");
  });

  it("orders the single gate slot by latest opening and puts missing timestamps last", async () => {
    const messages = [{ id: "m", role: "user", content: "later", created_at: "2026-11-01T11:00:00Z" }];
    const gates = [gate(), gate({ step: "second", step_id: "second", opened_at: "2026-11-01T12:00:00Z" })];
    await show(feed(task("A", { gates }), messages));
    expect(keys()).toEqual(["message:m", "gates:A"]);
    await show(feed(task("A", { gates: [gate()] }), messages));
    expect(keys()).toEqual(["gates:A", "message:m"]);
    type(container.querySelector("textarea")!, "Preserve while moving");
    await show(feed(task("A", { gates: [gate({ opened_at: null })] }), messages));
    expect(keys()).toEqual(["message:m", "gates:A"]);
    expect(container.querySelector("textarea")!.value).toBe("Preserve while moving");
  });

  it.each(["passed", "cancelled", "completed"])("provides no write entry for a %s gate/task and focuses the existing anchor", async state => {
    const closed = state !== "completed";
    await show(feed(task("A", {
      group: closed ? "in_progress" : "completed",
      gates: [gate(closed ? { state: state as "passed" | "cancelled", closed_at: time } : {})],
    })));
    expect(container.querySelector("textarea, form")).toBeNull();
    expect(button("发送")).toBeUndefined();
    expect(button("结束讨论，按当前结论继续")).toBeUndefined();
    act(() => openWorkGate("decision"));
    const anchor = container.querySelector("#gate-decision")!;
    expect(document.activeElement === anchor || anchor.contains(document.activeElement)).toBe(true);
    if (closed) expect(anchor.hasAttribute("open")).toBe(true);
    expect(api.postGateMessage).not.toHaveBeenCalled();
  });

  it("right-panel navigation focuses the one discussion editor and does not add a polling reader", async () => {
    vi.useFakeTimers();
    await show(feed(task("A", { gates: [gate({ state: "discussing", waiting_on: "planner" })] })));
    const reads = api.fetchGateThread.mock.calls.length;
    act(() => openWorkGate("decision"));
    expect(document.activeElement?.id).toBe("gate-decision-text");
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(api.fetchGateThread.mock.calls.length).toBe(reads + 1);
    expect(container.querySelectorAll("#gate-decision-text")).toHaveLength(1);
  });

  it("updates the page's shared detail after a gate reply without remounting the editor or sending chat", async () => {
    const detail = task("A", { gates: [gate()] });
    api.fetchTaskDetail.mockResolvedValue(detail);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { activity: [], seq: 0 } })));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    const input = container.querySelector<HTMLTextAreaElement>("#gate-decision-text")!;
    type(input, "My answer");
    const updated = gate({ topic: "Updated shared gate", state: "discussing", waiting_on: "planner" });
    api.postGateMessage.mockResolvedValue({ gate: updated, message: { id: 1, role: "user", text: "My answer", created_at: time }, delivered: "sent" });
    await click(button("发送"));
    expect(container.textContent).toContain("Updated shared gate");
    expect(container.querySelector("#gate-decision-text")).toBe(input);
    expect(container.querySelectorAll("#gate-decision")).toHaveLength(1);
    expect(api.fetchTaskDetail).toHaveBeenCalledTimes(2);
    expect(api.streamTaskActivity).toHaveBeenCalledTimes(1);
    expect(api.sendChat).not.toHaveBeenCalled();
  });

  it("does not overwrite a successful gate reply with an older shared detail read", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const before = task("A", { gates: [gate()] });
    const stale = pending<TaskDetail>();
    api.fetchTaskDetail.mockResolvedValueOnce(before).mockReturnValueOnce(stale.promise);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { activity: [], seq: 0 } })));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    type(container.querySelector("#gate-decision-text")!, "My answer");
    const updated = gate({ topic: "Fresh answer", state: "discussing" });
    api.postGateMessage.mockResolvedValue({ gate: updated, message: { id: 1, role: "user", text: "My answer", created_at: time }, delivered: "kept" });
    await click(button("发送"));
    await act(async () => stale.resolve(before));
    expect(container.textContent).toContain("Fresh answer");
    expect(container.textContent).not.toContain("Choose a direction");
  });

  it("refreshes asynchronous gate changes through one shared detail reader and stops on task change", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    api.fetchTaskDetail.mockResolvedValue(task("A"));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { activity: [], seq: 0 } })));
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    api.fetchTaskDetail.mockResolvedValue(task("A", { gates: [gate()] }));
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(container.querySelectorAll("#gate-decision-text")).toHaveLength(1);
    type(container.querySelector("#gate-decision-text")!, "Unsent answer");
    api.fetchTaskDetail.mockRejectedValueOnce(new Error("detail unavailable"));
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(container.textContent).toContain("detail unavailable");
    expect(container.querySelector<HTMLTextAreaElement>("#gate-decision-text")!.value).toBe("Unsent answer");
    api.fetchTaskDetail.mockResolvedValue(task("A", { gates: [gate({ state: "passed", closed_at: time })] }));
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(container.querySelector("#gate-decision-text")).toBeNull();
    expect(container.textContent).toContain("Unsent answer");
    const late = pending<TaskDetail>(); api.fetchTaskDetail.mockReturnValueOnce(late.promise);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    api.fetchTaskDetail.mockResolvedValue(task("B", { group: "completed" }));
    await show(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    await act(async () => late.resolve(task("A", { gates: [gate()] })));
    expect(container.querySelector("#gate-decision")).toBeNull();
    const reads = api.fetchTaskDetail.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(api.fetchTaskDetail).toHaveBeenCalledTimes(reads);
  });
});
