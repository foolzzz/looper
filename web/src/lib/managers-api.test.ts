import { afterEach, expect, it, vi } from "vitest";
import { createManager, deleteManager, deleteManagerTemplate, fetchManagers, putManagerTemplate, updateManager } from "./api";

const manager = { id: "manager-review", name: "Review" };
const snapshot = { managers: [manager], max_enabled: 8 };
const template = { name: "Review", description: "", project_id: null, work_dir: "", workspace_mode: null,
  requirement: "Review code.", planner_instructions: "", profiles: {}, role_templates: [], inputs: [] };
afterEach(() => vi.unstubAllGlobals());

it("uses the six v1 manager operations and preserves their wire results", async () => {
  const calls: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    calls.push([url, init.method, JSON.parse(init.body)]);
    const result = /\/(create|update)$/.test(url) ? { manager } : snapshot;
    return Response.json({ status: "ok", result });
  }));
  expect(await fetchManagers()).toEqual([manager]);
  expect(await createManager({ key: "review", name: "Review", system_prompt: "Review code." })).toEqual({ manager });
  expect(await updateManager(manager.id, { enabled: false })).toEqual({ manager });
  expect(await deleteManager(manager.id)).toEqual(snapshot);
  expect(await putManagerTemplate(manager.id, "review", template)).toEqual(snapshot);
  expect(await deleteManagerTemplate(manager.id, "review")).toEqual(snapshot);
  expect(calls).toEqual([
    ["/api/v1/managers/list", "POST", {}],
    ["/api/v1/managers/create", "POST", { key: "review", name: "Review", system_prompt: "Review code." }],
    ["/api/v1/managers/update", "POST", { manager_id: manager.id, fields: { enabled: false } }],
    ["/api/v1/managers/delete", "POST", { manager_id: manager.id }],
    ["/api/v1/manager_templates/put", "POST", { manager_id: manager.id, key: "review", ...template }],
    ["/api/v1/manager_templates/delete", "POST", { manager_id: manager.id, key: "review" }],
  ]);
});

it("does not treat a pending manager write as complete or confirm it automatically", async () => {
  const fetch = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "c-test",
    impact: "Retire Review", expires_in_seconds: 600 }, { status: 202 }));
  vi.stubGlobal("fetch", fetch);
  await expect(deleteManager(manager.id)).rejects.toMatchObject({ status: 202, message: "Retire Review" });
  expect(fetch).toHaveBeenCalledTimes(1);
});
