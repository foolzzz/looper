// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchChatMessages, fetchChatSession, fetchChatSessions, fetchChatTargets, fetchChatTurn,
  ForeignConversationError, saveChatMessage, sessionProfileLabel, stopChatReply } from "@/lib/api";
import { V1Error } from "@/lib/v1";

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("operator conversation JSON operations", () => {
  const message = { message_id: "saved", role: "user", content: "hello", created_at: "2026-01-01T00:00:00Z" };
  const session = { session_id: "old", channel: "web", first_message_at: message.created_at,
    last_message_at: message.created_at, last_role: "user", last_content: "hello", message_count: 1 };
  const targets = [
    { kind: "manager", id: "manager", can_send: true },
    { kind: "planner", id: "planner-a", can_send: true, task_id: "a", name: "Task A", requirement: "Build", status: "running" },
    { kind: "worker", id: "worker-a", can_send: false, task_id: "a", status: "success", step_index: null,
      instruction: "Build", started_at: message.created_at },
    { kind: "reviewer", id: "reviewer-a", can_send: false, task_id: "a", status: "success", step_index: 0,
      instruction: "Review", started_at: message.created_at },
  ];

  it("retains the target projection, recent list and explicitly requested older session", async () => {
    const fn = stubFetch(200, { status: "ok", result: { targets, sessions: [session], messages: [message] } });
    expect(await fetchChatTargets()).toEqual(targets);
    expect(await fetchChatSessions()).toEqual([session]);
    expect(await fetchChatSession("old", "manager-review")).toEqual(session);
    expect(await fetchChatMessages("old")).toEqual([message]);
    expect(fn.mock.calls.map(([path, init]) => [path, JSON.parse(String(init?.body))])).toEqual([
      ["/api/v1/operator_chat/targets", {}], ["/api/v1/operator_chat/sessions", {}],
      ["/api/v1/operator_chat/sessions", { session_id: "old", manager: "manager-review" }],
      ["/api/v1/operator_chat/messages", { session_id: "old" }],
    ]);
  });

  it.each(["messages", "session"])("maps a structured foreign-owner %s error", async (kind) => {
    stubFetch(409, { status: "error", error: { code: "conflict", message: "Open this conversation there.", details: { manager_id: "manager" } } });
    const read = kind === "messages" ? fetchChatMessages("old", undefined, "manager-review") : fetchChatSession("old", "manager-review");
    await expect(read).rejects.toMatchObject({ name: "ForeignConversationError", managerId: "manager", message: "Open this conversation there." });
  });

  it("keeps database failure distinguishable from empty history", async () => {
    stubFetch(200, { status: "ok", result: { messages: [] } });
    expect(await fetchChatMessages("new")).toEqual([]);
    stubFetch(503, { status: "error", error: { code: "unavailable", message: "History unavailable." } });
    await expect(fetchChatMessages("old")).rejects.toBeInstanceOf(V1Error);
    stubFetch(409, { status: "error", error: { code: "conflict", message: "Other conflict." } });
    await expect(fetchChatSession("old")).rejects.not.toBeInstanceOf(ForeignConversationError);
  });

  it.each(["messages", "session", "sessions"])("passes cancellation to the %s read", async (kind) => {
    vi.stubGlobal("fetch", vi.fn(async (_path: string, init: RequestInit) => init.signal!.throwIfAborted()));
    const abort = new AbortController();
    abort.abort();
    const read = kind === "messages" ? fetchChatMessages("old", abort.signal) : kind === "session"
      ? fetchChatSession("old", "manager", abort.signal) : fetchChatSessions("manager", abort.signal);
    await expect(read).rejects.toMatchObject({ name: "AbortError" });
  });

  it("reports persistence failure and never completes or confirms a pending save", async () => {
    const params = { session_id: "old", message_id: "saved", role: "user", content: "hello" };
    const fn = stubFetch(200, { status: "ok", result: { ok: true } });
    await expect(saveChatMessage(params)).resolves.toBeUndefined();
    expect(fn).toHaveBeenCalledWith("/api/v1/operator_chat/save_message", expect.objectContaining({ method: "POST", body: JSON.stringify(params) }));
    stubFetch(503, { status: "error", error: { code: "unavailable", message: "Save unavailable." } });
    await expect(saveChatMessage(params)).rejects.toThrow("Save unavailable.");
    const pending = stubFetch(202, { status: "needs_confirmation", confirmation_id: "pending", expires_in_seconds: 60, impact: "Save message." });
    await expect(saveChatMessage(params)).rejects.toMatchObject({ status: 202 });
    expect(pending).toHaveBeenCalledOnce();
  });
});

it.each([
  ["high", "Fast — codex · OpenAI / gpt · high"],
  [null, "Fast — codex · OpenAI / gpt"],
])("shows the session profile with effort %s", (effort, label) => {
  expect(sessionProfileLabel({
    profile_id: "fast", name: "Fast", runtime: "codex", provider: "OpenAI", model: "gpt", effort,
  })).toBe(label);
});

describe("chat stop and turn", () => {
  it("stopChatReply returns the state of a 409", async () => {
    const fn = stubFetch(409, { state: "not_running", error: "No turn is running." });
    expect(await stopChatReply("m1")).toEqual({ state: "not_running", method: null });
    expect(fn).toHaveBeenCalledWith("/api/chat/stop", expect.objectContaining({ method: "POST" }));
  });

  it("stopChatReply returns the state and method of a 200", async () => {
    stubFetch(200, { state: "stopping", method: "control" });
    expect(await stopChatReply("m1")).toEqual({ state: "stopping", method: "control" });
  });

  it("stopChatReply throws with the server's error", async () => {
    stubFetch(503, { error: "the Manager is not running" });
    await expect(stopChatReply("m1")).rejects.toThrow("the Manager is not running");
  });

  it("fetchChatTurn returns null when no turn runs", async () => {
    stubFetch(200, { turn: null });
    expect(await fetchChatTurn("s1")).toBeNull();
  });
});
