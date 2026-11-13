import { afterEach, expect, it, vi } from "vitest";
import { confirmManagerUpdate, ManagerUpdateConfirmation, createManager, deleteManager, deleteManagerTemplate, fetchManagers, putManagerTemplate, updateManager } from "./api";

const manager = { id: "manager-review", name: "Review", idle_timeout_seconds: 1800 };
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
  expect(await createManager({ key: "review", name: "Review", system_prompt: "Review code.", idle_timeout_seconds: 1800 })).toEqual({ manager });
  expect(await updateManager(manager.id, { enabled: false, idle_timeout_seconds: 0 })).toEqual({ manager });
  expect(await deleteManager(manager.id)).toEqual(snapshot);
  expect(await putManagerTemplate(manager.id, "review", template)).toEqual(snapshot);
  expect(await deleteManagerTemplate(manager.id, "review")).toEqual(snapshot);
  expect(calls).toEqual([
    ["/api/v1/managers/list", "POST", {}],
    ["/api/v1/managers/create", "POST", { key: "review", name: "Review", system_prompt: "Review code.", idle_timeout_seconds: 1800 }],
    ["/api/v1/managers/update", "POST", { manager_id: manager.id, fields: { enabled: false, idle_timeout_seconds: 0 } }],
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


it("preserves an all-tools update confirmation and explicitly confirms in the same session", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    calls.push({ url, init });
    return url.endsWith("/confirm") ? Response.json({ status: "ok", result: { manager } })
      : Response.json({ status: "needs_confirmation", confirmation_id: "open-chats", impact: "2 chats are open to everyone",
        expires_in_seconds: 600 }, { status: 202 });
  }));
  let pending: ManagerUpdateConfirmation | undefined;
  try { await updateManager(manager.id, { tools: "all" }); }
  catch (error) { expect(error).toBeInstanceOf(ManagerUpdateConfirmation); pending = error as ManagerUpdateConfirmation; }
  expect(calls).toHaveLength(1);
  expect(pending?.confirmation.confirmation_id).toBe("open-chats");
  expect(await confirmManagerUpdate(pending!.confirmation)).toEqual({ manager });
  expect(calls[1].url).toBe("/api/v1/confirm");
  expect(JSON.parse(String(calls[1].init.body))).toEqual({ confirmation_id: "open-chats" });
  expect((calls[1].init.headers as Record<string, string>)["X-Looper-Session"])
    .toBe((calls[0].init.headers as Record<string, string>)["X-Looper-Session"]);
});
