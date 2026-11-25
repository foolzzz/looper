import { afterEach, expect, it, vi } from "vitest";
import { fetchFailureHistory, releaseFailureHold, updateEngineSettings } from "./api-task-failures";

afterEach(() => vi.unstubAllGlobals());
it("keeps keyset cursors and exact hold versions, and never auto-confirms", async () => {
  const mock = vi.fn(async (_path: string, _init: RequestInit) => Response.json({ status: "ok", result: { items: [], next_after_id: null } }));
  vi.stubGlobal("fetch", mock);
  await fetchFailureHistory("task", { after_id: 12, limit: 50, step_id: "step" });
  expect(JSON.parse(String(mock.mock.calls[0][1]?.body))).toEqual({ task_id: "task", after_id: 12, limit: 50, step_id: "step" });
  mock.mockImplementation(async () => Response.json({ status: "needs_confirmation", confirmation_id: "confirm", impact: "Retry failed reviewer", expires_in_seconds: 600 }, { status: 202 }));
  const pending = await releaseFailureHold({ task_id: "task", step_id: "step", failure_id: 4, action: "retry" });
  expect(pending).toMatchObject({ status: "needs_confirmation", confirmation_id: "confirm" });
  expect(mock).toHaveBeenCalledTimes(2);
  await updateEngineSettings(null);
  expect(JSON.parse(String(mock.mock.calls[2][1]?.body))).toEqual({ failure_typing: null });
});
