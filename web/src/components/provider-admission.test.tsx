// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AdmissionSettings, ProviderAdmission } from "@/components/provider-admission";
import type { ProviderLimits } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
const limits: ProviderLimits = { enabled: true, providers: [{ provider_id: "sample", name: "Sample account",
  cost_basis: "reported", windows: [], usage: [], admission: { state: "frozen", until: "2030-01-07T00:00:00Z",
    until_known: true, running: 1, limit: 2, waiting: { interactive: 0, user_task: 2, recovery: 1, background: 3 } } }] };
const ok = (result: unknown) => Response.json({ status: "ok", result });
beforeEach(() => {
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
const click = async (text: string) => act(async () => [...container.querySelectorAll("button")]
  .find((button) => button.textContent === text)!.click());

it("does not read or show account admission when disabled", async () => {
  vi.stubGlobal("fetch", vi.fn());
  await act(async () => root.render(<ProviderAdmission providerId="sample" enabled={false} />));
  expect(fetch).not.toHaveBeenCalled();
  expect(container.textContent).toBe("");
});

it("shows frozen state, local reset, waiting runs and cap directly on the account", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(limits)));
  await act(async () => root.render(<ProviderAdmission providerId="sample" enabled />));
  expect(container.textContent).toContain("Frozen");
  expect(container.textContent).toContain("6 waiting");
  expect(container.textContent).toContain("1 running");
  expect(container.textContent).toContain("limit 2");
  expect(container.querySelector("time")?.dateTime).toBe("2030-01-07T00:00:00Z");
});

it("waits for the clear confirmation and refreshes only after it succeeds", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(ok(limits))
    .mockResolvedValueOnce(Response.json({ status: "needs_confirmation", confirmation_id: "sample-clear",
      expires_in_seconds: 60, impact: "Clear the account freeze and check waiting runs." }, { status: 202 }))
    .mockResolvedValueOnce(ok({ state: "open" }))
    .mockResolvedValueOnce(ok({ ...limits, providers: [{ ...limits.providers[0], admission: {
      ...limits.providers[0].admission!, state: "open", until: null } }] })));
  await act(async () => root.render(<ProviderAdmission providerId="sample" enabled />));
  await click("Check now");
  expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/providers/limits_clear", expect.objectContaining({
    body: JSON.stringify({ provider_id: "sample" }),
  }));
  expect(container.querySelector('[aria-label="Confirm account check"]')).not.toBeNull();
  expect(container.textContent).toContain("Frozen");
  await click("Confirm");
  expect(fetch).toHaveBeenNthCalledWith(3, "/api/v1/confirm", expect.objectContaining({
    body: JSON.stringify({ confirmation_id: "sample-clear" }),
  }));
  expect(container.textContent).toContain("Open");
});

it("keeps unknown resets and unlimited caps explicit", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok({ ...limits, providers: [{ ...limits.providers[0], admission: {
    ...limits.providers[0].admission!, until_known: false, limit: null } }] })));
  await act(async () => root.render(<ProviderAdmission providerId="sample" enabled />));
  expect(container.textContent).toContain("Reset time unknown");
  expect(container.textContent).toContain("no concurrency limit");
  expect(container.querySelector("time")).toBeNull();
});

it("surfaces a failed check without presenting the account as cleared", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(ok(limits)).mockRejectedValueOnce(new Error("Check unavailable")));
  await act(async () => root.render(<ProviderAdmission providerId="sample" enabled />));
  await click("Check now");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Check unavailable");
  expect(container.textContent).toContain("Frozen");
});

it("uses independent settings and explains the current turn coverage before enabling", async () => {
  const changed = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(ok({ enabled: false }))
    .mockResolvedValueOnce(Response.json({ status: "needs_confirmation", confirmation_id: "sample-enable",
      expires_in_seconds: 60, impact: "Enable account slots, lanes and quota freeze. Planner and Manager turns are not admitted yet." }, { status: 202 }))
    .mockResolvedValueOnce(ok({ enabled: true })));
  await act(async () => root.render(<AdmissionSettings onEnabled={changed} />));
  expect(changed).toHaveBeenLastCalledWith(false);
  await act(async () => container.querySelector<HTMLButtonElement>('[role="switch"]')!.click());
  expect(container.textContent).toContain("Planner and Manager turns are not admitted yet");
  expect(changed).toHaveBeenLastCalledWith(false);
  await click("Confirm");
  expect(changed).toHaveBeenLastCalledWith(true);
});
