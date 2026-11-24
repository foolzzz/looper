// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { updateCostGuard, type CostGuardPatch, type CostGuardView } from "@/lib/api-cost-guard";
import type { V1PendingConfirmation } from "@/lib/v1";

afterEach(() => vi.unstubAllGlobals());

const view = { mode: "observe", daily_usd: 5, daily_equivalent_usd: null, roles: {}, counters: [], recent: [],
  recommended_daily_usd: null, recommended_daily_equivalent_usd: null, problem: null } as CostGuardView;

it("sends_sparse_caps_and_preserves_confirmation_session", async () => {
  const calls: { url: string; body: string; session: string }[] = [];
  let updates = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    const headers = init.headers as Record<string, string>;
    calls.push({ url, body: String(init.body), session: headers["X-Looper-Session"] });
    if (url === "/api/v1/cost_guard/update") {
      updates += 1;
      if (updates === 1) return Response.json({ status: "needs_confirmation", confirmation_id: "guard-1",
        expires_in_seconds: 90, impact: "Update cost guard settings?" }, { status: 202 });
      return Response.json({ status: "ok", result: view });
    }
    return Response.json({ status: "ok", result: view });
  }));

  const patch: CostGuardPatch = { roles: { processor: { daily_usd: 1 } } };
  const pending = await updateCostGuard(patch);
  expect(pending).toMatchObject({ status: "needs_confirmation", confirmation_id: "guard-1", impact: "Update cost guard settings?" });
  expect(pending).not.toHaveProperty("mode");
  const confirmation = pending as V1PendingConfirmation;
  expect(JSON.parse(calls[0].body)).toEqual(patch);
  expect(Object.keys(JSON.parse(calls[0].body) as object)).toEqual(["roles"]);
  expect(calls.some((call) => call.url === "/api/v1/confirm")).toBe(false);

  const saved = await updateCostGuard(patch, confirmation);
  expect(saved).toEqual(view);
  expect(saved).not.toMatchObject({ status: "needs_confirmation" });
  expect(calls[1]).toMatchObject({ url: "/api/v1/confirm", session: confirmation.session,
    body: JSON.stringify({ confirmation_id: "guard-1" }) });

  const direct = await updateCostGuard({ mode: "observe", daily_usd: 0 });
  expect(direct).toEqual(view);
  expect(JSON.parse(calls[2].body)).toEqual({ mode: "observe", daily_usd: 0 });
  expect(calls[2].url).toBe("/api/v1/cost_guard/update");
});
