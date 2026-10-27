// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fetchSchedulerSettings, updateSchedulerSettings, getWaitSettings, updateWaitSettings,
  fetchVaultSettings, updateVaultSettings } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

const scheduler = {
  min_interval_seconds: { value: 300, source: "built-in", built_in: 300, minimum: 1, gated_value: 60 },
  run_timeout_seconds: { value: null, source: "built-in", built_in: null, minimum: 1, maximum: 2592000 },
};
const waits = {
  max_wait_seconds: { value: 604800, default: 604800, min: 60, max: 2592000, source: "built-in" },
  min_check_seconds: { value: 60, default: 300, min: 10, max: 86400, source: "setting" },
};
const vault = { configured: false, path: null, writable: [], rules_file: null, git: false, problem: "Folder missing" };
const cases = [
  ["scheduler_settings/get", {}, fetchSchedulerSettings, scheduler],
  ["scheduler_settings/update", { min_interval_seconds: null }, () => updateSchedulerSettings({ min_interval_seconds: null }), scheduler],
  ["wait_settings/get", {}, getWaitSettings, waits],
  ["wait_settings/update", { min_check_seconds: 60 }, () => updateWaitSettings({ min_check_seconds: 60 }), waits],
  ["knowledge_vault/get", {}, fetchVaultSettings, vault],
  ["knowledge_vault/update", { path: null }, () => updateVaultSettings({ path: null }), vault],
] as const;
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

it.each(cases)("%s retains the full snapshot through POST v1", async (operation, body, call, result) => {
  const fetcher = vi.fn(async (_path: RequestInfo | URL, _init?: RequestInit) => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  expect(await call()).toEqual(result);
  const [path, init] = fetcher.mock.calls[0];
  expect(path).toBe(`/api/v1/${operation}`);
  expect(init?.method).toBe("POST");
  expect(JSON.parse(String(init?.body))).toEqual(body);
});

it.each(cases)("%s refuses a pending confirmation as a completed snapshot", async (_, _body, call) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "Change settings" }, { status: 202 })));
  await expect(call()).rejects.toMatchObject({ status: 202, message: "Change settings" });
});

it("preserves typed errors and request cancellation", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "error",
    error: { code: "invalid", message: "Invalid folder" } }, { status: 400 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(updateVaultSettings({ path: "relative" })).rejects.toMatchObject({ status: 400, code: "invalid", message: "Invalid folder" });
  const cancelled = new DOMException("Aborted", "AbortError");
  fetcher.mockRejectedValueOnce(cancelled);
  await expect(getWaitSettings()).rejects.toBe(cancelled);
});

it("refuses every settings request after the operator session ends", async () => {
  const fetcher = vi.fn(async () => new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  await expect(fetchSchedulerSettings()).rejects.toBeInstanceOf(SessionEndedError);
  for (const [, , call] of cases) await expect(call()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledTimes(1);
});
