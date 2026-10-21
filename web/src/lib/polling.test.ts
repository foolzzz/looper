// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startPolling } from "@/lib/polling";

function visibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("startPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility("visible");
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts the next request only after the previous one settled", async () => {
    const releases: (() => void)[] = [];
    const tick = vi.fn(() => new Promise<void>((resolve) => releases.push(resolve)));
    const stop = startPolling(tick, 2000);
    expect(tick).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(tick).toHaveBeenCalledTimes(1);
    releases[0]();
    await vi.advanceTimersByTimeAsync(1999);
    expect(tick).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(tick).toHaveBeenCalledTimes(2);
    stop();
  });

  it("keeps polling after a failed request", async () => {
    const tick = vi.fn(async () => {
      throw new Error("down");
    });
    const stop = startPolling(tick, 1000);
    await vi.advanceTimersByTimeAsync(2500);
    expect(tick).toHaveBeenCalledTimes(3);
    stop();
  });

  it("stops when a tick answers true", async () => {
    const tick = vi.fn(async () => true);
    startPolling(tick, 1000);
    await vi.advanceTimersByTimeAsync(5000);
    expect(tick).toHaveBeenCalledTimes(1);
  });

  it("pauses while the tab is hidden and resumes at once when visible", async () => {
    const tick = vi.fn(async () => {});
    const stop = startPolling(tick, 1000);
    await vi.advanceTimersByTimeAsync(0);
    expect(tick).toHaveBeenCalledTimes(1);
    visibility("hidden");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(tick).toHaveBeenCalledTimes(1);
    visibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(tick).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1000);
    expect(tick).toHaveBeenCalledTimes(3);
    stop();
  });

  it("does not start a second request when the tab shows during one", async () => {
    const releases: (() => void)[] = [];
    const tick = vi.fn(() => new Promise<void>((resolve) => releases.push(resolve)));
    const stop = startPolling(tick, 1000);
    visibility("hidden");
    visibility("visible");
    expect(tick).toHaveBeenCalledTimes(1);
    stop();
  });

  it("aborts the request in flight and polls no more once stopped", async () => {
    let signal: AbortSignal | undefined;
    const tick = vi.fn((s: AbortSignal) => {
      signal = s;
      return new Promise<void>(() => {});
    });
    const stop = startPolling(tick, 1000);
    expect(signal?.aborted).toBe(false);
    stop();
    expect(signal?.aborted).toBe(true);
    visibility("hidden");
    visibility("visible");
    await vi.advanceTimersByTimeAsync(5000);
    expect(tick).toHaveBeenCalledTimes(1);
  });
});
