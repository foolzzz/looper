// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageStreamPage } from "@/pages/message-stream";
import {
  configureMessageStream, fetchMessageStream, fetchMessageStreamRuns, fetchMessageStreamChats,
  setMessageStreamChat, previewMessageStreamDeletion, deleteMessageStream, triageWhy,
  type MessageStreamRun, type MessageStreamSource,
} from "@/lib/api";
import { leadsChats, leadsSearch, leadsSettingsGet } from "@/lib/api-leads";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchMessageStream: vi.fn(), fetchMessageStreamRuns: vi.fn(), configureMessageStream: vi.fn(),
  fetchMessageStreamChats: vi.fn(), setMessageStreamChat: vi.fn(),
  previewMessageStreamDeletion: vi.fn(), deleteMessageStream: vi.fn(), triageWhy: vi.fn(),
}));
vi.mock("@/lib/api-leads", () => ({
  leadsSettingsGet: vi.fn(), leadsSearch: vi.fn(), leadsChats: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const source = (extra: Partial<MessageStreamSource> = {}): MessageStreamSource => ({
  source_id: "lark-user", name: "Lark messages", kind: "lark_user", ingest_mode: "bridge", bridge_dir: "/srv/collector", start_from: "end",
  cron: "*/2 * * * *", timezone: "UTC", user_open_id: null, health: "healthy", last_run_at: "2026-11-18T11:58:00Z",
  last_ok_at: "2026-11-18T11:58:00Z", committed_seq: 40, events: 1234, events_last_hour: 56,
  newest_sent_at: "2026-11-18T11:57:00Z", body_bytes: 2048, chats: 12, chats_excluded: 1, chats_failing: 0,
  body_retention_days: null, app_body_retention_days: 30, retention_last_at: null,
  events_last_day: 70, body_bytes_last_day: 1024, bodies_dropped: 4,
  last_run: null, ...extra,
});
const run = (id: number, extra: Partial<MessageStreamRun> = {}): MessageStreamRun => ({
  run_id: id, mode: "bridge", trigger: "scheduled", status: "ok", started_at: "2026-11-18T11:58:00Z",
  finished_at: "2026-11-18T11:58:03Z", events_new: 7, events_seen: 2, events_skipped: 1, skipped_by_deletion: 0, files_rescanned: null,
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
    vi.mocked(leadsSettingsGet).mockResolvedValue({ enabled: false, valid_days: 14, subscriptions: [] });
    vi.mocked(leadsSearch).mockResolvedValue({ notice: "来自飞书消息，未经确认；只当线索，不当权威。", leads: [], more: 0 });
    vi.mocked(leadsChats).mockResolvedValue({ source_id: null, chats: [], total: 0 });
    vi.mocked(fetchMessageStreamChats).mockResolvedValue({ source_id: "lark-user", chats: [] });
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
    expect(configureMessageStream).toHaveBeenCalledWith({ ingest_mode: "bridge", bridge_dir: "/srv/collector", start_from: "end", user_open_id: "ou_x" });
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

  it("hides the leads tab unless leads are on", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    await show();
    expect(container.querySelector('[role="tab"]')).toBeNull();
    expect(leadsSearch).not.toHaveBeenCalled();
    vi.mocked(leadsSettingsGet).mockResolvedValue({ enabled: true, valid_days: 14, subscriptions: [] });
    act(() => root.unmount());
    root = createRoot(container);
    await show();
    const tab = [...container.querySelectorAll('[role="tab"]')].find((item) => item.textContent === "线索")!;
    expect(tab).toBeTruthy();
    await act(async () => { (tab as HTMLButtonElement).click(); });
    expect(container.textContent).toContain("来自飞书消息，未经确认；只当线索，不当权威。");
    expect(leadsSearch).toHaveBeenCalledWith({ state: "active" });
  });
  const change = async (selector: string, value: string) => {
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>(selector)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const click = async (label: string) => {
    await act(async () => {
      const button = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === label)!;
      button.click();
    });
  };

  it("lists chat states and seven-day counts, searches, and excludes without deleting", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(fetchMessageStreamChats).mockResolvedValue({ source_id: "lark-user", chats: [
      { chat_id: "chat-demo", name: "Demo chat", kind: "group", inclusion: "included", state: "active", events_7d: 41, purged_at: null },
    ] });
    await show();
    expect(container.textContent).toContain("Demo chat");
    expect(container.textContent).toContain("41");
    expect(container.textContent).toContain("active");
    await act(async () => { container.querySelector<HTMLButtonElement>('button[aria-label="Include Demo chat"]')!.click(); });
    expect(setMessageStreamChat).toHaveBeenCalledWith("lark-user", "chat-demo", false);
    expect(deleteMessageStream).not.toHaveBeenCalled();
    await change('input[aria-label="Search chats"]', "Demo");
    await act(async () => { container.querySelector<HTMLFormElement>('form[aria-label="Search chats"]')!.requestSubmit(); });
    expect(fetchMessageStreamChats).toHaveBeenLastCalledWith("lark-user", "Demo", expect.any(AbortSignal));
  });

  it("limits a range to the current local time and refuses a future end", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    await show();
    await act(async () => {
      const select = container.querySelector<HTMLSelectElement>("select")!;
      select.value = "range";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const [start, end] = container.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]');
    const local = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    const now = new Date();
    expect(end.max).toBe(local(now));
    await change(`#${CSS.escape(start.id)}`, local(new Date(now.getTime() - 3600000)));
    await change(`#${CSS.escape(end.id)}`, local(new Date(now.getTime() + 3600000)));
    const review = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Review deletion")!;
    expect(review.disabled).toBe(true);
    await click("Review deletion");
    expect(previewMessageStreamDeletion).not.toHaveBeenCalled();
    await change(`#${CSS.escape(end.id)}`, local(now));
    expect(review.disabled).toBe(false);
  });

  it("shows deletion skips separately from stored duplicates and other skips", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(fetchMessageStreamRuns).mockResolvedValue({ source_id: "lark-user", runs: [
      run(1, { skipped_by_deletion: 3 }),
    ] });
    await show();
    expect(container.textContent).toContain("7 new · 2 already stored · 1 skipped · 3 skipped by deletion");
  });

  it("requires a review with deletion limits before the second confirmation", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(previewMessageStreamDeletion).mockResolvedValue({
      source_id: "lark-user", scope: "message", events: 1, confirmation_token: "delete-demo", session: "delete-session",
      backups: { count: 2, earliest_at: "2026-10-01T00:00:00Z" }, limits: [],
    });
    await show();
    await change('input[aria-label="Message ID"]', "message-demo");
    await click("Review deletion");
    expect(deleteMessageStream).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Already pushed outputs");
    expect(container.textContent).toContain("Processor run outputs");
    expect(container.textContent).toContain("2 upgrade backups");
    expect(container.textContent).toContain("daily message backups keep the newest 7 copies");
    expect(container.textContent).toContain("older copies age out");
    expect(container.textContent).toContain("2026-10-01");
    expect(container.textContent).toContain("lark-chat");
    await click("Confirm deletion");
    expect(deleteMessageStream).toHaveBeenCalledWith({
      source_id: "lark-user", scope: "message", message_id: "message-demo", confirmation_token: "delete-demo",
    }, "delete-session");
  });

  it("shows no chats when the directory is empty, while keeping storage and retention visible", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    await show();
    expect(container.textContent).toContain("No chats found");
    expect(container.textContent).toContain("Retention");
    expect(container.textContent).toContain("30 days");
  });


  it.each(["range", "chat"] as const)("previews a %s target, and Back permits editing without deleting", async (scope) => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(previewMessageStreamDeletion).mockResolvedValue({
      source_id: "lark-user", scope, events: 3, confirmation_token: "delete-demo", session: "delete-session",
      backups: { count: 0, earliest_at: null }, limits: [],
    });
    await show();
    await act(async () => {
      const select = container.querySelector<HTMLSelectElement>('select')!;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, scope);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await change('input[aria-label="Chat ID"]', " chat-demo ");
    if (scope === "range") {
      const inputs = container.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]');
      await change(`#${CSS.escape(inputs[0].id)}`, "2026-11-01T00:00");
      await change(`#${CSS.escape(inputs[1].id)}`, "2026-11-02T00:00");
    }
    await click("Review deletion");
    expect(previewMessageStreamDeletion).toHaveBeenCalledWith({
      source_id: "lark-user", scope, chat_id: "chat-demo",
      ...(scope === "range" ? { start: new Date("2026-11-01T00:00").toISOString(), end: new Date("2026-11-02T00:00").toISOString() } : {}),
    });
    await click("Back");
    expect(deleteMessageStream).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLInputElement>('input[aria-label="Chat ID"]')?.value).toBe(" chat-demo ");
  });

  it("keeps a failed deletion visible and permits a fresh preview", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(previewMessageStreamDeletion).mockResolvedValue({
      source_id: "lark-user", scope: "message", events: 1, confirmation_token: "delete-demo", session: "delete-session",
      backups: { count: 0, earliest_at: null }, limits: [],
    });
    vi.mocked(deleteMessageStream).mockRejectedValue(new Error("confirmation expired"));
    await show();
    await change('input[aria-label="Message ID"]', "message-demo");
    await click("Review deletion");
    await click("Confirm deletion");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("confirmation expired");
    await click("Back");
    await click("Review deletion");
    expect(previewMessageStreamDeletion).toHaveBeenCalledTimes(2);
  });


  it("marks a purged chat as deleted and disables re-inclusion", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(fetchMessageStreamChats).mockResolvedValue({ source_id: "lark-user", chats: [
      { chat_id: "chat-deleted", name: "", kind: "group", inclusion: "excluded", state: "active", events_7d: 0, purged_at: "2026-11-01T00:00:00Z" },
    ] });
    await show();
    expect(container.textContent).toContain("Deleted chat");
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Include chat-deleted"]')?.disabled).toBe(true);
  });

  it("looks up why a message was not reported and shows a failed read", async () => {
    vi.mocked(fetchMessageStream).mockResolvedValue({ sources: [source()] });
    vi.mocked(triageWhy).mockResolvedValueOnce({
      case: "event", event_id: "E-7", category: "important", reason: "synthetic-reason-delta",
      reason_kind: "model", handling: "not handed to the Manager yet (that step is not built)",
    });
    await show();
    expect(container.textContent).toContain("1,234");
    const form = container.querySelector<HTMLFormElement>('form[aria-label="为什么没告诉我"]')!;
    await change("#why-message", "om_synthetic");
    await act(async () => { form.requestSubmit(); });
    expect(triageWhy).toHaveBeenCalledWith("om_synthetic");
    const text = container.textContent ?? "";
    expect(text).toContain("有事件引用（重要）");
    expect(text).toContain("E-7");
    expect(text).toContain("synthetic-reason-delta");
    expect(text).toContain("not handed to the Manager yet (that step is not built)");
    vi.mocked(triageWhy).mockRejectedValueOnce(new Error("the read failed"));
    await act(async () => { form.requestSubmit(); });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("the read failed");
    expect(container.textContent).not.toContain("E-7");
  });

});
