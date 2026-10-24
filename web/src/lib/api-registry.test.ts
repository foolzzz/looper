// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fetchRegistry, probeRuntime, REGISTRY_CHANGED_EVENT } from "@/lib/api";
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
