// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DrainBanner } from "@/components/drain-banner";
import { resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let phase: "serving" | "draining" | "drained";
let unavailable: boolean;
let fetcher: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  resetSessionGuard();
  phase = "serving";
  unavailable = false;
  fetcher = vi.fn(async () => {
    if (unavailable) throw new TypeError("Connection unavailable");
    return Response.json({ status: "ok", result: { drain: { phase } } });
  });
  vi.stubGlobal("fetch", fetcher);
  container = document.createElement("div");
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it.each(["draining", "drained"] as const)("announces the upgrade while %s", async (nextPhase) => {
  phase = nextPhase;
  await act(async () => root.render(<DrainBanner />));
  expect(container.querySelector('[role="status"]')?.textContent).toBe(
    "Upgrading: new turns and runs start after the restart",
  );
  expect(fetcher).toHaveBeenCalledWith("/api/v1/system/status", expect.objectContaining({ method: "POST", body: "{}" }));
});

it("shows nothing while serving and removes the banner when a drain is cancelled", async () => {
  await act(async () => root.render(<DrainBanner />));
  expect(container.textContent).toBe("");
  phase = "draining";
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(container.textContent).toContain("Upgrading:");
  phase = "serving";
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(container.textContent).toBe("");
});

it("keeps the last observed drain visible during a restart and stops polling on unmount", async () => {
  phase = "draining";
  await act(async () => root.render(<DrainBanner />));
  unavailable = true;
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(container.textContent).toContain("Upgrading:");
  act(() => root.unmount());
  expect((fetcher.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  const calls = fetcher.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(9000));
  expect(fetcher).toHaveBeenCalledTimes(calls);
  root = createRoot(container);
});
