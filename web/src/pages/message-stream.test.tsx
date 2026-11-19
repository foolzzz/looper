// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageStreamPage } from "@/pages/message-stream";
import {
  configureMessageStream, fetchMessageStream, fetchMessageStreamRuns,
  type MessageStreamRun, type MessageStreamSource,
} from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchMessageStream: vi.fn(), fetchMessageStreamRuns: vi.fn(), configureMessageStream: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const source = (extra: Partial<MessageStreamSource> = {}): MessageStreamSource => ({
  source_id: "lark-user", name: "Lark messages", kind: "lark_user", ingest_mode: "bridge", bridge_dir: "/srv/collector",
  cron: "*/2 * * * *", timezone: "UTC", user_open_id: null, health: "healthy", last_run_at: "2026-11-18T11:58:00Z",
  last_ok_at: "2026-11-18T11:58:00Z", committed_seq: 40, events: 1234, events_last_hour: 56,
  newest_sent_at: "2026-11-18T11:57:00Z", body_bytes: 2048, chats: 12, chats_excluded: 1, chats_failing: 0,
  last_run: null, ...extra,
});
const run = (id: number, extra: Partial<MessageStreamRun> = {}): MessageStreamRun => ({
  run_id: id, mode: "bridge", trigger: "scheduled", status: "ok", started_at: "2026-11-18T11:58:00Z",
  finished_at: "2026-11-18T11:58:03Z", events_new: 7, events_seen: 2, events_skipped: 1, files_rescanned: null,
  error: null, ...extra,
});

describe("MessageStreamPage", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.setSystemTime(new Date("2026-11-18T12:00:00Z"));
    vi.resetAllMocks();
    vi.mocked(fetchMessageStreamRuns).mockResolvedValue({ source_id: "lark-user", runs: [] });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });
  const show = async () => {
    await act(async () => { root.render(<MessageStreamPage />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  };

  it("offers the setup form when there is no source and starts bridging with what was typed", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [] });
    vi.mocked(configureMessageStream).mockResolvedValue(source());
    await show();
    expect(container.textContent).toContain("Set up a source");
    const [dir, openId] = Array.from(container.querySelectorAll("input"));
    await act(async () => {
      for (const [input, value] of [[dir, " /srv/collector "], [openId, "ou_x"]] as const) {
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
        set.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(configureMessageStream).toHaveBeenCalledWith({ ingest_mode: "bridge", bridge_dir: "/srv/collector", user_open_id: "ou_x" });
  });

  it("shows the state in words, the counts, and every run with its error", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source({ health: "degraded" })] });
    vi.mocked(fetchMessageStreamRuns).mockResolvedValue({ source_id: "lark-user", runs: [
      run(2, { status: "partial", error: "1 of 3 files failed; first oc_a: RuntimeError", files_rescanned: 1 }), run(1)] });
    await show();
    const text = container.textContent ?? "";
    expect(text).toContain("Degraded");
    expect(text).toContain("1,234");
    expect(text).toContain("56 in the last hour");
    expect(text).toContain("7 new · 2 already stored · 1 skipped");
    expect(text).toContain("1 of 3 files failed; first oc_a: RuntimeError");
    expect(text).toContain("1 file read again");
  });

  it("switches reading off and on through the same operation, and says when it failed", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(configureMessageStream).mockRejectedValueOnce(new Error("bridge needs a bridge_dir")).mockResolvedValue(source({ ingest_mode: "off" }));
    await show();
    const toggle = container.querySelector<HTMLButtonElement>('button[role="switch"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    await act(async () => { toggle.click(); });
    expect(configureMessageStream).toHaveBeenCalledWith({ source_id: "lark-user", ingest_mode: "off" });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("bridge needs a bridge_dir");
  });

  it("says it cannot load, with a retry, instead of an empty page", async () => {
    vi.mocked(fetchMessageStream).mockRejectedValue(new Error("boom"));
    await show();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Cannot load the message stream (boom)");
    expect(container.textContent).not.toContain("Set up a source");
  });
});
