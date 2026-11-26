// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createWorkConversationStore, workConversationKey, WorkConversationStateProvider, useWorkConversationState } from "./use-work-conversation-state";
import type { WorkIdentity } from "@/components/work-conversation";
import { PlannerWorkConversation, WorkConversation, type WorkConversationProps } from "@/components/work-conversation";
import { clearWorkConversationMemory } from "./use-work-conversation-state";
import { useWorkManagerConversation } from "./use-work-manager-conversation";
import { ForeignConversationError, type DbChatMessage, type TaskDetail } from "./api";

const api = vi.hoisted(() => ({ fetchChatMessages: vi.fn(), fetchChatTargets: vi.fn(), fetchChatTurn: vi.fn(),
  fetchChatSettings: vi.fn(), watchChat: vi.fn(), sendChat: vi.fn(), streamChatReply: vi.fn(), stopChatReply: vi.fn(),
  fetchTaskActivityWindow: vi.fn() }));
vi.mock("./api", async original => ({ ...await original<typeof import("./api")>(), ...api }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const a: WorkIdentity = { kind: "planner", taskId: "a" };
const b: WorkIdentity = { kind: "planner", taskId: "b" };
const manager: WorkIdentity = { kind: "manager", managerId: "manager", sessionId: "thread" };
let host: HTMLDivElement;
let root: Root;
let state: ReturnType<typeof useWorkConversationState>;
function Pane({ identity }: { identity: WorkIdentity }) {
  state = useWorkConversationState(identity);
  return <textarea value={state.value.draft} onChange={event => state.setDraft(event.target.value)} />;
}
beforeEach(() => {
  vi.resetAllMocks(); clearWorkConversationMemory(); localStorage.clear();
  api.fetchChatMessages.mockResolvedValue([]); api.fetchChatTargets.mockResolvedValue(["a", "b"].map(task_id => ({ kind: "planner", task_id, can_send: true })));
  api.fetchChatSettings.mockResolvedValue({ stop_enabled: false }); api.fetchChatTurn.mockResolvedValue(null);
  api.watchChat.mockReturnValue(vi.fn()); api.streamChatReply.mockReturnValue(vi.fn()); api.sendChat.mockResolvedValue({ correlation_id: "reply" });
  api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
  sessionStorage.clear(); host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});

const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
const dbRow = (id: string, role = "assistant"): DbChatMessage => ({ message_id: id, role, content: id, created_at: "2026-01-01T00:00:00Z" });
function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject };
}
const mountTask = async (taskId: string) => { await act(async () => root.render(<PlannerWorkConversation key={taskId} taskId={taskId} />)); await flush(); };

it("shows task cache before deferred revalidation, retains failures and drops late A reads", async () => {
  api.fetchChatMessages.mockResolvedValue([dbRow("cached A")]); await mountTask("a");
  await mountTask("b");
  const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(pending.promise);
  await mountTask("a"); expect(host.textContent).toContain("cached A");
  await act(async () => pending.reject(new Error("offline")));
  expect(host.textContent).toContain("cached A"); expect(host.textContent).toContain("刷新失败");
  const late = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValueOnce(late.promise).mockResolvedValue([dbRow("fresh B")]);
  await mountTask("b"); await mountTask("a"); await mountTask("b");
  await act(async () => late.resolve([dbRow("late B")]));
  expect(host.querySelector("section")?.dataset.identity).toBe("planner:b");
  expect(host.textContent).toContain("fresh B"); expect(host.textContent).not.toContain("late B");
  expect(api.stopChatReply).not.toHaveBeenCalled();
});

it("gates real Manager cache until scoped ownership, rejects foreign cache and preserves confirmed refresh failures", async () => {
  let adapter!: ReturnType<typeof useWorkManagerConversation>;
  function Manager() { adapter = useWorkManagerConversation(); return <WorkConversation {...adapter} />; }
  localStorage.setItem("looper-chat-session-id", "thread");
  api.fetchChatMessages.mockResolvedValue([dbRow("private Manager")]);
  await act(async () => root.render(<Manager />)); await flush();
  expect(host.textContent).toContain("private Manager");
  await mountTask("a"); const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(pending.promise);
  api.watchChat.mockClear(); api.fetchChatTurn.mockClear();
  await act(async () => root.render(<Manager />)); await flush();
  expect(host.textContent).not.toContain("private Manager"); expect(host.querySelector("textarea")?.disabled).toBe(true);
  act(() => adapter.onSend("blocked before ownership"));
  expect(api.watchChat).not.toHaveBeenCalled(); expect(api.fetchChatTurn).not.toHaveBeenCalled();
  await act(async () => pending.resolve([])); expect(host.textContent).toContain("private Manager");
  api.fetchChatMessages.mockRejectedValue(new Error("offline"));
  // Retry uses the already confirmed owner, so a network failure retains that content.
  await act(async () => adapter.onRetry?.()); await flush();
  expect(host.textContent).toContain("private Manager"); expect(host.textContent).toContain("刷新失败");
  await mountTask("b"); const foreign = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(foreign.promise);
  api.watchChat.mockClear();
  await act(async () => root.render(<Manager />)); await flush();
  api.fetchChatMessages.mockReturnValue(new Promise(() => {}));
  await act(async () => foreign.reject(new ForeignConversationError("foreign", "other")));
  act(() => adapter.onSend("blocked foreign"));
  expect(api.watchChat).not.toHaveBeenCalled();
  expect(host.textContent).not.toContain("private Manager"); expect(api.sendChat).not.toHaveBeenCalled(); expect(api.stopChatReply).not.toHaveBeenCalled();
});

function geometry() {
  let height = 120;
  const sizes = new Map<string, number>();
  const size = (row: HTMLElement) => sizes.get(row.dataset.feedKey ?? "") ?? 60;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function(this: HTMLElement) {
    return [...this.querySelectorAll<HTMLElement>("[data-feed-key]")].reduce((total, row) => total + size(row), 0);
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function(this: HTMLElement) {
    const viewport = this.closest('[aria-label="对话历史"]') as HTMLElement | null;
    const rows = viewport ? [...viewport.querySelectorAll<HTMLElement>("[data-feed-key]")] : [];
    const index = rows.indexOf(this);
    const top = index < 0 ? 0 : rows.slice(0, index).reduce((total, row) => total + size(row), 0) - viewport!.scrollTop;
    return { top, bottom: top + size(this), height: size(this), left: 0, right: 600, width: 600, x: 0, y: top, toJSON() {} };
  });
  const observers: (() => void)[] = [];
  vi.stubGlobal("ResizeObserver", class { constructor(callback: () => void) { observers.push(callback); } observe() {} unobserve() {} disconnect() {} });
  return { expand: (key: string, size: number) => sizes.set(key, size), hide: () => { height = 0; }, show: () => { height = 120; observers.forEach(callback => callback()); } };
}
const message = (id: string) => ({ id, role: "assistant", content: id, created_at: "2026-01-01T00:00:00Z" });
const pane = (identity: WorkIdentity, ids: string[], extra: Partial<WorkConversationProps> = {}) => <WorkConversation
  identity={identity} canSend messages={ids.map(message)} history="ready" draft="" onDraft={() => {}} staged={[]}
  onStaged={() => {}} onSend={() => {}} {...extra} />;

it("restores hidden anchors, keeps prepends and live updates steady, and does not move the departure watermark", async () => {
  const layout = geometry();
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"]))); await flush();
  const viewport = () => host.querySelector('[aria-label="对话历史"]') as HTMLElement;
  viewport().scrollTop = 72; act(() => viewport().dispatchEvent(new Event("scroll")));
  await act(async () => root.render(pane(b, ["b"])));
  layout.hide(); await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5", "new"]))); await flush();
  expect(viewport().scrollTop).toBe(60);
  act(() => layout.show()); await flush(); expect(viewport().scrollTop).toBe(72);
  expect(host.querySelectorAll('[role="separator"]')).toHaveLength(1);
  expect(host.querySelector('[role="separator"]')?.parentElement?.dataset.feedKey).toBe("message:new");
  await act(async () => root.render(pane(a, ["earlier", "1", "2", "3", "4", "5", "new"]))); await flush();
  expect(viewport().scrollTop).toBe(132);
  await act(async () => root.render(pane(a, ["earlier", "1", "2", "3", "4", "5", "new", "live"]))); await flush();
  expect(viewport().scrollTop).toBe(132); expect(host.querySelectorAll('[role="separator"]')).toHaveLength(1);
  await act(async () => root.render(pane(b, ["b"]))); await act(async () => root.render(pane(a, ["earlier", "1", "2", "3", "4", "5", "new", "live"]))); await flush();
  expect(host.querySelectorAll('[role="separator"]')).toHaveLength(0);
});

it("keeps the viewport steady when a late tool result grows above it, then follows an explicit send only in that conversation", async () => {
  const layout = geometry();
  const task = { task_id: "a", status: "completed", gates: [] } as unknown as TaskDetail;
  const record = { source: "tool" as const, id: "tool", name: "lookup", created_at: "2025-01-01T00:00:00Z" };
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"], { task, records: [record] }))); await flush();
  const viewport = () => host.querySelector('[aria-label="对话历史"]') as HTMLElement;
  viewport().scrollTop = 132; act(() => viewport().dispatchEvent(new Event("scroll")));
  layout.expand("tool:tool", 180);
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"], { task, records: [{ ...record, result: "late result" }] })));
  act(() => layout.show()); await flush(); expect(viewport().scrollTop).toBe(252);
  await act(async () => root.render(pane(b, ["b1", "b2", "b3", "b4"]))); await flush();
  viewport().scrollTop = 30; act(() => viewport().dispatchEvent(new Event("scroll")));
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"], { draft: "send", task, records: [record] }))); await flush();
  act(() => host.querySelector<HTMLButtonElement>('button[aria-label="发送"]')!.click());
  expect(viewport().scrollTop).toBe(viewport().scrollHeight);
  await act(async () => root.render(pane(b, ["b1", "b2", "b3", "b4"]))); await flush();
  expect(viewport().scrollTop).toBe(30); expect(api.stopChatReply).not.toHaveBeenCalled();
});

it("restores the departure anchor even when it used to be the bottom before new rows arrived", async () => {
  geometry();
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"]))); await flush();
  const viewport = () => host.querySelector('[aria-label="对话历史"]') as HTMLElement;
  viewport().scrollTop = 180; act(() => viewport().dispatchEvent(new Event("scroll")));
  await act(async () => root.render(pane(b, ["b"])));
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5"]))); await flush();
  expect(viewport().scrollTop).toBe(180);
  await act(async () => root.render(pane(a, ["1", "2", "3", "4", "5", "6", "7", "8"]))); await flush();
  expect(viewport().scrollTop).toBe(180);
  expect(host.querySelector('[role="separator"]')?.parentElement?.dataset.feedKey).toBe("message:6");
});

it("reloads only composer and view state, falls back when an anchor is absent, and reads fresh history", async () => {
  geometry(); const store = createWorkConversationStore(); const key = workConversationKey(a);
  store.patch(key, { draft: "unsent", staged: ["queued"], history: ["my input"] });
  store.patchView(key, { anchor: { key: "message:removed", offset: -9 }, top: 77, bottom: false });
  const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(pending.promise);
  await act(async () => root.render(<WorkConversationStateProvider><PlannerWorkConversation taskId="a" /></WorkConversationStateProvider>)); await flush();
  expect(host.querySelector("textarea")?.value).toBe("unsent"); expect(host.querySelectorAll("[data-message-id]")).toHaveLength(0);
  expect(api.fetchChatMessages).toHaveBeenCalledWith("planner-a", expect.any(AbortSignal));
  await act(async () => pending.resolve([dbRow("fresh", "user"), ...["2", "3", "4", "5"].map(id => dbRow(id))])); await flush();
  expect((host.querySelector('[aria-label="对话历史"]') as HTMLElement).scrollTop).toBe(77);
  expect(host.textContent).toContain("queued"); expect(host.textContent).toContain("fresh");
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("isolates draft, queue, input history and view through tasks, Manager and reload", () => {
  const store = createWorkConversationStore();
  const show = (identity: WorkIdentity) => act(() => root.render(<WorkConversationStateProvider store={store}><Pane identity={identity} /></WorkConversationStateProvider>));
  sessionStorage.setItem("looper.work.scroll", "711");
  show(a);
  act(() => { state.setDraft("draft A"); state.setStaged(["queue A"]); state.rememberHistory(["input A"]); });
  act(() => store.patchView(workConversationKey(a), { anchor: { key: "message:a", offset: -12 }, top: 140, bottom: false }));
  show(b); expect(state.value.draft).toBe(""); act(() => state.setDraft("draft B"));
  show(manager); act(() => state.setDraft("draft Manager"));
  show(a); expect(state.value).toMatchObject({ draft: "draft A", staged: ["queue A"], history: ["input A"], view: { top: 140 } });
  expect(createWorkConversationStore().read(workConversationKey(a))).toEqual({ ...state.value, history: [] });
  expect(sessionStorage.getItem(workConversationKey(a))).not.toContain("input A");
  expect(sessionStorage.getItem("looper.work.scroll")).toBe("711");
});

it.each(["get", "set", "corrupt", "old"])("keeps typing and switching with %s storage", mode => {
  const key = workConversationKey(a);
  if (mode === "get") vi.spyOn(sessionStorage, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  if (mode === "set") vi.spyOn(sessionStorage, "setItem").mockImplementation(() => { throw new Error("full"); });
  if (mode === "corrupt") sessionStorage.setItem(key, "{broken");
  if (mode === "old") sessionStorage.setItem(key, JSON.stringify({ version: 0, draft: "obsolete" }));
  const store = createWorkConversationStore();
  expect(store.read(key).draft).toBe("");
  store.patch(key, { draft: "still typing", staged: ["queued"] });
  store.read(workConversationKey(b));
  if (mode === "get") expect(sessionStorage.getItem).toHaveBeenCalled();
  if (mode === "set") expect(sessionStorage.setItem).toHaveBeenCalled();
  expect(store.read(key)).toMatchObject({ draft: "still typing", staged: ["queued"] });
});

it("bounds only memory caches, keeps the selected conversation and anchor, and never serializes messages", () => {
  const store = createWorkConversationStore(); const key = workConversationKey(a);
  store.select(key); store.patch(key, { draft: "unsent", staged: ["queued"] });
  store.patchView(key, { anchor: { key: "message:0", offset: 8 }, top: 1, bottom: false });
  store.cache(key, { messages: Array.from({ length: 1200 }, (_, id) => ({ id: String(id), role: "assistant", content: "private reply", streaming: true })) });
  expect(store.cached(key)!.messages).toHaveLength(1000);
  expect(store.cached(key)!.messages.some(row => row.id === "0")).toBe(true);
  expect(store.cached(key)!.messages.every(row => !row.streaming)).toBe(true);
  for (let id = 0; id < 25; id++) store.cache(workConversationKey({ kind: "planner", taskId: String(id) }), { messages: [] });
  expect(store.cached(key)).toBeDefined();
  expect(1 + Array.from({ length: 25 }, (_, taskId) => store.cached(workConversationKey({ kind: "planner", taskId: String(taskId) }))).filter(Boolean).length).toBe(20);
  expect(store.cached(workConversationKey({ kind: "planner", taskId: "0" }))).toBeUndefined();
  store.select(workConversationKey(b));
  for (let id = 25; id < 50; id++) store.cache(workConversationKey({ kind: "planner", taskId: String(id) }), { messages: [] });
  expect(store.cached(key)).toBeUndefined();
  expect(store.read(key).draft).toBe("unsent"); expect(store.read(key).staged).toEqual(["queued"]);
  expect([...Array(sessionStorage.length)].map((_, i) => sessionStorage.getItem(sessionStorage.key(i)!)).join("")).not.toContain("private reply");
  expect(createWorkConversationStore().cached(key)).toBeUndefined();
});
