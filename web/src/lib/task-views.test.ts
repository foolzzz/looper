// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fetchTasks, fetchTaskDetail, TaskDetailError } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

it("reads full detail with a task status and passes through its AbortSignal", async () => {
  const result = { task_id: "task-a", status: "failed", steps: [{ id: "step-a" }], journal: [{ kind: "created" }],
    config: { profiles: { worker: "profile-a" }, concurrency: { worker: 2 } }, manager_name: "Manager",
    cost: { cost_usd: 1.25 }, task_workspace: { mode: "in_place" } };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const signal = new AbortController().signal;
  expect(await fetchTaskDetail("task-a", signal)).toEqual(result);
  expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/tasks/detail", expect.objectContaining({ method: "POST",
    signal, body: JSON.stringify({ task_id: "task-a" }) }));
});

it.each([404, 500])("preserves detail HTTP %s as TaskDetailError", async (status) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: { code: "not_found", message: "Unavailable" } }, { status })));
  const error = await fetchTaskDetail("missing").catch((failure) => failure);
  expect(error).toBeInstanceOf(TaskDetailError);
  expect(error.status).toBe(status);
});

it.each(["abort", "network"])("preserves a detail %s failure", async (kind) => {
  const failure = kind === "abort" ? new DOMException("Aborted", "AbortError") : new TypeError("Offline");
  vi.stubGlobal("fetch", vi.fn(async () => { throw failure; }));
  await expect(fetchTaskDetail("task-a")).rejects.toBe(failure);
});

it.each(["page", "detail"])("%s refuses pending responses without confirming them", async (kind) => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "Read task data" }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(kind === "page" ? fetchTasks() : fetchTaskDetail("task-a")).rejects.toMatchObject({ status: 202 });
  expect(fetcher).toHaveBeenCalledOnce();
});

it.each(["page", "detail"])("%s preserves the ended operator session and stops further requests", async (kind) => {
  const fetcher = vi.fn(async () => new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  const read = () => kind === "page" ? fetchTasks() : fetchTaskDetail("task-a");
  await expect(read()).rejects.toBeInstanceOf(SessionEndedError);
  await expect(read()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledOnce();
});
