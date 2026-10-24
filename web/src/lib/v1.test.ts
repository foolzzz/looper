// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { callV1, confirmV1, streamV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";
import { fetchStatus, updateTaskConfig, setTaskProject, setTaskAgents, continueTaskAtCap, PROJECTS_CHANGED_EVENT } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });
const path = "/api/v1/system/status";
const eventHeaders = { "Content-Type": "text/event-stream; charset=utf-8" };

it("unwraps results and preserves JSON, credentials, signal and the explicit session", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result: { status: "running" } }));
  vi.stubGlobal("fetch", fetcher);
  const signal = new AbortController().signal;
  expect(await callV1(path, {}, { signal, session: "action" })).toEqual({ status: "running" });
  expect(fetcher).toHaveBeenCalledWith(path, expect.objectContaining({ method: "POST", credentials: "same-origin",
    signal, body: "{}", headers: { "Content-Type": "application/json", Accept: "application/json", "X-Looper-Session": "action" } }));
  expect(await fetchStatus()).toEqual({ status: "running" });
});

it.each([200, 409, 503])("preserves the structured error at HTTP %s", async (status) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: {
    code: "conflict", message: "The turn ended.", details: { manager_id: "manager", phase: null },
  } }, { status })));
  await expect(callV1(path, {})).rejects.toMatchObject({ name: "V1Error", status, code: "conflict",
    message: "The turn ended.", details: { manager_id: "manager", phase: null } });
});

it("does not expose an unstructured failure body", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("private failure", { status: 502 })));
  await expect(callV1(path, {})).rejects.toBeInstanceOf(V1Error);
  await expect(callV1(path, {})).rejects.not.toThrow("private failure");
});

it("creates an action session when randomUUID is unavailable", async () => {
  const getRandomValues = crypto.getRandomValues.bind(crypto);
  vi.stubGlobal("crypto", { getRandomValues });
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result: {} }));
  vi.stubGlobal("fetch", fetcher);
  await callV1(path, {});
  expect(fetcher).toHaveBeenCalledWith(path, expect.objectContaining({
    headers: expect.objectContaining({ "X-Looper-Session": expect.stringMatching(/^[\da-f-]{36}$/) }),
  }));
});

it("keeps two pending actions independent and confirms only the explicitly chosen one", async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith("/confirm")
    ? Response.json({ status: "ok", result: { changed: true } })
    : Response.json({ status: "needs_confirmation", confirmation_id: crypto.randomUUID(),
      expires_in_seconds: 60, impact: "Remove the selected item." }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  const [first, second] = await Promise.all([callV1(path, {}), callV1(path, {})]) as V1PendingConfirmation[];
  expect(first.session).not.toBe(second.session);
  expect(first.confirmation_id).not.toBe(second.confirmation_id);
  expect(first.impact).toContain("selected item");
  expect(fetcher).toHaveBeenCalledTimes(2); // cancelling either choice sends no confirmation
  expect(await confirmV1(first.confirmation_id, first.session)).toEqual({ changed: true });
  expect(fetcher).toHaveBeenLastCalledWith("/api/v1/confirm", expect.objectContaining({
    body: JSON.stringify({ confirmation_id: first.confirmation_id }),
    headers: expect.objectContaining({ "X-Looper-Session": first.session }),
  }));
});

it("propagates cancellation without reporting a result", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => { init.signal!.throwIfAborted(); }));
  const abort = new AbortController();
  abort.abort();
  await expect(callV1(path, {}, { signal: abort.signal })).rejects.toMatchObject({ name: "AbortError" });
});

it.each(["call", "stream"])("%s never completes a malformed 202 response", async (kind) => {
  const body = { status: "ok", result: { changed: true } };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(body, { status: 202 })));
  await expect(kind === "call" ? callV1(path, {}) : streamV1(path, {}, vi.fn()))
    .rejects.toMatchObject({ name: "V1Error", status: 202, code: "invalid" });
});

it("returns a stream's pending confirmation with its session without confirming or sending events", async () => {
  const pending = { status: "needs_confirmation", confirmation_id: "chosen", expires_in_seconds: 60, impact: "Read selected events." };
  const fetcher = vi.fn(async () => Response.json(pending, { status: 202 })), onEvent = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  expect(await streamV1(path, {}, onEvent, { session: "stream-action" })).toEqual({ ...pending, session: "stream-action" });
  expect(fetcher).toHaveBeenCalledOnce();
  expect(onEvent).not.toHaveBeenCalled();
});

it.each([
  [{ status: "ok", result: {} }, "unavailable", "The request failed."],
  [{ status: "error", error: { code: "conflict", message: "The turn ended." } }, "conflict", "The turn ended."],
])("rejects a non-SSE JSON response: %j", async (body, code, message) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)));
  await expect(streamV1(path, {}, vi.fn())).rejects.toMatchObject({ name: "V1Error", status: 200, code, message });
});

it("reads POST SSE across chunks, with event names and multiline data", async () => {
  const fetcher = vi.fn(async () => new Response(new ReadableStream({ start(controller) {
    for (const chunk of ["event: up", "date\r\ndata: {\r\ndata: \"count\": 1}\r\n\r\n"]) {
      controller.enqueue(new TextEncoder().encode(chunk));
    }
    controller.close();
  } }), { headers: eventHeaders }));
  vi.stubGlobal("fetch", fetcher);
  const onEvent = vi.fn();
  await streamV1(path, {}, onEvent);
  expect(onEvent).toHaveBeenCalledExactlyOnceWith({ event: "update", data: { count: 1 } });
  expect(fetcher).toHaveBeenCalledWith(path, expect.objectContaining({ method: "POST",
    headers: expect.objectContaining({ Accept: "text/event-stream" }) }));
});

it("cancels a waiting stream and sends no late events", async () => {
  const cancel = vi.fn(), onEvent = vi.fn(), abort = new AbortController();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream({ cancel }), { headers: eventHeaders })));
  const running = streamV1(path, {}, onEvent, { signal: abort.signal });
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  abort.abort();
  await expect(running).rejects.toMatchObject({ name: "AbortError" });
  expect(cancel).toHaveBeenCalledOnce();
  expect(onEvent).not.toHaveBeenCalled();
});

it.each(["poll", "stream"])("a guarded 401 ends %s and prevents later requests", async (kind) => {
  const fetcher = vi.fn(async () => new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  const action = () => kind === "poll" ? callV1(path, {}) : streamV1(path, {}, vi.fn());
  await expect(action()).rejects.toBeInstanceOf(SessionEndedError);
  await expect(action()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledOnce();
});

it("a different request's 401 terminates an idle active stream", async () => {
  const cancel = vi.fn();
  const fetcher = vi.fn(async (url: string) => url.endsWith("/events")
    ? new Response(new ReadableStream({ cancel }), { headers: eventHeaders }) : new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  const running = streamV1("/api/v1/events", {}, vi.fn());
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  await expect(callV1(path, {})).rejects.toBeInstanceOf(SessionEndedError);
  await expect(running).rejects.toBeInstanceOf(SessionEndedError);
  expect(cancel).toHaveBeenCalledOnce();
});

const taskWrites = [
  { action: "configure", run: () => updateTaskConfig("task-a", { requirement: "Build", work_dir: "/workspace" }),
    body: { task_id: "task-a", requirement: "Build", work_dir: "/workspace" }, result: { status: "init" }, value: undefined },
  { action: "move", run: () => setTaskProject("task-a", "project-b"),
    body: { task_id: "task-a", project_id: "project-b" },
    result: { task_id: "task-a", project_id: "project-b", project_name: "Example" },
    value: { task_id: "task-a", project_id: "project-b", project_name: "Example" } },
  { action: "agents", run: () => setTaskAgents("task-a", { profiles: { worker: null }, concurrency: { worker: 2 },
      system_prompt_modes: { planner: "append" } }),
    body: { task_id: "task-a", profiles: { worker: null }, concurrency: { worker: 2 }, system_prompt_modes: { planner: "append" } },
    result: { task_id: "task-a", agents: { editable: true }, note: "Next run" }, value: { agents: { editable: true }, note: "Next run" } },
  { action: "continue", run: () => continueTaskAtCap("task-a", 2), body: { task_id: "task-a", task_budget_usd: 2 },
    result: { task_id: "task-a", project_id: "project-b", task_budget_usd: 2, spent_usd: 1, continued: "task-a" }, value: undefined },
];
it.each(taskWrites)("task $action uses v1 and keeps its return and project event", async ({ action, run, body, result, value }) => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
  try {
    expect(await run()).toEqual(value);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/v1/tasks/${action}`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }));
    expect(changed).toHaveBeenCalledTimes(action === "move" ? 1 : 0);
  } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
});
it.each(taskWrites)("task $action never reports a pending write as completed", async ({ run }) => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "Raise the project's cap." }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(run()).rejects.toMatchObject({ name: "V1Error", status: 202, message: "Raise the project's cap." });
  expect(fetcher).toHaveBeenCalledOnce();
});
it.each(taskWrites)("task $action stops after the operator session ends", async ({ run }) => {
  const fetcher = vi.fn(async () => new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  await expect(run()).rejects.toBeInstanceOf(SessionEndedError);
  await expect(run()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledOnce();
});
