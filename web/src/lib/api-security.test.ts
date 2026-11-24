// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fetchFenceSettings, fetchOutboundReviewSettings, fetchOutboundReviews, updateFenceSettings, updateOutboundReviewSettings } from "@/lib/api-security";
import { resetSessionGuard } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });
const ok = (result: unknown) => Response.json({ status: "ok", result });
it("sendsExactSecurityRequests", async () => {
  const fetcher = vi.fn().mockImplementation(async () => ok({ enabled: false })); vi.stubGlobal("fetch", fetcher);
  const { signal } = new AbortController();
  await fetchOutboundReviewSettings(signal); await updateOutboundReviewSettings(true);
  await fetchFenceSettings(signal); await updateFenceSettings(false);
  await fetchOutboundReviews({ task_id: "task-example", verdict: "block", cursor: "cursor", limit: 2 }, signal);
  expect(fetcher.mock.calls.map(([url, init]) => [url, JSON.parse(init.body)])).toEqual([
    ["/api/v1/outbound_review/get", {}], ["/api/v1/outbound_review/update", { enabled: true }],
    ["/api/v1/untrusted_fence/get", {}], ["/api/v1/untrusted_fence/update", { enabled: false }],
    ["/api/v1/outbound_review/list", { task_id: "task-example", verdict: "block", cursor: "cursor", limit: 2 }],
  ]);
  expect(fetcher.mock.calls[4][1].signal).toBe(signal);
});
it("keepsPendingConfirmationSession", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "needs_confirmation", confirmation_id: "confirm-review",
    expires_in_seconds: 60, impact: "Enable reviews" }, { status: 202 })).mockResolvedValueOnce(ok({ enabled: true }));
  vi.stubGlobal("fetch", fetcher);
  const result = await updateOutboundReviewSettings(true);
  expect(fetcher).toHaveBeenCalledTimes(1);
  if (!("status" in result)) throw new Error("expected pending");
  await updateOutboundReviewSettings(true, result);
  expect(fetcher.mock.calls[1][0]).toBe("/api/v1/confirm");
  expect(fetcher.mock.calls[1][1].headers["X-Looper-Session"]).toBe(fetcher.mock.calls[0][1].headers["X-Looper-Session"]);
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ confirmation_id: "confirm-review" });
});
it.each([403, 503])("propagatesForbiddenAndUnavailable %s", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => Response.json({ status: "error", error: {
    code: status === 403 ? "forbidden" : "unavailable", message: "Cannot read" } }, { status })));
  await expect(fetchFenceSettings()).rejects.toMatchObject({ status });
  await expect(fetchOutboundReviews({})).rejects.toMatchObject({ status });
});
