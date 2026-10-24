// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { checkWorkspace, deleteTask, discardStepWorkspace } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

const actions = {
  delete: () => deleteTask("task-example"), discard: () => discardStepWorkspace("task-example", 7),
  check: () => checkWorkspace({ dir: "/workspace", mode: "worktree", base: "main" }),
};
const paths = { delete: "tasks/delete", discard: "workspaces/discard", check: "workspaces/check" };
const bodies = { delete: { task_id: "task-example" }, discard: { task_id: "task-example", workspace_id: 7 },
  check: { dir: "/workspace", mode: "worktree", base: "main" } };

it.each(["delete", "discard", "check"] as const)("%s uses the v1 envelope and keeps its return value", async action => {
  const result = action === "delete" ? { deleted: "task-example" } : action === "discard"
    ? { workspace: { id: 7, state: "discarded" } } : { workspace: "/workspace", warnings: [{ kind: "not_git", text: "Warning" }] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const notified = vi.fn();
  window.addEventListener("looper:projects-changed", notified);
  try {
    await expect(actions[action]()).resolves.toEqual(action === "delete" ? undefined
      : action === "discard" ? (result as { workspace: unknown }).workspace : result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/v1/${paths[action]}`, expect.objectContaining({
      method: "POST", body: JSON.stringify(bodies[action]), credentials: "same-origin",
    }));
    expect(notified).toHaveBeenCalledTimes(action === "delete" ? 1 : 0);
  } finally { window.removeEventListener("looper:projects-changed", notified); }
});

it.each(["delete", "discard", "check"] as const)("%s never treats pending confirmation as completion", async action => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    impact: "Remove the workspace.", expires_in_seconds: 60 }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(actions[action]()).rejects.toMatchObject({ name: "V1Error", status: 202, message: "Remove the workspace." });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("workspace checks preserve cancellation", async () => {
  const controller = new AbortController();
  controller.abort();
  const fetcher = vi.fn(async (_path, init) => { init.signal.throwIfAborted(); });
  vi.stubGlobal("fetch", fetcher);
  await expect(checkWorkspace({ dir: "/workspace" }, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal);
});
