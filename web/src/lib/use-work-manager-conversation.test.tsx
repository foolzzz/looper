// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkConversation } from "@/components/work-conversation";
import { useWorkManagerConversation } from "@/lib/use-work-manager-conversation";
import { ForeignConversationError, type DbChatMessage } from "@/lib/api";

import { clearWorkConversationMemory, createWorkConversationStore, workConversationKey } from "@/lib/use-work-conversation-state";

const api = vi.hoisted(() => ({
  fetchChatMessages: vi.fn(), fetchChatSettings: vi.fn(), fetchChatTurn: vi.fn(),
  sendChat: vi.fn(), streamChatReply: vi.fn(), watchChat: vi.fn(), stopChatReply: vi.fn(),
}));
vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), ...api }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const SID = "session-current";
const POINTER = "looper-chat-session-id";
const row = (id: string, content: string, role = "assistant"): DbChatMessage => ({
  message_id: id, role, content, created_at: "2026-01-01T00:00:00Z",
});
const turn = (id = "running", can = true) => ({
  message_id: `envelope-${id}`, client_message_id: id, can_interrupt: can,
  channel: "web", state: "running", method: null, stopping: false, started_at: "2026-01-01T00:00:00Z",
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
let adapter: ReturnType<typeof useWorkManagerConversation>;
function Pane() { adapter = useWorkManagerConversation(); return <WorkConversation {...adapter} />; }
let host: HTMLDivElement;
let root: Root;
const flush = async () => { await act(async () => { await Promise.resolve(); }); };
const mount = async () => { await act(async () => root.render(<Pane />)); await flush(); };
const changeThread = async (sid: string) => {
  localStorage.setItem(POINTER, sid);
  await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: POINTER, newValue: sid })));
  await flush();
};
const send = async (text: string) => { act(() => adapter.onSend(text)); await flush(); };
const event = async (kind: string, payload: Record<string, unknown>) => {
  act(() => api.streamChatReply.mock.calls.at(-1)![1]({ kind, payload })); await flush();
};
const watched = (index = 0) => api.watchChat.mock.calls[index][1] as (message: DbChatMessage) => void;
const stage = () => act(() => adapter.onStaged(["queued"]));
const input = () => host.querySelector("textarea")!;

beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); clearWorkConversationMemory(); localStorage.clear(); sessionStorage.clear(); localStorage.setItem(POINTER, SID);
  api.fetchChatMessages.mockResolvedValue([]); api.fetchChatSettings.mockResolvedValue({ stop_enabled: true });
  api.fetchChatTurn.mockResolvedValue(null); api.sendChat.mockResolvedValue({ correlation_id: "reply" });
  api.watchChat.mockReturnValue(vi.fn()); api.streamChatReply.mockReturnValue(vi.fn());
  api.stopChatReply.mockResolvedValue({ state: "stopping" });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("Work default Manager", () => {
  it("reads and writes the current thread with original ids and text", async () => {
    api.fetchChatMessages.mockResolvedValue([row("original", "原话", "user")]); await mount();
    expect(localStorage.getItem(POINTER)).toBe(SID);
    expect(api.fetchChatMessages).toHaveBeenCalledWith(SID, expect.any(AbortSignal), "manager");
    expect(host.querySelector('[data-message-id="original"]')?.textContent).toBe("原话");
    await send("keep  this\n原话");
    expect(api.sendChat).toHaveBeenCalledWith("keep  this\n原话", SID, expect.any(String), { kind: "manager" }, expect.anything());
    const id = api.sendChat.mock.calls[0][2];
    expect(host.querySelector(`[data-message-id="${id}"]`)?.textContent).toBe("keep  this\n原话");
    expect(host.querySelector(`[data-message-id="${id}:reply"]`)).toBeTruthy();
  });
  it("merges autonomous reports, history and saved finals without duplicates or partial overwrite", async () => {
    api.fetchChatMessages.mockResolvedValue([row("report", "历史汇报")]); await mount();
    expect(api.watchChat).toHaveBeenCalledWith(SID, expect.any(Function), "manager");
    act(() => { watched()(row("report", "历史汇报")); watched()(row("new", "自发汇报")); watched()(row("new", "自发汇报")); });
    expect(host.querySelectorAll('[data-message-id="report"]')).toHaveLength(1);
    expect(host.querySelectorAll('[data-message-id="new"]')).toHaveLength(1);
    await send("hello"); const id = `${api.sendChat.mock.calls[0][2]}:reply`;
    await event("answer.delta", { text: "partial" }); expect(host.textContent).toContain("partial");
    act(() => watched()(row(id, "saved final")));
    await event("answer.delta", { text: "late partial" }); await event("answer.final", { text: "older final", outcome: "answered" });
    expect(host.querySelectorAll(`[data-message-id="${id}"]`)).toHaveLength(1);
    expect(host.querySelector(`[data-message-id="${id}"]`)?.textContent).toBe("saved final");
  });
  it("gates cached rows, watch, poll and send until the scoped ownership read", async () => {
    const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(pending.promise);
    localStorage.setItem(`looper-work-messages:manager:${SID}`, JSON.stringify([row("secret", "cached secret")]));
    localStorage.setItem(`looper-chat-messages-${SID}`, JSON.stringify([{ id: "secret", content: "legacy secret" }]));
    sessionStorage.setItem(`looper-work-draft:manager:${SID}`, "cached draft");
    sessionStorage.setItem(`looper-work-staged:manager:${SID}`, '["cached queue"]');
    await mount(); await send("blocked");
    expect(host.textContent).toContain("正在确认会话归属…"); expect(host.textContent).not.toContain("secret"); expect(input().disabled).toBe(true);
    expect(input().value).toBe(""); expect(adapter.staged).toEqual([]);
    expect(api.watchChat).not.toHaveBeenCalled(); expect(api.fetchChatTurn).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
    await act(async () => pending.resolve([])); expect(input().disabled).toBe(false); expect(api.watchChat).toHaveBeenCalledOnce();
  });
  it("replaces a foreign pointer and gates its replacement while preserving the original notice", async () => {
    const pending = deferred<DbChatMessage[]>();
    api.fetchChatMessages.mockRejectedValueOnce(new ForeignConversationError("foreign", "manager-other")).mockReturnValue(pending.promise);
    await mount(); await send("blocked"); const sid = localStorage.getItem(POINTER)!; expect(sid).not.toBe(SID);
    expect(host.textContent).toContain("The saved conversation belongs to another Manager, so this is a new conversation.");
    expect(api.watchChat).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
    await act(async () => pending.resolve([])); await send("own");
    expect(api.sendChat.mock.calls[0][1]).toBe(sid); expect(api.watchChat.mock.calls.map(call => call[0])).toEqual([sid]);
  });
  it("keeps read errors gated and retries explicitly", async () => {
    api.fetchChatMessages.mockRejectedValueOnce(new Error("offline")); await mount(); await send("blocked");
    expect(host.textContent).toContain("对话读取失败"); expect(input().disabled).toBe(true);
    expect(api.watchChat).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
    await act(async () => [...host.querySelectorAll("button")].find(b => b.textContent === "重试")!.click()); await flush();
    expect(input().disabled).toBe(false); expect(host.textContent).toContain("还没有对话");
  });
  it("revalidates ownership when returning to an earlier Manager thread", async () => {
    api.fetchChatMessages.mockResolvedValueOnce([row("private", "earlier thread")]); await mount();
    await changeThread("session-b");
    const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(pending.promise);
    api.watchChat.mockClear(); await changeThread(SID); await send("blocked");
    expect(host.textContent).not.toContain("earlier thread"); expect(input().disabled).toBe(true);
    expect(api.watchChat).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
    await act(async () => pending.resolve([])); expect(host.textContent).toContain("earlier thread");
  });
  it("fails closed for malformed history and unread Stop settings", async () => {
    api.fetchChatMessages.mockResolvedValue({}); api.fetchChatSettings.mockRejectedValue(new Error("offline"));
    await mount(); await send("blocked");
    expect(host.textContent).toContain("对话读取失败"); expect(input().disabled).toBe(true);
    expect(api.watchChat).not.toHaveBeenCalled(); expect(api.fetchChatTurn).not.toHaveBeenCalled(); expect(api.sendChat).not.toHaveBeenCalled();
    expect(adapter.canStop).toBe(false);
  });
  it.each(["answered", "failed", "stopped", "confirm"])("uses the Chat queue policy for %s", async outcome => {
    await mount(); await send("first"); stage(); act(() => adapter.onDraft("typing"));
    await event("answer.final", { text: "reply", outcome });
    if (outcome === "answered") { expect(api.sendChat).toHaveBeenCalledTimes(2); expect(api.sendChat.mock.calls[1][0]).toBe("queued"); expect(input().value).toBe("typing"); }
    else { expect(api.sendChat).toHaveBeenCalledOnce(); expect(input().value).toBe("queued\n\ntyping"); }
    expect(adapter.staged).toEqual([]);
  });
  it("follows a dropped stream until the turn ends, reads history and returns the queue", async () => {
    await mount(); await send("first"); stage(); const id = api.sendChat.mock.calls[0][2]; api.fetchChatTurn.mockResolvedValue(turn(id));
    act(() => api.streamChatReply.mock.calls[0][2]()); expect(adapter.replying).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(3000)); expect(adapter.replying).toBe(true); expect(api.sendChat).toHaveBeenCalledOnce();
    api.fetchChatTurn.mockResolvedValue(null); api.fetchChatMessages.mockResolvedValue([row(`${id}:reply`, "recovered")]);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(adapter.replying).toBe(false); expect(input().value).toBe("queued"); expect(host.textContent).toContain("recovered");
  });
  it("shows a server turn on reload and never auto-sends its queue", async () => {
    createWorkConversationStore().patch(workConversationKey({ kind: "manager", sessionId: SID }), { staged: ["waiting"] }); api.fetchChatTurn.mockResolvedValue(turn()); await mount();
    expect(adapter.replying).toBe(true); expect(input().placeholder).toContain("Replying"); api.fetchChatTurn.mockResolvedValue(null);
    await act(async () => vi.advanceTimersByTimeAsync(3000)); expect(input().value).toBe("waiting"); expect(api.sendChat).not.toHaveBeenCalled();
  });
  it.each([false, true])("keeps Stop gated by settings %s", async enabled => {
    api.fetchChatSettings.mockResolvedValue({ stop_enabled: enabled }); await mount(); await send("first");
    expect(adapter.canStop).toBe(enabled); await act(async () => adapter.onStop?.());
    if (enabled) expect(api.stopChatReply).toHaveBeenCalledWith(api.sendChat.mock.calls[0][2]);
    else { expect(api.stopChatReply).not.toHaveBeenCalled(); expect(host.textContent).toContain("Stopping a reply is off (Settings → Chat)."); }
  });
  it("disables Stop for an unsupported runtime with the original text", async () => {
    api.fetchChatTurn.mockResolvedValue(turn("running", false)); await mount(); expect(adapter.canStop).toBe(false);
    expect(host.querySelector('button[aria-label="Stop the reply: This runtime cannot interrupt a reply."]')?.hasAttribute("disabled")).toBe(true);
    await act(async () => adapter.onStop?.()); expect(api.stopChatReply).not.toHaveBeenCalled(); expect(host.textContent).toContain("This runtime cannot interrupt a reply.");
  });
  it.each(["disabled", "other_message", "cannot_interrupt"])("shows the original Stop refusal %s", async state => {
    api.stopChatReply.mockResolvedValue({ state }); await mount(); await send("first"); await act(async () => adapter.onStop?.());
    expect(host.textContent).toContain(state === "disabled" ? "Stopping a reply is off (Settings → Chat)." : state === "other_message"
      ? "The Manager is answering another message first; this one has not started." : "This runtime cannot interrupt a reply.");
  });
  it("returns rejected submissions ahead of the queue and draft without retrying", async () => {
    const pending = deferred<{ correlation_id: string }>(); api.sendChat.mockReturnValue(pending.promise);
    await mount(); await send("first"); stage(); act(() => adapter.onDraft("draft")); await act(async () => pending.reject(new Error("rejected")));
    expect(input().value).toBe("first\n\nqueued\n\ndraft"); expect(host.textContent).toContain("rejected"); expect(api.sendChat).toHaveBeenCalledOnce();
  });
  it.each(["pending", "stream", "poll"])("cancels clients on a thread switch during %s and drops stale callbacks", async phase => {
    const pending = deferred<{ correlation_id: string }>(); if (phase === "pending") api.sendChat.mockReturnValue(pending.promise);
    await mount(); const lateWatch = watched(); const closeWatch = api.watchChat.mock.results[0].value;
    await send("old"); const signal = api.sendChat.mock.calls[0][4].signal;
    const stream = api.streamChatReply.mock.calls[0]; const closeReply = api.streamChatReply.mock.results[0]?.value;
    if (phase === "poll") act(() => stream[2]());
    const oldPolls = api.fetchChatTurn.mock.calls.filter(call => call[0] === SID).length;
    await changeThread("session-b"); expect(signal.aborted).toBe(true); expect(closeWatch).toHaveBeenCalledOnce();
    if (closeReply) expect(closeReply).toHaveBeenCalledOnce();
    await act(async () => {
      lateWatch(row("late", "late watcher"));
      if (stream) { stream[1]({ kind: "answer.final", payload: { text: "late final" } }); stream[2](); }
      pending.promise.catch(() => {}); pending.reject(new Error("late error"));
    });
    expect(host.textContent).not.toContain("late"); expect(host.textContent).not.toContain("old");
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(api.fetchChatTurn.mock.calls.filter(call => call[0] === SID)).toHaveLength(oldPolls); expect(api.stopChatReply).not.toHaveBeenCalled();
  });
  it("drops late history after unmount without server Stop", async () => {
    const history = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValue(history.promise); await mount();
    const signal = api.fetchChatMessages.mock.calls[0][1]; act(() => root.render(null)); expect(signal.aborted).toBe(true);
    await act(async () => history.resolve([row("late", "late secret")])); expect(host.textContent).toBe(""); expect(api.watchChat).not.toHaveBeenCalled(); expect(api.stopChatReply).not.toHaveBeenCalled();
  });
  it("cancels live subscriptions and following on unmount, without stopping the server", async () => {
    await mount(); await send("first");
    const watch = watched(); const stream = api.streamChatReply.mock.calls[0];
    const closeWatch = api.watchChat.mock.results[0].value; const closeReply = api.streamChatReply.mock.results[0].value;
    act(() => stream[2]());
    const polls = api.fetchChatTurn.mock.calls.length;
    act(() => root.render(null));
    expect(closeWatch).toHaveBeenCalledOnce(); expect(closeReply).toHaveBeenCalledOnce();
    act(() => { watch(row("late", "late watch")); stream[1]({ kind: "answer.final", payload: { text: "late final" } }); stream[2](); });
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(api.fetchChatTurn).toHaveBeenCalledTimes(polls); expect(api.stopChatReply).not.toHaveBeenCalled(); expect(host.textContent).toBe("");
  });
  it("discards an old poll's pending history after switching owners", async () => {
    api.fetchChatTurn.mockResolvedValue(turn()); await mount();
    const pending = deferred<DbChatMessage[]>();
    api.fetchChatTurn.mockResolvedValue(null); api.fetchChatMessages.mockReturnValueOnce(pending.promise);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    await changeThread("session-b");
    await act(async () => pending.resolve([row("late", "old poll history")]));
    expect(host.textContent).not.toContain("old poll history"); expect(adapter.replying).toBe(false); expect(api.stopChatReply).not.toHaveBeenCalled();
  });
  it("keeps staging while followed history is pending, then returns the queue", async () => {
    api.fetchChatTurn.mockResolvedValue(turn()); await mount();
    const pending = deferred<DbChatMessage[]>(); api.fetchChatMessages.mockReturnValueOnce(pending.promise); api.fetchChatTurn.mockResolvedValue(null);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(adapter.replying).toBe(true); stage();
    await act(async () => pending.resolve([row("reply", "finished")]));
    expect(input().value).toBe("queued"); expect(adapter.replying).toBe(false); expect(api.sendChat).not.toHaveBeenCalled();
  });
  it("re-reads the current turn capability until its runtime can interrupt", async () => {
    await mount();
    api.fetchChatTurn.mockImplementation(async () => turn(api.sendChat.mock.calls[0][2], false));
    await send("first"); expect(adapter.canStop).toBe(false);
    api.fetchChatTurn.mockImplementation(async () => turn(api.sendChat.mock.calls[0][2], true));
    await act(async () => vi.advanceTimersByTimeAsync(3000)); expect(adapter.canStop).toBe(true);
  });
  it("stages with the real composer and uses typed /stop without sending it", async () => {
    await mount();
    const type = (text: string) => act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
    const enter = () => act(() => input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    type("first"); enter(); await flush(); expect(input().value).toBe("");
    type("queued"); enter(); expect(adapter.staged).toEqual(["queued"]); expect(api.sendChat).toHaveBeenCalledOnce();
    type("/stop"); enter(); await flush();
    expect(api.stopChatReply).toHaveBeenCalledWith(api.sendChat.mock.calls[0][2]); expect(api.sendChat).toHaveBeenCalledOnce();
  });
  it("works with blocked local and session storage", async () => {
    for (const method of ["getItem", "setItem", "removeItem"] as const) vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error("blocked"); });
    await mount(); await send("first"); stage(); await event("answer.final", { text: "failed", outcome: "failed" });
    expect(input().value).toBe("queued"); expect(api.sendChat).toHaveBeenCalledOnce();
  });
});
