// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StatusCard } from "@/components/status-card";
import type { StatusSection, StatusSnapshot } from "@/lib/status-card";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ok = (name: string, rest: object): StatusSection => ({ name, state: "ok", error: null, ...rest }) as StatusSection;
const snapshot = (sections: StatusSection[]): StatusSnapshot => ({ read_at: "2026-10-20T10:12:00Z", text: "", sections });
const manager = (sessions: object[], queued = 0) =>
  ok("manager", { managers: [{ manager_id: "m1", name: "Manager", queued, sessions }] });
const FULL = snapshot([
  manager([{ state: "idle" }], 2),
  ok("waiting", { kinds: [{ kind: "gates", state: "ok", count: 3, earliest: null, error: null },
    { kind: "logins", state: "ok", count: 0, earliest: null, error: null }] }),
  ok("reminders", { next: { due_at: "2026-10-20T11:00:00Z", text: "Send the report", timezone: null } }),
  ok("tasks", { count: 4 }),
  ok("schedules", { next: [{ name: "Nightly", next_fire_at: "2026-10-20T20:00:00Z" }], running: [{ run_id: "r" }] }),
]);

const answer = (body: object) => ({ ok: true, status: 200, json: async () => body }) as Response;
const okBody = (result: StatusSnapshot) => answer({ status: "ok", result });
const off = () => ({ ok: false, status: 409, json: async () => ({ status: "error", error: { code: "conflict", message: "off" } }) }) as Response;

describe("StatusCard", () => {
  let container: HTMLDivElement;
  let root: Root;
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
  const show = async () => { await act(async () => root.render(<StatusCard />)); };
  const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
  const text = () => container.textContent!;

  it("renders nothing and asks once when the switch is off", async () => {
    fetchMock.mockResolvedValue(off());
    await show();
    await advance(60_000);
    expect(container.innerHTML).toBe("");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/status_card/get");
  });

  it("shows the Manager, waiting counts, reminder, tasks, schedules and the read time", async () => {
    fetchMock.mockResolvedValue(okBody(FULL));
    await show();
    const t = text();
    for (const part of ["Idle", "2 queued", "3 gates", "Send the report", "4 running", "1 running", "As of"]) expect(t).toContain(part);
    expect(t).not.toContain("logins");
    expect(container.querySelector('a[href="#todo"]')!.textContent).toBe("3 gates");
    expect(container.querySelector('a[href="#tasks"]')).not.toBeNull();
    expect(container.querySelector('a[href="#schedules"]')).not.toBeNull();
  });

  it("describes an answering and a background session", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([manager([
      { state: "answering", channel: "lark", group: false, elapsed_seconds: 190 },
      { state: "background", origin: "completion", elapsed_seconds: 4000 },
    ], 1)])));
    await show();
    expect(text()).toContain("answering you in Lark (3 min)");
    expect(text()).toContain("running a background turn (wrapping up a task, 1 h 6 min)");
    expect(text()).toContain("1 queued");
  });

  it("shows a failed section's error in an alert, never a zero", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([{ name: "tasks", state: "error", error: "database down" } as StatusSection])));
    await show();
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain("database down");
    expect(text()).not.toContain("0 running");
  });

  it("shows an unknown section as not available and leaves an absent one out", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([{ name: "schedules", state: "unknown", error: null } as StatusSection,
      ok("tasks", { count: 1 })])));
    await show();
    expect(text()).toContain("Not available here");
    expect(text()).not.toContain("Next reminder");
  });

  it("keeps the last snapshot and alerts when a later refresh fails", async () => {
    fetchMock.mockResolvedValueOnce(okBody(FULL)).mockRejectedValue(new Error("network down"));
    await show();
    await advance(15_000);
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Cannot load the status (network down)");
    expect(text()).toContain("3 gates");
  });

  it("alerts instead of an empty card when the first read fails", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    await show();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Cannot load the status (network down).");
    expect(text()).not.toContain("Idle");
  });

  it("shows a waiting kind in unknown as not available, whatever the other kinds count", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([ok("waiting", { kinds: [
      { kind: "gates", state: "ok", count: 2, earliest: null, error: null },
      { kind: "logins", state: "unknown", count: 0, earliest: null, error: null }] })])));
    await show();
    expect(text()).toContain("logins: Not available here");
    expect(text()).toContain("2 gates");
  });

  it("shows a waiting kind in error with its reason, never Nothing", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([ok("waiting", { kinds: [
      { kind: "gates", state: "error", count: 0, earliest: null, error: "gate store down" }] })])));
    await show();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("gate store down");
    expect(text()).not.toContain("Nothing");
  });

  it("names a terminal reminder with the English label, never the raw key", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([ok("waiting", { kinds: [
      { kind: "reminders", state: "ok", count: 1, earliest: null, error: null }] })])));
    await show();
    const anchor = container.querySelector('a[href="#todo"]')!;
    expect(anchor.textContent).toBe("1 reminders not confirmed delivered");
    expect(text()).toContain("reminders not confirmed delivered");
    // The raw-key fallback would leave the bare word; every occurrence must carry the label.
    expect(text()).not.toMatch(/\breminders\b(?! not confirmed delivered)/);
  });

  it("hides the card when a later read answers 409", async () => {
    fetchMock.mockResolvedValueOnce(okBody(FULL)).mockResolvedValue(off());
    await show();
    expect(text()).toContain("3 gates");
    await advance(15_000);
    expect(container.innerHTML).toBe("");
    await advance(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows a reminder time in the snapshot's timezone", async () => {
    fetchMock.mockResolvedValue(okBody(snapshot([ok("reminders", {
      next: { due_at: "2026-10-20T23:30:00Z", text: "Call", timezone: "Asia/Tokyo" } })])));
    vi.setSystemTime(new Date("2026-10-20T23:00:00Z"));
    await show();
    expect(text()).toMatch(/08:30|8:30/);
  });
});
