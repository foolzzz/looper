// @vitest-environment happy-dom
import { act, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlannerWorkConversation, WorkConversation, type WorkConversationProps } from "@/components/work-conversation";
import { WorkPage } from "@/pages/work";

type Handler = (event: { kind: string; payload: Record<string, unknown> }) => void;

const api = vi.hoisted(() => ({
  fetchChatMessages: vi.fn(),
  fetchChatTargets: vi.fn(),
  sendChat: vi.fn(),
  streamChatReply: vi.fn(),
  stopChatReply: vi.fn(),
  fetchChatTurn: vi.fn(),
}));
const bridge = vi.hoisted(() => ({ real: null as typeof import("@/lib/api") | null }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  bridge.real = real;
  return {
    ...real,
    fetchChatMessages: api.fetchChatMessages,
    fetchChatTargets: api.fetchChatTargets,
    sendChat: api.sendChat,
    streamChatReply: api.streamChatReply,
    stopChatReply: api.stopChatReply,
    fetchChatTurn: api.fetchChatTurn,
  };
});
vi.mock("@/lib/use-projects", () => ({
  useProjects: () => ({ projects: [{ project_id: "p", name: "Beta" }] }),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const planner = (taskId: string, canSend = true) => ({
  kind: "planner" as const,
  id: `planner-${taskId}`,
  can_send: canSend,
  task_id: taskId,
  name: taskId,
  status: "running",
});

function TaskPane({ taskId }: { taskId: string }) {
  return (
    <PlannerWorkConversation
      taskId={taskId}
      detail={{ title: "Task", status: "running", project_name: "Beta", group: "in_progress" }}
    />
  );
}

describe("work conversation", () => {
  let container: HTMLDivElement;
  let root: Root;
  let emit: Handler;
  let done: (() => void) | undefined;

  const flush = () =>
    act(async () => {
      if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1);
      else await new Promise((resolve) => setTimeout(resolve, 0));
    });
  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const type = (text: string) =>
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
  const key = (name: string) =>
    act(() => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));
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
  async function show(node: ReactNode) {
    await act(async () => { root.render(node); });
    await flush();
  }
  async function remount(node: ReactNode) {
    act(() => root.unmount());
    root = createRoot(container);
    await show(node);
  }

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    for (const fn of Object.values(api)) fn.mockReset();
    api.sendChat.mockResolvedValue({ correlation_id: "reply-correlation" });
    api.streamChatReply.mockImplementation((_url: string, onEvent: Handler, onDone?: () => void) => {
      emit = onEvent;
      done = onDone;
      return () => {};
    });
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTargets.mockResolvedValue([planner("t1"), planner("t2")]);
    api.stopChatReply.mockResolvedValue({ state: "stopping", method: "interrupt" });
    api.fetchChatTurn.mockResolvedValue(null);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("accepts an external adapter and renders nothing for the manager", async () => {
    const onSend = vi.fn();
    function Box() {
      const [draft, setDraft] = useState("");
      const [staged, setStaged] = useState<string[]>([]);
      const props: WorkConversationProps = {
        identity: { kind: "planner", taskId: "ext" },
        canSend: true,
        detail: { status: "running" },
        messages: [{ id: "m", role: "user", content: "kept" }],
        events: [
          { kind: "answer.delta", payload: { text: "streamed" } },
          { kind: "agent.tool", payload: { name: "shell", text: "hidden" } },
        ],
        history: "ready",
        draft,
        onDraft: setDraft,
        staged,
        onStaged: setStaged,
        onSend,
      };
      return <WorkConversation {...props} />;
    }
    await show(<Box />);
    expect(container.textContent).toContain("kept");
    expect(container.textContent).toContain("streamed");
    expect(container.textContent).not.toContain("hidden");
    await say("outside");
    expect(onSend).toHaveBeenCalledWith("outside");
    expect(api.sendChat).not.toHaveBeenCalled();

    await show(
      <WorkConversation
        identity={{ kind: "manager" }}
        canSend
        messages={[{ id: "m", role: "assistant", content: "nope" }]}
        history="ready"
        draft=""
        onDraft={() => {}}
        staged={[]}
        onStaged={() => {}}
        onSend={() => {}}
      />,
    );
    expect(container.querySelector("textarea")).toBeNull();
    expect(container.textContent).not.toContain("任务管家");
    expect(container.textContent).not.toContain("nope");
  });

  it("keeps the manager selection as a link to the existing chat", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      let result: unknown = {};
      if (path.includes("work_list")) result = { items: [], schedules: [], next_cursor: null };
      if (path.includes("task_conversations")) result = { enabled: true };
      const status = init?.method === "GET" ? 405 : 200;
      return new Response(JSON.stringify({ status: "ok", result }), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }));
    await show(<WorkPage sub="manager" onNavigate={() => {}} />);
    expect(container.querySelector('a[href="#chat"]')?.textContent).toBe("Open the Manager chat");
    expect(container.querySelector("textarea")).toBeNull();
    expect(container.textContent).not.toContain("任务管家");
    expect(container.textContent).not.toContain("已接通");
  });

  it("sends the original text to the planner thread with the original message id", async () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue("11111111-1111-4111-8111-111111111111");
    await show(<TaskPane taskId="t1" />);
    await say("keep  this");
    expect(api.sendChat).toHaveBeenCalledWith(
      "keep  this",
      "planner-t1",
      "11111111-1111-4111-8111-111111111111",
      { kind: "planner", task_id: "t1" },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(api.fetchChatMessages).toHaveBeenCalledWith("planner-t1", expect.any(AbortSignal));
    expect(container.querySelector('[data-message-id="11111111-1111-4111-8111-111111111111"]')?.getAttribute("data-role")).toBe("user");
    expect(container.querySelector('[data-message-id="11111111-1111-4111-8111-111111111111:reply"]')?.getAttribute("data-role")).toBe("assistant");
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("planner:t1");
  });

  it("shows loading, empty, a failed read, and a retry that only reads again", async () => {
    let release!: (rows: unknown) => void;
    api.fetchChatMessages.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await show(<TaskPane taskId="t1" />);
    expect(container.textContent).toContain("正在加载对话…");
    await act(async () => { release([]); });
    await flush();
    expect(container.textContent).toContain("还没有对话");
    expect(container.textContent).toContain("仅显示最近的对话");

    api.fetchChatMessages.mockRejectedValueOnce(new Error("disk offline"));
    await show(<TaskPane taskId="t2" />);
    expect(container.textContent).toContain("对话读取失败");
    expect(container.textContent).toContain("disk offline");
    api.fetchChatMessages.mockResolvedValueOnce([]);
    const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "重试");
    expect(retry).toBeTruthy();
    await act(async () => { retry!.click(); });
    await flush();
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(container.textContent).toContain("还没有对话");
    expect(api.fetchChatMessages.mock.calls.filter((call) => call[0] === "planner-t2").length).toBeGreaterThanOrEqual(2);
  });

  it("reports an invalid history payload and a missing task without reading", async () => {
    api.fetchChatMessages.mockResolvedValueOnce(undefined);
    await show(<TaskPane taskId="t1" />);
    expect(container.textContent).toContain("对话读取失败");
    expect(container.textContent).toContain("The server returned invalid chat messages.");

    api.fetchChatMessages.mockClear();
    api.fetchChatTargets.mockClear();
    await show(<PlannerWorkConversation taskId="gone" missing />);
    expect(container.textContent).toContain("任务不存在");
    expect(container.querySelector("textarea")).toBeNull();
    expect(api.fetchChatMessages).not.toHaveBeenCalled();
    expect(api.fetchChatTargets).not.toHaveBeenCalled();
  });

  it("disables sending until this task's planner can send", async () => {
    let release!: (targets: unknown) => void;
    api.fetchChatTargets.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await show(<TaskPane taskId="t1" />);
    expect(container.textContent).toContain("Checking chat availability");
    expect(input().disabled).toBe(true);
    await act(async () => { release([planner("t1")]); });
    await flush();
    expect(input().disabled).toBe(false);
    expect(container.textContent).not.toContain("Checking chat availability");

    api.fetchChatTargets.mockRejectedValueOnce(new Error("catalog offline"));
    await remount(<TaskPane taskId="t1" />);
    expect(container.textContent).toContain("catalog offline");
    expect(input().disabled).toBe(true);

    api.fetchChatTargets.mockRejectedValueOnce(new Error("  "));
    await remount(<TaskPane taskId="t2" />);
    expect(container.textContent).toContain("Chat availability could not be checked");
    expect(input().disabled).toBe(true);

    api.fetchChatTargets.mockResolvedValueOnce([planner("other")]);
    await remount(<PlannerWorkConversation taskId="t1" detail={{ group: "in_progress", status: "running" }} />);
    expect(container.textContent).toContain("Only tasks with a running Planner can be chatted with");
    expect(input().disabled).toBe(true);
    expect(container.querySelector('a[href="#chat"]')).toBeNull();

    api.fetchChatTargets.mockResolvedValueOnce([planner("t1", false)]);
    await remount(<PlannerWorkConversation taskId="t1" detail={{ group: "in_progress", status: "running" }} />);
    expect(container.textContent).toContain("This task has ended, so its Planner no longer takes messages.");
    expect(input().disabled).toBe(true);

    api.fetchChatTargets.mockResolvedValueOnce([planner("t1", true)]);
    await remount(<PlannerWorkConversation taskId="t1" detail={{ group: "completed", status: "completed" }} />);
    expect(input().disabled).toBe(false);
    expect(container.querySelector("[data-task-status]")?.getAttribute("data-task-status")).toBe("completed");
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("planner:t1");
  });

  it("checks planner availability once and does not poll a turn", async () => {
    vi.useFakeTimers();
    await show(<TaskPane taskId="t1" />);
    expect(api.fetchChatTargets).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(api.fetchChatTargets).toHaveBeenCalledTimes(1);
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("applies a delta, a phase and an empty final, and sends the queue once when answered", async () => {
    await show(<TaskPane taskId="t1" />);
    await say("ask");
    await fire("answer.delta", { text: "partial" });
    await fire("agent.phase", { label: "Thinking" });
    expect(container.querySelector("[data-phase]")?.textContent).toBe("Thinking");
    await fire("answer.final", { text: "", outcome: "answered" });
    expect(container.textContent).toContain("partial");
    expect(container.textContent).not.toContain("Done.");
    expect(container.querySelector("[data-phase]")).toBeNull();

    await say("first");
    await say("a");
    await say("b");
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    await fire("answer.final", { text: "ok", outcome: "answered" });
    expect(api.sendChat).toHaveBeenCalledTimes(3);
    expect(api.sendChat.mock.calls[2][0]).toBe("a\n\nb");
    expect(api.sendChat.mock.calls[2][1]).toBe("planner-t1");
    expect(api.sendChat.mock.calls[2][3]).toEqual({ kind: "planner", task_id: "t1" });
    await fire("answer.final", { text: "again", outcome: "answered" });
    expect(api.sendChat).toHaveBeenCalledTimes(3);
  });

  it("uses Done when a final arrives with no streamed text", async () => {
    await show(<TaskPane taskId="t1" />);
    await say("ask");
    await fire("answer.final", { text: "" });
    expect(container.textContent).toContain("Done.");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
  });

  it.each(["failed", "stopped", "confirm"] as const)("returns the queue and does not resend when the outcome is %s", async (outcome) => {
    await show(<TaskPane taskId="t1" />);
    await say("first");
    await say("queued");
    await fire("answer.final", { text: "end", outcome });
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(input().value).toBe("queued");
    if (outcome === "stopped") expect(container.textContent).toContain("Stopped");
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
  });

  it("returns the queue when the stream ends without a final", async () => {
    await show(<TaskPane taskId="t1" />);
    await say("first");
    await say("queued");
    await act(async () => { done?.(); });
    await flush();
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(input().value).toBe("queued");
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("puts a rejected send back into the draft and does not retry it", async () => {
    api.sendChat.mockRejectedValueOnce(new Error("planner stopped"));
    await show(<TaskPane taskId="t1" />);
    await say("hello");
    expect(container.textContent).toContain("planner stopped");
    expect(input().value).toBe("hello");
    expect(container.querySelector('[data-role="user"]')).toBeNull();
    expect(api.sendChat).toHaveBeenCalledTimes(1);

    api.sendChat.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await say("again");
    expect(container.textContent).toContain("Failed to connect. Is the server running?");
    expect(input().value).toBe("again");
    expect(api.sendChat).toHaveBeenCalledTimes(2);
  });

  it("does not stop or follow a planner turn", async () => {
    await show(<TaskPane taskId="t1" />);
    await say("ask");
    expect(container.querySelector('button[aria-label^="Stop"]')).toBeNull();
    expect(input().getAttribute("placeholder")).not.toContain("Esc stops");
    key("Escape");
    type("/stop");
    key("Enter");
    await flush();
    expect(api.stopChatReply).not.toHaveBeenCalled();
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
    expect(api.sendChat).toHaveBeenCalledTimes(1);
  });

  it("cancels only the client when the task changes and ignores the old turn", async () => {
    let resolve!: (value: { correlation_id: string }) => void;
    api.sendChat.mockReturnValueOnce(new Promise((settle) => { resolve = settle; }));
    await show(<TaskPane taskId="t1" />);
    await say("from A");
    const signal = api.sendChat.mock.calls[0][4].signal as AbortSignal;
    await show(<TaskPane taskId="t2" />);
    expect(signal.aborted).toBe(true);
    expect(api.stopChatReply).not.toHaveBeenCalled();
    expect(input().value).toBe("");
    expect(container.textContent).not.toContain("from A");
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("planner:t2");
    await act(async () => { resolve({ correlation_id: "late" }); });
    await flush();
    expect(api.streamChatReply).not.toHaveBeenCalled();
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
  });

  it("ignores a late failure and a late stream event from the previous task", async () => {
    let reject!: (error: Error) => void;
    api.sendChat.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    await show(<TaskPane taskId="t1" />);
    await say("from A");
    await show(<TaskPane taskId="t2" />);
    await act(async () => { reject(new Error("old failure")); });
    await flush();
    expect(container.textContent).not.toContain("old failure");
    expect(input().value).toBe("");

    await say("from B");
    const late = emit;
    const lateDone = done;
    await show(<TaskPane taskId="t1" />);
    act(() => late({ kind: "answer.delta", payload: { text: "LEAK" } }));
    act(() => late({ kind: "answer.final", payload: { text: "LEAKED", outcome: "failed" } }));
    act(() => lateDone?.());
    await flush();
    expect(container.textContent).not.toContain("LEAK");
    expect(container.textContent).not.toContain("from B");
    expect(api.stopChatReply).not.toHaveBeenCalled();
    expect(api.fetchChatTurn).not.toHaveBeenCalled();
  });

  it("does not execute message html or treat an unknown role or event as a person", async () => {
    window.alert = vi.fn();
    api.fetchChatMessages.mockResolvedValueOnce([
      { message_id: "u", role: "user", content: '<img src="x" onerror="alert(1)">', created_at: "2026-11-25T00:00:00Z" },
      { message_id: "a", role: "assistant", content: "<script>alert(1)</script>", created_at: "2026-11-25T00:00:01Z" },
      { message_id: "t", role: "tool", content: "tool body", created_at: "2026-11-25T00:00:02Z" },
      { message_id: "e", role: "event", content: "event body", created_at: "2026-11-25T00:00:03Z" },
    ]);
    await show(<TaskPane taskId="t1" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(window.alert).not.toHaveBeenCalled();
    expect(container.querySelector('[data-role="user"]')?.textContent).toContain("<img");
    expect(container.querySelector('[data-role="tool"]')?.getAttribute("data-role")).toBe("tool");
    expect(container.querySelector('[data-role="event"]')?.getAttribute("data-role")).toBe("event");
    expect(container.querySelector('[data-message-id="t"]')?.getAttribute("data-role")).not.toBe("user");
    expect(container.querySelector('[data-message-id="t"]')?.getAttribute("data-role")).not.toBe("assistant");

    await say("hi");
    await fire("agent.tool", { name: "shell", text: "rm -rf payload" });
    await fire("weird.kind", { text: "forced user" });
    const roles = [...container.querySelectorAll("[data-role]")].map((node) => node.getAttribute("data-role"));
    expect(roles.filter((role) => role === "user" || role === "assistant")).toEqual(["user", "assistant", "user", "assistant"]);
    expect(container.textContent).not.toContain("rm -rf payload");
    expect(container.textContent).not.toContain("forced user");
  });

  it("restores a draft per task and still sends when storage throws", async () => {
    await show(<TaskPane taskId="t1" />);
    type("draft one");
    await flush();
    act(() => root.unmount());
    root = createRoot(container);
    await show(<TaskPane taskId="t1" />);
    expect(input().value).toBe("draft one");
    await show(<TaskPane taskId="t2" />);
    expect(input().value).toBe("");
    await show(<TaskPane taskId="t1" />);
    expect(input().value).toBe("draft one");

    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
    await show(<TaskPane taskId="t2" />);
    await say("still sends");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(api.sendChat.mock.calls[0][0]).toBe("still sends");
    expect(api.sendChat.mock.calls[0][3]).toEqual({ kind: "planner", task_id: "t2" });
  });

  it("sends a planner turn over HTTP and reads that turn back", async () => {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    let sent = false;
    const saved = [
      { message_id: "user-1", role: "user", content: "ship it", created_at: "2026-11-25T00:00:00Z" },
      { message_id: "user-1:reply", role: "assistant", content: "shipped", created_at: "2026-11-25T00:00:01Z" },
    ];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      posts.push({ url: String(url), body });
      const path = String(url);
      if (path.endsWith("/targets")) return json({ targets: [planner("t1")] });
      if (path.endsWith("/messages")) return json({ messages: sent ? saved : [] });
      if (path.endsWith("/send")) {
        sent = true;
        return json({ correlation_id: "corr-1" });
      }
      if (path.endsWith("/reply_events")) {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              'event: answer.delta\ndata: {"text":"ship"}\n\nevent: answer.final\ndata: {"text":"shipped","outcome":"answered"}\n\n',
            ));
            controller.close();
          },
        });
        return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return json({});
    }));
    const real = bridge.real!;
    api.fetchChatMessages.mockImplementation((sessionId: string, signal?: AbortSignal, managerId?: string) =>
      real.fetchChatMessages(sessionId, signal, managerId));
    api.fetchChatTargets.mockImplementation((signal?: AbortSignal) => real.fetchChatTargets(signal));
    api.sendChat.mockImplementation((...args: Parameters<typeof real.sendChat>) => real.sendChat(...args));
    api.streamChatReply.mockImplementation((...args: Parameters<typeof real.streamChatReply>) => real.streamChatReply(...args));
    api.stopChatReply.mockImplementation((...args: Parameters<typeof real.stopChatReply>) => real.stopChatReply(...args));
    api.fetchChatTurn.mockImplementation((...args: Parameters<typeof real.fetchChatTurn>) => real.fetchChatTurn(...args));

    await show(<TaskPane taskId="t1" />);
    const firstRead = posts.find((post) => post.url.endsWith("/messages"));
    expect(firstRead?.body).toEqual({ session_id: "planner-t1" });
    expect(firstRead?.body).not.toHaveProperty("before");
    expect(firstRead?.body).not.toHaveProperty("cursor");
    expect(firstRead?.body).not.toHaveProperty("manager");

    await say("ship it");
    for (let i = 0; i < 8; i += 1) await flush();
    expect(container.textContent).toContain("shipped");
    const send = posts.find((post) => post.url.endsWith("/send"));
    expect(send?.body).toMatchObject({
      text: "ship it",
      session_id: "planner-t1",
      target: { kind: "planner", task_id: "t1" },
    });
    expect(typeof send?.body.message_id).toBe("string");
    expect(send?.body).not.toHaveProperty("before");
    expect(posts.some((post) => post.url.includes("/stop") || post.url.endsWith("/turn"))).toBe(false);

    posts.length = 0;
    act(() => root.unmount());
    root = createRoot(container);
    await show(<TaskPane taskId="t1" />);
    expect(container.textContent).toContain("shipped");
    expect(posts.find((post) => post.url.endsWith("/messages"))?.body).toEqual({ session_id: "planner-t1" });
  });
});

function json(result: unknown): Response {
  return new Response(JSON.stringify({ status: "ok", result }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
