// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "@/pages/chat";

type Handler = (e: { kind: string; payload: Record<string, unknown> }) => void;

const api = vi.hoisted(() => ({
  sendChat: vi.fn(),
  streamSSE: vi.fn(),
  fetchChatMessages: vi.fn(),
  fetchChatTurn: vi.fn(),
  stopChatReply: vi.fn(),
  fetchChatTargets: vi.fn(),
  fetchChatSessions: vi.fn(),
  watchChat: vi.fn((_sid: string, _cb: (m: unknown) => void) => () => {}),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...real,
    ...api,
    fetchRoleConfigs: async () => [],
    fetchTaskActivity: async () => ({ activity: [] }),
    fetchTaskDetail: async () => ({ group: "running", gates: [] }),
    fetchLlmRecords: async () => [],
    saveChatMessage: async () => {},
    watchChat: (sid: string, cb: (m: unknown) => void) => api.watchChat(sid, cb),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MANAGER = { kind: "manager", id: "manager", can_send: true };
const PLANNER = { kind: "planner", id: "planner-t1", can_send: true, task_id: "t1", name: "Task one", status: "running" };
const SID = "session-test";

describe("chat composer flow", () => {
  let container: HTMLDivElement;
  let root: Root;
  let emit: Handler;
  let done: (() => void) | undefined;

  const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const type = (text: string) =>
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
  const key = (k: string) =>
    act(() => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
    });
  const say = async (text: string) => {
    type(text);
    key("Enter");
    await flush();
  };
  const fire = async (kind: string, payload: Record<string, unknown> = {}) => {
    act(() => emit({ kind, payload }));
    await flush();
  };
  const stopButton = () => container.querySelector('button[aria-label^="Stop the reply"]') as HTMLButtonElement | null;

  async function mount() {
    act(() => root.render(<ChatPage />));
    await flush();
  }

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem("looper-chat-session-id", SID);
    for (const fn of Object.values(api)) fn.mockReset();
    api.sendChat.mockResolvedValue({ events_url: "/events" });
    api.streamSSE.mockImplementation((_url: string, onEvent: Handler, onDone?: () => void) => {
      emit = onEvent;
      done = onDone;
      return () => {};
    });
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTurn.mockResolvedValue(null);
    api.stopChatReply.mockResolvedValue({ state: "stopping", method: "interrupt" });
    api.fetchChatTargets.mockResolvedValue([MANAGER, PLANNER]);
    api.fetchChatSessions.mockResolvedValue([]);
    api.watchChat.mockImplementation(() => () => {});
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("sends the messages typed during a reply once, joined, after an answered turn", async () => {
    await mount();
    await say("first");
    await say("a");
    await say("b");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    await fire("answer.final", { text: "ok", outcome: "answered" });
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    expect(api.sendChat.mock.calls[1][0]).toBe("a\n\nb");
    expect(sessionStorage.getItem(`looper-chat-staged-${SID}`)).toBeNull();
  });

  it.each(["failed", "stopped", "confirm"])("returns the queue to the draft after a %s turn", async (outcome) => {
    await mount();
    await say("first");
    await say("a");
    await say("b");
    await fire("answer.final", { text: "x", outcome });
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(input().value).toBe("a\n\nb");
    if (outcome === "stopped") expect(container.textContent).toContain("Stopped");
  });

  it("keeps the next turn running when the stream of the one before it closes", async () => {
    await mount();
    await say("first");
    await say("a");
    const closeFirst = done!;
    await fire("answer.final", { text: "ok", outcome: "answered" });
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    act(() => closeFirst());
    await flush();
    expect(stopButton()).not.toBeNull();
    await say("b");
    expect(api.sendChat).toHaveBeenCalledTimes(2);
  });

  it("stops the running message with Escape", async () => {
    await mount();
    await say("first");
    const userId = api.sendChat.mock.calls[0][2];
    key("Escape");
    await flush();
    expect(api.stopChatReply).toHaveBeenCalledWith(userId);
  });

  it("explains an other_message stop", async () => {
    api.stopChatReply.mockResolvedValue({ state: "other_message", method: null });
    await mount();
    await say("first");
    key("Escape");
    await flush();
    expect(container.textContent).toContain("answering another message first");
  });

  it("restores the queue after a reload while the turn runs, then moves it to the draft", async () => {
    sessionStorage.setItem(`looper-chat-staged-${SID}`, JSON.stringify(["a"]));
    api.fetchChatTurn.mockResolvedValue({ message_id: "m1", channel: "web", started_at: "", stopping: false });
    await mount();
    expect(container.textContent).toContain("a");
    expect(container.querySelector('button[aria-label="Remove queued message"]')).not.toBeNull();
    expect(stopButton()).not.toBeNull();
    expect(input().value).toBe("");

    api.fetchChatTurn.mockResolvedValue(null);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await flush();
    expect(input().value).toBe("a");
    expect(container.querySelector('button[aria-label="Remove queued message"]')).toBeNull();
  });

  it("puts a flushed batch back in the draft with the reason when its send fails", async () => {
    await mount();
    await say("first");
    await say("a");
    api.sendChat.mockRejectedValueOnce(new Error("manager is down"));
    await fire("answer.final", { text: "ok" });
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    expect(input().value).toBe("a");
    expect(container.textContent).toContain("manager is down");
  });

  it("stages for a Planner while it replies, with no Stop button", async () => {
    await mount();
    await act(async () => {
      (document.querySelector("button.flex.items-center.gap-2.rounded-lg") as HTMLButtonElement).click();
    });
    await flush();
    const row = [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Planner")) as HTMLButtonElement;
    await act(async () => row.click());
    await flush();
    await say("first");
    expect(api.sendChat.mock.calls[0][3]).toEqual({ kind: "planner", task_id: "t1" });
    await say("queued");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(container.querySelector('button[aria-label="Remove queued message"]')).not.toBeNull();
    expect(stopButton()).toBeNull();
  });

  const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  const envelopeTurn = { message_id: "env-1", client_message_id: "", channel: "web", started_at: "", stopping: false };

  it("keeps following a turn named by the envelope id after the page's own stream ends without a final", async () => {
    await mount();
    await say("first");
    const userId = api.sendChat.mock.calls[0][2];
    await say("queued");
    vi.useFakeTimers();
    api.fetchChatTurn.mockResolvedValue({ ...envelopeTurn, client_message_id: userId });
    act(() => done!());
    await advance(3000);
    expect(api.fetchChatTurn).toHaveBeenCalled();
    expect(stopButton()).not.toBeNull();
    expect(input().value).toBe("");
    const reads = api.fetchChatMessages.mock.calls.length;
    await advance(3000);
    expect(api.fetchChatTurn.mock.calls.length).toBeGreaterThan(1);
    expect(stopButton()).not.toBeNull();
    expect(api.fetchChatMessages.mock.calls.length).toBe(reads);

    api.fetchChatTurn.mockResolvedValue(null);
    await advance(3000);
    expect(stopButton()).toBeNull();
    expect(input().value).toBe("queued");
  });

  it("keeps following on reload when a later poll names the turn by either id", async () => {
    sessionStorage.setItem(`looper-chat-staged-${SID}`, JSON.stringify(["a"]));
    api.fetchChatTurn.mockResolvedValue({ ...envelopeTurn, client_message_id: "c1" });
    vi.useFakeTimers();
    act(() => root.render(<ChatPage />));
    await advance(0);
    expect(stopButton()).not.toBeNull();
    await advance(3000);
    expect(stopButton()).not.toBeNull();
    api.fetchChatTurn.mockResolvedValue({ ...envelopeTurn, message_id: "env-1", client_message_id: null });
    await advance(3000);
    expect(stopButton()).not.toBeNull();
    expect(input().value).toBe("");
    api.fetchChatTurn.mockResolvedValue(null);
    await advance(3000);
    expect(input().value).toBe("a");
  });

  it("replaces a partial reply with the saved one that arrives under the same id", async () => {
    let saved!: (m: unknown) => void;
    api.watchChat.mockImplementation((_sid: string, cb: (m: unknown) => void) => {
      saved = cb;
      return () => {};
    });
    await mount();
    await say("first");
    const userId = api.sendChat.mock.calls[0][2];
    await fire("answer.delta", { text: "partial words" });
    act(() => done!());
    await flush();
    expect(container.textContent).toContain("partial words");
    act(() => saved({ message_id: `${userId}:reply`, role: "assistant", content: "the full saved reply", created_at: "2026-01-01T00:00:00Z" }));
    await flush();
    expect(container.textContent).toContain("the full saved reply");
    expect(container.textContent).not.toContain("partial words");
  });
});
