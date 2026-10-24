// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sendChat, streamChatReply, watchChat } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

const headers = { "Content-Type": "text/event-stream" };
beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([{ kind: "manager" }, { kind: "planner", task_id: "task-example" }] as const)(
  "sends a dashboard $kind target once through v1 and returns its correlation id", async (target) => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: { correlation_id: "reply-example" } }));
    vi.stubGlobal("fetch", fetcher);
    expect(await sendChat("Continue", "thread-example", "message-example", target)).toEqual({ correlation_id: "reply-example" });
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/operator_chat/send", expect.objectContaining({ method: "POST",
      body: JSON.stringify({ text: "Continue", session_id: "thread-example", message_id: "message-example", target }) }));
  },
);

it("preserves the Manager owner from a structured send conflict", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: {
    code: "conflict", message: "Another Manager owns this thread.", details: { manager_id: "manager-example" },
  } }, { status: 409 })));
  await expect(sendChat("Continue", "thread-example")).rejects.toMatchObject({ status: 409, details: { manager_id: "manager-example" } });
});

it("does not complete or automatically confirm a pending send", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending-example",
    expires_in_seconds: 60, impact: "Send this message." }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(sendChat("Continue")).rejects.toMatchObject({ status: 202, message: "Send this message." });
  expect(fetcher).toHaveBeenCalledOnce();
});

it("follows named reply deltas and the canonical final outcome through a POST", async () => {
  const fetcher = vi.fn(async () => new Response('event: answer.delta\ndata: {"text":"Hello"}\n\nevent: answer.final\ndata: {"text":"Hello!","outcome":"answered","response":{"message":"Hello!"}}\n\n', { headers }));
  vi.stubGlobal("fetch", fetcher);
  const onEvent = vi.fn(), onDone = vi.fn();
  const stop = streamChatReply("reply-example", onEvent, onDone);
  try {
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(onEvent.mock.calls.map(([event]) => event)).toEqual([
      { kind: "answer.delta", payload: { text: "Hello" } },
      { kind: "answer.final", payload: { text: "Hello!", outcome: "answered", response: { message: "Hello!" } } },
    ]);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/operator_chat/reply_events", expect.objectContaining({ method: "POST",
      body: JSON.stringify({ correlation_id: "reply-example" }) }));
  } finally { stop(); }
});

it("reconnects the autonomous thread watch with its owner and cancels the idle reader", async () => {
  vi.useFakeTimers();
  const message = { message_id: "autonomous-example", role: "assistant", content: "Task finished", created_at: "2026-10-24T00:00:00Z" };
  const cancel = vi.fn();
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(`event: chat.message\ndata: ${JSON.stringify(message)}\n\n`, { headers }))
    .mockResolvedValueOnce(new Response(new ReadableStream({ cancel }), { headers }));
  vi.stubGlobal("fetch", fetcher);
  const onMessage = vi.fn();
  const stop = watchChat("thread-example", onMessage, "manager-example");
  await vi.advanceTimersByTimeAsync(1);
  expect(onMessage).toHaveBeenCalledExactlyOnceWith(message);
  await vi.advanceTimersByTimeAsync(3000);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher).toHaveBeenLastCalledWith("/api/v1/operator_chat/thread_events", expect.objectContaining({ method: "POST",
    body: JSON.stringify({ session_id: "thread-example", manager: "manager-example" }) }));
  stop();
  await vi.advanceTimersByTimeAsync(1);
  expect(cancel).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(6000);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("stops thread reconnects after another request ends the session", async () => {
  vi.useFakeTimers();
  const cancel = vi.fn();
  const fetcher = vi.fn(async (url: string) => url.endsWith("/thread_events")
    ? new Response(new ReadableStream({ cancel }), { headers }) : new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  const stop = watchChat("thread-example", vi.fn());
  try {
    await vi.advanceTimersByTimeAsync(1);
    await expect(sendChat("Continue")).rejects.toBeInstanceOf(SessionEndedError);
    await vi.advanceTimersByTimeAsync(6000);
    expect(cancel).toHaveBeenCalledOnce();
    expect(fetcher).toHaveBeenCalledTimes(2);
  } finally { stop(); }
});
