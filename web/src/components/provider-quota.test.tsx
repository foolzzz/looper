// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProviderQuota } from "@/components/provider-quota";
import type { ProviderLimits, ProviderLimitWindow } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
const window: ProviderLimitWindow = { window_name: "seven_day", status: "warning", utilization: 0.94,
  resets_at: "2030-01-07T00:00:00Z", window_minutes: null, source: "claude_stream",
  record_id: 7, observed_at: "2030-01-01T11:59:00Z" };
const response = (enabled = true, windows: ProviderLimitWindow[] = [window]): ProviderLimits => ({ enabled, providers: [{
  provider_id: "synthetic-provider", name: "Sample subscription", cost_basis: "api_equivalent", admission: null,
  windows, usage: [{ day: "2030-01-01", runs: 2, input_tokens: 120, output_tokens: 30,
    cache_read_input_tokens: 300, cache_creation_input_tokens: 50, cost_usd: 1.25, quota_failures: 1 }],
}] });
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2030-01-01T12:00:00Z"));
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const render = async (value = response()) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: value })));
  await act(async () => root.render(<ProviderQuota providerId="synthetic-provider" />));
};

it("reads the provider's windows and shows usage with the subscription cost label", async () => {
  await render();
  expect(fetch).toHaveBeenCalledWith("/api/v1/providers/limits", expect.objectContaining({
    method: "POST", body: JSON.stringify({ provider_id: "synthetic-provider" }),
  }));
  expect(container.textContent).toContain("94%");
  expect(container.textContent).toContain("Warning");
  expect(container.textContent).toContain("Resets");
  expect(container.textContent).toContain("Observed");
  expect(container.textContent).toContain("2 runs");
  expect(container.textContent).toContain("500 observed tokens");
  expect(container.textContent).toContain("1 quota failure");
  expect(container.textContent).toContain("$1.25");
  expect(container.textContent).toContain("at API prices");
});

it("explains disabled collection without presenting historical signals as current", async () => {
  await render(response(false));
  expect(container.textContent).toContain("Collection is off");
  expect(container.textContent).not.toContain("94%");
  expect(container.textContent).not.toContain("Warning");
});

it("keeps missing utilization and expired windows visibly unknown", async () => {
  await render(response(true, [{ ...window, utilization: null },
    { ...window, window_name: "five_hour", status: "allowed", resets_at: "2030-01-01T11:00:00Z" }]));
  expect(container.textContent).toContain("Usage unknown");
  expect(container.textContent).toContain("Window expired");
  expect(container.textContent).not.toContain("Allowed");
});

it("explains absent signals instead of assigning zero usage", async () => {
  await render(response(true, []));
  expect(container.textContent).toContain("No quota signals received");
  expect(container.textContent).not.toContain("0%");
});

it("labels a day-old signal as historical even before its window resets", async () => {
  await render(response(true, [{ ...window, status: "allowed", observed_at: "2029-12-30T12:00:00Z" }]));
  expect(container.textContent).toContain("Old signal");
  expect(container.textContent).toContain("94% last reported");
  expect(container.textContent).not.toContain("Allowed");
});

it("keeps read failures visible and retries without writing", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("Quota read unavailable"))
    .mockResolvedValueOnce(Response.json({ status: "ok", result: response() })));
  await act(async () => root.render(<ProviderQuota providerId="synthetic-provider" />));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Quota read unavailable");
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(container.textContent).toContain("94%");
});

it("waits for explicit confirmation before changing global collection", async () => {
  await render(response(false));
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ status: "needs_confirmation", confirmation_id: "synthetic",
    expires_in_seconds: 60, impact: "Collect rate-limit signals for all providers." }, { status: 202 }));
  await act(async () => container.querySelector<HTMLButtonElement>('[role="switch"]')!.click());
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(container.querySelector('[role="switch"]')?.getAttribute("aria-checked")).toBe("false");
  expect(container.querySelector('[aria-label="Confirm rate-limit collection"]')).not.toBeNull();
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ status: "ok", result: { enabled: true } }))
    .mockResolvedValueOnce(Response.json({ status: "ok", result: response() }));
  await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Confirm")!.click());
  expect(fetch).toHaveBeenNthCalledWith(3, "/api/v1/confirm", expect.objectContaining({ body: JSON.stringify({ confirmation_id: "synthetic" }) }));
  expect(container.querySelector('[role="switch"]')?.getAttribute("aria-checked")).toBe("true");
});

it("keeps unavailable quota failure counts unknown", async () => {
  const value = response();
  await render({ ...value, providers: [{ ...value.providers[0], usage: [{ ...value.providers[0].usage[0], quota_failures: null }] }] });
  expect(container.textContent).toContain("Quota failures unknown");
  expect(container.textContent).not.toContain("0 quota failures");
});

it("keeps unreported tokens and cost unknown instead of adding them as zero", async () => {
  const value = response();
  await render({ ...value, providers: [{ ...value.providers[0], usage: [{ ...value.providers[0].usage[0],
    input_tokens: null, output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null,
    cost_usd: null }] }] });
  expect(container.textContent).toContain("Tokens unknown");
  expect(container.textContent).toContain("Cost unknown");
  expect(container.textContent).not.toContain("0 observed tokens");
  expect(container.textContent).not.toContain("$0.00");
});
