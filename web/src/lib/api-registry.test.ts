// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { addModels, fetchProviderModels, fetchRegistry, probeRuntime, REGISTRY_CHANGED_EVENT } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

it("reads the operator registry through the v1 envelope without announcing a mutation", async () => {
  const registry = { runtimes: [{ runtime_id: "rt-codex", capabilities: ["resume"] }], profiles: [{ profile_id: "profile" }] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result: registry }));
  vi.stubGlobal("fetch", fetcher);
  const event = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, event);
  try {
    await expect(fetchRegistry()).resolves.toEqual(registry);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/registry/view", expect.objectContaining({
      method: "POST", body: "{}", credentials: "same-origin",
    }));
    expect(event).not.toHaveBeenCalled();
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, event); }
});

it("returns a probed runtime and announces exactly one registry reload", async () => {
  const result = { runtime: { runtime_id: "runtime/custom", capabilities: ["resume"], probe_error: null } };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const event = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, event);
  try {
    await expect(probeRuntime("runtime/custom")).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/runtimes/probe", expect.objectContaining({
      method: "POST", body: JSON.stringify({ runtime_id: "runtime/custom" }),
    }));
    expect(event).toHaveBeenCalledTimes(1);
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, event); }
});

it.each([404, 409, 202])("keeps HTTP %s probe failures or pending results from announcing success", async (status) => {
  const answer = status === 202
    ? { status: "needs_confirmation", confirmation_id: "pending", expires_in_seconds: 60, impact: "Probe pending" }
    : { status: "error", error: { code: status === 404 ? "not_found" : "conflict", message: "Probe unavailable" } };
  const fetcher = vi.fn(async () => Response.json(answer, { status }));
  vi.stubGlobal("fetch", fetcher);
  const event = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, event);
  try {
    await expect(probeRuntime("rt-codex")).rejects.toMatchObject({ name: "V1Error", status });
    expect(event).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, event); }
});

it("fetches models with only the stored provider id and without a registry mutation", async () => {
  const result = { provider_id: "provider", listings: [] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn(); window.addEventListener(REGISTRY_CHANGED_EVENT, changed);
  try {
    await expect(fetchProviderModels("provider")).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/provider_accounts/fetch_models", expect.objectContaining({
      method: "POST", body: JSON.stringify({ provider_id: "provider" }),
    }));
    expect(changed).not.toHaveBeenCalled();
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, changed); }
});

it.each([true, false])("announces a bulk addition only when models were added (%s)", async (added) => {
  const result = { models: added ? [{ name: "custom-model" }] : [], skipped: [], warnings: [] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn(); window.addEventListener(REGISTRY_CHANGED_EVENT, changed);
  try {
    await expect(addModels("provider", [{ name: "custom-model", protocol: "openai-chat" }])).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/models/add_many", expect.objectContaining({
      method: "POST", body: JSON.stringify({ provider_id: "provider", models: [{ name: "custom-model", protocol: "openai-chat" }] }),
    }));
    expect(changed).toHaveBeenCalledTimes(added ? 1 : 0);
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, changed); }
});
