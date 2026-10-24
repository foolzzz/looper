// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cancelTask, startReadyTask, startTask, stopTask } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });
const actions = {
  start: () => startReadyTask("task-a"), stop: () => stopTask("task-a"), cancel: () => cancelTask("task-a"),
  restart: () => startTask("task-a"), start_over: () => startTask("task-a", { startOver: true }),
};

it.each([
  ["start", 200], ["stop", 200], ["cancel", 200], ["restart", 200], ["start_over", 200],
  ["cancel", 202], ["restart", 202], ["start_over", 202], ["start", 409],
] as const)("%s dispatches once and respects HTTP %s", async (action, status) => {
  const flags = { start: "started", stop: "stopped", cancel: "cancelled", restart: "restarted", start_over: "started_over" };
  const answer = status === 202 ? { status: "needs_confirmation", confirmation_id: "pending", expires_in_seconds: 60,
    impact: "Cancel the current work." } : status === 409 ? { status: "error", error: { code: "conflict",
    message: "The task has ended.", details: { state: "completed" } } } : { status: "ok", result: {
    task_id: "task-a", [flags[action]]: true, ...(["restart", "start_over"].includes(action) ? { note: "Started." } : {}),
  } };
  const fetcher = vi.fn(async () => Response.json(answer, { status }));
  vi.stubGlobal("fetch", fetcher);
  if (status === 200) await expect(actions[action]()).resolves.toBeUndefined();
  else await expect(actions[action]()).rejects.toMatchObject(status === 202
    ? { name: "V1Error", status, message: "Cancel the current work." }
    : { name: "V1Error", status, code: "conflict", details: { state: "completed" } });
  expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/v1/ops/task_control/${action}`, expect.objectContaining({
    method: "POST", body: JSON.stringify({ task_id: "task-a" }),
  })); // Pending responses never dispatch a confirmation or report completion.
});
