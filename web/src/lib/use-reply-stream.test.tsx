// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "@/pages/chat";
import { useReplyStream, type ReplyStreamOwner, type ReplyStreamStart } from "@/lib/use-reply-stream";

type Handler = (event: { kind: string; payload: Record<string, unknown> }) => void;

const api = vi.hoisted(() => ({
  sendChat: vi.fn(),
  streamChatReply: vi.fn(),
  fetchChatMessages: vi.fn(),
  fetchChatTurn: vi.fn(),
  fetchChatSettings: vi.fn(),
  stopChatReply: vi.fn(),
  fetchChatTargets: vi.fn(),
  fetchChatSessions: vi.fn(),
  fetchChatSession: vi.fn(),
  fetchManagerSession: vi.fn(),
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
    watchChat: (sid: string, cb: (m: unknown) => void) => api.watchChat(sid, cb),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MANAGER = { kind: "manager", id: "manager", can_send: true };
const OTHER = { kind: "manager", id: "manager-other", name: "Other", can_send: true };
const PLANNER = { kind: "planner", id: "planner-t1", can_send: true, task_id: "t1", name: "Task one", status: "running" };
const SID = "session-test";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const flush = () => act(async () => {
  if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1);
  else await new Promise((r) => setTimeout(r, 0));
});

describe("useReplyStream", () => {
  let container: HTMLDivElement;
  let root: Root;
  const owner: { current: ReplyStreamOwner } = { current: { id: "manager", threadId: "thread-a" } };
  const handle: { start: (input: ReplyStreamStart) => () => void; cancel: () => void } = {
    start: () => () => {},
    cancel: () => {},
  };
  let emit: Handler;
  let done: () => void;

  function Probe() {
    const stream = useReplyStream(() => owner.current);
    handle.start = stream.start;
    handle.cancel = stream.cancel;
    return null;
  }

  const base = (over: Partial<ReplyStreamStart> = {}): ReplyStreamStart => ({
    target: { kind: "manager" },
    threadId: "thread-a",
    messageId: "m1",
    text: "hello",
    ownerId: "manager",
    onEvent: () => {},
    onDone: () => {},
    onError: () => {},
    ...over,
  });

  beforeEach(() => {
    owner.current = { id: "manager", threadId: "thread-a" };
    api.sendChat.mockReset();
    api.streamChatReply.mockReset();
    api.stopChatReply.mockReset();
    api.sendChat.mockResolvedValue({ correlation_id: "corr" });
    api.streamChatReply.mockImplementation((_id: string, onEvent: Handler, onDone?: () => void) => {
      emit = onEvent;
      done = onDone ?? (() => {});
      return () => {};
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    act(() => root.render(<Probe />));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("forwards delta, phase, final and terminal events in order; an empty final stays with the caller", async () => {
    const seen: string[] = [];
    let finalSeen = false;
    let content = "";
    handle.start(base({
      onEvent: (event) => {
        seen.push(event.kind);
        if (event.kind === "answer.delta") content += String(event.payload.text ?? "");
        if (event.kind === "agent.phase") seen.push(String(event.payload.label ?? ""));
        if (event.kind === "answer.final") {
          finalSeen = true;
          const finalText = String(event.payload.text ?? "");
          content = finalText || content || "Done.";
        }
      },
      onDone: () => { seen.push(finalSeen ? "done:final" : "done:open"); },
    }));
    await flush();
    act(() => {
      emit({ kind: "answer.delta", payload: { text: "Hi" } });
      emit({ kind: "agent.phase", payload: { label: "Thinking" } });
      emit({ kind: "answer.final", payload: { text: "", outcome: "answered" } });
      emit({ kind: "turn.completed", payload: {} });
      emit({ kind: "turn.failed", payload: {} });
      done();
    });
    expect(seen).toEqual([
      "answer.delta", "agent.phase", "Thinking", "answer.final", "turn.completed", "turn.failed", "done:final",
    ]);
    // The hook forwards the empty final. The caller keeps the streamed text, or "Done." when there is none.
    expect(content).toBe("Hi");

    content = "";
    finalSeen = false;
    handle.start(base({
      messageId: "m2",
      onEvent: (event) => {
        if (event.kind === "answer.final") {
          finalSeen = true;
          const finalText = String(event.payload.text ?? "");
          content = finalText || content || "Done.";
        }
      },
      onDone: () => { seen.push(finalSeen ? "second:final" : "second:open"); },
    }));
    await flush();
    act(() => {
      emit({ kind: "answer.final", payload: { text: "" } });
      done();
    });
    expect(content).toBe("Done.");
    expect(seen).toContain("second:final");
  });

  it("reports a rejected send and a done with no final, and does not stop the reply", async () => {
    const errors: unknown[] = [];
    const seen: string[] = [];
    api.sendChat.mockRejectedValueOnce(new TypeError("offline"));
    handle.start(base({
      onError: (error) => errors.push(error),
      onDone: () => seen.push("done"),
    }));
    await flush();
    expect(errors[0]).toBeInstanceOf(TypeError);
    expect(api.streamChatReply).not.toHaveBeenCalled();
    expect(api.stopChatReply).not.toHaveBeenCalled();
    expect(seen).toEqual([]);

    api.sendChat.mockResolvedValueOnce({ correlation_id: "open" });
    let finalSeen = false;
    handle.start(base({
      messageId: "m-open",
      onEvent: (event) => { if (event.kind === "answer.final") finalSeen = true; },
      onDone: () => seen.push(finalSeen ? "final" : "open"),
    }));
    await flush();
    act(() => done());
    expect(seen).toEqual(["open"]);
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("drops a resolve or reject that arrives after cancel or unmount, and an abort is not a connection error", async () => {
    const pending = deferred<{ correlation_id: string }>();
    const rejected = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(pending.promise);
    const errors: unknown[] = [];
    const cancel = handle.start(base({ onError: (error) => errors.push(error) }));
    const signal = api.sendChat.mock.calls[0][4].signal as AbortSignal;
    cancel();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ correlation_id: "late" }));
    await flush();
    expect(api.streamChatReply).not.toHaveBeenCalled();
    expect(errors).toEqual([]);

    api.sendChat.mockReturnValueOnce(rejected.promise);
    handle.start(base({ messageId: "m-abort", onError: (error) => errors.push(error) }));
    handle.cancel();
    await act(async () => rejected.reject(new TypeError("Failed to fetch")));
    await flush();
    expect(errors).toEqual([]);
    expect(api.streamChatReply).not.toHaveBeenCalled();
    expect(api.stopChatReply).not.toHaveBeenCalled();

    const hanging = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(hanging.promise);
    handle.start(base({ messageId: "m-unmount" }));
    const unmountCall = api.sendChat.mock.calls.at(-1);
    act(() => root.unmount());
    expect(unmountCall?.[4].signal.aborted).toBe(true);
    await act(async () => hanging.resolve({ correlation_id: "after-unmount" }));
    await flush();
    expect(api.streamChatReply).not.toHaveBeenCalled();
  });

  it("ignores the previous owner's event, error and done after the next send", async () => {
    const first = deferred<{ correlation_id: string }>();
    const failed = deferred<{ correlation_id: string }>();
    const second = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(first.promise).mockReturnValueOnce(failed.promise).mockReturnValueOnce(second.promise);
    const seen: string[] = [];
    let eventA: Handler = () => {};
    let doneA = () => {};
    const closeA = vi.fn();
    api.streamChatReply.mockImplementation((id: string, onEvent: Handler, onDone?: () => void) => {
      if (id === "A") {
        eventA = onEvent;
        doneA = onDone ?? (() => {});
        return closeA.mockImplementation(() => onDone?.());
      }
      emit = onEvent;
      done = onDone ?? (() => {});
      return () => {};
    });
    handle.start(base({
      messageId: "a",
      onEvent: () => seen.push("a-event"),
      onDone: () => seen.push("a-done"),
      onError: () => seen.push("a-error"),
    }));
    await act(async () => first.resolve({ correlation_id: "A" }));
    await flush();
    handle.start(base({
      messageId: "b",
      text: "next",
      onEvent: (event) => seen.push(`b:${event.kind}`),
      onDone: () => seen.push("b-done"),
      onError: () => seen.push("b-error"),
    }));
    expect(closeA).toHaveBeenCalledOnce();
    act(() => {
      eventA({ kind: "answer.delta", payload: { text: "stale" } });
      eventA({ kind: "answer.final", payload: { text: "stale" } });
      eventA({ kind: "turn.failed", payload: {} });
      doneA();
    });
    expect(api.sendChat.mock.calls[1][4].signal.aborted).toBe(false);
    // The send that was cancelled before it resolved must not surface on the next one.
    handle.cancel();
    handle.start(base({
      messageId: "c",
      onEvent: () => seen.push("c-event"),
      onDone: () => seen.push("c-done"),
      onError: () => seen.push("c-error"),
    }));
    await act(async () => failed.reject(new Error("old failure")));
    await act(async () => second.resolve({ correlation_id: "C" }));
    await flush();
    act(() => emit({ kind: "answer.delta", payload: { text: "fresh" } }));
    expect(seen).toEqual(["c-event"]);
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("does not let a synchronous final's close cancel the send it just started", async () => {
    const closes: Record<string, ReturnType<typeof vi.fn>> = {};
    api.sendChat.mockImplementation((text: string) => Promise.resolve({ correlation_id: text }));
    api.streamChatReply.mockImplementation((id: string, onEvent: Handler, onDone?: () => void) => {
      const close = vi.fn(() => onDone?.());
      closes[id] = close;
      if (id === "first") onEvent({ kind: "answer.final", payload: { text: "ok", outcome: "answered" } });
      return close;
    });
    const seen: string[] = [];
    handle.start(base({
      text: "first",
      messageId: "m-first",
      onEvent: (event) => {
        seen.push(`first:${event.kind}`);
        if (event.kind !== "answer.final") return;
        handle.start(base({
          text: "second",
          messageId: "m-second",
          onEvent: (next) => seen.push(`second:${next.kind}`),
          onDone: () => seen.push("second-done"),
          onError: () => seen.push("second-error"),
        }));
      },
      onDone: () => seen.push("first-done"),
      onError: () => seen.push("first-error"),
    }));
    await flush();
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    expect(api.sendChat.mock.calls[1][4].signal.aborted).toBe(false);
    expect(closes.first).toHaveBeenCalledOnce();
    expect(closes.second).not.toHaveBeenCalled();
    expect(seen).toEqual(["first:answer.final"]);
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("keeps delivering while the owner is unchanged, and drops events once the live owner differs", async () => {
    const seen: string[] = [];
    handle.start(base({ onEvent: (event) => seen.push(event.kind) }));
    await flush();
    const signal = api.sendChat.mock.calls[0][4].signal as AbortSignal;
    act(() => emit({ kind: "answer.delta", payload: { text: "kept" } }));
    expect(seen).toEqual(["answer.delta"]);
    expect(signal.aborted).toBe(false);

    owner.current = { id: "manager-other", threadId: "thread-a" };
    act(() => {
      emit({ kind: "answer.final", payload: { text: "nope" } });
      done();
    });
    expect(seen).toEqual(["answer.delta"]);
    expect(signal.aborted).toBe(false);
    expect(api.stopChatReply).not.toHaveBeenCalled();

    handle.cancel();
    expect(signal.aborted).toBe(true);
  });
});

describe("chat reply stream wiring", () => {
  let container: HTMLDivElement;
  let root: Root;
  let navigate: (query: string) => void;
  let setActive: (active: boolean) => void;
  let emit: Handler;
  let done: (() => void) | undefined;

  function Harness() {
    const [query, setQuery] = useState("");
    const [active, setActiveState] = useState(true);
    navigate = setQuery;
    setActive = setActiveState;
    return <ChatPage query={query} onQuery={setQuery} active={active} />;
  }

  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const type = (text: string) => act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(input(), text);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
  const key = (k: string) => act(() => {
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
  const go = async (query: string) => {
    act(() => navigate(query));
    await flush();
  };
  const stopButton = () => container.querySelector('button[aria-label^="Stop the reply"]') as HTMLButtonElement | null;

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem("looper-chat-session-id", SID);
    for (const fn of Object.values(api)) fn.mockReset();
    api.sendChat.mockResolvedValue({ correlation_id: "reply-correlation" });
    api.streamChatReply.mockImplementation((_id: string, onEvent: Handler, onDone?: () => void) => {
      emit = onEvent;
      done = onDone;
      return () => {};
    });
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTurn.mockResolvedValue(null);
    api.fetchChatSettings.mockResolvedValue({ stop_enabled: true });
    api.stopChatReply.mockResolvedValue({ state: "stopping", method: "interrupt" });
    api.fetchChatTargets.mockResolvedValue([MANAGER, OTHER, PLANNER]);
    api.fetchChatSessions.mockResolvedValue([]);
    api.fetchChatSession.mockResolvedValue(null);
    api.fetchManagerSession.mockResolvedValue({
      session: null, selected: null, effective: { profile: null, source: "system" }, choices: [], problem: null,
    });
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

  async function mount() {
    act(() => root.render(<Harness />));
    await flush();
  }

  it("keeps the chat's final, failure, Stop and Planner text", async () => {
    await mount();
    await say("first");
    await fire("answer.delta", { text: "partial" });
    await fire("answer.final", { text: "", outcome: "answered" });
    expect(container.textContent).toContain("partial");
    expect(container.textContent).not.toContain("Done.");

    await say("empty");
    await fire("answer.final", { text: "", outcome: "answered" });
    expect(container.textContent).toContain("Done.");

    await say("stop-me");
    await fire("answer.final", { text: "x", outcome: "stopped" });
    expect(container.textContent).toContain("Stopped");

    api.sendChat.mockRejectedValueOnce(new Error("manager is down"));
    await say("retry me");
    expect(input().value).toBe("retry me");
    expect(container.textContent).toContain("manager is down");
    expect(container.textContent).not.toContain("Failed to connect. Is the server running?");

    api.sendChat.mockRejectedValueOnce(new TypeError("offline"));
    await say("offline");
    expect(input().value).toBe("offline");
    expect(container.textContent).toContain("Failed to connect. Is the server running?");

    vi.useFakeTimers();
    const doneCount = container.textContent?.split("Done.").length ?? 0;
    await say("followed");
    await say("queued");
    api.fetchChatTurn.mockClear();
    act(() => done?.());
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(api.fetchChatTurn).toHaveBeenCalled();
    // Ending without a final does not invent another "Done." The earlier empty final still shows its own.
    expect(container.textContent?.split("Done.").length).toBe(doneCount);
    expect(container.textContent).toContain("queued");
    vi.useRealTimers();
  });

  it("keeps Stop off", async () => {
    api.fetchChatSettings.mockResolvedValue({ stop_enabled: false });
    await mount();
    await say("first");
    expect(stopButton()).toBeNull();
    await say("/stop");
    expect(api.stopChatReply).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Stopping a reply is off");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
  });

  it("keeps an unsupported runtime and a Planner without Stop", async () => {
    api.fetchChatTurn.mockImplementation(async () => {
      const sent = api.sendChat.mock.calls.at(-1);
      return sent
        ? { message_id: "env", client_message_id: sent[2], channel: "web", started_at: "", stopping: false, can_interrupt: false }
        : null;
    });
    await mount();
    await say("runtime");
    const stop = container.querySelector('button[aria-label^="Stop the reply:"]') as HTMLButtonElement | null;
    expect(stop?.disabled).toBe(true);
    expect(stop?.parentElement?.getAttribute("title")).toBe("This runtime cannot interrupt a reply.");
    key("Escape");
    await flush();
    expect(api.stopChatReply).not.toHaveBeenCalled();

    api.fetchChatTurn.mockResolvedValue(null);
    await act(async () => {
      (container.querySelector("button.flex.items-center.gap-2.rounded-lg") as HTMLButtonElement).click();
    });
    await flush();
    const row = [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Planner")) as HTMLButtonElement;
    await act(async () => row.click());
    await flush();
    await say("plan");
    expect(api.sendChat.mock.calls.at(-1)?.[3]).toEqual({ kind: "planner", task_id: "t1" });
    expect(stopButton()).toBeNull();
  });

  it("does not subscribe or show a connection error when the send is cancelled before it has a correlation", async () => {
    const pending = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(pending.promise);
    api.fetchChatSession.mockImplementation(async (id: string) => ({ session_id: id, channel: "web" }));
    await mount();
    await say("from A");
    const signal = api.sendChat.mock.calls[0][4].signal as AbortSignal;
    localStorage.setItem("looper-chat-session-id:manager-other", SID);
    await go(`target=manager-other&conversation=${SID}`);
    expect(signal.aborted).toBe(true);
    await act(async () => pending.reject(new TypeError("offline")));
    await flush();
    expect(api.streamChatReply).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("Failed to connect. Is the server running?");
    expect(input().value).toBe("");
    expect(api.stopChatReply).not.toHaveBeenCalled();

    const late = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(late.promise);
    await say("from B");
    act(() => root.unmount());
    await act(async () => late.resolve({ correlation_id: "late-B" }));
    await flush();
    expect(api.streamChatReply).not.toHaveBeenCalled();
  });

  it("keeps the original owner's reply across a read-only link, an unresolved link, and query or active changes", async () => {
    const transcript = {
      session_id: "lark:1", channel: "lark", message_count: 0, first_message_at: "2026-01-01T00:00:00Z",
      last_message_at: "2026-01-01T00:00:00Z", last_role: "user", last_content: "",
    };
    api.fetchChatSessions.mockResolvedValue([transcript]);
    api.fetchChatSession.mockImplementation(async (id: string) => (
      id === "lark:1" ? transcript : id === SID ? { session_id: id, channel: "web" } : null
    ));
    api.fetchChatMessages.mockResolvedValue([]);
    await mount();
    await say("hello");
    const signal = api.sendChat.mock.calls[0][4].signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    await go("conversation=lark:1");
    expect(container.textContent).toContain("Read-only");
    expect(signal.aborted).toBe(false);
    act(() => emit({ kind: "answer.delta", payload: { text: "kept reply" } }));
    await go(`conversation=${SID}`);
    expect(container.textContent).toContain("kept reply");
    expect(signal.aborted).toBe(false);

    await go("conversation=missing-thread");
    expect(container.textContent).toMatch(/conversation not found/i);
    expect(signal.aborted).toBe(false);
    act(() => emit({ kind: "answer.delta", payload: { text: " still kept" } }));
    await go("");
    expect(container.textContent).toContain("kept reply still kept");
    expect(signal.aborted).toBe(false);

    await go("target=manager");
    act(() => setActive(false));
    await flush();
    expect(signal.aborted).toBe(false);
    act(() => emit({ kind: "answer.final", payload: { text: "done now", outcome: "answered" } }));
    await flush();
    expect(container.textContent).toContain("done now");
    expect(signal.aborted).toBe(false);
    expect(api.stopChatReply).not.toHaveBeenCalled();

    await go("target=manager-other");
    expect(signal.aborted).toBe(true);
    expect(container.textContent).not.toContain("done now");
  });

  it("does not let the previous close clear the next turn the final started", async () => {
    const first = deferred<{ correlation_id: string }>();
    api.sendChat.mockReturnValueOnce(first.promise);
    api.sendChat.mockResolvedValue({ correlation_id: "second" });
    const closes = new Map<string, ReturnType<typeof vi.fn>>();
    api.streamChatReply.mockImplementation((id: string, onEvent: Handler, onDone?: () => void) => {
      const close = vi.fn(() => onDone?.());
      closes.set(id, close);
      if (id === "first") onEvent({ kind: "answer.final", payload: { text: "ok", outcome: "answered" } });
      return close;
    });
    await mount();
    await say("first");
    await say("queued");
    await act(async () => { first.resolve({ correlation_id: "first" }); });
    await flush();
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    expect(api.sendChat.mock.calls[1][0]).toBe("queued");
    expect(api.sendChat.mock.calls[1][4].signal.aborted).toBe(false);
    expect(closes.get("first")).toHaveBeenCalled();
    expect(closes.get("second")).not.toHaveBeenCalled();
    expect(stopButton()).not.toBeNull();
    expect(container.textContent).toContain("queued");
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });
});
